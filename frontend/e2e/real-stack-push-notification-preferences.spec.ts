import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  expect,
  test,
  type BrowserContext,
  type Locator,
  type Page,
  type Response,
} from "@playwright/test";

const SUBSCRIBE_PATH = "/api/notifications/subscribe/";
const PREFERENCES_PATH = "/api/notifications/preferences/";

interface PushPreferenceFixture {
  parent: {
    email: string;
    password: string;
  };
  student: {
    email: string;
    password: string;
    name: string;
  };
  child: {
    name: string;
  };
  expected: {
    parent: PushExpectation;
    student: PushExpectation;
  };
}

interface PushExpectation {
  endpoint: string;
  key_p256dh: string;
  key_auth: string;
  disabled_categories: string[];
}

interface BackendAssertResponse {
  ok?: boolean;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a push notification preferences fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): PushPreferenceFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<PushPreferenceFixture>;

  if (!data.parent?.email || !data.parent.password) {
    throw new Error("Fixture parent credentials are required.");
  }
  if (!data.student?.email || !data.student.password || !data.student.name) {
    throw new Error("Fixture student credentials are required.");
  }
  if (!data.child?.name) {
    throw new Error("Fixture child data is required.");
  }
  if (!isPushExpectation(data.expected?.parent) || !isPushExpectation(data.expected?.student)) {
    throw new Error("Fixture push expectations are required.");
  }

  return data as PushPreferenceFixture;
}

function isPushExpectation(value: unknown): value is PushExpectation {
  const expectation = value as Partial<PushExpectation> | undefined;
  return Boolean(
    expectation?.endpoint &&
      expectation.key_p256dh &&
      expectation.key_auth &&
      expectation.disabled_categories?.length,
  );
}

async function installPushMocks(context: BrowserContext, fixture: PushPreferenceFixture): Promise<void> {
  await context.addInitScript(
    ({ parent, student }) => {
      const grantedKey = "push_e2e_permission_granted";
      const roleKey = "push_e2e_role";

      const getCurrentExpectation = () =>
        window.localStorage.getItem(roleKey) === "student" ? student : parent;

      const createSubscription = () => {
        const expectation = getCurrentExpectation();
        return {
          endpoint: expectation.endpoint,
          toJSON: () => ({
            endpoint: expectation.endpoint,
            keys: {
              p256dh: expectation.keyP256dh,
              auth: expectation.keyAuth,
            },
          }),
          unsubscribe: async () => true,
        };
      };

      class MockNotification {
        static get permission() {
          return window.localStorage.getItem(grantedKey) === "true" ? "granted" : "default";
        }

        static async requestPermission() {
          window.localStorage.setItem(grantedKey, "true");
          return "granted";
        }
      }

      Object.defineProperty(window, "Notification", {
        configurable: true,
        value: MockNotification,
      });
      Object.defineProperty(window, "PushManager", {
        configurable: true,
        value: function MockPushManager() {},
      });
      Object.defineProperty(navigator, "serviceWorker", {
        configurable: true,
        value: {
          ready: Promise.resolve({
            pushManager: {
              getSubscription: async () =>
                window.localStorage.getItem(grantedKey) === "true" ? createSubscription() : null,
              subscribe: async () => createSubscription(),
            },
          }),
        },
      });
    },
    {
      parent: {
        endpoint: fixture.expected.parent.endpoint,
        keyP256dh: fixture.expected.parent.key_p256dh,
        keyAuth: fixture.expected.parent.key_auth,
      },
      student: {
        endpoint: fixture.expected.student.endpoint,
        keyP256dh: fixture.expected.student.key_p256dh,
        keyAuth: fixture.expected.student.key_auth,
      },
    },
  );
}

async function login(
  page: Page,
  credentials: { email: string; password: string },
  expectedPath: RegExp,
): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(credentials.email);
  await page.getByLabel("Пароль").fill(credentials.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(expectedPath);
}

function isSubscribeResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === SUBSCRIBE_PATH && response.request().method() === "POST";
}

function isPreferencesPutResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === PREFERENCES_PATH && response.request().method() === "PUT";
}

function isPreferencesGetResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === PREFERENCES_PATH && response.request().method() === "GET";
}

async function reloadWithPreferences(page: Page): Promise<void> {
  const preferencesResponse = page.waitForResponse(isPreferencesGetResponse, {
    timeout: 20_000,
  });
  await page.reload();
  expect((await preferencesResponse).ok()).toBe(true);
}

async function ensurePreferenceChecked(page: Page, label: string): Promise<Locator> {
  const preference = page.getByLabel(label);
  await expect(preference).toBeVisible({ timeout: 20_000 });
  if (!(await preference.isChecked())) {
    const preferenceResponse = page.waitForResponse(isPreferencesPutResponse, {
      timeout: 20_000,
    });
    await preference.click();
    expect((await preferenceResponse).ok()).toBe(true);
  }
  await expect(preference).toBeChecked();
  return preference;
}

async function disablePreference(page: Page, preference: Locator): Promise<void> {
  const preferenceResponse = page.waitForResponse(isPreferencesPutResponse, {
    timeout: 20_000,
  });
  await preference.click();
  expect((await preferenceResponse).ok()).toBe(true);
  await expect(preference).not.toBeChecked();
}

async function enablePushAndDisableChildCheckin(page: Page, fixture: PushPreferenceFixture): Promise<void> {
  await resetPushMockRole(page, "parent");
  await expect(page.getByRole("heading", { name: "Мой ребёнок" })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText(fixture.child.name).first()).toBeVisible();
  await expect(page.getByText("Следите за ребенком")).toBeVisible();

  const subscribeResponse = page.waitForResponse(isSubscribeResponse, {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Включить push-уведомления" }).click();
  expect((await subscribeResponse).status()).toBe(201);

  await reloadWithPreferences(page);
  const childCheckinPreference = await ensurePreferenceChecked(page, "Чек-ин ребенка");
  await expect(page.getByLabel("Абонемент ребенка")).toBeChecked();

  await disablePreference(page, childCheckinPreference);
  await expect(page.getByLabel("Абонемент ребенка")).toBeChecked();
}

async function resetPushMockRole(page: Page, role: "parent" | "student"): Promise<void> {
  await page.evaluate((nextRole) => {
    window.localStorage.setItem("push_e2e_role", nextRole);
    window.localStorage.removeItem("push_e2e_permission_granted");
    window.localStorage.removeItem("push_prompted");
  }, role);
}

async function enableStudentPushAndDisableTrainingReminders(
  page: Page,
  fixture: PushPreferenceFixture,
): Promise<void> {
  await resetPushMockRole(page, "student");
  await page.goto("/student/profile");
  await expect(page.getByRole("heading", { name: fixture.student.name })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByRole("button", { name: "Включить" })).toBeVisible();

  const subscribeResponse = page.waitForResponse(isSubscribeResponse, {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Включить" }).click();
  expect((await subscribeResponse).status()).toBe(201);

  await reloadWithPreferences(page);
  const trainingReminderPreference = await ensurePreferenceChecked(
    page,
    "Напоминания о тренировках",
  );
  await expect(page.getByLabel("Абонемент")).toBeChecked();

  await disablePreference(page, trainingReminderPreference);
  await expect(page.getByLabel("Абонемент")).toBeChecked();
}

async function logoutFromStudentProfile(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Выйти" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Вход в кабинет" })).toBeVisible();

  await page.goto("/student/profile");
  await expect(page).toHaveURL(/\/login$/);
}

function runBackendAssert(fixturePath: string): void {
  const command = process.env.REAL_STACK_E2E_ASSERT_COMMAND;
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = command
    ? execSync(command, {
        cwd: repoRoot,
        encoding: "utf8",
        env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
        stdio: ["ignore", "pipe", "pipe"],
      })
    : execFileSync(
        pythonBin,
        ["manage.py", "assert_push_notification_preferences_e2e", "--fixture", fixturePath],
        {
          cwd: repoRoot,
          encoding: "utf8",
          env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
          stdio: ["ignore", "pipe", "pipe"],
        },
      );

  const result = JSON.parse(output) as BackendAssertResponse;
  expect(result.ok).toBe(true);
}

test("real-stack parent and student manage push preferences and student logs out", async ({ context, page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await installPushMocks(context, fixture);
  await login(page, fixture.parent, /\/parent\/?$/);
  await enablePushAndDisableChildCheckin(page, fixture);
  await login(page, fixture.student, /\/student\/?$/);
  await enableStudentPushAndDisableTrainingReminders(page, fixture);
  await logoutFromStudentProfile(page);
  runBackendAssert(fixturePath);
});

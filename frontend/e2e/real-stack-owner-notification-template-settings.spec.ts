import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface OwnerNotificationTemplateSettingsFixture {
  owner: {
    email: string;
    password: string;
  };
  expected: {
    max_push_per_week: number;
    feedback_delay_hours: number;
    quiet_hours_start: string;
    quiet_hours_end: string;
    updated_title: string;
    updated_body: string;
    expiry_days_before: number;
  };
  admin_push: {
    endpoint: string;
  };
}

interface BackendAssertResponse {
  ok?: boolean;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to an owner notification template settings fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): OwnerNotificationTemplateSettingsFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<OwnerNotificationTemplateSettingsFixture>;
  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.expected?.updated_title || !data.expected.updated_body) {
    throw new Error("Fixture notification template expectations are required.");
  }
  if (!Number.isFinite(data.expected?.expiry_days_before)) {
    throw new Error("Fixture expiry notification lead-time expectation is required.");
  }
  if (!data.admin_push?.endpoint) {
    throw new Error("Fixture admin push endpoint is required.");
  }
  return data as OwnerNotificationTemplateSettingsFixture;
}

function responseFor(pathname: string): (response: Response) => boolean {
  return (response: Response) => {
    const url = new URL(response.url());
    return url.pathname === pathname && response.request().method() === "POST";
  };
}

async function loginAsOwner(page: Page, fixture: OwnerNotificationTemplateSettingsFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/?$/);
}

async function installAdminPushMock(page: Page, endpoint: string): Promise<void> {
  await page.addInitScript((mockEndpoint) => {
    localStorage.setItem("adminPushSubscribed", "true");
    localStorage.removeItem("adminPushDismissed");

    const subscription = {
      endpoint: mockEndpoint,
      unsubscribe: async () => true,
      getKey: () => new Uint8Array([1, 2, 3]).buffer,
      toJSON: () => ({
        endpoint: mockEndpoint,
        keys: {
          p256dh: "AQID",
          auth: "AQID",
        },
      }),
    };

    Object.defineProperty(window, "Notification", {
      configurable: true,
      value: class MockNotification {
        static permission = "granted";
        static requestPermission = async () => "granted";
      },
    });
    Object.defineProperty(window, "PushManager", {
      configurable: true,
      value: class MockPushManager {},
    });
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: {
        ready: Promise.resolve({
          pushManager: {
            getSubscription: async () => subscription,
            subscribe: async () => subscription,
          },
        }),
        register: async () => ({
          pushManager: {
            getSubscription: async () => subscription,
            subscribe: async () => subscription,
          },
        }),
      },
    });
  }, endpoint);
}

async function saveNotificationSettings(page: Page, fixture: OwnerNotificationTemplateSettingsFixture): Promise<void> {
  const expected = fixture.expected;

  await page.goto(backendUrl("/dashboard/settings/notifications/"));
  await expect(page.getByRole("heading", { name: "Настройки" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Шаблоны уведомлений" })).toBeVisible();
  await expect(page.getByText("Абонементы")).toBeVisible();
  await expect(page.getByText("Вовлечение")).toBeVisible();

  await page.locator("input[name='max_push_per_week']").fill(String(expected.max_push_per_week));
  await page.locator("input[name='feedback_delay_hours']").fill(String(expected.feedback_delay_hours));
  await page.locator("input[name='quiet_hours_start']").fill(expected.quiet_hours_start);
  await page.locator("input[name='quiet_hours_end']").fill(expected.quiet_hours_end);

  const saveResponse = page.waitForResponse(responseFor("/dashboard/settings/notifications/"), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Сохранить", exact: true }).click();
  expect((await saveResponse).ok()).toBe(true);
  await expect(page.getByText("Настройки сохранены")).toBeVisible({ timeout: 20_000 });
}

function trainingReminderRow(page: Page) {
  return page
    .locator("div.flex.items-center.justify-between.py-3")
    .filter({ hasText: "Напоминание о тренировке" })
    .first();
}

function subscriptionExpirySevenDayRow(page: Page) {
  return page
    .locator("div.flex.items-center.justify-between.py-3")
    .filter({ hasText: "Абонемент истекает через 7 дней" })
    .first();
}

async function toggleAndEditReminderTemplate(
  page: Page,
  fixture: OwnerNotificationTemplateSettingsFixture,
): Promise<void> {
  const row = trainingReminderRow(page);
  await expect(row).toBeVisible();

  const toggleResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname.match(/\/dashboard\/settings\/notifications\/templates\/\d+\/toggle\/$/)
      !== null && response.request().method() === "POST",
    { timeout: 20_000 },
  );
  await row.getByRole("switch", { name: /Напоминание о тренировке.*включено/ }).click();
  expect((await toggleResponse).ok()).toBe(true);
  await expect(page.getByRole("switch", { name: /Напоминание о тренировке.*выключено/ })).toBeVisible({
    timeout: 20_000,
  });

  await trainingReminderRow(page).getByTitle("Редактировать").click();
  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "ШАБЛОН УВЕДОМЛЕНИЯ" })).toBeVisible();
  await panel.locator("input[name='title_template']").fill(fixture.expected.updated_title);
  await panel.locator("textarea[name='body_template']").fill(fixture.expected.updated_body);

  const saveResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname.match(/\/dashboard\/settings\/notifications\/templates\/\d+\/form\/$/)
      !== null && response.request().method() === "POST",
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "СОХРАНИТЬ" }).click();
  expect((await saveResponse).status()).toBe(204);
  await expect(page).toHaveURL(/\/dashboard\/settings\/notifications\/?$/);
  await expect(page.getByText(fixture.expected.updated_body)).toBeVisible({ timeout: 20_000 });
}

async function editSubscriptionExpiryLeadTime(
  page: Page,
  fixture: OwnerNotificationTemplateSettingsFixture,
): Promise<void> {
  const row = subscriptionExpirySevenDayRow(page);
  await expect(row).toBeVisible();

  await row.getByTitle("Редактировать").click();
  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "ШАБЛОН УВЕДОМЛЕНИЯ" })).toBeVisible();
  await expect(panel.locator("input[name='days_before']")).toBeVisible();
  await panel.locator("input[name='days_before']").fill(String(fixture.expected.expiry_days_before));

  const saveResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname.match(/\/dashboard\/settings\/notifications\/templates\/\d+\/form\/$/)
      !== null && response.request().method() === "POST",
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "СОХРАНИТЬ" }).click();
  expect((await saveResponse).status()).toBe(204);
  await expect(page).toHaveURL(/\/dashboard\/settings\/notifications\/?$/);
}

async function unsubscribeAdminPush(page: Page): Promise<void> {
  await page.goto(backendUrl("/dashboard/settings/notifications/"));
  await expect(page.getByText("Push-уведомления включены")).toBeVisible({ timeout: 20_000 });

  const unsubscribeResponse = page.waitForResponse(responseFor("/api/notifications/unsubscribe/"), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Отключить" }).click();
  expect((await unsubscribeResponse).status()).toBe(204);
  await expect(page.getByText("Push-уведомления включены")).not.toBeVisible({ timeout: 20_000 });
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
        ["manage.py", "assert_owner_notification_template_settings_e2e", "--fixture", fixturePath],
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

test("real-stack owner configures notification timings and automatic templates", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await installAdminPushMock(page, fixture.admin_push.endpoint);
  await loginAsOwner(page, fixture);
  await saveNotificationSettings(page, fixture);
  await toggleAndEditReminderTemplate(page, fixture);
  await editSubscriptionExpiryLeadTime(page, fixture);
  await unsubscribeAdminPush(page);
  runBackendAssert(fixturePath);
});

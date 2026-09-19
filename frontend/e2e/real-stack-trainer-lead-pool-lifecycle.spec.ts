import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Browser, type BrowserContext, type Locator, type Page, type Response } from "@playwright/test";

const LEADS_PATH = "/api/leads/";
const PREFERENCES_PATH = "/api/notifications/preferences/";
const LOGOUT_PATH = "/api/auth/logout/";
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:4173";

interface Credentials {
  email: string;
  password: string;
}

interface FixtureLead {
  id: number;
  first_name: string;
  phone: string;
  masked_phone: string;
}

interface TrainerLeadPoolLifecycleFixture {
  trainer: Credentials;
  other_trainer: Credentials;
  pool_lead: FixtureLead;
  conflict_lead: FixtureLead;
  loss_lead: FixtureLead;
  hidden_other_trainer_lead: FixtureLead;
  foreign_pool_lead: FixtureLead;
  expected: {
    release_reason: string;
    loss_reason: string;
    profile_trainer_name: string;
    profile_club_name: string;
    profile_disabled_categories: string[];
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a trainer lead pool fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): TrainerLeadPoolLifecycleFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<TrainerLeadPoolLifecycleFixture>;
  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  if (!data.other_trainer?.email || !data.other_trainer.password) {
    throw new Error("Fixture other trainer credentials are required.");
  }
  for (const key of [
    "pool_lead",
    "conflict_lead",
    "loss_lead",
    "hidden_other_trainer_lead",
    "foreign_pool_lead",
  ] as const) {
    if (!data[key]?.id || !data[key]?.first_name || !data[key]?.phone || !data[key]?.masked_phone) {
      throw new Error(`Fixture ${key} data is required.`);
    }
  }
  if (!data.expected?.release_reason || !data.expected.loss_reason) {
    throw new Error("Fixture expected release/loss reasons are required.");
  }
  if (
    !data.expected.profile_trainer_name ||
    !data.expected.profile_club_name ||
    !data.expected.profile_disabled_categories?.length
  ) {
    throw new Error("Fixture trainer profile expectations are required.");
  }
  return data as TrainerLeadPoolLifecycleFixture;
}

function isClaimResponse(leadId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `${LEADS_PATH}${leadId}/claim` && response.request().method() === "POST";
  };
}

function isReleaseResponse(leadId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `${LEADS_PATH}${leadId}/release` && response.request().method() === "POST";
  };
}

function isLoseResponse(leadId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `${LEADS_PATH}${leadId}/lose` && response.request().method() === "POST";
  };
}

function isPreferencesPutResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === PREFERENCES_PATH && response.request().method() === "PUT";
}

function isLogoutResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === LOGOUT_PATH && response.request().method() === "POST";
}

async function loginAsTrainer(page: Page, credentials: Credentials): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill(credentials.email);
  await page.getByLabel("Пароль").fill(credentials.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

async function installGrantedPushMocks(context: BrowserContext): Promise<void> {
  await context.addInitScript(() => {
    const subscription = {
      endpoint: "https://push-e2e.invalid/trainer-profile-device",
      toJSON: () => ({
        endpoint: "https://push-e2e.invalid/trainer-profile-device",
        keys: {
          p256dh: "trainer-profile-p256dh",
          auth: "trainer-profile-auth",
        },
      }),
      unsubscribe: async () => true,
    };

    Object.defineProperty(window, "Notification", {
      configurable: true,
      value: {
        get permission() {
          return "granted";
        },
        requestPermission: async () => "granted",
      },
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
            getSubscription: async () => subscription,
            subscribe: async () => subscription,
          },
        }),
      },
    });
  });
}

async function openPool(page: Page): Promise<void> {
  await page.goto("/trainer/leads");
  await expect(page.getByRole("heading", { name: "Заявки" })).toBeVisible();
  await page.getByRole("tab", { name: "Свободные заявки" }).click();
}

function poolCard(page: Page, lead: FixtureLead): Locator {
  return page.locator("article").filter({ hasText: lead.first_name });
}

async function claimPoolLead(page: Page, lead: FixtureLead): Promise<Response> {
  const responsePromise = page.waitForResponse(isClaimResponse(lead.id), {
    timeout: 20_000,
  });
  await poolCard(page, lead).getByRole("button", { name: "Забрать" }).click();
  return responsePromise;
}

async function otherTrainerClaimsConflictLead(
  browser: Browser,
  fixture: TrainerLeadPoolLifecycleFixture,
): Promise<void> {
  const context = await browser.newContext({ baseURL });
  try {
    const otherPage = await context.newPage();
    await loginAsTrainer(otherPage, fixture.other_trainer);
    await openPool(otherPage);
    await expect(poolCard(otherPage, fixture.conflict_lead)).toBeVisible();
    const response = await claimPoolLead(otherPage, fixture.conflict_lead);
    expect(response.ok()).toBe(true);
  } finally {
    await context.close();
  }
}

async function releaseClaimedLead(page: Page, fixture: TrainerLeadPoolLifecycleFixture): Promise<void> {
  await page.getByRole("button", { name: new RegExp(fixture.pool_lead.first_name) }).click();
  await expect(page.getByRole("heading", { name: fixture.pool_lead.first_name })).toBeVisible();
  await page.getByRole("button", { name: "Передать администратору" }).click();
  await page.getByLabel("Причина передачи *").fill(fixture.expected.release_reason);

  const releaseResponse = page.waitForResponse(isReleaseResponse(fixture.pool_lead.id), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Передать администратору" }).click();
  expect((await releaseResponse).ok()).toBe(true);
  await expect(page.getByRole("heading", { name: fixture.pool_lead.first_name })).toBeHidden();
}

async function loseLead(page: Page, fixture: TrainerLeadPoolLifecycleFixture): Promise<void> {
  await page.getByRole("button", { name: new RegExp(fixture.loss_lead.first_name) }).click();
  await expect(page.getByRole("heading", { name: fixture.loss_lead.first_name })).toBeVisible();
  await page.getByRole("button", { name: "Потерян" }).click();
  await page.getByLabel("Причина потери *").selectOption(fixture.expected.loss_reason);

  const loseResponse = page.waitForResponse(isLoseResponse(fixture.loss_lead.id), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Сохранить потерю" }).click();
  expect((await loseResponse).ok()).toBe(true);
  await expect(page.getByRole("heading", { name: fixture.loss_lead.first_name })).toBeHidden();
}

async function assertTrainerProfileAndPreferences(
  page: Page,
  fixture: TrainerLeadPoolLifecycleFixture,
): Promise<void> {
  await page.goto("/trainer/profile");
  await expect(page.getByRole("heading", { name: fixture.expected.profile_trainer_name })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText("Клуб", { exact: true }).locator("..")).toContainText(
    fixture.expected.profile_club_name,
  );
  await expect(page.getByRole("button", { name: /мой заработок/i })).toBeVisible();
  await expect(page.getByText(/за текущий месяц/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Уведомления" })).toBeVisible();

  const tasksSwitch = page.getByRole("switch", { name: "Задачи" });
  await expect(tasksSwitch).toBeChecked();
  const preferenceResponse = page.waitForResponse(isPreferencesPutResponse, {
    timeout: 20_000,
  });
  await tasksSwitch.click();
  expect((await preferenceResponse).ok()).toBe(true);
  await expect(tasksSwitch).not.toBeChecked();
}

async function logoutFromTrainerProfile(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Выйти из аккаунта" }).click();
  await expect(page.getByText("Вы уверены?")).toBeVisible();
  await page.getByRole("button", { name: "Отмена" }).click();
  await expect(page.getByRole("button", { name: "Выйти из аккаунта" })).toBeVisible();
  await page.getByRole("button", { name: "Выйти из аккаунта" }).click();
  const logoutResponse = page.waitForResponse(isLogoutResponse, {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Выйти" }).click();
  expect((await logoutResponse).status()).toBe(204);
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Вход в кабинет" })).toBeVisible();
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
        ["manage.py", "assert_trainer_lead_pool_lifecycle_e2e", "--fixture", fixturePath],
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

test("real-stack trainer lead pool claim, conflict, release, and loss lifecycle pass", async ({
  page,
  browser,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await installGrantedPushMocks(page.context());
  await loginAsTrainer(page, fixture.trainer);
  await openPool(page);

  await expect(poolCard(page, fixture.pool_lead)).toContainText(fixture.pool_lead.masked_phone);
  await expect(page.getByText(fixture.pool_lead.phone)).not.toBeVisible();
  await expect(poolCard(page, fixture.conflict_lead)).toBeVisible();
  await expect(page.getByText(fixture.hidden_other_trainer_lead.first_name)).not.toBeVisible();
  await expect(page.getByText(fixture.foreign_pool_lead.first_name)).not.toBeVisible();

  await otherTrainerClaimsConflictLead(browser, fixture);
  const conflictResponse = await claimPoolLead(page, fixture.conflict_lead);
  expect(conflictResponse.status()).toBe(409);
  await expect(page.getByText("Заявку уже забрали")).toBeVisible();

  const claimResponse = await claimPoolLead(page, fixture.pool_lead);
  expect(claimResponse.ok()).toBe(true);
  const claimedLeadCard = page.getByRole("button", { name: new RegExp(fixture.pool_lead.first_name) });
  await expect(claimedLeadCard).toBeVisible();
  await expect(claimedLeadCard).toContainText(fixture.pool_lead.phone);
  await releaseClaimedLead(page, fixture);

  await page.getByRole("tab", { name: "Свободные заявки" }).click();
  await expect(poolCard(page, fixture.pool_lead)).toContainText(fixture.pool_lead.masked_phone);
  await expect(poolCard(page, fixture.conflict_lead)).toBeHidden();

  await page.getByRole("tab", { name: "Мои" }).click();
  await expect(page.getByRole("button", { name: new RegExp(fixture.loss_lead.first_name) })).toBeVisible();
  await loseLead(page, fixture);
  await expect(page.getByText(fixture.loss_lead.first_name)).not.toBeVisible();
  await page.getByRole("tab", { name: "Свободные заявки" }).click();
  await expect(page.getByText(fixture.loss_lead.first_name)).not.toBeVisible();

  await assertTrainerProfileAndPreferences(page, fixture);
  runBackendAssert(fixturePath);
  await logoutFromTrainerProfile(page);
});

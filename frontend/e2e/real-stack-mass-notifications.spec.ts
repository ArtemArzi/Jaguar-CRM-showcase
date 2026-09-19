import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

const MASS_NOTIFICATIONS_PATH = "/api/notifications/mass/";
const MASS_NOTIFICATIONS_PREVIEW_PATH = "/api/notifications/mass/preview/";

interface MassNotificationsFixture {
  owner: {
    email: string;
    password: string;
  };
  trainer: {
    email: string;
    password: string;
  };
  owned_schedule_id: number;
  foreign_schedule_id: number;
  expected: {
    owner_message: string;
    trainer_message: string;
    trainer_club_message: string;
    trainer_foreign_message: string;
    recipient_count: number;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a mass notifications fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): MassNotificationsFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<MassNotificationsFixture>;

  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  if (!data.owned_schedule_id || !data.foreign_schedule_id) {
    throw new Error("Fixture schedule ids are required.");
  }
  if (
    !data.expected?.owner_message ||
    !data.expected.trainer_message ||
    !data.expected.trainer_club_message ||
    !data.expected.trainer_foreign_message ||
    !data.expected.recipient_count
  ) {
    throw new Error("Fixture notification expectations are required.");
  }

  return data as MassNotificationsFixture;
}

async function loginToDashboard(page: Page, fixture: MassNotificationsFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

function isDashboardPreviewResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === "/dashboard/notifications/preview/" && response.request().method() === "POST";
}

function isDashboardNotificationPost(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === "/dashboard/notifications/" && response.request().method() === "POST";
}

async function sendOwnerGroupNotification(page: Page, fixture: MassNotificationsFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/notifications/"));
  await expect(page.getByRole("heading", { name: "PUSH-УВЕДОМЛЕНИЯ" })).toBeVisible();

  await page.getByText("By group").click();
  await page.getByPlaceholder("ID группы, локации или статус").fill(String(fixture.owned_schedule_id));

  const previewResponse = page.waitForResponse(isDashboardPreviewResponse, {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Рассчитать" }).click();
  expect((await previewResponse).ok()).toBe(true);
  await expect(page.locator("#preview-count")).toContainText(
    `Will be sent to ${fixture.expected.recipient_count}`,
  );

  await page.locator("textarea[name='text']").fill(fixture.expected.owner_message);
  page.once("dialog", (dialog) => void dialog.accept());
  const sendResponse = page.waitForResponse(isDashboardNotificationPost, {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Отправить" }).click();
  expect((await sendResponse).ok()).toBe(true);

  await expect(
    page.getByText(`Notification sent to ${fixture.expected.recipient_count} recipients`),
  ).toBeVisible();
  await expect(page.getByText(fixture.expected.owner_message)).toBeVisible();
}

async function loginAsTrainer(page: Page, fixture: MassNotificationsFixture): Promise<string> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(fixture.trainer.email);
  await page.getByLabel("Пароль").fill(fixture.trainer.password);
  const loginResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/_allauth/app/v1/auth/login" && response.request().method() === "POST";
  }, { timeout: 20_000 });
  await page.getByRole("button", { name: "Войти" }).click();
  const response = await loginResponse;
  expect(response.ok()).toBe(true);
  const body = await response.json() as { meta?: { access_token?: unknown } };
  const accessToken = body.meta?.access_token;
  expect(typeof accessToken).toBe("string");
  await expect(page).toHaveURL(/\/trainer\/?$/);
  return accessToken as string;
}

async function assertTrainerNotificationRules(
  page: Page,
  fixture: MassNotificationsFixture,
  accessToken: string,
): Promise<void> {
  const headers = { Authorization: `Bearer ${accessToken}` };
  const ownPreview = await page.request.post(backendUrl(MASS_NOTIFICATIONS_PREVIEW_PATH), {
    headers,
    data: {
      text: "Preview own group",
      segment_type: "group",
      segment_filter: { schedule_id: fixture.owned_schedule_id },
    },
  });
  expect(ownPreview.status()).toBe(200);
  expect((await ownPreview.json()).recipient_count).toBe(fixture.expected.recipient_count);

  const foreignPreview = await page.request.post(backendUrl(MASS_NOTIFICATIONS_PREVIEW_PATH), {
    headers,
    data: {
      text: "Preview foreign group",
      segment_type: "group",
      segment_filter: { schedule_id: fixture.foreign_schedule_id },
    },
  });
  expect(foreignPreview.status()).toBe(403);

  const clubSend = await page.request.post(backendUrl(MASS_NOTIFICATIONS_PATH), {
    headers,
    data: {
      text: fixture.expected.trainer_club_message,
      segment_type: "club",
      segment_filter: {},
    },
  });
  expect(clubSend.status()).toBe(403);

  const foreignSend = await page.request.post(backendUrl(MASS_NOTIFICATIONS_PATH), {
    headers,
    data: {
      text: fixture.expected.trainer_foreign_message,
      segment_type: "group",
      segment_filter: { schedule_id: fixture.foreign_schedule_id },
    },
  });
  expect(foreignSend.status()).toBe(403);

  const ownSend = await page.request.post(backendUrl(MASS_NOTIFICATIONS_PATH), {
    headers,
    data: {
      text: fixture.expected.trainer_message,
      segment_type: "group",
      segment_filter: { schedule_id: fixture.owned_schedule_id },
    },
  });
  expect(ownSend.status()).toBe(201);
  expect((await ownSend.json()).recipient_count).toBe(fixture.expected.recipient_count);
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
        ["manage.py", "assert_mass_notifications_e2e", "--fixture", fixturePath],
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

test("real-stack owner and trainer mass notifications respect group scoping", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginToDashboard(page, fixture);
  await sendOwnerGroupNotification(page, fixture);

  const trainerAccessToken = await loginAsTrainer(page, fixture);
  await assertTrainerNotificationRules(page, fixture, trainerAccessToken);

  runBackendAssert(fixturePath);
});

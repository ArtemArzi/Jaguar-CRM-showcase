import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

const CHECKIN_PATH = "/api/checkins/kiosk/";

interface RealStackFixture {
  kiosk_pin: string;
  phone_suffix: string;
}

interface KioskCheckinResponse {
  subscription_effect?: string;
  salary_queued?: boolean;
  parent_notification_queued?: boolean;
  grade_progress_queued?: boolean;
}

interface BackendAssertResponse {
  ok?: boolean;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a real-stack fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): RealStackFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<RealStackFixture>;

  if (!data.kiosk_pin || !/^\d{6}$/.test(data.kiosk_pin)) {
    throw new Error("Fixture kiosk_pin must be a 6-digit string.");
  }
  if (!data.phone_suffix || !/^\d{4}$/.test(data.phone_suffix)) {
    throw new Error("Fixture phone_suffix must be a 4-digit string.");
  }

  return {
    kiosk_pin: data.kiosk_pin,
    phone_suffix: data.phone_suffix,
  };
}

function isKioskCheckinResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === CHECKIN_PATH && response.request().method() === "POST";
}

async function enterPin(page: Page, pin: string): Promise<void> {
  await page.goto("/kiosk/");

  for (const [index, digit] of [...pin].entries()) {
    await page.getByLabel(`PIN цифра ${index + 1}`).fill(digit);
  }

  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
}

async function enterPhoneSuffix(page: Page, phoneSuffix: string): Promise<Response> {
  const checkinResponse = page.waitForResponse(isKioskCheckinResponse, {
    timeout: 20_000,
  });

  for (const digit of phoneSuffix) {
    await page.getByRole("button", { name: `Цифра ${digit}` }).click();
  }

  const nextStep = await Promise.race([
    checkinResponse.then((response) => ({
      type: "checkin" as const,
      response,
    })),
    page
      .getByText("Выберите тренировку")
      .waitFor({ state: "visible", timeout: 5_000 })
      .then(() => ({ type: "manual-selection" as const }))
      .catch(() => ({ type: "wait-for-checkin" as const })),
  ]);

  if (nextStep.type === "checkin") {
    return nextStep.response;
  }

  if (nextStep.type === "wait-for-checkin") {
    return await checkinResponse;
  }

  await page.getByRole("button", { name: /00:00-23:59/ }).first().click();

  return await checkinResponse;
}

async function assertVisibleCheckinSuccess(
  page: Page,
  checkinResponse: Response,
): Promise<void> {
  expect(checkinResponse.ok()).toBe(true);
  const result = (await checkinResponse.json()) as KioskCheckinResponse;

  expect(result.subscription_effect).toBe("deducted");
  await expect(page.getByText("Посещение сохранено")).toBeVisible();
  await expect(page.getByText("Абонемент списан")).toBeVisible();
  await expect(page.getByText("Зарплата тренеру в очереди")).not.toBeVisible();
  await expect(page.getByText("Уведомление родителю в очереди")).not.toBeVisible();
  await expect(page.getByText("Прогресс в очереди")).not.toBeVisible();
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
        ["manage.py", "assert_real_stack_e2e", "--fixture", fixturePath],
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

test("real-stack kiosk check-in completes browser flow and backend side-effect assertions", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await enterPin(page, fixture.kiosk_pin);
  const checkinResponse = await enterPhoneSuffix(page, fixture.phone_suffix);
  await assertVisibleCheckinSuccess(page, checkinResponse);
  runBackendAssert(fixturePath);
});

import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

const GUEST_BOOK_AND_CHECKIN_PATH = "/api/checkins/kiosk/guest-book-and-checkin/";

interface KioskGuestScenario {
  phone_suffix: string;
  schedule_id: number;
  training_type_id: number;
  checkin_date: string;
  expected: {
    group_name: string;
    student_name: string;
    is_debt: boolean;
    debt_effect: string;
    subscription_effect: string;
  };
}

interface KioskGuestBookCheckinFixture {
  kiosk_pin: string;
  phone_suffix: string;
  schedule_id: number;
  training_type_id: number;
  checkin_date: string;
  scenarios?: Record<string, KioskGuestScenario>;
  expected: {
    group_name: string;
    student_name: string;
  };
}

interface KioskGuestCheckinResponse {
  checkin_id?: number;
  is_debt?: boolean;
  subscription_id?: number | null;
  alerts?: unknown[];
  created?: boolean;
  duplicate?: boolean;
  subscription_effect?: string;
  debt_effect?: string;
}

interface BackendAssertResponse {
  ok?: boolean;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a kiosk guest fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): KioskGuestBookCheckinFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<KioskGuestBookCheckinFixture>;

  if (!data.kiosk_pin || !/^\d{6}$/.test(data.kiosk_pin)) {
    throw new Error("Fixture kiosk_pin must be a 6-digit string.");
  }
  if (!data.phone_suffix || !/^\d{4}$/.test(data.phone_suffix)) {
    throw new Error("Fixture phone_suffix must be a 4-digit string.");
  }
  if (!data.schedule_id || !data.training_type_id || !data.checkin_date) {
    throw new Error("Fixture schedule, training type, and date are required.");
  }
  if (!data.expected?.group_name || !data.expected.student_name) {
    throw new Error("Fixture expected display values are required.");
  }
  for (const [name, scenario] of Object.entries(data.scenarios ?? {})) {
    if (!scenario.phone_suffix || !/^\d{4}$/.test(scenario.phone_suffix)) {
      throw new Error(`Scenario ${name} phone_suffix must be a 4-digit string.`);
    }
    if (!scenario.schedule_id || !scenario.training_type_id || !scenario.checkin_date) {
      throw new Error(`Scenario ${name} schedule, training type, and date are required.`);
    }
    if (!scenario.expected?.group_name || !scenario.expected.student_name) {
      throw new Error(`Scenario ${name} expected display values are required.`);
    }
  }

  return data as KioskGuestBookCheckinFixture;
}

function isGuestBookAndCheckinResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === GUEST_BOOK_AND_CHECKIN_PATH && response.request().method() === "POST";
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function enterPin(page: Page, pin: string): Promise<void> {
  await page.goto("/kiosk/");

  for (const [index, digit] of [...pin].entries()) {
    await page.getByLabel(`PIN цифра ${index + 1}`).fill(digit);
  }

  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
}

async function enterPhoneSuffixAndWaitForGuestCheckin(
  page: Page,
  scenario: KioskGuestScenario,
): Promise<Response> {
  const guestResponse = page.waitForResponse(isGuestBookAndCheckinResponse, {
    timeout: 20_000,
  });

  for (const digit of scenario.phone_suffix) {
    await page.getByRole("button", { name: `Цифра ${digit}` }).click();
  }

  const nextStep = await Promise.race([
    guestResponse.then((response) => ({
      type: "guest-response" as const,
      response,
    })),
    page
      .getByText("Выберите тренировку")
      .waitFor({ state: "visible", timeout: 5_000 })
      .then(() => ({ type: "manual-selection" as const }))
      .catch(() => ({ type: "wait-for-guest-response" as const })),
  ]);

  if (nextStep.type === "guest-response") {
    return nextStep.response;
  }

  if (nextStep.type === "manual-selection") {
    await page
      .getByRole("button", {
        name: new RegExp(escapeRegExp(scenario.expected.group_name)),
      })
      .click();
  }

  return await guestResponse;
}

async function assertVisibleGuestSuccess(
  page: Page,
  response: Response,
  scenario: KioskGuestScenario,
): Promise<void> {
  expect(response.ok()).toBe(true);
  const result = (await response.json()) as KioskGuestCheckinResponse;

  expect(result.checkin_id).toBeGreaterThan(0);
  expect(result.created).toBe(true);
  expect(result.duplicate).toBe(false);
  expect(result.is_debt).toBe(scenario.expected.is_debt);
  if (scenario.expected.is_debt) {
    expect(result.subscription_id ?? null).toBeNull();
  } else if (scenario.expected.subscription_effect === "deducted") {
    expect(result.subscription_id).toBeGreaterThan(0);
  } else {
    expect(result.subscription_id ?? null).toBeNull();
  }
  expect(result.subscription_effect).toBe(scenario.expected.subscription_effect);
  expect(result.debt_effect).toBe(scenario.expected.debt_effect);
  expect(result.alerts).toEqual([]);

  await expect(page.getByText(scenario.expected.student_name)).toBeVisible();
  await expect(page.getByText("Посещение сохранено")).toBeVisible();
  if (scenario.expected.is_debt) {
    await expect(page.getByText("Занятие в долг")).toBeVisible();
  } else if (scenario.expected.subscription_effect === "deducted") {
    await expect(page.getByText("Абонемент списан")).toBeVisible();
  } else {
    await expect(page.getByText("Занятие в долг")).not.toBeVisible();
    await expect(page.getByText("Абонемент списан")).not.toBeVisible();
  }
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
        ["manage.py", "assert_kiosk_guest_book_checkin_e2e", "--fixture", fixturePath],
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

test("real-stack kiosk books a guest visit and immediately checks in the student", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);
  const scenarios = fixture.scenarios ?? {
    subscription: {
      phone_suffix: fixture.phone_suffix,
      schedule_id: fixture.schedule_id,
      training_type_id: fixture.training_type_id,
      checkin_date: fixture.checkin_date,
      expected: {
        group_name: fixture.expected.group_name,
        student_name: fixture.expected.student_name,
        is_debt: false,
        debt_effect: "none",
        subscription_effect: "deducted",
      },
    },
  };

  await enterPin(page, fixture.kiosk_pin);
  for (const scenario of Object.values(scenarios)) {
    const guestResponse = await enterPhoneSuffixAndWaitForGuestCheckin(page, scenario);
    await assertVisibleGuestSuccess(page, guestResponse, scenario);
    await page.getByRole("button", { name: "Вернуться к вводу" }).click();
    await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
  }
  runBackendAssert(fixturePath);
});

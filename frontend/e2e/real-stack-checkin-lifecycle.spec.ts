import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

const KIOSK_CHECKIN_PATH = "/api/checkins/kiosk/";

interface CheckinLifecycleFixture {
  kiosk_pin: string;
  phone_suffix: string;
  owner: {
    email: string;
    password: string;
  };
  student: {
    name: string;
  };
  schedule_id: number;
  checkin_date: string;
  expected: {
    group_name: string;
  };
}

interface KioskCheckinResponse {
  checkin_id?: number;
  subscription_effect?: string;
}

interface BackendAssertResponse {
  ok?: boolean;
  stage?: string;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a check-in lifecycle fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): CheckinLifecycleFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<CheckinLifecycleFixture>;

  if (!data.kiosk_pin || !/^\d{6}$/.test(data.kiosk_pin)) {
    throw new Error("Fixture kiosk_pin must be a 6-digit string.");
  }
  if (!data.phone_suffix || !/^\d{4}$/.test(data.phone_suffix)) {
    throw new Error("Fixture phone_suffix must be a 4-digit string.");
  }
  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.student?.name || !data.schedule_id || !data.checkin_date) {
    throw new Error("Fixture student, schedule and date are required.");
  }
  if (!data.expected?.group_name) {
    throw new Error("Fixture expected group name is required.");
  }

  return data as CheckinLifecycleFixture;
}

function isKioskCheckinResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === KIOSK_CHECKIN_PATH && response.request().method() === "POST";
}

function isSessionCheckinsResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === "/dashboard/trainers/sessions/checkins/" && response.request().method() === "GET";
}

function isCancelCheckinResponse(checkinId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `/dashboard/checkin/${checkinId}/cancel/` && response.request().method() === "POST";
  };
}

function compactDate(value: string): string {
  return value.replaceAll("-", "");
}

function reversedName(value: string): string {
  return value.trim().split(/\s+/).reverse().join(" ");
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

async function createKioskCheckin(page: Page, fixture: CheckinLifecycleFixture): Promise<number> {
  await enterPin(page, fixture.kiosk_pin);
  const checkinResponse = await enterPhoneSuffix(page, fixture.phone_suffix);
  expect(checkinResponse.ok()).toBe(true);

  const result = (await checkinResponse.json()) as KioskCheckinResponse;
  expect(result.subscription_effect).toBe("deducted");
  expect(result.checkin_id).toEqual(expect.any(Number));
  await expect(page.getByText("Посещение сохранено")).toBeVisible();
  await expect(page.getByText("Абонемент списан")).toBeVisible();
  return result.checkin_id as number;
}

async function loginToDashboard(page: Page, fixture: CheckinLifecycleFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

async function cancelCheckinFromDashboard(
  page: Page,
  fixture: CheckinLifecycleFixture,
  checkinId: number,
): Promise<void> {
  const sessionsUrl = `/dashboard/trainers/sessions/?date_from=${fixture.checkin_date}&date_to=${fixture.checkin_date}&schedule_id=${fixture.schedule_id}`;
  await page.goto(backendUrl(sessionsUrl));
  await expect(page.getByRole("heading", { name: "ТРЕНЕРЫ" })).toBeVisible();
  const sessionGroupCell = page.getByRole("cell", {
    name: fixture.expected.group_name,
    exact: true,
  });
  await expect(sessionGroupCell).toBeVisible();

  const checkinsResponse = page.waitForResponse(isSessionCheckinsResponse, {
    timeout: 20_000,
  });
  await sessionGroupCell.click();
  expect((await checkinsResponse).ok()).toBe(true);

  const checkins = page.locator(`#checkins-${fixture.schedule_id}-${compactDate(fixture.checkin_date)}`);
  const studentName = checkins.getByText(new RegExp(`${fixture.student.name}|${reversedName(fixture.student.name)}`));
  await expect(studentName).toBeVisible();
  await checkins.getByRole("button", { name: "✕" }).click();
  await expect(checkins.getByText("Отменить?")).toBeVisible();

  const cancelResponse = page.waitForResponse(isCancelCheckinResponse(checkinId), {
    timeout: 20_000,
  });
  await checkins.getByRole("button", { name: "да" }).click();
  expect((await cancelResponse).ok()).toBe(true);
  await expect(checkins.getByText("Нет посещений")).toBeVisible();
  await expect(studentName).toBeHidden();
}

function runBackendAssert(fixturePath: string, stage: "forward" | "cancelled", checkinId: number): void {
  const command = process.env.REAL_STACK_E2E_ASSERT_COMMAND;
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const args = [
    "manage.py",
    "assert_checkin_lifecycle_e2e",
    "--fixture",
    fixturePath,
    "--stage",
    stage,
    "--checkin-id",
    String(checkinId),
  ];
  const output = command
    ? execSync(`${command} --stage ${stage} --checkin-id ${checkinId}`, {
        cwd: repoRoot,
        encoding: "utf8",
        env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
        stdio: ["ignore", "pipe", "pipe"],
      })
    : execFileSync(pythonBin, args, {
        cwd: repoRoot,
        encoding: "utf8",
        env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
        stdio: ["ignore", "pipe", "pipe"],
      });

  const result = JSON.parse(output) as BackendAssertResponse;
  expect(result.ok).toBe(true);
  expect(result.stage).toBe(stage);
}

test("real-stack kiosk check-in can be cancelled by owner with forward and reverse side-effect proof", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  const checkinId = await createKioskCheckin(page, fixture);
  runBackendAssert(fixturePath, "forward", checkinId);

  await loginToDashboard(page, fixture);
  await cancelCheckinFromDashboard(page, fixture, checkinId);
  runBackendAssert(fixturePath, "cancelled", checkinId);
});

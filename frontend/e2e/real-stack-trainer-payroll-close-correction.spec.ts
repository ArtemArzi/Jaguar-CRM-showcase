import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface TrainerPayrollCloseCorrectionFixture {
  owner: {
    email: string;
    password: string;
  };
  admin: {
    email: string;
    password: string;
    role: string;
  };
  trainer: {
    trainer_id: number;
    email: string;
    password: string;
    role: string;
    name: string;
  };
  target_trainer: {
    trainer_id: number;
    name: string;
  };
  period: {
    date_from: string;
    date_to: string;
    reason: string;
  };
  earnings: {
    corrected_id: number;
    corrected_checkin_id: number;
    blocked_id: number;
    blocked_checkin_id: number;
  };
  students: {
    corrected_name: string;
    blocked_name: string;
  };
  expected: {
    correction_reason: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a trainer payroll close fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): TrainerPayrollCloseCorrectionFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<TrainerPayrollCloseCorrectionFixture>;

  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.admin?.email || !data.admin.password || data.admin.role !== "admin") {
    throw new Error("Fixture admin credentials are required.");
  }
  if (
    !data.trainer?.trainer_id ||
    !data.trainer.name ||
    !data.trainer.email ||
    !data.trainer.password ||
    data.trainer.role !== "trainer"
  ) {
    throw new Error("Fixture source trainer and denied credentials are required.");
  }
  if (!data.target_trainer?.trainer_id || !data.target_trainer.name) {
    throw new Error("Fixture target trainer is required.");
  }
  if (!data.period?.date_from || !data.period.date_to || !data.period.reason) {
    throw new Error("Fixture payroll period is required.");
  }
  if (
    !data.earnings?.corrected_id ||
    !data.earnings.corrected_checkin_id ||
    !data.earnings.blocked_id ||
    !data.earnings.blocked_checkin_id
  ) {
    throw new Error("Fixture earning/check-in ids are required.");
  }
  if (!data.students?.corrected_name || !data.students.blocked_name) {
    throw new Error("Fixture student labels are required.");
  }
  if (!data.expected?.correction_reason) {
    throw new Error("Fixture expected correction reason is required.");
  }

  return data as TrainerPayrollCloseCorrectionFixture;
}

function trainerDetailPath(fixture: TrainerPayrollCloseCorrectionFixture): string {
  return `/dashboard/trainers/${fixture.trainer.trainer_id}/?date_from=${fixture.period.date_from}&date_to=${fixture.period.date_to}`;
}

function isCorrectionPostResponse(earningId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/dashboard/trainers/earnings/${earningId}/correction/` &&
      response.request().method() === "POST"
    );
  };
}

function isPayrollCloseResponse(fixture: TrainerPayrollCloseCorrectionFixture) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/dashboard/trainers/${fixture.trainer.trainer_id}/payroll-close/` &&
      response.request().method() === "POST"
    );
  };
}

async function loginToDashboard(
  page: Page,
  credentials: { email: string; password: string },
  options: { expectManagement: boolean } = { expectManagement: true },
): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(credentials.email);
  await page.getByLabel("Пароль").fill(credentials.password);
  await page.getByRole("button", { name: "Войти" }).click();
  if (!options.expectManagement) {
    await expect(page.getByText("Access denied: insufficient role")).toBeVisible();
    return;
  }
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

async function logoutDashboard(page: Page): Promise<void> {
  await page.goto(backendUrl("/dashboard/logout/"));
  await expect(page).toHaveURL(/\/dashboard\/login\/$/);
}

async function openTrainerPayroll(
  page: Page,
  fixture: TrainerPayrollCloseCorrectionFixture,
  expectedCorrectionButtons = 2,
): Promise<void> {
  await page.goto(backendUrl(trainerDetailPath(fixture)));
  await expect(page.getByRole("heading", { name: /PAYROLL SOURCE/ })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText("АУДИТ И КОРРЕКТИРОВКИ")).toBeVisible();
  await expect(page.getByText(`check-in #${fixture.earnings.corrected_checkin_id}`)).toBeVisible();
  await expect(page.getByText(`check-in #${fixture.earnings.blocked_checkin_id}`)).toBeVisible();
  await expect(page.getByRole("button", { name: "Корректировка выплаты" })).toHaveCount(expectedCorrectionButtons);
}

async function correctFirstEarning(page: Page, fixture: TrainerPayrollCloseCorrectionFixture): Promise<void> {
  const correctedRow = page
    .locator("div.px-5.py-4")
    .filter({ hasText: `check-in #${fixture.earnings.corrected_checkin_id}` })
    .first();
  await correctedRow.getByRole("button", { name: "Корректировка выплаты" }).click();
  await expect(page.getByRole("heading", { name: "КОРРЕКТИРОВКА ВЫПЛАТЫ" })).toBeVisible();

  await page.locator('select[name="target_trainer_id"]').selectOption(String(fixture.target_trainer.trainer_id));
  await page.locator('textarea[name="reason"]').fill(fixture.expected.correction_reason);

  const correctionResponse = page.waitForResponse(isCorrectionPostResponse(fixture.earnings.corrected_id), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "СОХРАНИТЬ КОРРЕКТИРОВКУ" }).click();
  const response = await correctionResponse;
  expect(response.ok()).toBe(true);
  await expect(page.getByText("Корректировка сохранена")).toBeVisible();
}

async function closePayrollPeriod(page: Page, fixture: TrainerPayrollCloseCorrectionFixture): Promise<void> {
  await page.goto(backendUrl(trainerDetailPath(fixture)));
  await page.getByPlaceholder("Например: выплаты за период согласованы").fill(fixture.period.reason);

  const closeResponse = page.waitForResponse(isPayrollCloseResponse(fixture), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "ЗАКРЫТЬ ПЕРИОД" }).click();
  const response = await closeResponse;
  expect(response.status()).toBe(204);

  await page.goto(backendUrl(trainerDetailPath(fixture)));
  await expect(page.getByText("Период выплат закрыт", { exact: true })).toBeVisible();
  await expect(page.getByText(fixture.period.reason).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Корректировка выплаты" })).toHaveCount(0);
}

async function assertTrainerDeniedPayrollRoute(page: Page, fixture: TrainerPayrollCloseCorrectionFixture): Promise<void> {
  const response = await page.goto(backendUrl(trainerDetailPath(fixture)));
  expect(response?.status()).toBe(403);
  await expect(page.getByText("Access denied: insufficient role")).toBeVisible();
}

function runBackendAssert(fixturePath: string, stage: "corrected" | "closed"): void {
  const command = process.env.REAL_STACK_E2E_ASSERT_COMMAND;
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = command
    ? execSync(command, {
        cwd: repoRoot,
        encoding: "utf8",
        env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath, REAL_STACK_E2E_ASSERT_STAGE: stage },
        stdio: ["ignore", "pipe", "pipe"],
      })
    : execFileSync(
        pythonBin,
        ["manage.py", "assert_trainer_payroll_close_correction_e2e", "--fixture", fixturePath, "--stage", stage],
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

test("real-stack owner/admin correct payroll, trainer is denied, and closed period blocks mutations", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginToDashboard(page, fixture.owner);
  await openTrainerPayroll(page, fixture);
  await correctFirstEarning(page, fixture);
  runBackendAssert(fixturePath, "corrected");

  await logoutDashboard(page);
  await loginToDashboard(page, fixture.admin);
  await openTrainerPayroll(page, fixture, 1);
  await closePayrollPeriod(page, fixture);
  runBackendAssert(fixturePath, "closed");

  await logoutDashboard(page);
  await loginToDashboard(page, fixture.trainer, { expectManagement: false });
  await assertTrainerDeniedPayrollRoute(page, fixture);
});

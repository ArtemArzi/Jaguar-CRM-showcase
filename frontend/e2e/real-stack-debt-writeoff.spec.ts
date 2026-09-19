import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface DebtWriteoffFixture {
  owner: {
    email: string;
    password: string;
  };
  target_debt_id: number;
  reserved_debt_id: number;
  closed_debt_id: number;
  expected: {
    writeoff_reason: string;
    reserved_error_text: string;
    closed_error_text: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a debt write-off fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): DebtWriteoffFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<DebtWriteoffFixture>;

  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.target_debt_id || !data.reserved_debt_id || !data.closed_debt_id) {
    throw new Error("Fixture target_debt_id, reserved_debt_id and closed_debt_id are required.");
  }
  if (!data.expected?.writeoff_reason || !data.expected.reserved_error_text || !data.expected.closed_error_text) {
    throw new Error("Fixture expected write-off values are required.");
  }

  return {
    owner: data.owner,
    target_debt_id: data.target_debt_id,
    reserved_debt_id: data.reserved_debt_id,
    closed_debt_id: data.closed_debt_id,
    expected: data.expected,
  };
}

function isDebtWriteoffResponse(debtId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/dashboard/billing/debts/${debtId}/write-off/` &&
      response.request().method() === "POST"
    );
  };
}

async function loginAsOwner(page: Page, fixture: DebtWriteoffFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

async function assertReservedDebtHiddenFromDebtors(page: Page): Promise<void> {
  await page.goto(backendUrl("/dashboard/billing/"));
  await expect(page.getByRole("heading", { name: "ДОЛЖНИКИ" })).toBeVisible();
  const reservedRow = page.locator("tr").filter({ hasText: "Reserved Student" });
  const targetRow = page.locator("tr").filter({ hasText: "Writeoff Student" });
  await expect(reservedRow).toHaveCount(0);
  await expect(targetRow).toBeVisible();
}

async function assertClosedPeriodDebtCannotBeWrittenOff(page: Page, fixture: DebtWriteoffFixture): Promise<void> {
  const closedRow = page.locator("tr").filter({ hasText: "Closed Payroll Student" });
  await expect(closedRow).toBeVisible();
  await closedRow.getByPlaceholder("Комментарий").fill("Closed period bypass attempt");

  await closedRow.getByRole("button", { name: /Списать/ }).click();
  const confirmDialog = page.getByRole("dialog", { name: "Подтверждение" });
  await expect(confirmDialog).toBeVisible();
  const writeoffResponse = page.waitForResponse(isDebtWriteoffResponse(fixture.closed_debt_id), {
    timeout: 20_000,
  });
  await confirmDialog.getByRole("button", { name: "Подтвердить" }).click();
  const response = await writeoffResponse;

  expect(response.status()).toBe(200);
  await expect(page.getByText(fixture.expected.closed_error_text)).toBeVisible();
  await expect(closedRow).toBeVisible();
}

async function writeOffTargetDebt(page: Page, fixture: DebtWriteoffFixture): Promise<void> {
  const targetRow = page.locator("tr").filter({ hasText: "Writeoff Student" });
  await expect(targetRow).toBeVisible();
  await targetRow.getByPlaceholder("Комментарий").fill(fixture.expected.writeoff_reason);

  await targetRow.getByRole("button", { name: /Списать/ }).click();
  const confirmDialog = page.getByRole("dialog", { name: "Подтверждение" });
  await expect(confirmDialog).toBeVisible();
  const writeoffResponse = page.waitForResponse(isDebtWriteoffResponse(fixture.target_debt_id), {
    timeout: 20_000,
  });
  await confirmDialog.getByRole("button", { name: "Подтвердить" }).click();
  const response = await writeoffResponse;

  expect(response.status()).toBe(200);
  await expect(targetRow).toBeHidden();
  await expect(page.locator("tr").filter({ hasText: "Reserved Student" })).toHaveCount(0);
  await expect(page.locator("tr").filter({ hasText: "Closed Payroll Student" })).toBeVisible();
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
        ["manage.py", "assert_debt_writeoff_e2e", "--fixture", fixturePath],
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

test("real-stack owner writes off debt and reserved pending-payment debt stays protected", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsOwner(page, fixture);
  await assertReservedDebtHiddenFromDebtors(page);
  await assertClosedPeriodDebtCannotBeWrittenOff(page, fixture);
  await writeOffTargetDebt(page, fixture);
  runBackendAssert(fixturePath);
});

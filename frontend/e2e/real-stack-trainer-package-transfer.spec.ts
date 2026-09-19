import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface TrainerPackageTransferFixture {
  owner: {
    email: string;
    password: string;
  };
  actual_trainer: {
    email: string;
    password: string;
  };
  student_id: number;
  student_name: string;
  tariff_id: number;
  tariff_name: string;
  package_owner_trainer_id: number;
  package_owner_trainer_name: string;
  actual_trainer_id: number;
  actual_trainer_name: string;
  checkin_date: string;
  expected: {
    salary_amount: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a trainer package transfer fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): TrainerPackageTransferFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<TrainerPackageTransferFixture>;

  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.actual_trainer?.email || !data.actual_trainer.password) {
    throw new Error("Fixture actual trainer credentials are required.");
  }
  if (!data.student_id || !data.student_name || !data.tariff_id || !data.tariff_name) {
    throw new Error("Fixture student and tariff data are required.");
  }
  if (!data.package_owner_trainer_id || !data.package_owner_trainer_name) {
    throw new Error("Fixture package owner data is required.");
  }
  if (!data.actual_trainer_id || !data.actual_trainer_name || !data.checkin_date) {
    throw new Error("Fixture actual trainer and check-in data are required.");
  }
  if (!data.expected?.salary_amount) {
    throw new Error("Fixture expected salary amount is required.");
  }

  return data as TrainerPackageTransferFixture;
}

function isCreateSubscriptionResponse(response: Response): boolean {
  const url = new URL(response.url());
  return (
    url.pathname === "/dashboard/billing/subscriptions/create/" &&
    response.request().method() === "POST"
  );
}

function isTrainerEarningsResponse(
  response: Response,
  fixture: TrainerPackageTransferFixture,
): boolean {
  const url = new URL(response.url());
  return (
    url.pathname === `/api/trainers/${fixture.actual_trainer_id}/earnings/` &&
    response.request().method() === "GET"
  );
}

function isTrainerEarningsSummaryResponse(
  response: Response,
  fixture: TrainerPackageTransferFixture,
): boolean {
  const url = new URL(response.url());
  return (
    url.pathname === `/api/trainers/${fixture.actual_trainer_id}/earnings/summary/` &&
    response.request().method() === "GET"
  );
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function rubAmountPattern(amount: string): RegExp {
  const [whole] = amount.split(".");
  const groupedWhole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, "\\s*");
  return new RegExp(`${groupedWhole}\\s*₽`);
}

function subscriptionRow(page: Page, fixture: TrainerPackageTransferFixture) {
  return page
    .getByRole("row", {
      name: new RegExp(escapeRegExp(fixture.student_name)),
    })
    .filter({ hasText: fixture.tariff_name })
    .filter({ hasText: fixture.package_owner_trainer_name })
    .first();
}

async function loginAsOwner(page: Page, fixture: TrainerPackageTransferFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

async function loginAsTrainer(page: Page, fixture: TrainerPackageTransferFixture): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(fixture.actual_trainer.email);
  await page.getByLabel("Пароль").fill(fixture.actual_trainer.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

async function createSubscriptionThroughDashboard(
  page: Page,
  fixture: TrainerPackageTransferFixture,
): Promise<void> {
  await page.goto(backendUrl("/dashboard/billing/subscriptions/"));
  await expect(page.getByRole("heading", { name: "Абонементы" })).toBeVisible();

  const existingRow = subscriptionRow(page, fixture);
  if ((await existingRow.count()) > 0) {
    await expect(existingRow).toBeVisible();
    return;
  }

  await page.getByRole("button", { name: "Новый абонемент" }).click();

  const form = page.locator("#create-sub-form");
  await expect(form).toBeVisible();

  await form.locator('select[name="student_id"]').selectOption(String(fixture.student_id));
  await form.locator('select[name="tariff_id"]').selectOption(String(fixture.tariff_id));
  await form.locator('select[name="payment_method"]').selectOption("cash");
  await form.locator('select[name="seller_trainer_id"]').selectOption(String(fixture.package_owner_trainer_id));
  await expect(form.getByText("Владелец пакета")).toBeVisible();
  await form.locator('select[name="package_owner_trainer_id"]').selectOption(
    String(fixture.package_owner_trainer_id),
  );

  const createResponse = page.waitForResponse(isCreateSubscriptionResponse, {
    timeout: 20_000,
  });
  await form.getByRole("button", { name: "СОЗДАТЬ" }).click();
  expect((await createResponse).status()).toBe(204);

  await expect(subscriptionRow(page, fixture)).toBeVisible();
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
        ["manage.py", "assert_trainer_package_transfer_e2e", "--fixture", fixturePath],
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

async function assertTrainerPwaShowsPackageTransferMetadata(
  page: Page,
  fixture: TrainerPackageTransferFixture,
): Promise<void> {
  await page.goto(`/trainer/students/${fixture.student_id}`);
  await expect(page.getByRole("heading", { name: fixture.student_name })).toBeVisible({
    timeout: 20_000,
  });
  await expect(
    page.getByText(`Пакет тренера: ${fixture.package_owner_trainer_name}`),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Записать" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Принять оплату" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Заморозить" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Повысить грейд" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "+ Дисциплина" })).toHaveCount(0);
  await expect(page.getByTitle("Убрать дисциплину")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Отправить опрос" })).toHaveCount(0);

  const earningsResponse = page.waitForResponse((response) =>
    isTrainerEarningsResponse(response, fixture),
  );
  const summaryResponse = page.waitForResponse((response) =>
    isTrainerEarningsSummaryResponse(response, fixture),
  );
  await page.goto("/trainer/salary");
  await expect(page.getByRole("heading", { name: "Заработок" })).toBeVisible({
    timeout: 20_000,
  });
  expect((await earningsResponse).status()).toBe(200);
  expect((await summaryResponse).status()).toBe(200);

  const salaryAmount = rubAmountPattern(fixture.expected.salary_amount);
  await expect(page.getByText(salaryAmount).first()).toBeVisible();
  await expect(page.getByText("1 тренировка")).toBeVisible();

  const transferSalaryCard = page
    .locator("div")
    .filter({ hasText: `Пакет куплен у ${fixture.package_owner_trainer_name}` })
    .filter({ hasText: salaryAmount })
    .first();
  await expect(transferSalaryCard).toBeVisible();
  await expect(
    page.getByText(`Пакет куплен у ${fixture.package_owner_trainer_name}`),
  ).toBeVisible();
  await expect(page.getByText("Не влияет на сумму зарплаты")).toBeVisible();
}

test("real-stack owner creates trainer-owned package and backend transfer salary assertions pass", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsOwner(page, fixture);
  await createSubscriptionThroughDashboard(page, fixture);
  runBackendAssert(fixturePath);
  await loginAsTrainer(page, fixture);
  await assertTrainerPwaShowsPackageTransferMetadata(page, fixture);
});

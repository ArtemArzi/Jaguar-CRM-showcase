import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Locator, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface PaymentConfirmFixture {
  training_type_id: number;
  owner: {
    email: string;
    password: string;
  };
  trainer: {
    email: string;
    password: string;
  };
  student: {
    id: number;
    name: string;
  };
  child: {
    id: number;
    name: string;
    parent_phone_input: string;
  };
  retained: {
    id: number;
    name: string;
    debt_id: number;
    debt_checkin_id: number;
  };
  tariff: {
    id: number;
    name: string;
    price: string;
  };
  target_group: {
    training_group_id: number;
    rollout_mode: string;
    new_writes_enabled: boolean;
    manual_operational_admission_enabled: boolean;
    schedule_id: number;
    name: string;
    start_date: string;
    second_schedule_id: number;
    second_start_date: string;
  };
  kiosk: {
    activation_pin: string;
  };
  discount: {
    id: number;
    name: string;
    discount_type: string;
    value: string;
  };
  expected: {
    payment_original_amount: string;
    payment_amount: string;
    payment_method: string;
  };
}

interface PaymentResponse {
  id: number;
  amount: string;
  original_amount: string;
  payment_method: string;
  status: string;
  discount_ids: number[];
  target_schedule_id: number | null;
  target_training_group_id?: number | null;
  target_start_date: string | null;
}

interface AccountAccessIssueResponse {
  student_id: number;
  username: string;
  temporary_password: string | null;
  status: string;
}

interface BackendAssertResponse {
  ok?: boolean;
}

interface CabinetFinancialState {
  operational_admission: {
    payment_id: number;
    payment_status: string;
    subscription_status: string | null;
    enrollment_status: string;
    covered_visit_count: number;
  } | null;
  covered_visits: Array<{
    debt_id: number;
    checkin_id: number;
    coverage_state: string;
    is_payable: boolean;
  }>;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a payment confirmation fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): PaymentConfirmFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<PaymentConfirmFixture>;

  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  if (
    !data.student?.id ||
    !data.student.name ||
    !data.child?.id ||
    !data.child.name ||
    !data.retained?.id ||
    !data.retained.name ||
    !data.retained.debt_id ||
    !data.retained.debt_checkin_id
  ) {
    throw new Error("Fixture adult, child, and retained payment targets are required.");
  }
  if (!data.training_type_id || !data.tariff?.id || !data.tariff.name || !data.discount?.id || !data.discount.name) {
    throw new Error("Fixture tariff and discount are required.");
  }
  if (
    !data.target_group?.schedule_id ||
    !data.target_group.name ||
    !data.target_group.start_date ||
    !data.target_group.training_group_id ||
    data.target_group.rollout_mode !== "active" ||
    data.target_group.new_writes_enabled !== true ||
    data.target_group.manual_operational_admission_enabled !== true ||
    !data.target_group.second_schedule_id ||
    !data.target_group.second_start_date
  ) {
    throw new Error("Fixture must prove an active canonical group with two explicit weekdays.");
  }
  if (!data.expected?.payment_original_amount || !data.expected.payment_amount) {
    throw new Error("Fixture payment expectations are required.");
  }
  if (!data.kiosk?.activation_pin) {
    throw new Error("Fixture kiosk activation PIN is required.");
  }
  return data as PaymentConfirmFixture;
}

function isPaymentVerifyResponse(paymentId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/dashboard/billing/payments/${paymentId}/verify/` &&
      response.request().method() === "POST"
    );
  };
}

function pendingPaymentCard(page: Page, paymentId: number): Locator {
  return page
    .locator(`button[hx-post="/dashboard/billing/payments/${paymentId}/verify/"]`)
    .locator("xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' p-4 ')][1]");
}

async function loginAsOwner(page: Page, fixture: PaymentConfirmFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

function isCreatePaymentResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === "/api/billing/payments/" && response.request().method() === "POST";
}

function isAccountAccessOpenResponse(studentId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/api/students/${studentId}/account-access/open/` &&
      response.request().method() === "POST"
    );
  };
}

function isStudentFinancialStateResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === "/api/students/me/financial-state/" && response.request().method() === "GET";
}

async function loginAsTrainer(page: Page, fixture: PaymentConfirmFixture): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(fixture.trainer.email);
  await page.getByLabel("Пароль").fill(fixture.trainer.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function formatFixtureDate(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${value}T00:00:00Z`));
}

async function selectCanonicalGroupOccurrence(
  paymentDialog: Locator,
  fixture: PaymentConfirmFixture,
): Promise<void> {
  const targetGroup = paymentDialog.getByRole("radio", {
    name: new RegExp(escapeRegExp(fixture.target_group.name)),
  });
  await expect(targetGroup).toHaveCount(1);
  await targetGroup.click();

  const occurrences = paymentDialog
    .getByRole("radiogroup", { name: "Выбор первого занятия" })
    .getByRole("radio");
  const primaryOccurrence = occurrences.filter({
    hasText: formatFixtureDate(fixture.target_group.start_date),
  });
  const secondaryOccurrence = occurrences.filter({
    hasText: formatFixtureDate(fixture.target_group.second_start_date),
  });
  await expect(primaryOccurrence).toHaveCount(1);
  await expect(secondaryOccurrence).toHaveCount(1);
  await primaryOccurrence.click();
}

async function createRetainedDiscountedPayment(
  page: Page,
  fixture: PaymentConfirmFixture,
): Promise<PaymentResponse> {
  await loginAsTrainer(page, fixture);
  await page.goto(`/trainer/students/${fixture.retained.id}`);
  await expect(page.getByRole("heading", { name: fixture.retained.name })).toBeVisible({
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Принять оплату", exact: true }).click();
  const paymentDialog = page.getByRole("dialog", { name: "Принять оплату" });
  await paymentDialog
    .getByRole("button", { name: new RegExp(escapeRegExp(fixture.tariff.name)) })
    .click();
  await selectCanonicalGroupOccurrence(paymentDialog, fixture);
  await paymentDialog
    .getByLabel(new RegExp(`Долг #${fixture.retained.debt_checkin_id}`))
    .check();
  await expect(paymentDialog.getByText("1/1")).toBeVisible();
  const discountGroup = paymentDialog.getByRole("radiogroup", { name: "Скидка" });
  await discountGroup
    .getByRole("radio", { name: new RegExp(escapeRegExp(fixture.discount.name)) })
    .click();
  await expect(
    paymentDialog.getByRole("region", { name: "Итог оплаты" }).getByText(/3\s600\s₽/),
  ).toBeVisible();

  const paymentResponse = page.waitForResponse(isCreatePaymentResponse, { timeout: 20_000 });
  await paymentDialog.getByRole("button", { name: "Принять оплату" }).click();
  const response = await paymentResponse;
  expect(response.status()).toBe(201);
  const payment = (await response.json()) as PaymentResponse;
  expect(payment.status).toBe("pending");
  expect(Number(payment.original_amount)).toBe(Number(fixture.expected.payment_original_amount));
  expect(Number(payment.amount)).toBe(Number(fixture.expected.payment_amount) * 0.9);
  expect(payment.payment_method).toBe(fixture.expected.payment_method);
  expect(payment.discount_ids).toEqual([fixture.discount.id]);
  expect(payment.target_schedule_id).toBe(fixture.target_group.schedule_id);
  expect(payment.target_training_group_id).toBe(fixture.target_group.training_group_id);
  expect(payment.target_start_date).toBe(fixture.target_group.start_date);
  return payment;
}

function writeRuntimeEvidence(
  fixturePath: string,
  retainedPaymentId: number,
): void {
  const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as Record<string, unknown>;
  writeFileSync(
    fixturePath,
    `${JSON.stringify({
      ...fixture,
      runtime: {
        retained_payment_id: retainedPaymentId,
      },
    }, null, 2)}\n`,
    "utf8",
  );
}

function assertCabinetFinancialState(
  financialState: CabinetFinancialState,
  expected: {
    paymentId: number;
    paymentStatus: "pending" | "confirmed";
    subscriptionStatus: "pending" | "active";
    coveredCheckinIds?: number[];
  },
): void {
  expect(financialState.operational_admission).toMatchObject({
    payment_id: expected.paymentId,
    payment_status: expected.paymentStatus,
    subscription_status: expected.subscriptionStatus,
    enrollment_status: "active",
    covered_visit_count: expected.coveredCheckinIds?.length ?? 0,
  });
  if (expected.coveredCheckinIds) {
    expect(financialState.covered_visits).toHaveLength(expected.coveredCheckinIds.length);
    expect(financialState.covered_visits.map((visit) => visit.checkin_id).sort()).toEqual(
      [...expected.coveredCheckinIds].sort(),
    );
    for (const visit of financialState.covered_visits) {
      expect(visit).toMatchObject({
        coverage_state: "covered_awaiting_confirmation",
        is_payable: false,
      });
      expect(visit.debt_id).toBeGreaterThan(0);
    }
  } else {
    expect(financialState.covered_visits).toEqual([]);
  }
}

async function readStableResponseJson<T>(page: Page, response: Response): Promise<T> {
  const authorization = await response.request().headerValue("authorization");
  const replay = await page.request.get(response.url(), {
    headers: authorization ? { Authorization: authorization } : undefined,
  });
  expect(replay.ok()).toBe(true);
  return (await replay.json()) as T;
}

async function assertCoveredVisitIsNonPayable(page: Page): Promise<void> {
  await expect(page.getByText(/покрыто оплатой$/).first()).toBeVisible();
  await expect(
    page.getByText("Отдельная оплата и ссылка не нужны до подтверждения.").first(),
  ).toBeVisible();
  await expect(page.getByText("Есть задолженность")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Оплатить", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Продлить через СБП", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Открыть предпросмотр" })).toHaveCount(0);
}

async function openRetainedAccountAccess(
  page: Page,
  fixture: PaymentConfirmFixture,
): Promise<AccountAccessIssueResponse> {
  await loginAsTrainer(page, fixture);
  await page.goto(`/trainer/students/${fixture.retained.id}`);
  await expect(page.getByRole("heading", { name: fixture.retained.name })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText("Можно открыть кабинет")).toBeVisible();
  const responsePromise = page.waitForResponse(
    isAccountAccessOpenResponse(fixture.retained.id),
    { timeout: 20_000 },
  );
  await page.getByRole("button", { name: "Открыть кабинет" }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(201);
  const issue = (await response.json()) as AccountAccessIssueResponse;
  expect(issue.student_id).toBe(fixture.retained.id);
  expect(issue.status).toBe("open");
  expect(issue.temporary_password).toBeTruthy();
  return issue;
}

async function loginStudentAndAssertFinancialState(
  page: Page,
  issue: AccountAccessIssueResponse,
  expected: {
    paymentId: number;
    paymentStatus: "pending" | "confirmed";
    subscriptionStatus: "pending" | "active";
    coveredCheckinIds?: number[];
  },
): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(issue.username);
  await page.getByLabel("Пароль").fill(issue.temporary_password ?? "");
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/student\/?$/);

  const financialStatePromise = page.waitForResponse(isStudentFinancialStateResponse, {
    timeout: 20_000,
  });
  await page.reload();
  const response = await financialStatePromise;
  expect(response.ok()).toBe(true);
  assertCabinetFinancialState(
    await readStableResponseJson<CabinetFinancialState>(page, response),
    expected,
  );
  await expect(
    page.getByText(expected.paymentStatus === "pending" ? "Оплата ожидает подтверждения" : "Оплата подтверждена").first(),
  ).toBeVisible();
  if (expected.coveredCheckinIds) await assertCoveredVisitIsNonPayable(page);
}

async function confirmPendingPayment(page: Page, paymentId: number): Promise<Response> {
  const verifyButton = page.locator(`button[hx-post="/dashboard/billing/payments/${paymentId}/verify/"]`);
  const actionContainer = verifyButton.locator("xpath=ancestor::div[@x-data][1]");
  await actionContainer.getByRole("button", { name: "Подтвердить" }).click();
  await expect(actionContainer.getByText("Подтвердить?")).toBeVisible();
  const verifyResponse = page.waitForResponse(isPaymentVerifyResponse(paymentId), {
    timeout: 20_000,
  });
  await actionContainer.getByRole("button", { name: "Да, подтвердить" }).click();
  return await verifyResponse;
}

async function assertVisibleConfirmationResult(page: Page, verifyResponse: Response): Promise<void> {
  expect(verifyResponse.status()).toBe(204);
  await expect(page).toHaveURL(/\/dashboard\/billing\/payments\/$/);
  await expect(page.getByText("Все оплаты проверены")).toBeVisible();
}

function runBackendAssert(
  fixturePath: string,
  stage: "admission" | "confirmed" = "confirmed",
): void {
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
        ["manage.py", "assert_payment_confirm_e2e", "--fixture", fixturePath, "--stage", stage],
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

test("real-stack trainer records a discounted debt payment and owner confirms it", async ({ page }) => {
  test.setTimeout(120_000);
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  const retainedPayment = await createRetainedDiscountedPayment(page, fixture);
  writeRuntimeEvidence(fixturePath, retainedPayment.id);
  runBackendAssert(fixturePath, "admission");
  await loginAsOwner(page, fixture);
  await page.goto(backendUrl("/dashboard/billing/payments/"));
  await expect(page.getByRole("heading", { name: "Оплаты" })).toBeVisible();
  const retainedPaymentCard = pendingPaymentCard(page, retainedPayment.id);
  await expect(retainedPaymentCard.getByText(fixture.retained.name, { exact: true })).toBeVisible();
  await expect(retainedPaymentCard.getByText("Групповое обучение", { exact: true })).toBeVisible();
  await expect(retainedPaymentCard.getByText(fixture.target_group.name, { exact: true })).toBeVisible();
  await expect(
    page.getByText(new RegExp(`Каноническая группа: ${escapeRegExp(fixture.target_group.name)}`)).first(),
  ).toBeVisible();
  await expect(page.getByText(/Статус оплаты: ожидает подтверждения/).first()).toBeVisible();
  await expect(retainedPaymentCard.getByText(fixture.discount.name)).toBeVisible();
  const verifyResponse = await confirmPendingPayment(page, retainedPayment.id);
  await assertVisibleConfirmationResult(page, verifyResponse);
  await page.getByRole("link", { name: "История" }).click();
  await expect(page).toHaveURL(/\/dashboard\/billing\/payments\/\?queue=history/);
  const confirmedPaymentCard = page.locator(`#payment-${retainedPayment.id}`);
  await expect(confirmedPaymentCard.getByText(fixture.retained.name, { exact: true })).toBeVisible();
  await expect(confirmedPaymentCard.getByText("Подтверждён", { exact: true })).toBeVisible();
  await expect(confirmedPaymentCard.getByRole("button", { name: "Подтвердить" })).toHaveCount(0);
  const retainedAccess = await openRetainedAccountAccess(page, fixture);
  runBackendAssert(fixturePath);
  runBackendAssert(fixturePath);
  await loginStudentAndAssertFinancialState(page, retainedAccess, {
    paymentId: retainedPayment.id,
    paymentStatus: "confirmed",
    subscriptionStatus: "active",
  });
});

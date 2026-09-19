import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface PaymentRejectFixture {
  owner: {
    email: string;
    password: string;
  };
  trainer: {
    email: string;
    password: string;
  };
  student_id: number;
  child: {
    id: number;
    name: string;
    parent_phone_input: string;
  };
  adult: {
    id: number;
    name: string;
  };
  tariff: {
    id: number;
    name: string;
  };
  schedule: {
    id: number;
    name: string;
    start_date: string;
    training_group_id: number;
    rollout_mode: string;
    second_schedule_id: number;
    second_start_date: string;
    new_writes_enabled: boolean;
    manual_operational_admission_enabled: boolean;
  };
  training_type_id: number;
  kiosk: { activation_pin: string };
  expected: {
    commercial_journey_protocol_version: "v2";
    rejection_reason: string;
  };
}

interface BackendAssertResponse {
  ok?: boolean;
}

interface AccountAccessIssueResponse {
  student_id: number;
  username: string;
  temporary_password: string | null;
  status: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a payment rejection fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): PaymentRejectFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<PaymentRejectFixture>;

  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (
    !data.trainer?.email ||
    !data.trainer.password ||
    !data.student_id ||
    !data.child?.name ||
    !data.child.parent_phone_input ||
    !data.adult?.id ||
    !data.adult.name ||
    !data.tariff?.id ||
    !data.tariff.name ||
    !data.schedule?.id ||
    !data.schedule.start_date ||
    !data.schedule.training_group_id ||
    data.schedule.rollout_mode !== "active" ||
    !data.schedule.second_schedule_id ||
    !data.schedule.second_start_date ||
    data.schedule.new_writes_enabled !== true ||
    data.schedule.manual_operational_admission_enabled !== true ||
    !data.training_type_id ||
    !data.kiosk?.activation_pin
  ) {
    throw new Error("Fixture trainer and child account-access data are required.");
  }
  if (
    data.expected?.commercial_journey_protocol_version !== "v2" ||
    !data.expected.rejection_reason
  ) {
    throw new Error("Fixture expected v2 protocol and rejection reason are required.");
  }

  return data as PaymentRejectFixture;
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

function isAccountAccessOpenResponse(studentId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/api/students/${studentId}/account-access/open/` &&
      response.request().method() === "POST"
    );
  };
}

function isParentChildResponse(childId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `/api/parents/children/${childId}/` && response.request().method() === "GET";
  };
}

function isStudentFinancialStateResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === "/api/students/me/financial-state/" && response.request().method() === "GET";
}

function isCreatePaymentResponse(response: Response): boolean {
  const url = new URL(response.url());
  return (
    url.pathname === "/api/billing/v2/group-sales/manual/" &&
    response.request().method() === "POST"
  );
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function loginAsOwner(page: Page, fixture: PaymentRejectFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

async function loginAsTrainer(page: Page, fixture: PaymentRejectFixture): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(fixture.trainer.email);
  await page.getByLabel("Пароль").fill(fixture.trainer.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

async function createLeadPayment(
  page: Page,
  fixture: PaymentRejectFixture,
  student: { id: number; name: string },
): Promise<number> {
  await loginAsTrainer(page, fixture);
  await page.goto(`/trainer/leads?lead=${student.id}`);
  await expect(page.getByRole("heading", { name: student.name })).toBeVisible();
  await page.getByRole("button", { name: "Ещё" }).click();
  await page.getByRole("button", { name: "Оформить сразу в группу" }).click();
  await expect(page.getByRole("heading", { name: "Оформить обучение" })).toBeVisible();
  await page.getByRole("button", { name: new RegExp(escapeRegExp(fixture.tariff.name)) }).click();
  await page.getByRole("button", { name: new RegExp(escapeRegExp(fixture.schedule.name)) }).click();
  await page.getByRole("button", { name: fixture.schedule.start_date, exact: true }).click();
  await page.getByRole("button", { name: "Проверить условия" }).click();
  await expect(page.getByRole("heading", { name: "Проверка перед оформлением" })).toBeVisible();
  const responsePromise = page.waitForResponse(isCreatePaymentResponse, { timeout: 20_000 });
  await page.getByRole("button", { name: /^Зафиксировать наличные/ }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(201);
  const requestPayload = response.request().postDataJSON() as Record<string, unknown>;
  expect(requestPayload).toMatchObject({
    protocol_version: "v2",
    student_id: student.id,
    tariff_id: fixture.tariff.id,
    target_training_group_id: fixture.schedule.training_group_id,
    target_schedule_id: fixture.schedule.id,
    target_start_date: fixture.schedule.start_date,
    payment_method: "cash",
  });
  expect(requestPayload.expected_offer_digest).toEqual(expect.stringMatching(/^v2\.[a-f0-9]{64}$/));
  expect(requestPayload.idempotency_key).toEqual(expect.stringMatching(/^.{1,120}$/));
  const result = (await response.json()) as {
    payment_id?: number;
    workspace_state?: string;
    finance_state?: string;
  };
  expect(result.payment_id).toEqual(expect.any(Number));
  expect(result.workspace_state).toBe("student");
  expect(result.finance_state).toBe("pending_manual");
  return result.payment_id as number;
}

async function activateKiosk(page: Page, fixture: PaymentRejectFixture): Promise<string> {
  const activation = await page.request.post(backendUrl("/api/checkins/kiosk/activate/"), {
    data: { pin: fixture.kiosk.activation_pin },
  });
  expect(activation.status()).toBe(200);
  const token = ((await activation.json()) as { token?: string }).token;
  expect(token).toBeTruthy();
  return token ?? "";
}

async function createPendingWindowKioskCheckin(
  page: Page,
  fixture: PaymentRejectFixture,
  studentId: number,
  kioskToken: string,
): Promise<number> {
  const checkin = await page.request.post(backendUrl("/api/checkins/kiosk/"), {
    headers: { "X-Kiosk-Token": kioskToken },
    data: {
      student_id: studentId,
      schedule_id: fixture.schedule.id,
      training_type_id: fixture.training_type_id,
      checkin_date: fixture.schedule.start_date,
    },
  });
  const result = (await checkin.json()) as { checkin_id?: number; created?: boolean; is_debt?: boolean };
  expect(checkin.status()).toBe(200);
  expect(result.created).toBe(true);
  expect(result.is_debt).toBe(true);
  expect(result.checkin_id).toBeGreaterThan(0);
  return result.checkin_id ?? 0;
}

function writeRuntimeEvidence(
  fixturePath: string,
  childPaymentId: number,
  adultPaymentId: number,
  childCheckinId?: number,
  adultCheckinId?: number,
): void {
  const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as Record<string, unknown>;
  writeFileSync(
    fixturePath,
    `${JSON.stringify({
      ...fixture,
      runtime: {
        child_payment_id: childPaymentId,
        adult_payment_id: adultPaymentId,
        child_checkin_id: childCheckinId,
        adult_checkin_id: adultCheckinId,
      },
    }, null, 2)}\n`,
    "utf8",
  );
}

function assertCabinetFinancialState(
  state: CabinetFinancialState,
  expected: {
    paymentId: number;
    paymentStatus: "pending" | "rejected";
    subscriptionStatus: "pending" | "cancelled";
    enrollmentStatus: "active" | "cancelled";
    coveredCheckinId?: number;
  },
): void {
  expect(state.operational_admission).toMatchObject({
    payment_id: expected.paymentId,
    payment_status: expected.paymentStatus,
    subscription_status: expected.subscriptionStatus,
    enrollment_status: expected.enrollmentStatus,
    covered_visit_count: expected.coveredCheckinId ? 1 : 0,
  });
  if (expected.coveredCheckinId) {
    expect(state.covered_visits).toHaveLength(1);
    expect(state.covered_visits[0]).toMatchObject({
      checkin_id: expected.coveredCheckinId,
      coverage_state: "covered_awaiting_confirmation",
      is_payable: false,
    });
    expect(state.covered_visits[0]?.debt_id).toBeGreaterThan(0);
  } else {
    expect(state.covered_visits).toEqual([]);
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

async function openParentAccountAccess(
  page: Page,
  fixture: PaymentRejectFixture,
): Promise<AccountAccessIssueResponse> {
  await loginAsTrainer(page, fixture);

  await page.goto(`/trainer/students/${fixture.student_id}`);
  await expect(page.getByRole("heading", { name: fixture.child.name })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Можно открыть кабинет")).toBeVisible();
  const openButton = page.getByRole("button", { name: "Открыть кабинет" });
  await expect(openButton).toBeDisabled();
  await page.getByLabel("Телефон родителя").fill(fixture.child.parent_phone_input);
  const responsePromise = page.waitForResponse(isAccountAccessOpenResponse(fixture.student_id), {
    timeout: 20_000,
  });
  await openButton.click();
  const response = await responsePromise;
  expect(response.status()).toBe(201);
  const issue = (await response.json()) as AccountAccessIssueResponse;
  expect(issue.student_id).toBe(fixture.student_id);
  expect(issue.status).toBe("open");
  expect(issue.temporary_password).toBeTruthy();
  return issue;
}

async function loginParentAndAssertCabinet(
  page: Page,
  fixture: PaymentRejectFixture,
  issue: AccountAccessIssueResponse,
  expected: {
    paymentId: number;
    paymentStatus: "pending" | "rejected";
    subscriptionStatus: "pending" | "cancelled";
    enrollmentStatus: "active" | "cancelled";
    coveredCheckinId?: number;
  },
): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(issue.username);
  await page.getByLabel("Пароль").fill(issue.temporary_password ?? "");
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/parent\/?$/);

  const childResponsePromise = page.waitForResponse(isParentChildResponse(fixture.student_id), {
    timeout: 20_000,
  });
  await page.goto(`/parent/child/${fixture.student_id}`);
  const childResponse = await childResponsePromise;
  expect(childResponse.ok()).toBe(true);
  const profile = await readStableResponseJson<{ financial_state: CabinetFinancialState }>(
    page, childResponse,
  );
  assertCabinetFinancialState(profile.financial_state, expected);
  if (expected.paymentStatus === "pending") {
    await expect(page.getByText("Оплата ожидает подтверждения").first()).toBeVisible();
    if (expected.coveredCheckinId) await assertCoveredVisitIsNonPayable(page);
  } else {
    await expect(page.getByText("Оплата ожидает подтверждения")).toHaveCount(0);
    await expect(
      page.getByText("Запись отменена; будущая готовность к чекину снята.").first(),
    ).toBeVisible();
    await expect(page.getByText("Есть задолженность").first()).toBeVisible();
  }

  const reloadChildResponsePromise = page.waitForResponse(isParentChildResponse(fixture.student_id), {
    timeout: 20_000,
  });
  await page.reload();
  const reloadChildResponse = await reloadChildResponsePromise;
  expect(reloadChildResponse.ok()).toBe(true);
  const reloadedProfile = await readStableResponseJson<{ financial_state: CabinetFinancialState }>(
    page, reloadChildResponse,
  );
  assertCabinetFinancialState(reloadedProfile.financial_state, expected);
  if (expected.paymentStatus === "pending") {
    await expect(page.getByText("Оплата ожидает подтверждения").first()).toBeVisible();
    if (expected.coveredCheckinId) await assertCoveredVisitIsNonPayable(page);
  } else {
    await expect(page.getByText("Оплата ожидает подтверждения")).toHaveCount(0);
    await expect(
      page.getByText("Запись отменена; будущая готовность к чекину снята.").first(),
    ).toBeVisible();
    await expect(page.getByText("Есть задолженность").first()).toBeVisible();
  }
}

async function openStudentAccountAccess(
  page: Page,
  fixture: PaymentRejectFixture,
): Promise<AccountAccessIssueResponse> {
  await page.goto(`/trainer/students/${fixture.adult.id}`);
  await expect(page.getByRole("heading", { name: fixture.adult.name })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Можно открыть кабинет")).toBeVisible();
  const responsePromise = page.waitForResponse(isAccountAccessOpenResponse(fixture.adult.id), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Открыть кабинет" }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(201);
  const issue = (await response.json()) as AccountAccessIssueResponse;
  expect(issue.student_id).toBe(fixture.adult.id);
  expect(issue.status).toBe("open");
  expect(issue.temporary_password).toBeTruthy();
  return issue;
}

async function loginStudentAndAssertCabinet(
  page: Page,
  issue: AccountAccessIssueResponse,
  expected: {
    paymentId: number;
    paymentStatus: "pending" | "rejected";
    subscriptionStatus: "pending" | "cancelled";
    enrollmentStatus: "active" | "cancelled";
    coveredCheckinId?: number;
  },
): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(issue.username);
  await page.getByLabel("Пароль").fill(issue.temporary_password ?? "");
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/student\/?$/);
  const responsePromise = page.waitForResponse(isStudentFinancialStateResponse, { timeout: 20_000 });
  await page.reload();
  const response = await responsePromise;
  expect(response.ok()).toBe(true);
  assertCabinetFinancialState(
    await readStableResponseJson<CabinetFinancialState>(page, response),
    expected,
  );
  if (expected.paymentStatus === "pending") {
    await expect(page.getByText("Оплата ожидает подтверждения").first()).toBeVisible();
    if (expected.coveredCheckinId) await assertCoveredVisitIsNonPayable(page);
  } else {
    await expect(page.getByText("Оплата ожидает подтверждения")).toHaveCount(0);
    await expect(
      page.getByText("Запись отменена; будущая готовность к чекину снята.").first(),
    ).toBeVisible();
    await expect(page.getByText("Есть задолженность").first()).toBeVisible();
  }
}

async function rejectPendingPayment(
  page: Page,
  fixture: PaymentRejectFixture,
  paymentId: number,
  verifyBlankReason: boolean,
): Promise<Response> {
  await page.goto(backendUrl("/dashboard/billing/payments/"));
  await expect(page.getByRole("heading", { name: "Оплаты" })).toBeVisible();

  const verifyButton = page.locator(`button[hx-post="/dashboard/billing/payments/${paymentId}/verify/"]`);
  const actionContainer = verifyButton.locator("xpath=ancestor::div[@x-data][1]");
  await expect(actionContainer).toBeVisible();
  await actionContainer.getByRole("button", { name: "Отклонить" }).click();
  await expect(actionContainer.getByText("Отклонить?")).toBeVisible();
  if (verifyBlankReason) {
    await actionContainer.getByPlaceholder("Причина").fill("   ");
    const blankReasonResponse = page.waitForResponse(isPaymentVerifyResponse(paymentId), {
      timeout: 20_000,
    });
    await actionContainer.getByRole("button", { name: "Да, отклонить" }).click();
    expect((await blankReasonResponse).status()).toBe(200);
    await expect(page.getByRole("alert")).toContainText("Укажите причину отклонения оплаты");
    await actionContainer.getByRole("button", { name: "Отклонить" }).click();
    await expect(actionContainer.getByText("Отклонить?")).toBeVisible();
  }
  await actionContainer.getByPlaceholder("Причина").fill(fixture.expected.rejection_reason);
  const verifyResponse = page.waitForResponse(isPaymentVerifyResponse(paymentId), {
    timeout: 20_000,
  });
  await actionContainer.getByRole("button", { name: "Да, отклонить" }).click();
  return await verifyResponse;
}

async function assertVisibleRejectionResult(page: Page, verifyResponse: Response): Promise<void> {
  expect(verifyResponse.status()).toBe(204);
  await expect(page).toHaveURL(/\/dashboard\/billing\/payments\/$/);
  await expect(page.getByText("Все оплаты проверены")).toBeVisible();
}

function runBackendAssert(fixturePath: string, stage: "admission" | "checked_in" | "rejected" = "rejected"): void {
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
        ["manage.py", "assert_payment_reject_e2e", "--fixture", fixturePath, "--stage", stage],
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

test(
  "real-stack owner rejects adult and child admissions; cabinets show reopened payable debt",
  async ({ page }) => {
    test.setTimeout(120_000);
    const fixturePath = requireFixturePath();
    const fixture = readFixture(fixturePath);

    const childPaymentId = await createLeadPayment(page, fixture, fixture.child);
    const parentAccess = await openParentAccountAccess(page, fixture);
    await loginParentAndAssertCabinet(page, fixture, parentAccess, {
      paymentId: childPaymentId,
      paymentStatus: "pending",
      subscriptionStatus: "pending",
      enrollmentStatus: "active",
    });
    const adultPaymentId = await createLeadPayment(page, fixture, fixture.adult);
    const studentAccess = await openStudentAccountAccess(page, fixture);
    await loginStudentAndAssertCabinet(page, studentAccess, {
      paymentId: adultPaymentId,
      paymentStatus: "pending",
      subscriptionStatus: "pending",
      enrollmentStatus: "active",
    });
    writeRuntimeEvidence(fixturePath, childPaymentId, adultPaymentId);
    runBackendAssert(fixturePath, "admission");
    const kioskToken = await activateKiosk(page, fixture);
    const childCheckinId = await createPendingWindowKioskCheckin(
      page, fixture, fixture.child.id, kioskToken,
    );
    const adultCheckinId = await createPendingWindowKioskCheckin(
      page, fixture, fixture.adult.id, kioskToken,
    );
    writeRuntimeEvidence(
      fixturePath,
      childPaymentId,
      adultPaymentId,
      childCheckinId,
      adultCheckinId,
    );
    runBackendAssert(fixturePath, "checked_in");
    await loginParentAndAssertCabinet(page, fixture, parentAccess, {
      paymentId: childPaymentId,
      paymentStatus: "pending",
      subscriptionStatus: "pending",
      enrollmentStatus: "active",
      coveredCheckinId: childCheckinId,
    });
    await loginStudentAndAssertCabinet(page, studentAccess, {
      paymentId: adultPaymentId,
      paymentStatus: "pending",
      subscriptionStatus: "pending",
      enrollmentStatus: "active",
      coveredCheckinId: adultCheckinId,
    });
    await loginAsOwner(page, fixture);
    const childVerifyResponse = await rejectPendingPayment(page, fixture, childPaymentId, true);
    expect(childVerifyResponse.status()).toBe(204);
    const verifyResponse = await rejectPendingPayment(page, fixture, adultPaymentId, false);
    await assertVisibleRejectionResult(page, verifyResponse);
    runBackendAssert(fixturePath);
    await loginParentAndAssertCabinet(page, fixture, parentAccess, {
      paymentId: childPaymentId,
      paymentStatus: "rejected",
      subscriptionStatus: "cancelled",
      enrollmentStatus: "cancelled",
    });
    await loginStudentAndAssertCabinet(page, studentAccess, {
      paymentId: adultPaymentId,
      paymentStatus: "rejected",
      subscriptionStatus: "cancelled",
      enrollmentStatus: "cancelled",
    });
  },
);

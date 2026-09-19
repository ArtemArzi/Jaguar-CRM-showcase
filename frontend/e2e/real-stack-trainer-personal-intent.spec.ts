import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { openResetLogin } from "./auth-helpers";
import { backendUrl } from "./support/real-stack-urls";

interface SlotFixture {
  id: number;
  date: string;
  start_time: string;
  end_time: string;
}

interface StaffIntentFixture {
  trainer: { trainer_id: number; email: string; password: string; name: string };
  cash_lead: { student_id: number; name: string };
  sbp_student: { student_id: number; name: string };
  entitlement_student: { student_id: number; name: string; subscription_id: number };
  pay_at_visit_student: { student_id: number; name: string };
  pay_at_visit_sbp_student: { student_id: number; name: string };
  terminal_sbp_student: { student_id: number; name: string };
  direct_student: { student_id: number; name: string };
  direct_terminal_sbp_student: { student_id: number; name: string };
  correction_student: { student_id: number; name: string };
  direct_correction_student: { student_id: number; name: string };
  kiosk: { activation_pin: string };
  location: { id: number; name: string };
  personal_training_type: { id: number; name: string };
  cash_slot: SlotFixture;
  sbp_slot: SlotFixture;
  entitlement_slot: SlotFixture;
  pay_at_visit_slot: SlotFixture;
  pay_at_visit_sbp_slot: SlotFixture;
  terminal_sbp_slot: SlotFixture;
  correction_slot: SlotFixture;
  correction_destination_slot: SlotFixture;
  personal_discount: { id: number; name: string; value: string };
  direct_booking: { date: string; start_time: string; end_time: string };
  direct_terminal_booking: {
    date: string;
    start_time: string;
    end_time: string;
    starts_at_utc: string;
  };
  direct_correction_booking: { date: string; start_time: string; end_time: string };
  expected: {
    amount_display: string;
    discount_amount_display: string;
    discounted_amount_display: string;
    cash_status: string;
    sbp_status: string;
    entitlement_status: string;
    pay_at_visit_status: string;
    debt_open_status: string;
  };
}

interface StaffIntentReceipt {
  booking_id?: number | null;
  reservation_id?: number | null;
  bank_payment_order_id?: number | null;
  debt_id?: number | null;
  schedule_id?: number | null;
  amount?: string | number | null;
}

interface CommercialContextReceipt extends StaffIntentReceipt {
  status: string;
  payment_method?: string | null;
}

interface CommercialContextResponse {
  attempts: CommercialContextReceipt[];
}

interface BackendAssertResponse {
  ok?: boolean;
  cash?: { booking_id?: number; payment_id?: number; status?: string };
  sbp?: { reservation_id?: number; bank_payment_order_id?: number; status?: string };
  entitlement?: { enrollment_id?: number; subscription_id?: number };
  pay_at_visit?: { booking_id?: number; debt_id?: number; payment_id?: number };
  pay_at_visit_sbp?: { booking_id?: number; debt_id?: number; bank_payment_order_id?: number };
  terminal_sbp?: {
    live_reservation_id?: number;
    live_bank_payment_order_id?: number;
    terminal_reservation_ids?: number[];
  };
  direct?: { booking_id?: number; payment_id?: number };
  direct_terminal_sbp?: {
    live_reservation_id?: number;
    live_bank_payment_order_id?: number;
    terminal_reservation_id?: number;
    starts_at_utc?: string;
  };
  payment_correction?: {
    correction_id?: number;
    original_reservation_id?: number;
    replacement_booking_id?: number;
    status?: string;
  };
  direct_payment_correction?: {
    correction_id?: number;
    original_reservation_id?: number;
    replacement_booking_id?: number;
    payment_id?: number;
  };
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) throw new Error("REAL_STACK_E2E_FIXTURE must point to a staff personal intent fixture.");
  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  return fixturePath;
}

function readFixture(fixturePath: string): StaffIntentFixture {
  const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<StaffIntentFixture>;
  if (
    !fixture.trainer?.trainer_id ||
    !fixture.trainer.email ||
    !fixture.trainer.password ||
    !fixture.cash_lead?.student_id ||
    !fixture.cash_lead.name ||
    !fixture.sbp_student?.student_id ||
    !fixture.sbp_student.name ||
    !fixture.entitlement_student?.subscription_id ||
    !fixture.entitlement_student.name ||
    !fixture.pay_at_visit_student?.student_id ||
    !fixture.pay_at_visit_student.name ||
    !fixture.pay_at_visit_sbp_student?.student_id ||
    !fixture.pay_at_visit_sbp_student.name ||
    !fixture.terminal_sbp_student?.student_id ||
    !fixture.terminal_sbp_student.name ||
    !fixture.direct_student?.student_id ||
    !fixture.direct_student.name ||
    !fixture.direct_terminal_sbp_student?.student_id ||
    !fixture.direct_terminal_sbp_student.name ||
    !fixture.correction_student?.student_id ||
    !fixture.correction_student.name ||
    !fixture.direct_correction_student?.student_id ||
    !fixture.direct_correction_student.name ||
    !fixture.kiosk?.activation_pin ||
    !fixture.location?.id ||
    !fixture.personal_training_type?.id ||
    !fixture.cash_slot?.date ||
    !fixture.sbp_slot?.date ||
    !fixture.entitlement_slot?.date ||
    !fixture.pay_at_visit_slot?.date ||
    !fixture.pay_at_visit_sbp_slot?.date ||
    !fixture.terminal_sbp_slot?.date ||
    !fixture.correction_slot?.date ||
    !fixture.correction_destination_slot?.date ||
    !fixture.personal_discount?.id ||
    !fixture.direct_booking?.date ||
    !fixture.direct_terminal_booking?.starts_at_utc ||
    !fixture.direct_correction_booking?.date ||
    !fixture.expected?.amount_display ||
    !fixture.expected?.discounted_amount_display
  ) {
    throw new Error("Staff personal intent fixture is missing required browser data.");
  }
  return fixture as StaffIntentFixture;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function flexibleAmountPattern(value: string): string {
  return escapeRegExp(value).replace(/\s+/g, "\\s*");
}

function personalActionName(
  action: "cash" | "sbp" | "pay_at_visit" | "retry_sbp",
  studentName: string,
  amountDisplay: string,
): RegExp {
  const student = escapeRegExp(studentName);
  const amount = flexibleAmountPattern(amountDisplay);
  if (action === "cash") return new RegExp(`^Записать ${student}, наличные, на\\s*${amount}$`);
  if (action === "sbp") return new RegExp(`^Создать ссылку СБП для ${student} на\\s*${amount}$`);
  if (action === "pay_at_visit") {
    return new RegExp(`^Записать ${student}, оплата при посещении, на\\s*${amount}$`);
  }
  return new RegExp(`^Повторить оплату СБП для ${student} на\\s*${amount}$`);
}

function availabilityDateButtonName(date: string): RegExp {
  const label = new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "long",
    timeZone: "UTC",
  }).format(new Date(`${date}T00:00:00Z`));
  return new RegExp(`^${escapeRegExp(label)}\\. Слотов: \\d+$`);
}

async function selectAvailabilityDate(page: Page, date: string): Promise<void> {
  const dateButton = page.getByRole("button", { name: availabilityDateButtonName(date) });
  for (let week = 0; week < 8; week += 1) {
    if ((await dateButton.count()) === 1) {
      await dateButton.click();
      return;
    }
    const firstVisibleDay = page.locator('button[aria-label*="Слотов:"]').first();
    const previousLabel = await firstVisibleDay.getAttribute("aria-label");
    if (!previousLabel) throw new Error("Availability calendar must expose weekday buttons with accessible labels.");
    await page.getByRole("button", { name: "Следующая неделя" }).click();
    await expect(firstVisibleDay).not.toHaveAttribute("aria-label", previousLabel);
  }
  throw new Error(`Availability date ${date} was not reached within eight calendar weeks.`);
}

async function loginAsTrainer(page: Page, fixture: StaffIntentFixture): Promise<string> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(fixture.trainer.email);
  await page.getByLabel("Пароль").fill(fixture.trainer.password);
  const loginResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/_allauth/app/v1/auth/login" && response.request().method() === "POST";
  });
  await page.getByRole("button", { name: "Войти" }).click();
  const loginResponse = await loginResponsePromise;
  expect(loginResponse.ok()).toBe(true);
  const loginBody = (await loginResponse.json()) as { meta?: { access_token?: unknown } };
  expect(typeof loginBody.meta?.access_token).toBe("string");
  await expect(page).toHaveURL(/\/trainer\/?$/);
  return loginBody.meta?.access_token as string;
}

async function navigateWithinTrainerApp(page: Page, path: string): Promise<void> {
  await page.evaluate((nextPath) => {
    window.history.pushState({}, "", nextPath);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, path);
}

async function openStudentAvailability(
  page: Page,
  studentId: number,
): Promise<void> {
  await navigateWithinTrainerApp(page, `/trainer/students/${studentId}`);
  const bookPersonalButton = page.getByRole("button", { name: "Записать", exact: true });
  await expect(bookPersonalButton).toBeVisible();
  await bookPersonalButton.click();
  await expect(page).toHaveURL(new RegExp(`/trainer/availability\\?student_id=${studentId}`));
}

async function assertPersonalAvailabilityEnabled(page: Page, trainerAccessToken: string): Promise<void> {
  const response = await page.request.get(backendUrl("/api/personal-availability/capability/"), {
    headers: { Authorization: `Bearer ${trainerAccessToken}` },
  });
  expect(response.status()).toBe(200);
  expect(await response.json()).toEqual({
    enabled: true,
    staff_command_protocol_version: "v1",
  });
}

async function selectFixedSlot(page: Page, fixture: StaffIntentFixture, date: string): Promise<void> {
  await selectAvailabilityDate(page, date);
  const slot = page.getByRole("button", {
    name: new RegExp(
      `${escapeRegExp(fixture.personal_training_type.name)}.*${escapeRegExp(fixture.location.name)}.*Свободно`,
    ),
  });
  await expect(slot).toHaveCount(1);
  await slot.click();
  await expect(page.getByLabel("Фиксированный слот")).toBeVisible();
  await expect(page.getByLabel("Дата *")).toHaveCount(0);
  await expect(page.getByLabel("Разовая персоналка *")).toHaveCount(0);
}

function assertNoClientPriceAuthority(payload: Record<string, unknown>): void {
  expect(Object.keys(payload)).not.toContain("tariff_id");
  expect(Object.keys(payload)).not.toContain("amount");
  expect(Object.keys(payload)).not.toContain("discount_ids");
}

function assertFixedStaffIntentPayload(
  response: Response,
  studentId: number,
  paymentMethod: "cash" | "sbp" | "pay_at_visit",
  discountId?: number,
  includeDiscountField = true,
): Record<string, unknown> {
  const payload = response.request().postDataJSON() as Record<string, unknown>;
  expect(payload).toEqual(
    expect.objectContaining({
      student_id: studentId,
      payment_method: paymentMethod,
      ...(includeDiscountField ? { discount_id: discountId ?? null } : {}),
      offer_digest: expect.any(String),
      idempotency_key: expect.any(String),
    }),
  );
  expect(Object.keys(payload).sort()).toEqual([
    ...(includeDiscountField ? ["discount_id"] : []),
    "idempotency_key",
    "offer_digest",
    "payment_method",
    "student_id",
  ]);
  assertNoClientPriceAuthority(payload);
  return payload;
}

async function selectPersonalDiscount(page: Page, fixture: StaffIntentFixture): Promise<void> {
  await page.getByRole("radio", { name: new RegExp(escapeRegExp(fixture.personal_discount.name)) }).click();
  const summary = page.getByLabel("Итог персональной записи");
  await expect(summary).toContainText(fixture.expected.discount_amount_display);
  await expect(summary).toContainText(fixture.expected.discounted_amount_display);
}

function assertEntitlementPayload(response: Response, fixture: StaffIntentFixture): void {
  const payload = response.request().postDataJSON() as Record<string, unknown>;
  expect(payload).toEqual({
    student_id: fixture.entitlement_student.student_id,
    subscription_id: fixture.entitlement_student.subscription_id,
    payment_method: "entitlement",
    idempotency_key: expect.any(String),
  });
  assertNoClientPriceAuthority(payload);
}

function assertDirectStaffIntentPayload(
  response: Response,
  fixture: StaffIntentFixture,
  studentId: number,
  booking: { date: string; start_time: string; end_time: string },
  paymentMethod: "cash" | "sbp",
  discountId?: number,
  includeDiscountField = true,
): Record<string, unknown> {
  const payload = response.request().postDataJSON() as Record<string, unknown>;
  expect(payload).toEqual(
    expect.objectContaining({
      student_id: studentId,
      trainer_id: fixture.trainer.trainer_id,
      location_id: fixture.location.id,
      training_type_id: fixture.personal_training_type.id,
      starts_at: `${booking.date}T${booking.start_time}:00`,
      ends_at: `${booking.date}T${booking.end_time}:00`,
      payment_method: paymentMethod,
      offer_digest: expect.any(String),
      idempotency_key: expect.any(String),
      ...(includeDiscountField ? { discount_id: discountId ?? null } : {}),
    }),
  );
  expect(Object.keys(payload).sort()).toEqual([
    ...(includeDiscountField ? ["discount_id"] : []),
    "ends_at",
    "idempotency_key",
    "location_id",
    "offer_digest",
    "payment_method",
    "starts_at",
    "student_id",
    "trainer_id",
    "training_type_id",
  ]);
  assertNoClientPriceAuthority(payload);
  return payload;
}

function staffIntentResponse(page: Page): Promise<Response> {
  return page.waitForResponse((response) => {
    const url = new URL(response.url());
    return /\/api\/personal-availability\/slots\/\d+\/staff-intents\/$/.test(url.pathname)
      && response.request().method() === "POST";
  });
}

function directStaffIntentResponse(page: Page): Promise<Response> {
  return page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/personal-availability/staff-intents/direct/"
      && response.request().method() === "POST";
  });
}

function directOfferResponse(page: Page): Promise<Response> {
  return page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/personal-availability/direct-offer/"
      && response.request().method() === "GET";
  });
}

function studentCommercialContextResponse(page: Page, studentId: number): Promise<Response> {
  return page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === `/api/students/${studentId}/commercial-context/`
      && response.request().method() === "GET";
  });
}

async function reloadStudentCommercialContext(
  page: Page,
  studentId: number,
  predicate: (receipt: CommercialContextReceipt) => boolean,
): Promise<void> {
  const contextResponsePromise = studentCommercialContextResponse(page, studentId);
  await navigateWithinTrainerApp(page, `/trainer/students/${studentId}`);
  const contextResponse = await contextResponsePromise;
  expect(contextResponse.ok()).toBe(true);
  const context = (await contextResponse.json()) as CommercialContextResponse;
  expect(context.attempts.some(predicate)).toBe(true);
}

function runBackendAssert(fixturePath: string): BackendAssertResponse {
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
        ["manage.py", "assert_trainer_personal_intent_e2e", "--fixture", fixturePath],
        { cwd: repoRoot, encoding: "utf8", env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath } },
      );
  return JSON.parse(output) as BackendAssertResponse;
}

async function activateKiosk(page: Page, fixture: StaffIntentFixture): Promise<string> {
  const activation = await page.request.post(backendUrl("/api/checkins/kiosk/activate/"), {
    data: { pin: fixture.kiosk.activation_pin },
  });
  expect(activation.status()).toBe(200);
  const body = (await activation.json()) as { token?: string };
  expect(body.token).toEqual(expect.any(String));
  return body.token ?? "";
}

test("real-stack trainer personal intent keeps contextual commercial truth across every staff mode", async ({ page }) => {
  test.setTimeout(180_000);
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);
  const trainerAccessToken = await loginAsTrainer(page, fixture);
  const trainerHeaders = { Authorization: `Bearer ${trainerAccessToken}` };
  await assertPersonalAvailabilityEnabled(page, trainerAccessToken);

  await navigateWithinTrainerApp(page, "/trainer/leads");
  await page.getByRole("button", { name: new RegExp(escapeRegExp(fixture.cash_lead.name)) }).click();
  const leadDialog = page.getByRole("dialog", { name: fixture.cash_lead.name });
  await expect(leadDialog).toBeVisible();
  await leadDialog.getByRole("button", { name: "Ещё", exact: true }).click();
  await leadDialog.getByRole("button", { name: "Записать персоналку", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/trainer/availability\\?student_id=${fixture.cash_lead.student_id}`));
  await selectFixedSlot(page, fixture, fixture.cash_slot.date);
  await page.getByRole("button", { name: "Наличные", exact: true }).click();
  const cashResponsePromise = staffIntentResponse(page);
  await page.getByRole("button", {
    name: personalActionName("cash", fixture.cash_lead.name, fixture.expected.amount_display),
  }).click();
  const cashResponse = await cashResponsePromise;
  expect(cashResponse.ok()).toBe(true);
  assertFixedStaffIntentPayload(cashResponse, fixture.cash_lead.student_id, "cash");
  const cashReceipt = page.getByRole("region", { name: "Коммерческий контекст персоналки" });
  await expect(cashReceipt).toContainText(fixture.expected.amount_display);
  await expect(cashReceipt).toContainText(fixture.expected.cash_status);
  await page
    .getByLabel("Подтверждение персональной записи")
    .getByRole("button", { name: "Закрыть", exact: true })
    .click();

  await navigateWithinTrainerApp(page, `/trainer/leads?lead=${fixture.cash_lead.student_id}`);
  await expect(page).toHaveURL(new RegExp(`/trainer/leads\\?lead=${fixture.cash_lead.student_id}`));
  const persistedCashReceipt = page.getByRole("region", { name: "Коммерческий контекст персоналки" });
  await expect(persistedCashReceipt).toContainText(fixture.expected.amount_display);
  await expect(persistedCashReceipt).toContainText(fixture.expected.cash_status);

  await openStudentAvailability(page, fixture.sbp_student.student_id);
  await selectFixedSlot(page, fixture, fixture.sbp_slot.date);
  await page.getByRole("button", { name: "Оплата через СБП", exact: true }).click();
  const sbpResponsePromise = staffIntentResponse(page);
  await page.getByRole("button", {
    name: personalActionName("sbp", fixture.sbp_student.name, fixture.expected.amount_display),
  }).click();
  const sbpResponse = await sbpResponsePromise;
  expect(sbpResponse.ok()).toBe(true);
  assertFixedStaffIntentPayload(sbpResponse, fixture.sbp_student.student_id, "sbp");
  const sbpReceipt = page.getByRole("region", { name: "Коммерческий контекст персоналки" });
  await expect(sbpReceipt).toContainText(fixture.expected.amount_display);
  await expect(sbpReceipt).toContainText(fixture.expected.sbp_status);
  await expect(page.getByLabel("Ссылка на оплату СБП")).toBeVisible();
  await expect(page.getByRole("link", { name: "Открыть предпросмотр" })).toBeVisible();
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  await reloadStudentCommercialContext(
    page,
    fixture.sbp_student.student_id,
    (receipt) =>
      receipt.payment_method === "sbp" && ["created", "pending"].includes(receipt.status),
  );
  await expect(page.getByRole("region", { name: "Коммерческий контекст персоналки" })).toContainText(
    fixture.expected.sbp_status,
  );
  await expect(page.getByLabel("Ссылка на оплату СБП")).toBeVisible();

  await openStudentAvailability(page, fixture.entitlement_student.student_id);
  await selectFixedSlot(page, fixture, fixture.entitlement_slot.date);
  await page.getByRole("button", { name: "По абонементу", exact: true }).click();
  await page
    .getByLabel("Абонемент *")
    .selectOption(String(fixture.entitlement_student.subscription_id));
  const entitlementResponsePromise = staffIntentResponse(page);
  await page.getByRole("button", {
    name: new RegExp(`^Записать ${escapeRegExp(fixture.entitlement_student.name)} по абонементу$`),
  }).click();
  const entitlementResponse = await entitlementResponsePromise;
  expect(entitlementResponse.ok()).toBe(true);
  assertEntitlementPayload(entitlementResponse, fixture);
  await expect(page.getByRole("region", { name: "Коммерческий контекст персоналки" })).toContainText(
    fixture.expected.entitlement_status,
  );
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  await reloadStudentCommercialContext(
    page,
    fixture.entitlement_student.student_id,
    (receipt) => receipt.payment_method === "entitlement" && receipt.status === "booked",
  );
  await expect(page.getByRole("region", { name: "Коммерческий контекст персоналки" })).toContainText(
    fixture.expected.entitlement_status,
  );

  await openStudentAvailability(page, fixture.pay_at_visit_student.student_id);
  await selectFixedSlot(page, fixture, fixture.pay_at_visit_slot.date);
  await page.getByRole("button", { name: "Оплата при посещении", exact: true }).click();
  await selectPersonalDiscount(page, fixture);
  const payAtVisitResponsePromise = staffIntentResponse(page);
  await page.getByRole("button", {
    name: personalActionName(
      "pay_at_visit",
      fixture.pay_at_visit_student.name,
      fixture.expected.discounted_amount_display,
    ),
  }).click();
  const payAtVisitResponse = await payAtVisitResponsePromise;
  expect(payAtVisitResponse.ok()).toBe(true);
  assertFixedStaffIntentPayload(
    payAtVisitResponse,
    fixture.pay_at_visit_student.student_id,
    "pay_at_visit",
    fixture.personal_discount.id,
  );
  const payAtVisitReceipt = (await payAtVisitResponse.json()) as StaffIntentReceipt;
  expect(payAtVisitReceipt.booking_id).toBeGreaterThan(0);
  expect(payAtVisitReceipt.debt_id).toBeNull();
  const preCheckinReceipt = page.getByRole("region", { name: "Коммерческий контекст персоналки" });
  await expect(preCheckinReceipt).toContainText(fixture.expected.pay_at_visit_status);
  await expect(preCheckinReceipt).not.toContainText(fixture.expected.debt_open_status);
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();

  const kioskToken = await activateKiosk(page, fixture);
  const checkin = await page.request.post(backendUrl("/api/checkins/kiosk/"), {
    headers: { "X-Kiosk-Token": kioskToken },
    data: {
      student_id: fixture.pay_at_visit_student.student_id,
      schedule_id: payAtVisitReceipt.schedule_id,
      training_type_id: fixture.personal_training_type.id,
      checkin_date: fixture.pay_at_visit_slot.date,
    },
  });
  expect(checkin.status()).toBe(200);
  expect(await checkin.json()).toEqual(
    expect.objectContaining({ created: true, is_debt: true, subscription_id: null }),
  );
  const debtContextResponsePromise = studentCommercialContextResponse(
    page,
    fixture.pay_at_visit_student.student_id,
  );
  await navigateWithinTrainerApp(page, `/trainer/students/${fixture.pay_at_visit_student.student_id}`);
  const debtContext = (await (await debtContextResponsePromise).json()) as CommercialContextResponse;
  const frozenDebtReceipt = debtContext.attempts.find(
    (attempt) => attempt.booking_id === payAtVisitReceipt.booking_id,
  );
  expect(frozenDebtReceipt).toEqual(
    expect.objectContaining({
      debt_id: expect.any(Number),
      status: "debt_open",
      amount: "2200.00",
    }),
  );
  const debtReceipt = page.getByRole("region", { name: "Коммерческий контекст персоналки" });
  await expect(debtReceipt).toContainText(fixture.expected.discounted_amount_display);
  await expect(debtReceipt).toContainText(fixture.expected.debt_open_status);
  await debtReceipt.getByRole("button", { name: "Принять уже полученную оплату", exact: true }).click();
  const settlementDialog = page.getByRole("dialog", { name: "Принять оплату" });
  await expect(settlementDialog.getByLabel("Зафиксированная сумма оплаты")).toContainText(
    fixture.expected.discounted_amount_display,
  );
  await expect(settlementDialog).toContainText("Сумма и долг этой персоналки зафиксированы");
  await settlementDialog.getByRole("button", { name: "Наличные", exact: true }).click();
  const settlementResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return /\/api\/personal-drop-in-bookings\/\d+\/payments\/$/.test(url.pathname)
      && response.request().method() === "POST";
  });
  await settlementDialog.getByRole("button", { name: "Принять оплату", exact: true }).click();
  const settlementResponse = await settlementResponsePromise;
  expect(settlementResponse.ok()).toBe(true);
  const settlementPayload = settlementResponse.request().postDataJSON() as Record<string, unknown>;
  expect(settlementPayload).toEqual({
    payment_method: "cash",
    debt_id: expect.any(Number),
    discount_ids: [],
    idempotency_key: expect.any(String),
  });
  expect(settlementPayload.debt_id).toBe(frozenDebtReceipt?.debt_id);
  expect(Object.keys(settlementPayload)).not.toContain("tariff_id");
  expect(Object.keys(settlementPayload)).not.toContain("amount");

  await openStudentAvailability(page, fixture.pay_at_visit_sbp_student.student_id);
  await selectFixedSlot(page, fixture, fixture.pay_at_visit_sbp_slot.date);
  await page.getByRole("button", { name: "Оплата при посещении", exact: true }).click();
  const payAtVisitSbpResponsePromise = staffIntentResponse(page);
  await page.getByRole("button", {
    name: personalActionName(
      "pay_at_visit",
      fixture.pay_at_visit_sbp_student.name,
      fixture.expected.amount_display,
    ),
  }).click();
  const payAtVisitSbpResponse = await payAtVisitSbpResponsePromise;
  expect(payAtVisitSbpResponse.ok()).toBe(true);
  assertFixedStaffIntentPayload(
    payAtVisitSbpResponse,
    fixture.pay_at_visit_sbp_student.student_id,
    "pay_at_visit",
  );
  const payAtVisitSbpReceipt = (await payAtVisitSbpResponse.json()) as StaffIntentReceipt;
  expect(payAtVisitSbpReceipt.booking_id).toBeGreaterThan(0);
  expect(payAtVisitSbpReceipt.debt_id).toBeNull();
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();

  const exactDebtCheckin = await page.request.post(backendUrl("/api/checkins/kiosk/"), {
    headers: { "X-Kiosk-Token": kioskToken },
    data: {
      student_id: fixture.pay_at_visit_sbp_student.student_id,
      schedule_id: payAtVisitSbpReceipt.schedule_id,
      training_type_id: fixture.personal_training_type.id,
      checkin_date: fixture.pay_at_visit_sbp_slot.date,
    },
  });
  expect(exactDebtCheckin.status()).toBe(200);
  expect(await exactDebtCheckin.json()).toEqual(
    expect.objectContaining({ created: true, is_debt: true, subscription_id: null }),
  );
  const exactDebtContextResponsePromise = studentCommercialContextResponse(
    page,
    fixture.pay_at_visit_sbp_student.student_id,
  );
  await navigateWithinTrainerApp(page, `/trainer/students/${fixture.pay_at_visit_sbp_student.student_id}`);
  const exactDebtContext = (await (await exactDebtContextResponsePromise).json()) as CommercialContextResponse;
  const exactDebtReceipt = exactDebtContext.attempts.find(
    (attempt) => attempt.booking_id === payAtVisitSbpReceipt.booking_id,
  );
  expect(exactDebtReceipt).toEqual(
    expect.objectContaining({
      debt_id: expect.any(Number),
      status: "debt_open",
      amount: "2700.00",
    }),
  );
  const exactDebtRegion = page.getByRole("region", { name: "Коммерческий контекст персоналки" });
  await exactDebtRegion.getByRole("button", { name: "Принять уже полученную оплату", exact: true }).click();
  const exactDebtDialog = page.getByRole("dialog", { name: "Принять оплату" });
  await expect(exactDebtDialog.getByLabel("Зафиксированная сумма оплаты")).toContainText(
    fixture.expected.amount_display,
  );
  await expect(exactDebtDialog).toContainText("Сумма и долг этой персоналки зафиксированы");
  await exactDebtDialog.getByRole("button", { name: "СБП", exact: true }).click();
  const exactDebtSbpResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return /\/api\/personal-drop-in-bookings\/\d+\/bank-payment-orders\/$/.test(url.pathname)
      && response.request().method() === "POST";
  });
  await exactDebtDialog.getByRole("button", { name: "Создать ссылку СБП", exact: true }).click();
  const exactDebtSbpResponse = await exactDebtSbpResponsePromise;
  expect(exactDebtSbpResponse.status()).toBe(201);
  const exactDebtSbpPayload = exactDebtSbpResponse.request().postDataJSON() as Record<string, unknown>;
  expect(exactDebtSbpPayload).toEqual({
    debt_id: exactDebtReceipt?.debt_id,
    idempotency_key: expect.any(String),
  });
  assertNoClientPriceAuthority(exactDebtSbpPayload);
  await exactDebtDialog.getByRole("button", { name: "Закрыть", exact: true }).click();
  await reloadStudentCommercialContext(
    page,
    fixture.pay_at_visit_sbp_student.student_id,
    (receipt) =>
      receipt.booking_id === payAtVisitSbpReceipt.booking_id &&
      receipt.payment_method === "sbp" &&
      ["created", "pending"].includes(receipt.status),
  );
  const persistedExactDebtRegion = page.getByRole("region", {
    name: "Коммерческий контекст персоналки",
  });
  await expect(persistedExactDebtRegion).toContainText(fixture.expected.amount_display);
  await expect(persistedExactDebtRegion).toContainText(fixture.expected.sbp_status);
  await expect(page.getByLabel("Ссылка на оплату СБП")).toBeVisible();

  await openStudentAvailability(page, fixture.terminal_sbp_student.student_id);
  await selectFixedSlot(page, fixture, fixture.terminal_sbp_slot.date);
  await page.getByRole("button", { name: "Оплата через СБП", exact: true }).click();
  const terminalFirstResponsePromise = staffIntentResponse(page);
  await page.getByRole("button", {
    name: personalActionName(
      "sbp",
      fixture.terminal_sbp_student.name,
      fixture.expected.amount_display,
    ),
  }).click();
  const terminalFirstResponse = await terminalFirstResponsePromise;
  expect(terminalFirstResponse.ok()).toBe(true);
  const terminalFirstPayload = terminalFirstResponse.request().postDataJSON() as Record<string, unknown>;
  assertFixedStaffIntentPayload(terminalFirstResponse, fixture.terminal_sbp_student.student_id, "sbp");
  const terminalFirstReceipt = (await terminalFirstResponse.json()) as StaffIntentReceipt;
  expect(terminalFirstReceipt.bank_payment_order_id).toBeGreaterThan(0);
  const sameKeyReplay = await page.request.post(
    backendUrl(`/api/personal-availability/slots/${fixture.terminal_sbp_slot.id}/staff-intents/`),
    { headers: trainerHeaders, data: terminalFirstPayload },
  );
  expect(sameKeyReplay.status()).toBe(200);
  expect(await sameKeyReplay.json()).toEqual(
    expect.objectContaining({
      reservation_id: terminalFirstReceipt.reservation_id,
      bank_payment_order_id: terminalFirstReceipt.bank_payment_order_id,
    }),
  );
  const cancelOrder = await page.request.post(
    backendUrl(`/api/billing/bank-payment-orders/${terminalFirstReceipt.bank_payment_order_id}/cancel/`),
    { headers: trainerHeaders },
  );
  expect(cancelOrder.status()).toBe(200);
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  await navigateWithinTrainerApp(page, `/trainer/students/${fixture.terminal_sbp_student.student_id}`);
  const terminalReceipt = page.getByRole("region", { name: "Коммерческий контекст персоналки" });
  await expect(terminalReceipt).toContainText("Оплата отменена");
  await terminalReceipt.getByRole("button", { name: "Повторить оплату СБП", exact: true }).click();
  const retryDialog = page.getByRole("dialog", { name: "Записать персоналку" });
  await expect(retryDialog).toContainText("Предыдущая попытка сохранится в истории");
  const retryResponsePromise = staffIntentResponse(page);
  await retryDialog.getByRole("button", {
    name: personalActionName(
      "retry_sbp",
      fixture.terminal_sbp_student.name,
      fixture.expected.amount_display,
    ),
  }).click();
  const retryResponse = await retryResponsePromise;
  expect(retryResponse.ok(), await retryResponse.text()).toBe(true);
  const retryPayload = retryResponse.request().postDataJSON() as Record<string, unknown>;
  assertFixedStaffIntentPayload(
    retryResponse,
    fixture.terminal_sbp_student.student_id,
    "sbp",
    undefined,
    false,
  );
  expect(retryPayload.idempotency_key).not.toBe(terminalFirstPayload.idempotency_key);
  await reloadStudentCommercialContext(
    page,
    fixture.terminal_sbp_student.student_id,
    (receipt) =>
      receipt.payment_method === "sbp" && ["created", "pending"].includes(receipt.status),
  );
  const allSbpAttempts = page.getByRole("region", { name: "Коммерческий контекст персоналки" });
  await expect(allSbpAttempts).toHaveCount(1);
  await page.getByText("История попыток (1)", { exact: true }).click();
  await expect(allSbpAttempts).toHaveCount(2);
  await expect(allSbpAttempts.filter({ hasText: fixture.expected.sbp_status })).toHaveCount(1);
  await expect(allSbpAttempts.filter({ hasText: "Оплата отменена" })).toHaveCount(1);

  await openStudentAvailability(page, fixture.correction_student.student_id);
  await selectFixedSlot(page, fixture, fixture.correction_slot.date);
  await page.getByRole("button", { name: "Оплата через СБП", exact: true }).click();
  await selectPersonalDiscount(page, fixture);
  const correctionSbpResponsePromise = staffIntentResponse(page);
  await page.getByRole("button", {
    name: personalActionName(
      "sbp",
      fixture.correction_student.name,
      fixture.expected.discounted_amount_display,
    ),
  }).click();
  const correctionSbpResponse = await correctionSbpResponsePromise;
  expect(correctionSbpResponse.ok()).toBe(true);
  assertFixedStaffIntentPayload(
    correctionSbpResponse,
    fixture.correction_student.student_id,
    "sbp",
    fixture.personal_discount.id,
  );
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  await navigateWithinTrainerApp(page, `/trainer/students/${fixture.correction_student.student_id}`);
  const correctionReceipt = page.getByRole("region", { name: "Коммерческий контекст персоналки" });
  await expect(correctionReceipt).toContainText(fixture.expected.discounted_amount_display);
  await correctionReceipt.getByRole("button", { name: "Заменить способ оплаты", exact: true }).click();
  const correctionResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname ===
      `/api/students/${fixture.correction_student.student_id}/personal-commercial-attempts/replace-payment-method/`
      && response.request().method() === "POST";
  });
  await correctionReceipt.getByRole("button", { name: "Оплата при посещении", exact: true }).click();
  const correctionResponse = await correctionResponsePromise;
  expect(correctionResponse.ok()).toBe(true);
  const correctionPayload = correctionResponse.request().postDataJSON() as Record<string, unknown>;
  expect(correctionPayload).toEqual({
    reservation_id: expect.any(Number),
    replacement_payment_method: "pay_at_visit",
    reason: "staff_payment_method_correction",
    idempotency_key: expect.any(String),
  });
  assertNoClientPriceAuthority(correctionPayload);
  await reloadStudentCommercialContext(
    page,
    fixture.correction_student.student_id,
    (receipt) =>
      receipt.payment_method === "pay_at_visit" &&
      receipt.status === "pay_at_visit" &&
      receipt.amount === "2200.00",
  );
  await expect(page.getByText("История попыток (1)", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Коммерческий контекст персоналки" })).toContainText(
    fixture.expected.discounted_amount_display,
  );
  await page.getByRole("button", { name: "Перенести", exact: true }).click();
  const correctionRescheduleDialog = page.getByRole("dialog", { name: "Перенести персоналку" });
  await expect(correctionRescheduleDialog).toBeVisible();
  await correctionRescheduleDialog
    .getByRole("button")
    .filter({
      hasText: `${fixture.correction_destination_slot.start_time}–${fixture.correction_destination_slot.end_time}`,
    })
    .click();
  const correctionRescheduleResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return /\/api\/personal-drop-in-bookings\/\d+\/reschedule\/$/.test(url.pathname)
      && response.request().method() === "POST";
  });
  await correctionRescheduleDialog.getByRole("button", { name: "Подтвердить перенос", exact: true }).click();
  const correctionRescheduleResponse = await correctionRescheduleResponsePromise;
  expect(correctionRescheduleResponse.ok()).toBe(true);
  expect(correctionRescheduleResponse.request().postDataJSON()).toEqual({
    destination_slot_id: fixture.correction_destination_slot.id,
    reason: "Перенос по согласованию с клиентом",
    idempotency_key: expect.any(String),
  });
  await expect(correctionRescheduleDialog).not.toBeVisible();

  await openStudentAvailability(page, fixture.direct_student.student_id);
  await page.getByRole("button", { name: "Указать время", exact: true }).click();
  const directDialog = page.getByRole("dialog", { name: "Записать персоналку" });
  await directDialog.getByLabel("Дата *").fill(fixture.direct_booking.date);
  await directDialog.getByLabel("Начало *").fill(fixture.direct_booking.start_time);
  await directDialog.getByLabel("Конец *").fill(fixture.direct_booking.end_time);
  await directDialog
    .getByLabel("Тип тренировки *")
    .selectOption(String(fixture.personal_training_type.id));
  await directDialog.getByLabel("Зал *").selectOption(String(fixture.location.id));
  await expect(directDialog.getByLabel("Итог персональной записи")).toContainText(
    fixture.expected.amount_display,
  );
  await directDialog.getByRole("button", { name: "Наличные", exact: true }).click();
  const directResponsePromise = directStaffIntentResponse(page);
  await directDialog.getByRole("button", {
    name: personalActionName("cash", fixture.direct_student.name, fixture.expected.amount_display),
  }).click();
  const directResponse = await directResponsePromise;
  expect(directResponse.ok()).toBe(true);
  assertDirectStaffIntentPayload(
    directResponse,
    fixture,
    fixture.direct_student.student_id,
    fixture.direct_booking,
    "cash",
  );
  await expect(directDialog.getByRole("region", { name: "Коммерческий контекст персоналки" })).toContainText(
    fixture.expected.cash_status,
  );
  await page
    .getByLabel("Подтверждение персональной записи")
    .getByRole("button", { name: "Закрыть", exact: true })
    .click();
  await reloadStudentCommercialContext(
    page,
    fixture.direct_student.student_id,
    (receipt) => receipt.payment_method === "cash" && receipt.status === "pending",
  );
  await expect(page.getByRole("region", { name: "Коммерческий контекст персоналки" })).toContainText(
    fixture.expected.amount_display,
  );

  await openStudentAvailability(page, fixture.direct_terminal_sbp_student.student_id);
  await page.getByRole("button", { name: "Указать время", exact: true }).click();
  const directTerminalDialog = page.getByRole("dialog", { name: "Записать персоналку" });
  await directTerminalDialog.getByLabel("Дата *").fill(fixture.direct_terminal_booking.date);
  await directTerminalDialog
    .getByLabel("Начало *")
    .fill(fixture.direct_terminal_booking.start_time);
  await directTerminalDialog
    .getByLabel("Конец *")
    .fill(fixture.direct_terminal_booking.end_time);
  await directTerminalDialog
    .getByLabel("Тип тренировки *")
    .selectOption(String(fixture.personal_training_type.id));
  await directTerminalDialog.getByLabel("Зал *").selectOption(String(fixture.location.id));
  await expect(directTerminalDialog.getByLabel("Итог персональной записи")).toContainText(
    fixture.expected.amount_display,
  );
  await directTerminalDialog.getByRole("button", { name: "Оплата через СБП", exact: true }).click();
  const directTerminalFirstResponsePromise = directStaffIntentResponse(page);
  await directTerminalDialog.getByRole("button", {
    name: personalActionName(
      "sbp",
      fixture.direct_terminal_sbp_student.name,
      fixture.expected.amount_display,
    ),
  }).click();
  const directTerminalFirstResponse = await directTerminalFirstResponsePromise;
  expect(directTerminalFirstResponse.ok()).toBe(true);
  const directTerminalFirstPayload = assertDirectStaffIntentPayload(
    directTerminalFirstResponse,
    fixture,
    fixture.direct_terminal_sbp_student.student_id,
    fixture.direct_terminal_booking,
    "sbp",
  );
  const directTerminalFirstReceipt = (await directTerminalFirstResponse.json()) as StaffIntentReceipt;
  expect(directTerminalFirstReceipt.bank_payment_order_id).toBeGreaterThan(0);
  const cancelDirectTerminalOrder = await page.request.post(
    backendUrl(`/api/billing/bank-payment-orders/${directTerminalFirstReceipt.bank_payment_order_id}/cancel/`),
    { headers: trainerHeaders },
  );
  expect(cancelDirectTerminalOrder.status()).toBe(200);
  await page
    .getByLabel("Подтверждение персональной записи")
    .getByRole("button", { name: "Закрыть", exact: true })
    .click();
  await navigateWithinTrainerApp(page, `/trainer/students/${fixture.direct_terminal_sbp_student.student_id}`);
  const directTerminalReceipt = page.getByRole("region", { name: "Коммерческий контекст персоналки" });
  await expect(directTerminalReceipt).toContainText("Оплата отменена");
  const retryDirectOfferPromise = directOfferResponse(page);
  await directTerminalReceipt.getByRole("button", { name: "Повторить оплату СБП", exact: true }).click();
  const retryDirectOffer = await retryDirectOfferPromise;
  expect(retryDirectOffer.ok()).toBe(true);
  const retryDirectOfferUrl = new URL(retryDirectOffer.url());
  expect(retryDirectOfferUrl.searchParams.get("starts_at")).toBe(
    `${fixture.direct_terminal_booking.date}T${fixture.direct_terminal_booking.start_time}:00`,
  );
  expect(retryDirectOfferUrl.searchParams.get("ends_at")).toBe(
    `${fixture.direct_terminal_booking.date}T${fixture.direct_terminal_booking.end_time}:00`,
  );
  const directRetryDialog = page.getByRole("dialog", { name: "Записать персоналку" });
  await expect(directRetryDialog.getByLabel("Дата *")).toHaveValue(fixture.direct_terminal_booking.date);
  await expect(directRetryDialog.getByLabel("Начало *")).toHaveValue(
    fixture.direct_terminal_booking.start_time,
  );
  await expect(directRetryDialog.getByLabel("Конец *")).toHaveValue(
    fixture.direct_terminal_booking.end_time,
  );
  await expect(directRetryDialog).toContainText("Предыдущая попытка сохранится в истории");
  const directRetryResponsePromise = directStaffIntentResponse(page);
  await directRetryDialog.getByRole("button", {
    name: personalActionName(
      "retry_sbp",
      fixture.direct_terminal_sbp_student.name,
      fixture.expected.amount_display,
    ),
  }).click();
  const directRetryResponse = await directRetryResponsePromise;
  expect(directRetryResponse.ok(), await directRetryResponse.text()).toBe(true);
  const directRetryPayload = assertDirectStaffIntentPayload(
    directRetryResponse,
    fixture,
    fixture.direct_terminal_sbp_student.student_id,
    fixture.direct_terminal_booking,
    "sbp",
    undefined,
    false,
  );
  expect(directRetryPayload.idempotency_key).not.toBe(directTerminalFirstPayload.idempotency_key);
  await reloadStudentCommercialContext(
    page,
    fixture.direct_terminal_sbp_student.student_id,
    (receipt) => receipt.payment_method === "sbp" && ["created", "pending"].includes(receipt.status),
  );
  const allDirectSbpAttempts = page.getByRole("region", { name: "Коммерческий контекст персоналки" });
  await expect(allDirectSbpAttempts).toHaveCount(1);
  await page.getByText("История попыток (1)", { exact: true }).click();
  await expect(allDirectSbpAttempts).toHaveCount(2);
  await expect(allDirectSbpAttempts.filter({ hasText: fixture.expected.sbp_status })).toHaveCount(1);
  await expect(allDirectSbpAttempts.filter({ hasText: "Оплата отменена" })).toHaveCount(1);

  await openStudentAvailability(page, fixture.direct_correction_student.student_id);
  await page.getByRole("button", { name: "Указать время", exact: true }).click();
  const directCorrectionDialog = page.getByRole("dialog", { name: "Записать персоналку" });
  await directCorrectionDialog.getByLabel("Дата *").fill(fixture.direct_correction_booking.date);
  await directCorrectionDialog.getByLabel("Начало *").fill(fixture.direct_correction_booking.start_time);
  await directCorrectionDialog.getByLabel("Конец *").fill(fixture.direct_correction_booking.end_time);
  await directCorrectionDialog
    .getByLabel("Тип тренировки *")
    .selectOption(String(fixture.personal_training_type.id));
  await directCorrectionDialog.getByLabel("Зал *").selectOption(String(fixture.location.id));
  await directCorrectionDialog.getByRole("button", { name: "Оплата через СБП", exact: true }).click();
  await selectPersonalDiscount(page, fixture);
  const directCorrectionSbpResponsePromise = directStaffIntentResponse(page);
  await directCorrectionDialog.getByRole("button", {
    name: personalActionName(
      "sbp",
      fixture.direct_correction_student.name,
      fixture.expected.discounted_amount_display,
    ),
  }).click();
  const directCorrectionSbpResponse = await directCorrectionSbpResponsePromise;
  expect(directCorrectionSbpResponse.ok()).toBe(true);
  assertDirectStaffIntentPayload(
    directCorrectionSbpResponse,
    fixture,
    fixture.direct_correction_student.student_id,
    fixture.direct_correction_booking,
    "sbp",
    fixture.personal_discount.id,
  );
  await page
    .getByLabel("Подтверждение персональной записи")
    .getByRole("button", { name: "Закрыть", exact: true })
    .click();
  await navigateWithinTrainerApp(page, `/trainer/students/${fixture.direct_correction_student.student_id}`);
  const directCorrectionReceipt = page.getByRole("region", { name: "Коммерческий контекст персоналки" });
  await expect(directCorrectionReceipt).toContainText(fixture.expected.discounted_amount_display);
  await directCorrectionReceipt.getByRole("button", { name: "Заменить способ оплаты", exact: true }).click();
  const directCorrectionResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname ===
      `/api/students/${fixture.direct_correction_student.student_id}/personal-commercial-attempts/replace-payment-method/`
      && response.request().method() === "POST";
  });
  await directCorrectionReceipt.getByRole("button", { name: "Наличные", exact: true }).click();
  const directCorrectionResponse = await directCorrectionResponsePromise;
  expect(directCorrectionResponse.ok()).toBe(true);
  expect(directCorrectionResponse.request().postDataJSON()).toEqual({
    reservation_id: expect.any(Number),
    replacement_payment_method: "cash",
    reason: "staff_payment_method_correction",
    idempotency_key: expect.any(String),
  });
  await reloadStudentCommercialContext(
    page,
    fixture.direct_correction_student.student_id,
    (receipt) => receipt.payment_method === "cash" && receipt.status === "pending" && receipt.amount === "2200.00",
  );

  const assertion = runBackendAssert(fixturePath);
  expect(assertion.ok).toBe(true);
  expect(assertion.cash?.payment_id).toBeTruthy();
  expect(assertion.sbp?.bank_payment_order_id).toBeTruthy();
  expect(assertion.entitlement?.enrollment_id).toBeTruthy();
  expect(assertion.pay_at_visit?.debt_id).toBeTruthy();
  expect(assertion.pay_at_visit_sbp?.bank_payment_order_id).toBeTruthy();
  expect(assertion.terminal_sbp?.terminal_reservation_ids).toHaveLength(1);
  expect(assertion.terminal_sbp?.live_bank_payment_order_id).toBeTruthy();
  expect(assertion.payment_correction?.correction_id).toBeTruthy();
  expect(assertion.payment_correction?.replacement_booking_id).toBeTruthy();
  expect(assertion.payment_correction?.status).toBe("scheduled");
  expect(assertion.direct_payment_correction?.replacement_booking_id).toBeTruthy();
  expect(assertion.direct?.payment_id).toBeTruthy();
  expect(assertion.direct_terminal_sbp?.live_bank_payment_order_id).toBeTruthy();
  expect(assertion.direct_terminal_sbp?.starts_at_utc).toBe(
    fixture.direct_terminal_booking.starts_at_utc,
  );
});

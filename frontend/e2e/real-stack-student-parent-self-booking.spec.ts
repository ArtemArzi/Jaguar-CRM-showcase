import { openResetLogin } from "./auth-helpers";
import { backendUrl } from "./support/real-stack-urls";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Locator, type Page, type Response } from "@playwright/test";

interface Credentials {
  email: string;
  password: string;
}

interface LegacyFixture {
  student: Credentials & { student_id: number; user_id: number; name: string };
  parent: Credentials & { child_id: number; user_id: number; child_name: string };
  foreign_child_id: number;
  group_schedule_id: number;
  booking_date: string;
  student_personal_slot_id: number;
  parent_personal_slot_id: number;
  expected: {
    group_name: string;
    personal_training_type_name: string;
    student_personal_group_name: string;
    parent_personal_group_name: string;
    student_personal_time_label: string;
    parent_personal_time_label: string;
    foreign_child_name: string;
  };
}

interface UnifiedFixture {
  club_id: number;
  booking_date: string;
  payment_date: string;
  student: Credentials & { user_id: number; student_id: number; subscription_id: number };
  parent: Credentials & { user_id: number; child_id: number; child_name: string; subscription_id: number };
  payer: Credentials & { user_id: number; student_id: number };
  other_actor: Credentials & { user_id: number; student_id: number };
  personal_tariff: { id: number; price: string };
  student_slot_id: number;
  parent_slot_id: number;
  payer_slot_id: number;
  expected: {
    training_type_name: string;
    student_time_label: string;
    parent_time_label: string;
    payer_time_label: string;
  };
}

interface SelfBookingFixture {
  fixture_id: string;
  legacy: LegacyFixture;
  unified: UnifiedFixture;
}

interface BackendAssertResponse {
  ok?: boolean;
}

interface FlagTransitionResponse {
  ok: boolean;
  club_id: number;
  previous_enabled: boolean;
  enabled: boolean;
}

interface LegacyGroupBookingResult {
  enrollment_id: number;
  schedule_id: number;
  created: boolean;
}

interface LegacyPersonalBookingResult {
  enrollment_id: number;
  availability_slot_id: number;
  created: boolean;
}

interface LegacyPersonalAvailabilityOption {
  slot_id: number;
  booking_status: "can_book" | "blocked";
  subscription_id: number | null;
}

interface UnifiedOption {
  slot_id: number;
  capability: "can_book" | "can_pay";
  offer_digest: string;
}

interface UnifiedCommandCard {
  command_id: number;
  slot_id: number;
  capability: "can_book" | "can_pay";
  status: string;
  bank_payment_order_id: number | null;
  provider_payment_url: string;
  order_status: string;
  allowed_actions: string[];
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) throw new Error("REAL_STACK_E2E_FIXTURE must point to a student/parent self-booking fixture JSON file.");
  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  return fixturePath;
}

function readFixture(fixturePath: string): SelfBookingFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<SelfBookingFixture>;
  if (!data.fixture_id || !data.legacy || !data.unified) {
    throw new Error("Fixture must contain independent legacy and unified self-booking data.");
  }
  const { legacy, unified } = data;
  if (
    !legacy.student?.email || !legacy.student.password || !legacy.student.student_id ||
    !legacy.parent?.email || !legacy.parent.password || !legacy.parent.child_id ||
    !legacy.group_schedule_id || !legacy.booking_date || !legacy.student_personal_slot_id ||
    !legacy.parent_personal_slot_id || !legacy.expected?.group_name ||
    !legacy.expected.personal_training_type_name
  ) {
    throw new Error("Legacy fixture booking credentials and targets are required.");
  }
  if (
    !unified.club_id || !unified.booking_date || !unified.payment_date ||
    !unified.student?.email || !unified.student.password || !unified.student.student_id ||
    !unified.parent?.email || !unified.parent.password || !unified.parent.child_id ||
    !unified.payer?.email || !unified.payer.password || !unified.payer.student_id ||
    !unified.other_actor?.email || !unified.other_actor.password ||
    !unified.personal_tariff?.id || !unified.student_slot_id || !unified.parent_slot_id ||
    !unified.payer_slot_id || !unified.expected?.training_type_name
  ) {
    throw new Error("Unified fixture credentials, slots, and payment offer are required.");
  }
  return data as SelfBookingFixture;
}

function isPostTo(pathname: string) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === pathname && response.request().method() === "POST";
  };
}

function isGetToDate(pathname: string, date: string, childStudentId?: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === pathname &&
      url.searchParams.get("date") === date &&
      (childStudentId === undefined || url.searchParams.get("child_student_id") === String(childStudentId)) &&
      response.request().method() === "GET" &&
      response.status() !== 401
    );
  };
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function login(page: Page, credentials: Credentials, destination: RegExp): Promise<string> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(credentials.email);
  await page.getByLabel("Пароль").fill(credentials.password);
  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/_allauth/app/v1/auth/login" && response.request().method() === "POST";
  });
  await page.getByRole("button", { name: "Войти" }).click();
  const response = await responsePromise;
  expect(response.ok()).toBe(true);
  const body = (await response.json()) as { meta?: { access_token?: unknown } };
  expect(typeof body.meta?.access_token).toBe("string");
  await expect(page).toHaveURL(destination);
  return body.meta?.access_token as string;
}

async function apiAccessToken(page: Page, credentials: Credentials): Promise<string> {
  const response = await page.request.post(backendUrl("/_allauth/app/v1/auth/login"), {
    data: { email: credentials.email, password: credentials.password },
  });
  expect(response.status()).toBe(200);
  const body = (await response.json()) as { meta?: { access_token?: unknown } };
  expect(typeof body.meta?.access_token).toBe("string");
  return body.meta.access_token as string;
}

async function assertCapability(page: Page, accessToken: string, enabled: boolean): Promise<void> {
  const response = await page.request.get(backendUrl("/api/personal-availability/capability/"), {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  expect(response.status()).toBe(200);
  expect(await response.json()).toEqual({
    enabled,
    staff_command_protocol_version: "v1",
  });
}

async function selectBookingDate(page: Page, dateIso: string): Promise<void> {
  const date = new Date(`${dateIso}T12:00:00`);
  const dateLabel = date.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
  const dialog = page.getByRole("dialog", { name: "Записаться" });
  const dateButtonPattern = new RegExp(escapeRegExp(dateLabel));
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const dateButton = dialog.getByRole("button", { name: dateButtonPattern }).first();
    try {
      await expect(dateButton).toBeVisible({ timeout: attempt === 0 ? 750 : 5_000 });
      await dateButton.click();
      return;
    } catch (error) {
      if (attempt === 7) throw error;
    }
    await dialog.getByRole("button", { name: "Следующая неделя" }).click();
  }
}

async function bookLegacyGroup(page: Page, fixture: LegacyFixture): Promise<LegacyGroupBookingResult> {
  const groupSection = page.getByRole("region", { name: /групповую тренировку/ });
  await expect(groupSection.getByText(fixture.expected.group_name)).toBeVisible();
  const responsePromise = page.waitForResponse(
    isPostTo(`/api/schedules/${fixture.group_schedule_id}/guest-bookings/`),
    { timeout: 20_000 },
  );
  await groupSection.getByRole("button", { name: "Записаться" }).first().click();
  const response = await responsePromise;
  expect(response.ok()).toBe(true);
  const result = (await response.json()) as LegacyGroupBookingResult;
  expect(result).toEqual(expect.objectContaining({ created: true, schedule_id: fixture.group_schedule_id }));
  await expect(groupSection.getByText("Запись создана").first()).toBeVisible();
  return result;
}

async function bookLegacyPersonal(
  page: Page,
  slotId: number,
  fixture: LegacyFixture,
  timeLabel: string,
): Promise<LegacyPersonalBookingResult> {
  await page.getByRole("tab", { name: "Персоналка" }).click();
  const section = page.getByRole("region", { name: "Запись на персональную тренировку" });
  await expect(section.getByText(fixture.expected.personal_training_type_name).first()).toBeVisible();
  const slotCard = section.getByRole("group", {
    name: new RegExp(`${escapeRegExp(fixture.expected.personal_training_type_name)}.*${escapeRegExp(timeLabel.split("-")[0] ?? timeLabel)}`),
  });
  await expect(slotCard).toBeVisible();
  const responsePromise = page.waitForResponse(isPostTo(`/api/personal-availability/${slotId}/book/`), {
    timeout: 20_000,
  });
  await slotCard.getByRole("button", { name: "Записаться" }).click();
  const response = await responsePromise;
  expect(response.ok()).toBe(true);
  const result = (await response.json()) as LegacyPersonalBookingResult;
  expect(result).toEqual(expect.objectContaining({ created: true, availability_slot_id: slotId }));
  await expect(section.getByText("Запись создана").first()).toBeVisible();
  return result;
}

async function cancelLegacyBooking(page: Page, pathname: string, bookingName: string): Promise<void> {
  const responsePromise = page.waitForResponse(isPostTo(pathname), { timeout: 20_000 });
  await page.getByRole("button", { name: `Отменить запись ${bookingName}` }).click();
  expect((await responsePromise).ok()).toBe(true);
}

async function expectLegacyPersonalSlot(responsePromise: Promise<Response>, slotId: number): Promise<void> {
  const response = await responsePromise;
  expect(response.ok()).toBe(true);
  const options = (await response.json()) as LegacyPersonalAvailabilityOption[];
  const option = options.find((item) => item.slot_id === slotId);
  expect(option).toEqual(expect.objectContaining({ booking_status: "can_book", subscription_id: expect.any(Number) }));
}

async function verifyLegacyStudentAndParentFlow(page: Page, fixture: LegacyFixture): Promise<void> {
  const studentToken = await login(page, fixture.student, /\/student\/?$/);
  await assertCapability(page, studentToken, false);
  await page.goto("/student/schedule");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible({ timeout: 20_000 });
  const studentOptions = page.waitForResponse(
    isGetToDate("/api/personal-availability/options/", fixture.booking_date),
    { timeout: 20_000 },
  );
  await page.getByRole("button", { name: "Записаться" }).click();
  await selectBookingDate(page, fixture.booking_date);
  await expectLegacyPersonalSlot(studentOptions, fixture.student_personal_slot_id);
  const studentGroup = await bookLegacyGroup(page, fixture);
  const studentPersonal = await bookLegacyPersonal(
    page,
    fixture.student_personal_slot_id,
    fixture,
    fixture.expected.student_personal_time_label,
  );
  await page.getByRole("button", { name: "Закрыть" }).click();
  await expect(page.getByText(fixture.expected.student_personal_group_name).first()).toBeVisible();
  await cancelLegacyBooking(page, `/api/guest-bookings/${studentGroup.enrollment_id}/cancel/`, fixture.expected.group_name);
  await cancelLegacyBooking(
    page,
    `/api/personal-bookings/${studentPersonal.enrollment_id}/cancel/`,
    fixture.expected.student_personal_group_name,
  );

  const parentToken = await login(page, fixture.parent, /\/parent\/?$/);
  await assertCapability(page, parentToken, false);
  await page.goto(`/parent/child/${fixture.parent.child_id}`);
  await expect(page.getByRole("heading", { name: fixture.parent.child_name })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(fixture.expected.foreign_child_name)).not.toBeVisible();
  const parentOptions = page.waitForResponse(
    isGetToDate("/api/personal-availability/options/", fixture.booking_date),
    { timeout: 20_000 },
  );
  await page.getByRole("button", { name: "Записаться" }).click();
  await selectBookingDate(page, fixture.booking_date);
  await expectLegacyPersonalSlot(parentOptions, fixture.parent_personal_slot_id);
  const parentGroup = await bookLegacyGroup(page, fixture);
  const parentPersonal = await bookLegacyPersonal(
    page,
    fixture.parent_personal_slot_id,
    fixture,
    fixture.expected.parent_personal_time_label,
  );
  await page.getByRole("button", { name: "Закрыть" }).click();
  await expect(page.getByText(fixture.expected.parent_personal_group_name).first()).toBeVisible();
  await cancelLegacyBooking(page, `/api/guest-bookings/${parentGroup.enrollment_id}/cancel/`, fixture.expected.group_name);
  await cancelLegacyBooking(
    page,
    `/api/personal-bookings/${parentPersonal.enrollment_id}/cancel/`,
    fixture.expected.parent_personal_group_name,
  );
}

function bookingSection(page: Page): Locator {
  return page.getByRole("region", { name: "Запись на персональную тренировку" });
}

function commandCardsSection(page: Page, childName?: string): Locator {
  return page.getByRole("region", {
    name: childName ? `Персональные тренировки: ${childName}` : "Персональные тренировки",
    exact: true,
  });
}

async function selectUnifiedDate(
  page: Page,
  section: Locator,
  date: string,
  childStudentId?: number,
): Promise<UnifiedOption[]> {
  const responsePromise = page.waitForResponse(
    isGetToDate("/api/personal-availability/self-service/options/", date, childStudentId),
    { timeout: 20_000 },
  );
  await section.getByLabel("Дата").fill(date);
  const response = await responsePromise;
  expect(response.ok()).toBe(true);
  return (await response.json()) as UnifiedOption[];
}

function exactOption(options: UnifiedOption[], slotId: number, capability: UnifiedOption["capability"]): UnifiedOption {
  const option = options.find((item) => item.slot_id === slotId);
  expect(option).toEqual(expect.objectContaining({ capability }));
  return option as UnifiedOption;
}

function optionCard(section: Locator, timeLabel: string): Locator {
  return section.locator("article").filter({ hasText: timeLabel }).first();
}

function commandPayload(response: Response): Record<string, unknown> {
  return response.request().postDataJSON() as Record<string, unknown>;
}

async function assertLegacyCreationBlocked(
  page: Page,
  token: string,
  fixture: UnifiedFixture,
  paidOption: UnifiedOption,
): Promise<void> {
  const headers = { Authorization: `Bearer ${token}` };
  const legacyBook = await page.request.post(
    backendUrl(`/api/personal-availability/${fixture.student_slot_id}/book/`),
    {
      headers,
      data: {
        subscription_id: fixture.student.subscription_id,
        idempotency_key: "e2e-unified-legacy-book-rejected",
      },
    },
  );
  expect(legacyBook.status()).toBe(400);
  expect((await legacyBook.json()) as { code?: unknown }).toEqual(
    expect.objectContaining({ code: "unified_personal_command_required" }),
  );
  const legacyPay = await page.request.post(
    backendUrl(`/api/personal-availability/${fixture.payer_slot_id}/payment-reservations/`),
    {
      headers,
      data: {
        tariff_id: fixture.personal_tariff.id,
        offer_digest: paidOption.offer_digest,
        idempotency_key: "e2e-unified-legacy-pay-rejected",
      },
    },
  );
  expect(legacyPay.status()).toBe(400);
  expect((await legacyPay.json()) as { code?: unknown }).toEqual(
    expect.objectContaining({ code: "unified_personal_command_required" }),
  );
}

async function verifyUnifiedEntitlementBookings(page: Page, fixture: UnifiedFixture): Promise<{
  studentToken: string;
  parentToken: string;
  studentCommand: UnifiedCommandCard;
  parentCommand: UnifiedCommandCard;
}> {
  const studentToken = await login(page, fixture.student, /\/student\/?$/);
  await assertCapability(page, studentToken, true);
  await page.goto("/student/schedule");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible({ timeout: 20_000 });
  const studentBooking = bookingSection(page);
  const options = await selectUnifiedDate(page, studentBooking, fixture.booking_date);
  const option = exactOption(options, fixture.student_slot_id, "can_book");
  expect(option.offer_digest).toBe("");
  const responsePromise = page.waitForResponse(
    isPostTo(`/api/personal-availability/self-service/slots/${fixture.student_slot_id}/command/`),
    { timeout: 20_000 },
  );
  await optionCard(studentBooking, fixture.expected.student_time_label)
    .getByRole("button", { name: "Записаться" })
    .click();
  const response = await responsePromise;
  expect(response.status()).toBe(201);
  const studentPayload = commandPayload(response);
  expect(Object.keys(studentPayload).sort()).toEqual(["idempotency_key"]);
  const studentCommand = (await response.json()) as UnifiedCommandCard;
  expect(studentCommand).toEqual(
    expect.objectContaining({ capability: "can_book", slot_id: fixture.student_slot_id, booking_id: expect.any(Number) }),
  );
  await expect(commandCardsSection(page)).toContainText("Запись сохранена в расписании.");
  await page.reload();
  await expect(commandCardsSection(page)).toContainText("Запись сохранена в расписании.");

  const parentToken = await login(page, fixture.parent, /\/parent\/?$/);
  await assertCapability(page, parentToken, true);
  await page.goto(`/parent/child/${fixture.parent.child_id}`);
  await expect(page.getByRole("heading", { name: fixture.parent.child_name })).toBeVisible({ timeout: 20_000 });
  const parentBooking = bookingSection(page);
  const parentOptions = await selectUnifiedDate(page, parentBooking, fixture.booking_date, fixture.parent.child_id);
  const parentOption = exactOption(parentOptions, fixture.parent_slot_id, "can_book");
  expect(parentOption.offer_digest).toBe("");
  const parentResponsePromise = page.waitForResponse(
    isPostTo(`/api/personal-availability/self-service/slots/${fixture.parent_slot_id}/command/`),
    { timeout: 20_000 },
  );
  await optionCard(parentBooking, fixture.expected.parent_time_label)
    .getByRole("button", { name: "Записаться" })
    .click();
  const parentResponse = await parentResponsePromise;
  expect(parentResponse.status()).toBe(201);
  const parentPayload = commandPayload(parentResponse);
  expect(Object.keys(parentPayload).sort()).toEqual(["child_student_id", "idempotency_key"]);
  expect(parentPayload.child_student_id).toBe(fixture.parent.child_id);
  const parentCommand = (await parentResponse.json()) as UnifiedCommandCard;
  expect(parentCommand).toEqual(
    expect.objectContaining({ capability: "can_book", slot_id: fixture.parent_slot_id, booking_id: expect.any(Number) }),
  );
  await expect(commandCardsSection(page, fixture.parent.child_name)).toContainText("Запись сохранена в расписании.");
  await page.reload();
  await expect(commandCardsSection(page, fixture.parent.child_name)).toContainText("Запись сохранена в расписании.");

  return { studentToken, parentToken, studentCommand, parentCommand };
}

async function verifyUnifiedSbpAndRetry(page: Page, fixture: UnifiedFixture): Promise<{
  payerToken: string;
  commandId: number;
  terminalCommandId: number;
  idempotencyKey: string;
  offerDigest: string;
}> {
  const payerToken = await login(page, fixture.payer, /\/student\/?$/);
  await assertCapability(page, payerToken, true);
  await page.goto("/student/schedule");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible({ timeout: 20_000 });
  const section = bookingSection(page);
  const options = await selectUnifiedDate(page, section, fixture.payment_date);
  const option = exactOption(options, fixture.payer_slot_id, "can_pay");
  expect(option.offer_digest).toMatch(/^[a-f0-9]{64}$/);
  const commandResponsePromise = page.waitForResponse(
    isPostTo(`/api/personal-availability/self-service/slots/${fixture.payer_slot_id}/command/`),
    { timeout: 20_000 },
  );
  await optionCard(section, fixture.expected.payer_time_label)
    .getByRole("button", { name: "Оплатить через СБП" })
    .click();
  const commandResponse = await commandResponsePromise;
  expect(commandResponse.status()).toBe(201);
  const initialPayload = commandPayload(commandResponse);
  expect(Object.keys(initialPayload).sort()).toEqual(["idempotency_key", "offer_digest"]);
  expect(initialPayload.offer_digest).toBe(option.offer_digest);
  const initial = (await commandResponse.json()) as UnifiedCommandCard;
  expect(initial).toEqual(
    expect.objectContaining({
      capability: "can_pay",
      slot_id: fixture.payer_slot_id,
      command_id: expect.any(Number),
      bank_payment_order_id: expect.any(Number),
      order_status: expect.stringMatching(/^(created|pending)$/),
    }),
  );
  expect(initial.allowed_actions).toContain("open_bank_payment_order");

  const cards = commandCardsSection(page);
  const paymentPanel = page.getByRole("region", { name: "Онлайн-оплата персональной тренировки" });
  const paymentLink = paymentPanel.getByRole("link", { name: "Оплатить через СБП" });
  await expect(paymentLink).toBeVisible({ timeout: 20_000 });
  const providerUrl = await paymentLink.getAttribute("href");
  expect(providerUrl).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/mock-payments\//);
  const returnUrl = new URL(providerUrl as string).searchParams.get("return_url");
  expect(returnUrl).toMatch(/\/payments\/return\?state=/);
  await paymentLink.click();
  await expect(page.getByRole("heading", { name: "Тестовая оплата СБП" })).toBeVisible({ timeout: 20_000 });
  await page.goBack();
  await expect(cards).toBeVisible({ timeout: 20_000 });
  await page.reload();
  await expect(paymentPanel.getByRole("link", { name: "Оплатить через СБП" })).toHaveAttribute("href", providerUrl as string);

  const returnExchange = page.waitForResponse(
    isPostTo("/api/billing/payment-returns/exchange/"),
    { timeout: 20_000 },
  );
  await page.goto(returnUrl as string);
  const exchangeResponse = await returnExchange;
  expect(exchangeResponse.ok()).toBe(true);
  expect(await exchangeResponse.json()).toEqual({ status: "checking" });
  await expect(page.getByRole("heading", { name: "Проверяем оплату" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Оплата подтверждена" })).toHaveCount(0);
  const returnStatusAfterReload = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/billing/payment-returns/status/" &&
      response.request().method() === "GET" &&
      response.status() === 200,
    { timeout: 20_000 },
  );
  await page.reload();
  await returnStatusAfterReload;
  await expect(page.getByRole("heading", { name: "Проверяем оплату" })).toBeVisible();
  const scheduleAuthBootstrap = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/auth/refresh/" &&
      response.request().method() === "POST" &&
      response.status() === 200,
    { timeout: 20_000 },
  );
  await page.goto("/student/schedule");
  await scheduleAuthBootstrap;
  await expect(paymentPanel.getByRole("link", { name: "Оплатить через СБП" })).toHaveAttribute("href", providerUrl as string);

  await assertLegacyCreationBlocked(page, payerToken, fixture, option);
  const cancelResponsePromise = page.waitForResponse(
    isPostTo(`/api/personal-availability/self-service/commands/${initial.command_id}/cancel/`),
    { timeout: 20_000 },
  );
  await cards.getByRole("button", { name: "Отменить оплату" }).click();
  const cancelResponse = await cancelResponsePromise;
  expect(cancelResponse.ok()).toBe(true);
  expect(commandPayload(cancelResponse)).toEqual({});
  const terminal = (await cancelResponse.json()) as UnifiedCommandCard;
  expect(terminal).toEqual(
    expect.objectContaining({ command_id: initial.command_id, status: "cancelled", order_status: "" }),
  );
  expect(terminal.allowed_actions).toContain("retry_bank_payment");
  await expect(cards).toContainText("Отменено");
  await page.reload();
  await expect(cards).toContainText("Отменено");

  const freshOptionsPromise = page.waitForResponse(
    isGetToDate("/api/personal-availability/self-service/options/", fixture.payment_date),
    { timeout: 20_000 },
  );
  const retryResponsePromise = page.waitForResponse(
    isPostTo(`/api/personal-availability/self-service/slots/${fixture.payer_slot_id}/command/`),
    { timeout: 20_000 },
  );
  await cards.getByRole("button", { name: "Оплатить заново" }).click();
  const freshOptionsResponse = await freshOptionsPromise;
  expect(freshOptionsResponse.ok()).toBe(true);
  const freshOption = exactOption(
    (await freshOptionsResponse.json()) as UnifiedOption[],
    fixture.payer_slot_id,
    "can_pay",
  );
  const retryResponse = await retryResponsePromise;
  expect(retryResponse.status()).toBe(201);
  const retryPayload = commandPayload(retryResponse);
  expect(Object.keys(retryPayload).sort()).toEqual(["idempotency_key", "offer_digest"]);
  expect(retryPayload.offer_digest).toBe(freshOption.offer_digest);
  expect(retryPayload.idempotency_key).not.toBe(initialPayload.idempotency_key);
  const retry = (await retryResponse.json()) as UnifiedCommandCard;
  expect(retry).toEqual(
    expect.objectContaining({ capability: "can_pay", command_id: expect.any(Number), slot_id: fixture.payer_slot_id }),
  );
  expect(retry.command_id).not.toBe(initial.command_id);
  await expect(cards).toContainText("Отменено");
  await expect(cards.getByRole("link", { name: "Оплатить через СБП" })).toBeVisible();
  expect(typeof retryPayload.idempotency_key).toBe("string");
  return {
    payerToken,
    commandId: retry.command_id,
    terminalCommandId: initial.command_id,
    idempotencyKey: retryPayload.idempotency_key as string,
    offerDigest: freshOption.offer_digest,
  };
}

function disableUnifiedJourney(fixturePath: string): FlagTransitionResponse {
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = execFileSync(
    pythonBin,
    ["manage.py", "disable_unified_client_journey_e2e", "--fixture", fixturePath],
    {
      cwd: repoRoot,
      encoding: "utf8",
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  return JSON.parse(output) as FlagTransitionResponse;
}

async function verifyUnifiedFlagOffDrain(
  page: Page,
  fixturePath: string,
  fixture: UnifiedFixture,
  payment: Awaited<ReturnType<typeof verifyUnifiedSbpAndRetry>>,
): Promise<void> {
  expect(disableUnifiedJourney(fixturePath)).toEqual({
    ok: true,
    club_id: fixture.club_id,
    previous_enabled: true,
    enabled: false,
  });

  await page.goto("/student/schedule");
  await assertCapability(page, payment.payerToken, false);
  const cards = commandCardsSection(page);
  await expect(cards).toContainText("Ожидает оплаты", { timeout: 20_000 });
  await expect(cards.getByRole("button", { name: "Отменить оплату" })).toBeVisible();

  const cancelResponsePromise = page.waitForResponse(
    isPostTo(`/api/personal-availability/self-service/commands/${payment.commandId}/cancel/`),
    { timeout: 20_000 },
  );
  await cards.getByRole("button", { name: "Отменить оплату" }).click();
  const cancelResponse = await cancelResponsePromise;
  expect(cancelResponse.ok()).toBe(true);
  const cancelled = (await cancelResponse.json()) as UnifiedCommandCard;
  expect(cancelled).toEqual(
    expect.objectContaining({ command_id: payment.commandId, status: "cancelled" }),
  );
  expect(cancelled.allowed_actions).not.toContain("retry_bank_payment");
  await expect(cards).toContainText("Отменено", { timeout: 20_000 });

  await page.reload();
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible({ timeout: 20_000 });
  await expect(commandCardsSection(page)).toContainText("Отменено", { timeout: 20_000 });

  const headers = { Authorization: `Bearer ${payment.payerToken}` };
  const commands = await page.request.get(
    backendUrl("/api/personal-availability/self-service/commands/"),
    { headers },
  );
  expect(commands.status()).toBe(200);
  const commandHistory = (await commands.json()) as {
    live: UnifiedCommandCard[];
    latest_terminal: UnifiedCommandCard[];
  };
  expect(commandHistory.live).toEqual([]);
  expect(commandHistory.latest_terminal).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ command_id: payment.commandId, status: "cancelled" }),
    ]),
  );
  expect(
    commandHistory.latest_terminal.find((item) => item.command_id === payment.commandId)
      ?.allowed_actions,
  ).not.toContain("retry_bank_payment");

  const replay = await page.request.post(
    backendUrl(`/api/personal-availability/self-service/slots/${fixture.payer_slot_id}/command/`),
    {
      headers,
      data: {
        idempotency_key: payment.idempotencyKey,
        offer_digest: payment.offerDigest,
      },
    },
  );
  expect(replay.status()).toBe(200);
  expect((await replay.json()) as UnifiedCommandCard).toEqual(
    expect.objectContaining({ command_id: payment.commandId, status: "cancelled" }),
  );

  const blockedNew = await page.request.post(
    backendUrl(`/api/personal-availability/self-service/slots/${fixture.payer_slot_id}/command/`),
    {
      headers,
      data: {
        idempotency_key: `e2e-flag-off-new-${fixture.payer_slot_id}`,
        offer_digest: payment.offerDigest,
      },
    },
  );
  expect(blockedNew.status()).toBe(400);
  expect(await blockedNew.json()).toEqual(
    expect.objectContaining({ code: "unified_client_journey_disabled" }),
  );

  const parentToken = await login(page, fixture.parent, /\/parent\/?$/);
  await assertCapability(page, parentToken, false);
  await page.goto(`/parent/child/${fixture.parent.child_id}`);
  await expect(page.getByRole("heading", { name: fixture.parent.child_name })).toBeVisible({
    timeout: 20_000,
  });
  await expect(commandCardsSection(page, fixture.parent.child_name)).toContainText(
    "Запись сохранена в расписании.",
    { timeout: 20_000 },
  );
  await page.reload();
  await expect(commandCardsSection(page, fixture.parent.child_name)).toContainText(
    "Запись сохранена в расписании.",
    { timeout: 20_000 },
  );
}

async function verifyUnifiedReadIsolation(
  page: Page,
  fixture: UnifiedFixture,
  parentToken: string,
  commandId: number,
): Promise<void> {
  const parentHeaders = { Authorization: `Bearer ${parentToken}` };
  const sourceDenied = await page.request.get(
    backendUrl(`/api/personal-availability/self-service/commands/${commandId}/?child_student_id=${fixture.parent.child_id}`),
    { headers: parentHeaders },
  );
  expect(sourceDenied.status()).toBe(404);
  const otherChildDenied = await page.request.get(
    backendUrl(`/api/personal-availability/self-service/commands/${commandId}/?child_student_id=${fixture.payer.student_id}`),
    { headers: parentHeaders },
  );
  expect(otherChildDenied.status()).toBe(404);

  const otherToken = await apiAccessToken(page, fixture.other_actor);
  const otherActorDenied = await page.request.get(
    backendUrl(`/api/personal-availability/self-service/commands/${commandId}/`),
    { headers: { Authorization: `Bearer ${otherToken}` } },
  );
  expect(otherActorDenied.status()).toBe(404);
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
        ["manage.py", "assert_student_parent_self_booking_e2e", "--fixture", fixturePath],
        {
          cwd: repoRoot,
          encoding: "utf8",
          env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
          stdio: ["ignore", "pipe", "pipe"],
        },
      );
  expect((JSON.parse(output) as BackendAssertResponse).ok).toBe(true);
}

test("real-stack student and parent self-booking keeps flag-off parity and proves the unified command lifecycle", async ({ page }) => {
  test.setTimeout(120_000);
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await verifyLegacyStudentAndParentFlow(page, fixture.legacy);
  const { parentToken } = await verifyUnifiedEntitlementBookings(page, fixture.unified);
  const payment = await verifyUnifiedSbpAndRetry(page, fixture.unified);
  await verifyUnifiedFlagOffDrain(page, fixturePath, fixture.unified, payment);
  await verifyUnifiedReadIsolation(page, fixture.unified, parentToken, payment.commandId);
  runBackendAssert(fixturePath);
});

import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface TrainerPersonalDropInFixture {
  kiosk_pin: string;
  trainer: { email: string; password: string };
  owner: { email: string; password: string };
  assigned_lead: { student_id: number; name: string };
  unrelated_lead: { student_id: number };
  location: { id: number; name: string };
  personal_training_type: { id: number; name: string };
  personal_tariff: { id: number; name: string; price: string };
  booking: { date: string; start_time: string; end_time: string };
  availability_slot: { date: string; client_search: string; client_name: string; student_id: number };
  past_booking: { student_id: number };
  grandfathered_personal_trial: { student_id: number; enrollment_id: number; schedule_id: number; checkin_date: string };
}

interface BookingResponse {
  booking_id?: number;
  schedule_id?: number;
  id?: number;
}

interface BackendAssertResponse {
  ok?: boolean;
  grandfathered_personal_trial_preserved?: boolean;
  grandfathered_personal_trial_completed?: boolean;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a trainer personal drop-in fixture JSON file.");
  }
  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): TrainerPersonalDropInFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<TrainerPersonalDropInFixture>;
  if (!data.kiosk_pin || !/^\d{6}$/.test(data.kiosk_pin)) throw new Error("Fixture kiosk_pin must be a six digit string.");
  if (!data.trainer?.email || !data.trainer.password || !data.owner?.email || !data.owner.password) throw new Error("Fixture trainer and owner credentials are required.");
  if (!data.assigned_lead?.student_id || !data.assigned_lead.name || !data.unrelated_lead?.student_id) throw new Error("Fixture assigned and unrelated leads are required.");
  if (!data.location?.id || !data.personal_training_type?.id || !data.personal_tariff?.id) throw new Error("Fixture personal booking configuration is required.");
  if (!data.booking?.date || !data.booking.start_time || !data.booking.end_time) throw new Error("Fixture future booking time is required.");
  if (!data.availability_slot?.date || !data.availability_slot.client_search || !data.availability_slot.client_name || !data.availability_slot.student_id) throw new Error("Fixture slot date and client search data are required.");
  if (!data.past_booking?.student_id) throw new Error("Fixture past unattended booking is required.");
  if (
    !data.grandfathered_personal_trial?.student_id
    || !data.grandfathered_personal_trial.enrollment_id
    || !data.grandfathered_personal_trial.schedule_id
    || !data.grandfathered_personal_trial.checkin_date
  ) {
    throw new Error("Fixture grandfathered personal trial check-in data is required.");
  }
  return data as TrainerPersonalDropInFixture;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
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

async function loginAsTrainer(page: Page, fixture: TrainerPersonalDropInFixture): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(fixture.trainer.email);
  await page.getByLabel("Пароль").fill(fixture.trainer.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

async function enterKioskPin(page: Page, pin: string): Promise<string> {
  await page.goto("/kiosk/");
  for (const [index, digit] of [...pin].entries()) {
    await page.getByLabel(`PIN цифра ${index + 1}`).fill(digit);
  }
  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
  const token = await page.evaluate(() => localStorage.getItem("kiosk_device_token"));
  if (!token) throw new Error("Kiosk token was not stored after fixture PIN activation.");
  return token;
}

async function kioskPost(page: Page, token: string, path: string, body: unknown) {
  return page.evaluate(
    async ({ path, token, body }) => {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Kiosk-Token": token },
        body: JSON.stringify(body),
      });
      return { ok: response.ok, status: response.status, body: await response.json() };
    },
    { path, token, body },
  );
}

async function createPayAtClubBooking(page: Page, fixture: TrainerPersonalDropInFixture): Promise<BookingResponse> {
  await page.goto("/trainer/leads");
  await page.getByRole("button", { name: new RegExp(escapeRegExp(fixture.assigned_lead.name)) }).click();
  await page.getByRole("button", { name: "Записать персоналку" }).click();
  await expect(page.getByRole("dialog", { name: "Записать персоналку" })).toBeVisible();
  await expect(page.getByRole("dialog", { name: fixture.assigned_lead.name })).toHaveCount(0);

  const form = page.getByRole("dialog", { name: "Записать персоналку" });
  await form.getByRole("button", { name: "Оплата в клубе" }).click();
  await form.getByLabel("Разовая персоналка *").selectOption(String(fixture.personal_tariff.id));
  await form.getByLabel("Дата *").fill(fixture.booking.date);
  await form.getByLabel("Начало *").fill(fixture.booking.start_time);
  await form.getByLabel("Конец *").fill(fixture.booking.end_time);
  await form.getByLabel("Тип тренировки *").selectOption(String(fixture.personal_training_type.id));
  await form.getByLabel("Зал *").selectOption(String(fixture.location.id));

  const bookingResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === `/api/students/${fixture.assigned_lead.student_id}/personal-drop-in-bookings/` && response.request().method() === "POST";
  });
  await form.getByRole("button", { name: "Записать с оплатой в клубе" }).click();
  const response = await bookingResponse;
  expect(response.ok()).toBe(true);
  await expect(form).toBeHidden();
  return (await response.json()) as BookingResponse;
}

async function assertPrimaryBookingVisibleInTrainerSchedule(
  page: Page,
  fixture: TrainerPersonalDropInFixture,
  booking: BookingResponse,
): Promise<void> {
  const scheduleId = booking.schedule_id;
  if (!scheduleId) throw new Error("Drop-in response must contain schedule_id.");
  await page.goto(`/trainer/schedule/${scheduleId}/checkin?date=${fixture.booking.date}`);
  await expect(page).toHaveURL(new RegExp(`/trainer/schedule/${scheduleId}/checkin\\?date=${fixture.booking.date}`));
  await expect(page.getByRole("heading", { level: 1 })).not.toHaveText("Загрузка...");
  const rosterName = fixture.assigned_lead.name.split(" ").reverse().join(" ");
  await expect(
    page.getByRole("button", {
      name: new RegExp(`^Открыть контекст ученика ${escapeRegExp(rosterName)}:`),
    }),
  ).toBeVisible();
}

async function assertExactKioskAndCheckIn(
  page: Page,
  fixture: TrainerPersonalDropInFixture,
  booking: BookingResponse,
): Promise<string> {
  const scheduleId = booking.schedule_id;
  if (!scheduleId) throw new Error("Drop-in response must contain schedule_id.");
  const token = await enterKioskPin(page, fixture.kiosk_pin);

  const options = await kioskPost(page, token, "/api/checkins/kiosk/options/", {
    student_id: fixture.assigned_lead.student_id,
    date: fixture.booking.date,
  });
  expect(options.ok, `kiosk options returned ${options.status}`).toBe(true);
  expect(JSON.stringify(options.body)).toContain(String(scheduleId));

  const unrelated = await kioskPost(page, token, "/api/checkins/kiosk/options/", {
    student_id: fixture.unrelated_lead.student_id,
    date: fixture.booking.date,
  });
  expect(unrelated.ok).toBe(false);
  expect(unrelated.status).toBe(400);
  expect((unrelated.body as { code?: string }).code).toBe("student_ineligible");
  expect(JSON.stringify(unrelated.body)).not.toContain(String(scheduleId));

  const payload = {
    student_id: fixture.assigned_lead.student_id,
    schedule_id: scheduleId,
    training_type_id: fixture.personal_training_type.id,
    checkin_date: fixture.booking.date,
  };
  const checkin = await kioskPost(page, token, "/api/checkins/kiosk/", payload);
  expect(checkin.ok, `kiosk check-in returned ${checkin.status}`).toBe(true);
  const first = checkin.body as { checkin_id?: number; created?: boolean; is_debt?: boolean };
  expect(first.created).toBe(true);
  expect(first.is_debt).toBe(true);
  expect(first.checkin_id).toBeTruthy();

  const replay = await kioskPost(page, token, "/api/checkins/kiosk/", payload);
  expect(replay.ok, `kiosk check-in replay returned ${replay.status}`).toBe(true);
  const repeated = replay.body as { checkin_id?: number; created?: boolean; duplicate?: boolean; is_debt?: boolean };
  expect(repeated.created).toBe(false);
  expect(repeated.duplicate).toBe(true);
  expect(repeated.checkin_id).toBe(first.checkin_id);
  expect(repeated.is_debt).toBe(true);
  return token;
}

async function completeGrandfatheredPersonalTrial(
  page: Page,
  fixture: TrainerPersonalDropInFixture,
  token: string,
): Promise<void> {
  const trial = fixture.grandfathered_personal_trial;
  const options = await kioskPost(page, token, "/api/checkins/kiosk/options/", {
    student_id: trial.student_id,
    date: trial.checkin_date,
  });
  expect(options.ok, `grandfathered trial kiosk options returned ${options.status}`).toBe(true);
  expect(JSON.stringify(options.body)).toContain(String(trial.schedule_id));

  const payload = {
    student_id: trial.student_id,
    schedule_id: trial.schedule_id,
    training_type_id: fixture.personal_training_type.id,
    checkin_date: trial.checkin_date,
  };
  const checkin = await kioskPost(page, token, "/api/checkins/kiosk/", payload);
  expect(checkin.ok, `grandfathered trial kiosk check-in returned ${checkin.status}`).toBe(true);
  expect((checkin.body as { created?: boolean; is_debt?: boolean }).created).toBe(true);
  expect((checkin.body as { is_debt?: boolean }).is_debt).toBe(false);

  const replay = await kioskPost(page, token, "/api/checkins/kiosk/", payload);
  expect(replay.ok, `grandfathered trial kiosk replay returned ${replay.status}`).toBe(true);
  expect((replay.body as { created?: boolean; duplicate?: boolean }).created).toBe(false);
  expect((replay.body as { duplicate?: boolean }).duplicate).toBe(true);
}

async function recordAndConfirmExactPayment(page: Page, fixture: TrainerPersonalDropInFixture): Promise<void> {
  await page.goto(`/trainer/students/${fixture.assigned_lead.student_id}`);
  await expect(page.getByRole("heading", { name: fixture.assigned_lead.name })).toBeVisible({ timeout: 20_000 });
  const personalDebtBlock = page.getByText("Долг за персоналку", { exact: true }).locator("..");
  const personalDebtPaymentButton = personalDebtBlock.getByRole("button", {
    name: "Принять оплату",
    exact: true,
  });
  await expect(personalDebtPaymentButton).toBeVisible();
  await personalDebtPaymentButton.click();
  const paymentSheet = page.getByRole("dialog", { name: "Принять оплату" });
  await expect(paymentSheet.getByText("Тариф и долг этой персоналки зафиксированы. Другую оплату или долг выбрать нельзя.")).toBeVisible();
  const paymentResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return /\/api\/personal-drop-in-bookings\/\d+\/payments\/$/.test(url.pathname) && response.request().method() === "POST";
  });
  await paymentSheet.getByRole("button", { name: "Принять оплату" }).click();
  expect((await paymentResponse).ok()).toBe(true);

  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await page.goto(backendUrl("/dashboard/billing/payments/"));
  await expect(page.getByRole("heading", { name: "Оплаты" })).toBeVisible();
  const verifyResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return /\/dashboard\/billing\/payments\/\d+\/verify\/$/.test(url.pathname) && response.request().method() === "POST";
  });
  await page.getByRole("button", { name: "Подтвердить" }).first().click();
  const paymentListReload = page.waitForNavigation({
    waitUntil: "domcontentloaded",
    url: /\/dashboard\/billing\/payments\/$/,
  });
  await page.getByRole("button", { name: "Подтвердить" }).last().click();
  const [verified] = await Promise.all([verifyResponse, paymentListReload]);
  expect(verified.status()).toBe(204);
  const verifyRequest = verified.request();
  const verifyBody = verifyRequest.postData() ?? "action=confirm";
  const verifyHeaders = verifyRequest.headers();
  const csrfToken = verifyHeaders["x-csrftoken"];
  if (!csrfToken) throw new Error("Owner payment verification request must include a CSRF token.");

  const replay = await page.evaluate(
    async ({ url, body, csrfToken }) => {
      const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
          "HX-Request": "true",
          "X-CSRFToken": csrfToken,
        },
        body,
      });
      return { status: response.status, redirect: response.headers.get("HX-Redirect") };
    },
    { url: verifyRequest.url(), body: verifyBody, csrfToken },
  );
  expect(replay.status).toBe(204);
  expect(replay.redirect).toBe("/dashboard/billing/payments/");
}

async function bookAndCancelAvailabilitySlot(page: Page, fixture: TrainerPersonalDropInFixture): Promise<void> {
  await loginAsTrainer(page, fixture);
  await page.goto("/trainer/availability");
  await selectAvailabilityDate(page, fixture.availability_slot.date);
  const publishedSlot = page.getByRole("button", {
    name: new RegExp(
      `${escapeRegExp(fixture.personal_training_type.name)}.*${escapeRegExp(fixture.location.name)}.*Свободно$`,
    ),
  });
  await expect(publishedSlot).toHaveCount(1);
  await publishedSlot.click();
  const bookClientButton = page.getByRole("button", { name: "Записать клиента", exact: true });
  await expect(bookClientButton).toHaveCount(1);
  await bookClientButton.click();
  await page.getByLabel("Поиск клиента").fill(fixture.availability_slot.client_search);
  await page.getByRole("button", { name: new RegExp(escapeRegExp(fixture.availability_slot.client_name)) }).click();
  await expect(page.getByText("Слот зафиксирован")).toBeVisible();
  await expect(page.getByLabel("Дата *")).toHaveCount(0);
  await page.getByRole("button", { name: "Оплата в клубе" }).click();
  await page.getByLabel("Разовая персоналка *").selectOption(String(fixture.personal_tariff.id));
  const createResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return /\/api\/personal-availability\/slots\/\d+\/drop-in-bookings\/$/.test(url.pathname) && response.request().method() === "POST";
  });
  await page.getByRole("button", { name: "Записать с оплатой в клубе" }).click();
  expect((await createResponse).ok()).toBe(true);
  await expect(page.getByText("Клиент записан:")).toBeVisible();

  await page.goto(`/trainer/students/${fixture.availability_slot.student_id}`);
  await expect(page.getByRole("button", { name: "Отменить запись" })).toBeVisible();
  const cancelResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return /\/api\/personal-drop-in-bookings\/\d+\/cancel\/$/.test(url.pathname) && response.request().method() === "POST";
  });
  await page.getByRole("button", { name: "Отменить запись" }).click();
  expect((await cancelResponse).ok()).toBe(true);
}

async function markPastBookingNoShow(page: Page, fixture: TrainerPersonalDropInFixture): Promise<void> {
  await page.goto(`/trainer/students/${fixture.past_booking.student_id}`);
  await expect(page.getByRole("button", { name: "Не пришёл" })).toBeVisible();
  const noShowResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return /\/api\/personal-drop-in-bookings\/\d+\/no-show\/$/.test(url.pathname) && response.request().method() === "POST";
  });
  await page.getByRole("button", { name: "Не пришёл" }).click();
  expect((await noShowResponse).ok()).toBe(true);
}

function runBackendAssert(fixturePath: string): void {
  const command = process.env.REAL_STACK_E2E_ASSERT_COMMAND;
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const assertionEnv = {
    ...process.env,
    REAL_STACK_E2E_FIXTURE: fixturePath,
    REAL_STACK_E2E_REQUIRE_GRANDFATHERED_COMPLETION: "1",
  };
  const output = command
    ? execSync(command, { cwd: repoRoot, encoding: "utf8", env: assertionEnv })
    : execFileSync(pythonBin, ["manage.py", "assert_trainer_personal_drop_in_e2e", "--fixture", fixturePath, "--require-grandfathered-completion"], {
        cwd: repoRoot,
        encoding: "utf8",
        env: assertionEnv,
      });
  const assertion = JSON.parse(output) as BackendAssertResponse;
  expect(assertion.ok).toBe(true);
  expect(assertion.grandfathered_personal_trial_preserved).toBe(true);
  expect(assertion.grandfathered_personal_trial_completed).toBe(true);
}

test("real-stack trainer personal drop-in locks exact booking, kiosk debt, payment, slot, and no-show paths", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);
  await loginAsTrainer(page, fixture);
  const booking = await createPayAtClubBooking(page, fixture);
  await assertPrimaryBookingVisibleInTrainerSchedule(page, fixture, booking);
  const kioskToken = await assertExactKioskAndCheckIn(page, fixture, booking);
  await recordAndConfirmExactPayment(page, fixture);
  await bookAndCancelAvailabilitySlot(page, fixture);
  await markPastBookingNoShow(page, fixture);
  await completeGrandfatheredPersonalTrial(page, fixture, kioskToken);
  runBackendAssert(fixturePath);
});

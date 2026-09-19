import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  expect,
  test,
  type Locator,
  type Page,
  type Request,
  type Response,
  type Route,
  type TestInfo,
} from "@playwright/test";

test.use({ serviceWorkers: "block" });

interface Credentials {
  email: string;
  password: string;
}

interface PwaUxLedgerFixture {
  fixture_id: string;
  club_id: number;
  control_club_id: number;
  trainer: Credentials & { trainer_id: number };
  student: Credentials & { student_id: number; name: string };
  package_student: Credentials & { student_id: number; name: string };
  parent: Credentials & { child_id: number; child_name: string };
  location: {
    id: number;
    name: string;
  };
  tariffs: {
    group_name: string;
    personal_name: string;
    personal_id: number;
    personal_training_type_id: number;
    personal_price: string;
  };
  renewal: {
    order_id: number;
  };
  trainer_recovery: {
    order_id: number;
    subscription_id: number;
  };
  booking_date: string;
  group_schedule: {
    id: number;
    name: string;
  };
  trainer_session: {
    schedule_id: number;
    date: string;
    group_name: string;
    roster_student_name: string;
  };
  slots: Record<string, { id: number; starts_at: string; ends_at: string; time_label: string }>;
}

interface BackendAssertResponse {
  ok?: boolean;
  stage?: string;
}

type CapabilityState = "loading" | "error" | "malformed";

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a PWA UX ledger fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): PwaUxLedgerFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<PwaUxLedgerFixture>;
  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  if (!data.student?.email || !data.student.password || !data.student.student_id) {
    throw new Error("Fixture student credentials are required.");
  }
  if (!data.package_student?.email || !data.package_student.password) {
    throw new Error("Fixture package student credentials are required.");
  }
  if (!data.parent?.email || !data.parent.password || !data.parent.child_id) {
    throw new Error("Fixture parent credentials are required.");
  }
  if (!data.location?.id || !data.location.name) {
    throw new Error("Fixture location is required.");
  }
  if (!data.trainer_recovery?.order_id || !data.trainer_recovery.subscription_id) {
    throw new Error("Fixture trainer recovery order is required.");
  }
  if (
    !data.tariffs?.personal_name ||
    !data.tariffs.personal_id ||
    !data.tariffs.personal_training_type_id ||
    !data.booking_date ||
    !data.slots
  ) {
    throw new Error("Fixture tariffs, booking date, and slots are required.");
  }
  return data as PwaUxLedgerFixture;
}

function isPostTo(pathname: string | RegExp) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    const matches = typeof pathname === "string" ? url.pathname === pathname : pathname.test(url.pathname);
    return matches && response.request().method() === "POST";
  };
}

function isRelevantPaymentCreateRequest(request: Request): boolean {
  if (request.method() !== "POST") return false;
  const pathname = new URL(request.url()).pathname;
  return [
    /^\/api\/billing\/bank-payment-orders\/$/,
    /^\/api\/students\/me\/bank-payment-orders\/$/,
    /^\/api\/parents\/children\/\d+\/bank-payment-orders\/$/,
    /^\/api\/personal-availability\/\d+\/payment-reservations\/$/,
    /^\/api\/personal-availability\/slots\/\d+\/staff-payment-reservations\/$/,
    /^\/api\/students\/\d+\/personal-booking-payment-reservations\/$/,
    /^\/api\/personal-drop-in-bookings\/\d+\/bank-payment-orders\/$/,
  ].some((pattern) => pattern.test(pathname));
}

async function installPaymentCapabilityState(
  page: Page,
  state: CapabilityState,
): Promise<{ intercepted: Promise<void>; release: () => Promise<void> }> {
  let resolveIntercepted: (() => void) | undefined;
  const intercepted = new Promise<void>((resolvePromise) => {
    resolveIntercepted = resolvePromise;
  });
  let loadingRoute: Route | undefined;
  const handler = async (route: Route) => {
    if (state === "loading") {
      loadingRoute = route;
      resolveIntercepted?.();
      return;
    }
    await route.fulfill({
      status: state === "error" ? 503 : 200,
      contentType: "application/json",
      body: JSON.stringify(
        state === "error"
          ? { detail: "controlled capability unavailable" }
          : { online_payments_enabled: "true" },
      ),
    });
    resolveIntercepted?.();
  };
  await page.route("**/api/billing/payment-capabilities/", handler);
  return {
    intercepted,
    release: async () => {
      if (loadingRoute) {
        await loadingRoute.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ online_payments_enabled: false }),
        });
      }
      await page.unroute("**/api/billing/payment-capabilities/", handler);
    },
  };
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function textContainsAllWords(value: string): RegExp {
  const words = value.trim().split(/\s+/).filter(Boolean).map(escapeRegExp);
  return new RegExp(`${words.map((word) => `(?=.*${word})`).join("")}.*`);
}

async function login(page: Page, credentials: Credentials, expectedPath: RegExp): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(credentials.email);
  await page.getByLabel("Пароль").fill(credentials.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(expectedPath);
}

async function openTrainerPaymentDialog(page: Page): Promise<Locator> {
  const paymentAction = page.getByRole("button", { name: "Принять оплату", exact: true });
  await expect(paymentAction).toBeVisible();
  await paymentAction.click();
  const dialog = page.getByRole("dialog", { name: "Принять оплату" });
  await expect(dialog).toBeVisible();
  return dialog;
}

function runBackendAssert(fixturePath: string, stage: string): BackendAssertResponse {
  const command = process.env.REAL_STACK_E2E_ASSERT_COMMAND;
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = command
    ? execSync(`${command} --stage ${stage}`, {
        cwd: repoRoot,
        encoding: "utf8",
        env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
        stdio: ["ignore", "pipe", "pipe"],
      })
    : execFileSync(
        pythonBin,
        ["manage.py", "assert_pwa_ux_ledger_smoke_e2e", "--fixture", fixturePath, "--stage", stage],
        {
          cwd: repoRoot,
          encoding: "utf8",
          env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
          stdio: ["ignore", "pipe", "pipe"],
        },
      );

  const result = JSON.parse(output) as BackendAssertResponse;
  expect(result.ok).toBe(true);
  expect(result.stage).toBe(stage);
  return result;
}

function paymentPanel(pageOrScope: Page | Locator, title: string): Locator {
  return pageOrScope.getByRole("region", { name: title }).first();
}

async function expectQrCollapsed(panel: Locator): Promise<void> {
  await expect(panel.getByRole("button", { name: "Показать QR" })).toHaveAttribute(
    "aria-expanded",
    "false",
  );
  await expect(panel.getByRole("img", { name: "QR-код ссылки на оплату" })).toHaveCount(0);
}

async function expandQr(panel: Locator): Promise<void> {
  await panel.getByRole("button", { name: "Показать QR" }).click();
  await expect(panel.getByRole("button", { name: "Скрыть QR" })).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await expect(panel.getByRole("img", { name: "QR-код ссылки на оплату" })).toBeVisible();
}

async function cancelPaymentPanel(
  page: Page,
  panel: Locator,
  cancelLabel: string,
  cancelPath: string | RegExp,
): Promise<void> {
  await panel.getByRole("button", { name: cancelLabel }).click();
  const cancelSheet = page.getByRole("dialog").filter({ hasText: "Отменить оплату?" });
  await expect(cancelSheet).toBeVisible();

  const cancelResponse = page.waitForResponse(isPostTo(cancelPath), {
    timeout: 20_000,
  });
  await cancelSheet.getByRole("button", { name: cancelLabel }).click();
  expect((await cancelResponse).ok()).toBe(true);
  await expect(cancelSheet).toHaveCount(0);
}

async function installControlledApiFailure(
  page: Page,
  pathname: string | RegExp,
  attempts = 2,
): Promise<() => Promise<void>> {
  let remaining = attempts;
  const handler = async (route: Route) => {
    const requestPath = new URL(route.request().url()).pathname;
    const matches =
      typeof pathname === "string"
        ? requestPath === pathname
        : pathname.test(requestPath);
    if (matches && remaining > 0) {
      remaining -= 1;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "controlled unavailable response" }),
      });
      return;
    }
    await route.fallback();
  };

  await page.route("**/api/**", handler);
  return () => page.unroute("**/api/**", handler);
}

async function openBookingSheet(page: Page, buttonName: string | RegExp): Promise<Locator> {
  await page.getByRole("button", { name: buttonName }).click();
  const dialog = page.getByRole("dialog", { name: "Записаться" });
  await expect(dialog).toBeVisible({ timeout: 20_000 });
  return dialog;
}

async function selectBookingDate(dialog: Locator, dateIso: string): Promise<void> {
  const day = String(new Date(`${dateIso}T12:00:00`).getDate());
  const dayPattern = new RegExp(`\\b${day}\\b`);

  for (let attempt = 0; attempt < 8; attempt += 1) {
    const dateButton = dialog.getByRole("button").filter({ hasText: dayPattern }).first();
    try {
      await expect(dateButton).toBeVisible({ timeout: attempt === 0 ? 750 : 5_000 });
      await dateButton.click();
      return;
    } catch (error) {
      if (attempt === 7) {
        throw error;
      }
    }

    await dialog.getByRole("button", { name: "Следующая неделя" }).click();
  }
}

async function openPersonalTab(dialog: Locator): Promise<void> {
  await dialog.getByRole("tab", { name: "Персоналка" }).click();
  await expect(dialog.getByRole("heading", { name: "Выберите свободный слот" })).toBeVisible({
    timeout: 20_000,
  });
}

function personalSlot(dialog: Locator, fixture: PwaUxLedgerFixture, slotKey: string): Locator {
  const slot = fixture.slots[slotKey];
  const start = (slot.starts_at.includes("T") ? slot.starts_at.split("T")[1] : slot.time_label).slice(0, 5);
  return dialog
    .getByRole("group", {
      name: new RegExp(`Персональный слот .*${escapeRegExp(start)}`),
    })
    .first();
}

async function bookExistingPackagePersonal(
  page: Page,
  fixturePath: string,
  fixture: PwaUxLedgerFixture,
): Promise<void> {
  await login(page, fixture.package_student, /\/student\/?$/);
  await page.getByRole("link", { name: "Расписание", exact: true }).click();
  await expect(page).toHaveURL(/\/student\/schedule\/?$/);
  const dialog = await openBookingSheet(page, "Записаться");
  await selectBookingDate(dialog, fixture.booking_date);
  await openPersonalTab(dialog);

  const slot = personalSlot(dialog, fixture, "package_primary");
  await expect(slot).toBeVisible({ timeout: 20_000 });
  await expect(slot.getByText("По абонементу")).toBeVisible();
  await expect(slot.getByRole("button", { name: "Записаться" })).toBeEnabled();
  const bookResponse = page.waitForResponse(
    isPostTo(`/api/personal-availability/${fixture.slots.package_primary.id}/book/`),
    { timeout: 20_000 },
  );
  await slot.getByRole("button", { name: "Записаться" }).click();
  expect((await bookResponse).ok()).toBe(true);
  await expect(slot.getByRole("button", { name: "Записан" })).toBeVisible({ timeout: 20_000 });
  await expect(slot.getByRole("button", { name: "Оплатить" })).toHaveCount(0);
  runBackendAssert(fixturePath, "existing_package_booked");
}

async function assertMockProviderSafety(
  page: Page,
  fixturePath: string,
  fixture: PwaUxLedgerFixture,
): Promise<void> {
  const unsafeCreatePosts: string[] = [];
  const recordCreate = (request: Request) => {
    if (isRelevantPaymentCreateRequest(request)) {
      unsafeCreatePosts.push(new URL(request.url()).pathname);
    }
  };
  page.on("request", recordCreate);

  await login(page, fixture.trainer, /\/trainer\/?$/);
  await page.goto(`/trainer/students/${fixture.student.student_id}`);
  const trainerRecoveryPanel = paymentPanel(page, "Онлайн-оплата");
  await expect(trainerRecoveryPanel).toBeVisible({ timeout: 20_000 });
  await expect(
    trainerRecoveryPanel.getByRole("link", { name: "Открыть предпросмотр" }),
  ).toBeVisible();
  await expectQrCollapsed(trainerRecoveryPanel);
  await expandQr(trainerRecoveryPanel);
  runBackendAssert(fixturePath, "trainer_direct_order_pending");
  await cancelPaymentPanel(
    page,
    trainerRecoveryPanel,
    "Отменить оплату",
    `/api/billing/bank-payment-orders/${fixture.trainer_recovery.order_id}/cancel/`,
  );
  const cancelledTrainerPanel = paymentPanel(page, "Онлайн-оплата");
  await expect(cancelledTrainerPanel).toBeVisible();
  await expect(
    cancelledTrainerPanel.locator('[data-slot="badge"]').filter({ hasText: "Отменена" }),
  ).toBeVisible();
  await expect(cancelledTrainerPanel.getByText(/^Ссылка отменена/)).toBeVisible();
  await expect(cancelledTrainerPanel.getByRole("link")).toHaveCount(0);
  await expect(cancelledTrainerPanel.getByRole("button", { name: "Отменить оплату" })).toHaveCount(0);
  runBackendAssert(fixturePath, "trainer_direct_order_cancelled");

  await login(page, fixture.student, /\/student\/?$/);
  const renewalPanel = paymentPanel(page, "Продление ожидает оплаты");
  await expect(renewalPanel).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("button", { name: "Продлить через СБП" })).toHaveCount(0);
  runBackendAssert(fixturePath, "renewal_reused");
  await cancelPaymentPanel(
    page,
    renewalPanel,
    "Отменить продление",
    `/api/students/me/bank-payment-orders/${fixture.renewal.order_id}/cancel/`,
  );
  const cancelledRenewalPanel = paymentPanel(page, "Последняя онлайн-оплата");
  await expect(cancelledRenewalPanel).toBeVisible({ timeout: 20_000 });
  await expect(
    cancelledRenewalPanel.locator('[data-slot="badge"]').filter({ hasText: "Отменена" }),
  ).toBeVisible();
  await expect(cancelledRenewalPanel.getByText(/^Ссылка отменена/)).toBeVisible();
  await expect(cancelledRenewalPanel.getByRole("link")).toHaveCount(0);
  await expect(cancelledRenewalPanel.getByRole("button", { name: "Отменить продление" })).toHaveCount(0);
  runBackendAssert(fixturePath, "renewal_cancelled");

  await page.getByRole("link", { name: "Расписание", exact: true }).click();
  await expect(page).toHaveURL(/\/student\/schedule\/?$/);
  const studentDialog = await openBookingSheet(page, "Записаться");
  await selectBookingDate(studentDialog, fixture.booking_date);
  await openPersonalTab(studentDialog);
  await expect(
    personalSlot(studentDialog, fixture, "student_primary").getByRole("button", { name: "Оплатить" }),
  ).toBeEnabled();

  await login(page, fixture.parent, /\/parent\/?$/);
  await page.goto(`/parent/child/${fixture.parent.child_id}`);
  await expect(page.getByRole("button", { name: "Продлить через СБП" })).toHaveCount(0);
  const parentDialog = await openBookingSheet(page, "Записать ребёнка");
  await selectBookingDate(parentDialog, fixture.booking_date);
  await openPersonalTab(parentDialog);
  await expect(
    personalSlot(parentDialog, fixture, "parent_primary").getByRole("button", { name: "Оплатить" }),
  ).toBeEnabled();

  await login(page, fixture.trainer, /\/trainer\/?$/);
  await page.goto(`/trainer/students/${fixture.student.student_id}`);
  const paymentDialog = await openTrainerPaymentDialog(page);
  await page.getByRole("button", { name: new RegExp(escapeRegExp(fixture.tariffs.group_name)) }).click();
  const groupChoice = paymentDialog.getByRole("radiogroup", { name: "Выбор постоянной группы" });
  await expect(groupChoice.getByRole("radio", { checked: true })).toHaveCount(0);
  await expect(paymentDialog.getByRole("button", { name: "Принять оплату" })).toBeDisabled();
  await paymentDialog.getByRole("button", { name: "СБП" }).click();
  await expect(paymentDialog.getByRole("button", { name: "Создать ссылку СБП" })).toBeDisabled();
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "Записать" }).first().click();
  const trainerPersonalDialog = page.getByRole("dialog", { name: "Записать персоналку" });
  await trainerPersonalDialog.getByRole("button", { name: "Оплата через СБП" }).click();
  await expect(
    trainerPersonalDialog.getByRole("button", { name: "Создать ссылку СБП" }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");

  const loadingCapability = await installPaymentCapabilityState(page, "loading");
  await page.goto(`/trainer/students/${fixture.student.student_id}`);
  const loadingDialog = await openTrainerPaymentDialog(page);
  await loadingDialog
    .getByRole("button", { name: new RegExp(escapeRegExp(fixture.tariffs.group_name)) })
    .click();
  await loadingCapability.intercepted;
  await expect(
    loadingDialog
      .getByRole("radiogroup", { name: "Выбор постоянной группы" })
      .getByRole("radio"),
  ).toHaveCount(0);
  await expect(loadingDialog.getByRole("button", { name: "СБП" })).toBeDisabled();
  await loadingCapability.release();

  const errorCapability = await installPaymentCapabilityState(page, "error");
  await login(page, fixture.student, /\/student\/?$/);
  await errorCapability.intercepted;
  await expect(page.getByRole("button", { name: "Продлить через СБП" })).toHaveCount(0);
  await page.getByRole("link", { name: "Расписание", exact: true }).click();
  await expect(page).toHaveURL(/\/student\/schedule\/?$/);
  const errorDialog = await openBookingSheet(page, "Записаться");
  await selectBookingDate(errorDialog, fixture.booking_date);
  await openPersonalTab(errorDialog);
  await expect(
    personalSlot(errorDialog, fixture, "student_primary").getByRole("button", { name: "Оплатить" }),
  ).toBeDisabled();
  await errorCapability.release();

  const malformedCapability = await installPaymentCapabilityState(page, "malformed");
  await login(page, fixture.parent, /\/parent\/?$/);
  await page.goto(`/parent/child/${fixture.parent.child_id}`);
  await malformedCapability.intercepted;
  await expect(page.getByRole("button", { name: "Продлить через СБП" })).toHaveCount(0);
  const malformedDialog = await openBookingSheet(page, "Записать ребёнка");
  await selectBookingDate(malformedDialog, fixture.booking_date);
  await openPersonalTab(malformedDialog);
  await expect(
    personalSlot(malformedDialog, fixture, "parent_primary").getByRole("button", { name: "Оплатить" }),
  ).toBeDisabled();
  await malformedCapability.release();

  expect(unsafeCreatePosts).toEqual([]);
  page.off("request", recordCreate);
}

async function assertStudentScheduleClarity(page: Page, fixture: PwaUxLedgerFixture): Promise<void> {
  await login(page, fixture.student, /\/student\/?$/);
  await page.getByRole("link", { name: "Расписание", exact: true }).click();
  await expect(page).toHaveURL(/\/student\/schedule\/?$/);
  const dialog = await openBookingSheet(page, "Записаться");
  await selectBookingDate(dialog, fixture.booking_date);
  await expect(dialog.getByText(fixture.group_schedule.name)).toBeVisible({ timeout: 20_000 });
  await expect(dialog.getByRole("button", { name: "Уже записан" })).toBeDisabled();
  await openPersonalTab(dialog);
  await expect(dialog.getByText("Ledger Locked Personal")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Недоступно" }).first()).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
}

async function assertAttendanceEmpty(page: Page, fixturePath: string): Promise<void> {
  await page.getByRole("link", { name: "Посещения", exact: true }).click();
  await expect(page).toHaveURL(/\/student\/attendance\/?$/);
  await expect(page.getByText("Посещений пока нет")).toBeVisible({ timeout: 20_000 });
  await expect(
    page.getByText("После первой отметки тренера здесь появятся дата, группа, тренер и зал."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Календарь" }).click();
  await expect(page.getByText("За этот месяц отметок пока нет.")).toBeVisible();
  const disabledDay = page.getByRole("button", { name: /нет посещений/ }).first();
  await expect(disabledDay).toBeDisabled();
  await disabledDay.click({ force: true });
  await expect(page.getByRole("dialog")).toHaveCount(0);
  runBackendAssert(fixturePath, "empty_states");
}

function screenshotPath(testInfo: TestInfo, fileName: string): string {
  const logDir = process.env.REAL_STACK_E2E_LOG_DIR;
  if (logDir) {
    mkdirSync(logDir, { recursive: true });
    return join(logDir, fileName);
  }
  return testInfo.outputPath(fileName);
}

async function assertTrainerResponsiveRoster(
  page: Page,
  fixturePath: string,
  fixture: PwaUxLedgerFixture,
  testInfo: TestInfo,
): Promise<void> {
  await login(page, fixture.trainer, /\/trainer\/?$/);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("heading", { name: "Мои тренировки" })).toBeVisible({
    timeout: 20_000,
  });
  const card = page
    .getByRole("group", {
      name: new RegExp(`Тренировка .*${escapeRegExp(fixture.trainer_session.group_name)}`),
    })
    .first();
  await expect(card).toContainText("Ledger Trainer");
  await expect(card.getByRole("button", { name: "Статус" })).toBeVisible();
  await card.screenshot({ path: screenshotPath(testInfo, "trainer-card-responsive.png") });

  await card.getByRole("button", { name: "Статус" }).click();
  await expect(page).toHaveURL(new RegExp(`/trainer/schedule/${fixture.trainer_session.schedule_id}/checkin`));
  await expect(page.getByRole("heading", { name: /Ledger Trainer Session/ })).toBeVisible({
    timeout: 20_000,
  });

  const checkinPosts: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (request.method() === "POST" && url.pathname.startsWith("/api/checkins/")) {
      checkinPosts.push(url.pathname);
    }
  });
  const rosterNameMatcher = textContainsAllWords(fixture.trainer_session.roster_student_name);
  await page
    .getByRole("button", {
      name: new RegExp(`(?=.*Открыть контекст ученика)${rosterNameMatcher.source}`),
    })
    .click();
  const sheet = page.getByRole("dialog", { name: rosterNameMatcher });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByText("Статус", { exact: true })).toBeVisible();
  await expect(sheet.getByRole("button", { name: "Открыть карточку" })).toBeVisible();
  await expect(sheet.getByRole("button", { name: "Отметить" })).toHaveCount(0);
  await expect(sheet.getByRole("button", { name: "Снять отметку" })).toHaveCount(0);
  expect(checkinPosts).toEqual([]);
  await sheet.screenshot({ path: screenshotPath(testInfo, "trainer-roster-sheet-responsive.png") });
  runBackendAssert(fixturePath, "trainer_roster_readonly");

  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  await page.getByRole("link", { name: "Задачи", exact: true }).click();
  await expect(page).toHaveURL(/\/trainer\/tasks\/?$/);
  await expect(page.getByText("Задач сейчас нет")).toBeVisible({ timeout: 20_000 });
  runBackendAssert(fixturePath, "empty_states");
}

test("trainer PWA exposes controlled API failures, blocks unsafe actions, and recovers", async ({
  page,
}) => {
  test.setTimeout(60_000);
  const fixture = readFixture(requireFixturePath());

  const removeIdentityFailure = await installControlledApiFailure(
    page,
    "/api/trainers/me/",
  );
  await login(page, fixture.trainer, /\/trainer\/?$/);
  await expect(page.getByText("Не удалось загрузить профиль тренера")).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByRole("heading", { name: "Мои тренировки" })).toHaveCount(0);

  const removeScheduleFailure = await installControlledApiFailure(
    page,
    "/api/schedules/today/",
  );
  await page.getByRole("button", { name: "Повторить" }).click();
  await expect(page.getByText("Не удалось загрузить тренировки")).toBeVisible({
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Повторить загрузку тренировок" }).click();
  await expect(page.getByText("Не удалось загрузить тренировки")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Мои тренировки" })).toBeVisible();
  await removeIdentityFailure();
  await removeScheduleFailure();

  const removeSalaryFailure = await installControlledApiFailure(
    page,
    /\/api\/trainers\/\d+\/earnings\/summary\/$/,
    Number.POSITIVE_INFINITY,
  );
  await page.goto("/trainer/salary");
  await expect(page.getByText("Не удалось загрузить итог зарплаты")).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText(/^0\s*₽$/)).toHaveCount(0);
  await removeSalaryFailure();
  await page.getByRole("button", { name: "Повторить загрузку итога" }).click();
  await expect(page.getByText("Не удалось загрузить итог зарплаты")).toHaveCount(0);

  const removeDebtFailure = await installControlledApiFailure(
    page,
    "/api/billing/debts/",
  );
  await page.goto(`/trainer/students/${fixture.student.student_id}`);
  await expect(
    page.getByRole("heading", { name: textContainsAllWords(fixture.student.name) }),
  ).toBeVisible({ timeout: 20_000 });
  const paymentDialog = await openTrainerPaymentDialog(page);
  await expect(paymentDialog.getByText("Не удалось загрузить долги")).toBeVisible({
    timeout: 20_000,
  });
  await paymentDialog.locator('button[aria-pressed="false"]').first().click();
  await expect(paymentDialog.getByRole("button", { name: "Принять оплату" })).toBeDisabled();
  await paymentDialog.getByRole("button", { name: "Повторить загрузку долгов" }).click();
  await expect(paymentDialog.getByText("Не удалось загрузить долги")).toHaveCount(0);
  await expect(paymentDialog.getByRole("button", { name: "Принять оплату" })).toBeDisabled();
  await paymentDialog
    .getByRole("radiogroup", { name: "Выбор постоянной группы" })
    .getByRole("radio", { name: new RegExp(escapeRegExp(fixture.group_schedule.name)) })
    .click();
  await expect(paymentDialog.getByRole("button", { name: "Принять оплату" })).toBeEnabled();
  await removeDebtFailure();
});

test("real-stack PWA UX ledger smoke covers payment panels, personal holds, empty states, and roster context", async ({
  page,
}, testInfo) => {
  test.setTimeout(150_000);
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);
  await assertMockProviderSafety(page, fixturePath, fixture);
  await bookExistingPackagePersonal(page, fixturePath, fixture);
  await assertStudentScheduleClarity(page, fixture);
  await assertAttendanceEmpty(page, fixturePath);
  await assertTrainerResponsiveRoster(page, fixturePath, fixture, testInfo);
  runBackendAssert(fixturePath, "all");
});

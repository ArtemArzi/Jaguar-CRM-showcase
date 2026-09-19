import { expect, test, type Page } from "@playwright/test";
import {
  authenticateAs,
  failUnhandledApi,
  mockBranding,
  mockJson,
} from "./support/mock-api";

function collectConsoleErrors(page: Page) {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

test("trainer mobile shell opens schedule with mocked API", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await failUnhandledApi(page);
  await authenticateAs(page, "trainer");
  await mockBranding(page);

  await mockJson(page, "**/api/trainers/me/", {
    id: 201,
    first_name: "Анна",
    last_name: "Тренер",
    student_count: 12,
  });
  await mockJson(page, "**/api/schedules/today/", []);
  await mockJson(page, "**/api/schedules/unclosed/**", []);
  await mockJson(page, "**/api/trainers/201/earnings/summary/", {
    trainer_id: 201,
    period_start: "2026-06-01",
    period_end: "2026-06-30",
    total_amount: "0.00",
    paid_amount: "0.00",
    pending_amount: "0.00",
    session_count: 0,
  });
  await mockJson(page, "**/api/retention/tasks/**", { items: [] });
  await page.goto("/app");

  await expect(page).toHaveURL(/\/trainer\/?$/);
  await expect(page.getByRole("heading", { name: /Привет, Анна/ })).toBeVisible();
  await expect(page.getByText("Сегодня нет тренировок")).toBeVisible();
  await expect(page.getByRole("navigation")).toContainText("Расписание");
});

test("trainer availability finds a scoped client from a phone fragment", async ({ page }) => {
  const consoleErrors = collectConsoleErrors(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await failUnhandledApi(page);
  await authenticateAs(page, "trainer");
  await mockBranding(page);

  const today = new Date().toISOString().slice(0, 10);
  let receivedQuery = "";
  await mockJson(page, "**/api/trainers/me/", {
    id: 201,
    first_name: "Анна",
    last_name: "Тренер",
  });
  await mockJson(page, "**/api/retention/tasks/**", { items: [] });
  await mockJson(page, "**/api/clubs/locations/", [{ id: 2, name: "Основной зал" }]);
  await mockJson(page, "**/api/billing/training-types/", [
    { id: 7, name: "Персональная", kind: "personal", is_active: true },
  ]);
  await mockJson(page, "**/api/personal-availability/slots/**", [
    {
      id: 51,
      date: today,
      starts_at: `${today}T18:00:00`,
      ends_at: `${today}T19:00:00`,
      trainer_id: 201,
      trainer_name: "Анна Тренер",
      location_id: 2,
      location_name: "Основной зал",
      training_type_id: 7,
      training_type_name: "Персональная",
      training_type_kind: "personal",
      status: "published",
      block_reason: "",
      booked_enrollment_id: null,
      can_block: true,
      can_unblock: false,
      can_cancel: true,
    },
  ]);
  await page.route("**/api/students/**", async (route) => {
    const url = new URL(route.request().url());
    receivedQuery = url.searchParams.get("q") ?? "";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        count: 1,
        items: [
          {
            id: 44,
            first_name: "Иван",
            last_name: "Телефон",
            phone: "+79123456789",
            guardian_phone: "",
            email: "",
            status: "active",
            is_child: false,
            date_of_birth: null,
            source: "other",
          },
        ],
      }),
    });
  });

  await page.goto("/trainer/availability");
  await expect(page).toHaveURL(/\/trainer\/availability$/);
  await expect(page).toHaveTitle("CRM Jaguar");
  await expect(page.getByRole("heading", { name: "Доступность" })).toBeVisible();
  await page.locator("button").filter({ hasText: "Персональная" }).filter({ hasText: "Свободно" }).click();
  await page.getByRole("button", { name: "Записать клиента" }).click();
  await page.getByLabel("Поиск клиента").fill("8912 345");

  await expect(page.getByRole("button", { name: /Иван Телефон/ })).toBeVisible();
  expect(receivedQuery).toBe("8912 345");
  expect(consoleErrors).toEqual([]);
});

test("trainer personal booking sheet stays anchored on a mobile viewport", async ({ page }) => {
  const consoleErrors = collectConsoleErrors(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await failUnhandledApi(page);
  await authenticateAs(page, "trainer");
  await mockBranding(page);
  await mockJson(page, "**/api/billing/payment-capabilities/", { online_payments_enabled: false });
  await mockJson(page, "**/api/students/intakes/capability", { enabled: false });
  await mockJson(page, "**/api/personal-availability/capability/", { enabled: false });
  await mockJson(page, "**/api/billing/bank-payment-orders/**", { items: [] });
  await mockJson(page, "**/api/students/7/personal-booking-payment-reservations/**", []);
  await mockJson(page, "**/api/students/7/commercial-context/", {
    student_id: 7,
    attempts: [],
  });

  await mockJson(page, "**/api/trainers/me/", {
    id: 201,
    first_name: "Анна",
    last_name: "Тренер",
  });
  await mockJson(page, "**/api/retention/tasks/**", { items: [] });
  await mockJson(page, "**/api/students/7/", {
    id: 7,
    first_name: "Иван",
    last_name: "Клиент",
    phone: "+79123456789",
    guardian_phone: "",
    email: "",
    status: "active",
    is_child: false,
    contraindications: "",
    notes: [],
    can_manage_feedback: true,
    can_manage_sensitive_actions: true,
    can_manage_account_access: false,
    account_access: null,
  });
  await mockJson(page, "**/api/billing/subscriptions/**", []);
  await mockJson(page, "**/api/students/7/personal-bookings/", []);
  await mockJson(page, "**/api/grades/students/7/progress/", []);
  await mockJson(page, "**/api/students/7/checkins/**", []);
  await mockJson(page, "**/api/feedback/students/7/responses/", []);
  await mockJson(page, "**/api/clubs/locations/", [{ id: 2, name: "Основной зал" }]);
  await mockJson(page, "**/api/billing/training-types/", [
    { id: 7, name: "Персональная", slug: "personal", kind: "personal", is_active: true },
  ]);
  await mockJson(page, "**/api/billing/tariffs/**", {
    items: [
      {
        id: 33,
        name: "Разовая персоналка с очень длинным названием для мобильной проверки",
        price: 2000,
        training_type: {
          id: 7,
          name: "Персональная",
          slug: "personal",
          kind: "personal",
          is_active: true,
        },
        trainings_limit: 1,
        duration_days: 1,
        scope: "club",
        location_id: null,
        is_active: true,
      },
    ],
  });

  await page.goto("/trainer/students/7");
  await expect(page).toHaveURL(/\/trainer\/students\/7$/);
  await expect(page).toHaveTitle("CRM Jaguar");
  await expect(page.getByRole("heading", { name: "Иван Клиент" })).toBeVisible();
  await page.getByRole("button", { name: "Записать", exact: true }).click();

  const dialog = page.getByRole("dialog", { name: "Записать персоналку" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Оплата в клубе" })).toBeVisible();
  await expect(dialog).toHaveCSS("translate", "none");

  const before = await dialog.evaluate((sheet) => {
    const scrollRegion = sheet.querySelector<HTMLElement>('[data-slot="personal-booking-scroll"]');
    const rect = sheet.getBoundingClientRect();
    const scrollStyle = scrollRegion ? getComputedStyle(scrollRegion) : null;
    return {
      left: rect.left,
      top: rect.top,
      viewportWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.scrollingElement?.scrollWidth ?? 0,
      sheetClientWidth: sheet.clientWidth,
      sheetScrollWidth: sheet.scrollWidth,
      touchAction: scrollStyle?.touchAction ?? "",
      overflowX: scrollStyle?.overflowX ?? "",
      overscrollY: scrollStyle?.overscrollBehaviorY ?? "",
    };
  });

  const scrollRegion = dialog.locator('[data-slot="personal-booking-scroll"]');
  await scrollRegion.evaluate((element) => {
    element.scrollTop = 160;
    element.scrollLeft = 160;
  });
  const after = await dialog.evaluate((sheet) => {
    const scroll = sheet.querySelector<HTMLElement>('[data-slot="personal-booking-scroll"]');
    const rect = sheet.getBoundingClientRect();
    return { left: rect.left, top: rect.top, scrollLeft: scroll?.scrollLeft ?? -1 };
  });

  expect(before.pageScrollWidth).toBeLessThanOrEqual(before.viewportWidth);
  expect(before.sheetScrollWidth).toBeLessThanOrEqual(before.sheetClientWidth + 1);
  expect(before.touchAction).toBe("pan-y");
  expect(before.overflowX).toBe("hidden");
  expect(before.overscrollY).toBe("contain");
  expect(after.left).toBeCloseTo(before.left, 1);
  expect(after.top).toBeCloseTo(before.top, 1);
  expect(after.scrollLeft).toBe(0);
  expect(consoleErrors).toEqual([]);
});

test("new lead sheet waits for a deliberate field tap before focusing an input", async ({ page }) => {
  const consoleErrors = collectConsoleErrors(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await failUnhandledApi(page);
  await authenticateAs(page, "trainer");
  await mockBranding(page);

  await mockJson(page, "**/api/trainers/me/", {
    id: 201,
    first_name: "Анна",
    last_name: "Тренер",
  });
  await mockJson(page, "**/api/retention/tasks/**", { items: [] });
  await mockJson(page, "**/api/leads/**", { count: 0, items: [] });
  await mockJson(page, "**/api/billing/payment-capabilities/", { online_payments_enabled: false });
  await mockJson(page, "**/api/students/intakes/capability", { enabled: false });
  await mockJson(page, "**/api/personal-availability/capability/", { enabled: false });

  await page.goto("/trainer/leads");
  await expect(page).toHaveURL(/\/trainer\/leads$/);
  await expect(page).toHaveTitle("CRM Jaguar");
  await expect(page.getByRole("heading", { name: "Заявки" })).toBeVisible();
  await page.getByRole("button", { name: "Новая заявка" }).click();

  const dialog = page.getByRole("dialog", { name: "Новая заявка" });
  const nameInput = dialog.getByLabel("Имя клиента *");
  await expect(dialog).toBeVisible();
  await expect(dialog).toBeFocused();
  await expect(nameInput).not.toBeFocused();

  await nameInput.click();
  await expect(nameInput).toBeFocused();
  expect(consoleErrors).toEqual([]);
});

test("student mobile shell opens home with mocked API", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await failUnhandledApi(page);
  await authenticateAs(page, "student");
  await mockBranding(page);
  await mockJson(page, "**/api/billing/payment-capabilities/", { online_payments_enabled: false });
  await mockJson(page, "**/api/students/intakes/capability", { enabled: false });
  await mockJson(page, "**/api/personal-availability/capability/", { enabled: false });

  await mockJson(page, "**/api/students/me/", { id: 301 });
  await mockJson(page, "**/api/students/me/financial-state/", {
    operational_admission: null,
    covered_visits: [],
  });
  await mockJson(page, "**/api/students/me/bank-payment-orders/**", []);
  await mockJson(page, "**/api/students/me/subscriptions/", [
    {
      id: 401,
      tariff_name: "Месячный",
      trainings_used: 3,
      trainings_total: 12,
      trainings_left: 9,
      expires_at: "2026-07-15",
      status: "active",
      freeze_status: null,
    },
  ]);
  await mockJson(page, "**/api/students/me/debts/", []);
  await mockJson(page, "**/api/grades/my-progress/", [
    {
      grade_system_name: "Каратэ",
      current_grade: { id: 1, name: "Белый пояс", order: 1, min_trainings: 0 },
      trainings_since_last_grade: 4,
      next_grade: { id: 2, name: "Жёлтый пояс", order: 2, min_trainings: 10 },
      trainings_to_next: 6,
    },
  ]);
  await mockJson(page, "**/api/students/me/schedule-week/**", []);
  await mockJson(page, "**/api/personal-availability/payment-reservations/**", []);
  await page.goto("/app");

  await expect(page).toHaveURL(/\/student\/?$/);
  await expect(page.getByRole("heading", { name: "Главная" })).toBeVisible();
  await expect(page.getByText("Белый пояс")).toBeVisible();
  await expect(page.getByText("Месячный")).toBeVisible();
});

test("student personal booking sheet keeps long payment tariff inside mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await failUnhandledApi(page);
  await authenticateAs(page, "student");
  await mockBranding(page);
  await mockJson(page, "**/api/billing/payment-capabilities/", { online_payments_enabled: false });
  await mockJson(page, "**/api/personal-availability/capability/", { enabled: false });

  const tariffName = "Персоналка Максим Бранзовой по чек-ину";
  await mockJson(page, "**/api/students/me/", { id: 301 });
  await mockJson(page, "**/api/students/me/schedule-week/**", []);
  await mockJson(page, "**/api/students/me/schedule/**", []);
  await mockJson(page, "**/api/schedules/guest-booking-options/**", []);
  await mockJson(page, "**/api/personal-availability/payment-reservations/**", []);
  await mockJson(page, "**/api/personal-availability/options/**", [
    {
      slot_id: 56,
      date: "2026-04-13",
      starts_at: "2026-04-13T03:00:00Z",
      ends_at: "2026-04-13T04:00:00Z",
      trainer_id: 3,
      trainer_name: "Ivan Petrov",
      location_id: 2,
      location_name: "Main hall",
      training_type_id: 8,
      training_type_name: "Персональная тренировка",
      booking_status: "can_pay",
      reason_code: "payment_required",
      subscription_id: null,
      payment_tariff_id: 12,
      payment_tariff_name: tariffName,
      payment_amount: "3000.00",
    },
  ]);

  await page.goto("/student/schedule");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible();
  await page.getByRole("button", { name: "Записаться" }).click();

  const dialog = page.getByRole("dialog", { name: "Записаться" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("tab", { name: "Персоналка" }).click();

  const paymentBadge = dialog.getByText(tariffName);
  await expect(paymentBadge).toBeVisible();
  await expect(paymentBadge).toHaveClass(/max-w-full/);
  await expect(paymentBadge).toHaveClass(/whitespace-normal/);
  await expect(paymentBadge).toHaveClass(/break-words/);

  const metrics = await page.evaluate((text) => {
    const sheet = document.querySelector('[data-slot="sheet-content"][data-side="bottom"]');
    const badge = Array.from(document.querySelectorAll("[data-slot='badge']")).find((node) =>
      node.textContent?.includes(text),
    );
    const sheetRect = sheet?.getBoundingClientRect();
    const badgeRect = badge?.getBoundingClientRect();
    return {
      viewportWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.scrollingElement?.scrollWidth ?? 0,
      sheetClientWidth: sheet?.clientWidth ?? 0,
      sheetScrollWidth: sheet?.scrollWidth ?? 0,
      sheetRight: sheetRect?.right ?? 0,
      badgeRight: badgeRect?.right ?? 0,
    };
  }, tariffName);

  expect(metrics.pageScrollWidth).toBeLessThanOrEqual(metrics.viewportWidth);
  expect(metrics.sheetScrollWidth).toBeLessThanOrEqual(metrics.sheetClientWidth + 1);
  expect(metrics.badgeRight).toBeLessThanOrEqual(metrics.sheetRight + 1);
});

test("parent mobile shell opens child overview with mocked API", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await failUnhandledApi(page);
  await authenticateAs(page, "parent");
  await mockBranding(page);
  await mockJson(page, "**/api/billing/payment-capabilities/", { online_payments_enabled: false });
  await mockJson(page, "**/api/personal-availability/capability/", { enabled: false });

  await mockJson(page, "**/api/parents/children/", [
    {
      id: 501,
      first_name: "Иван",
      last_name: "Петров",
      status: "active",
      is_child: true,
      grade_name: "Белый пояс",
      subscription_remaining: 8,
      subscription_total: 12,
      subscription_status: "active",
      subscription_freeze_status: null,
      last_visit_date: "2026-06-14",
      next_training_day_of_week: 2,
      next_training_start_time: "18:00:00",
      next_training_group_name: "Kids Boxing",
      next_training_trainer_name: "Анна Тренер",
      next_training_is_rescheduled: false,
      next_training_is_substitute: false,
    },
  ]);
  await page.goto("/app");

  await expect(page).toHaveURL(/\/parent\/?$/);
  await expect(page.getByRole("heading", { name: "Мой ребёнок" })).toBeVisible();
  await expect(page.getByText("Иван Петров")).toBeVisible();
  await expect(page.getByText("Kids Boxing")).toBeVisible();
});

import { expect, test } from "@playwright/test";

function timePart(date: Date): string {
  return date.toTimeString().slice(0, 8);
}

function localDatePart(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

const TABLET_VIEWPORTS = [
  { name: "portrait", width: 768, height: 1024 },
  { name: "landscape", width: 1024, height: 768 },
];
const SYNTHETIC_DEVICE_CREDENTIAL = ["synthetic", "device", "credential"].join("-");

for (const viewport of TABLET_VIEWPORTS) {
  test(`tablet kiosk activates, accepts 4 digits, and confirms check-in in ${viewport.name}`, async ({
    page,
  }) => {
  await page.setViewportSize({
    width: viewport.width,
    height: viewport.height,
  });
  const now = new Date();
  const start = new Date(now.getTime() - 5 * 60 * 1000);
  const end = new Date(now.getTime() + 55 * 60 * 1000);
  const opensAt = new Date(start.getTime() - 30 * 60 * 1000);
  const checkinDate = localDatePart(now);

  let activated = false;
  let lookedUpSuffix = "";
  let checkedIn = false;

  await page.route("**/api/checkins/kiosk/activate/", async (route) => {
    const body = route.request().postDataJSON() as { pin?: string };
    expect(body.pin).toBe("123456");
    activated = true;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        token: SYNTHETIC_DEVICE_CREDENTIAL,
        club_id: 7,
        club_name: "Jaguar Gym",
      }),
    });
  });

  await page.route("**/api/checkins/kiosk/branding/", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        primary_color: "#111111",
        accent_color: "#ff6b00",
        club_name_display: "Jaguar Gym",
        logo_url: "",
      }),
    });
  });

  await page.route("**/api/checkins/kiosk/schedules/today/", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          schedule_id: 31,
          effective_date: checkinDate,
          start_time: timePart(start),
          end_time: timePart(end),
          group_name: "Kids Boxing",
          trainer_name: "Coach One",
          location_name: "Main Hall",
          training_type_id: 12,
          training_type_name: "Group",
        },
      ]),
    });
  });

  await page.route("**/api/checkins/kiosk/lookup/", async (route) => {
    const body = route.request().postDataJSON() as { phone_suffix?: string };
    lookedUpSuffix = body.phone_suffix ?? "";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: 101,
          first_name: "Ivan",
          last_name: "Petrov",
          masked_phone: "***-**-45-67",
          lookup_suffix: "4567",
          group_name: "Kids Boxing",
          grade_name: "Yellow belt",
          subscription_name: "Monthly",
          subscription_status: "active",
          trainings_left: 7,
        },
      ]),
    });
  });

  await page.route("**/api/checkins/kiosk/roster/", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: 101,
          first_name: "Ivan",
          last_name: "Petrov",
          masked_phone: "***-**-45-67",
          lookup_suffix: "4567",
          group_name: "Kids Boxing",
          grade_name: "Yellow belt",
          subscription_name: "Monthly",
          subscription_status: "active",
          trainings_left: 7,
        },
      ]),
    });
  });

  await page.route("**/api/checkins/kiosk/options/", async (route) => {
    const body = route.request().postDataJSON() as {
      student_id?: number;
      date?: string;
    };
    expect(body.student_id).toBe(101);
    if (body.date !== undefined) {
      expect(body.date).toBe(checkinDate);
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        student_id: 101,
        date: checkinDate,
        options: [
          {
            schedule_id: 31,
            effective_date: checkinDate,
            start_time: timePart(start),
            end_time: timePart(end),
            group_name: "Kids Boxing",
            trainer_name: "Coach One",
            location_name: "Main Hall",
            training_type_id: 12,
            training_type_name: "Group",
            self_checkin_status: "can_checkin",
            reason_code: "",
            financial_status: "subscription",
            subscription_id: 88,
            drop_in_price: null,
            existing_checkin_id: null,
            checkin_window_status: "open",
            checkin_opens_at: opensAt.toISOString(),
            checkin_closes_at: end.toISOString(),
          },
        ],
      }),
    });
  });

  await page.route("**/api/checkins/kiosk/", async (route) => {
    const body = route.request().postDataJSON() as {
      student_id?: number;
      schedule_id?: number;
      training_type_id?: number;
    };
    expect(body).toMatchObject({
      student_id: 101,
      schedule_id: 31,
      training_type_id: 12,
      checkin_date: checkinDate,
    });
    checkedIn = true;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        checkin_id: 501,
        created: true,
        duplicate: false,
        is_debt: false,
        subscription_id: 88,
        subscription_effect: "deducted",
        debt_effect: "none",
        salary_queued: true,
        parent_notification_queued: true,
        grade_progress_queued: true,
        alerts: [],
      }),
    });
  });

  await page.goto("/kiosk/");

  for (const [index, digit] of [..."123456"].entries()) {
    await page.getByLabel(`PIN цифра ${index + 1}`).fill(digit);
  }

  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
  await expect.poll(() => activated).toBe(true);

  for (const digit of "4567") {
    await page.getByRole("button", { name: `Цифра ${digit}` }).click();
  }

  await expect.poll(() => lookedUpSuffix).toBe("4567");
  await expect.poll(() => checkedIn).toBe(true);
  await expect(page.getByText("Посещение сохранено")).toBeVisible();
  await expect(page.getByText("Абонемент списан")).toBeVisible();
  await expect(page.getByText("Зарплата тренеру в очереди")).not.toBeVisible();
  await expect(page.getByText("Уведомление родителю в очереди")).not.toBeVisible();
  await expect(page.getByText("Прогресс в очереди")).not.toBeVisible();
  });
}

test("kiosk waits for the student's personal booking without exposing payment state", async ({
  page,
}) => {
  const checkinDate = localDatePart(new Date());
  let checkinAttempted = false;

  await page.addInitScript(
    ({ token }) => {
      localStorage.setItem("kiosk_device_token", token);
      localStorage.setItem("kiosk_club_id", "7");
      localStorage.setItem("kiosk_club_name", "Jaguar Gym");
    },
    { token: SYNTHETIC_DEVICE_CREDENTIAL },
  );

  await page.route("**/api/checkins/kiosk/branding/", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        primary_color: "#111111",
        accent_color: "#ff6b00",
        club_name_display: "Jaguar Gym",
        logo_url: "",
      }),
    });
  });

  await page.route("**/api/checkins/kiosk/schedules/today/", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });

  await page.route("**/api/checkins/kiosk/roster/", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });

  await page.route("**/api/checkins/kiosk/lookup/", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: 101,
          first_name: "Руслан",
          last_name: "Иванов",
          masked_phone: "***-**-45-67",
          lookup_suffix: "4567",
          group_name: "",
          grade_name: "",
          subscription_name: "",
          subscription_status: "",
          trainings_left: null,
        },
      ]),
    });
  });

  await page.route("**/api/checkins/kiosk/options/", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        student_id: 101,
        date: checkinDate,
        options: [
          {
            schedule_id: 5,
            effective_date: checkinDate,
            start_time: "20:00:00",
            end_time: "21:00:00",
            group_name: "Текущая группа",
            trainer_name: "",
            location_name: "Зал",
            training_type_id: 2,
            training_type_name: "Групповая",
            self_checkin_status: "blocked",
            reason_code: "drop_in_price_required",
            financial_status: "blocked",
            subscription_id: null,
            drop_in_price: null,
            existing_checkin_id: null,
            checkin_window_status: "open",
            checkin_opens_at: `${checkinDate}T19:30:00+05:00`,
            checkin_closes_at: `${checkinDate}T21:00:00+05:00`,
          },
          {
            schedule_id: 17,
            effective_date: checkinDate,
            start_time: "21:00:00",
            end_time: "22:00:00",
            group_name: "Персональная тренировка",
            trainer_name: "",
            location_name: "Зал",
            training_type_id: 3,
            training_type_name: "Персональная",
            self_checkin_status: "can_checkin",
            reason_code: "",
            financial_status: "drop_in_debt",
            subscription_id: null,
            drop_in_price: "2000.00",
            existing_checkin_id: null,
            checkin_window_status: "too_early",
            checkin_opens_at: `${checkinDate}T20:30:00+05:00`,
            checkin_closes_at: `${checkinDate}T22:00:00+05:00`,
          },
        ],
      }),
    });
  });

  await page.route("**/api/checkins/kiosk/", async (route) => {
    checkinAttempted = true;
    await route.fulfill({ status: 500, body: "{}" });
  });

  await page.goto("/kiosk/");
  for (const digit of "4567") {
    await page.getByRole("button", { name: `Цифра ${digit}` }).click();
  }

  await expect(page.getByText("Персональная тренировка")).toBeVisible();
  await expect(page.getByText("Чек-ин откроется в 20:30")).toBeVisible();
  await expect(
    page.getByText("Введите номер снова после этого времени"),
  ).toBeVisible();
  await expect(page.getByText("Текущая группа")).not.toBeVisible();
  await expect(page.getByText(/оплат|подтвержден|долг/i)).not.toBeVisible();
  expect(checkinAttempted).toBe(false);
});

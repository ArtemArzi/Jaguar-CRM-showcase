import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

interface TrainerGuestPersonalBookingFixture {
  trainer: {
    email: string;
    password: string;
  };
  location: {
    id: number;
    name: string;
  };
  group_schedule_id: number;
  booking_date: string;
  guest_student: {
    first_name: string;
    last_name: string;
    name: string;
    search: string;
  };
  personal_student: {
    student_id: number;
    name: string;
  };
  personal_training_type: {
    id: number;
    name: string;
  };
  personal_tariff: {
    id: number;
    name: string;
  };
  personal_subscription_id: number;
  personal_booking: {
    date: string;
    start_time: string;
    end_time: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a trainer guest/personal booking fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): TrainerGuestPersonalBookingFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<TrainerGuestPersonalBookingFixture>;

  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  if (!data.location?.id || !data.location.name) {
    throw new Error("Fixture location is required.");
  }
  if (!data.group_schedule_id || !data.booking_date) {
    throw new Error("Fixture group schedule and booking date are required.");
  }
  if (!data.guest_student?.first_name || !data.guest_student.last_name || !data.guest_student.search) {
    throw new Error("Fixture guest student data is required.");
  }
  if (!data.personal_student?.student_id || !data.personal_student.name) {
    throw new Error("Fixture personal student data is required.");
  }
  if (!data.personal_training_type?.id || !data.personal_training_type.name) {
    throw new Error("Fixture personal training type is required.");
  }
  if (!data.personal_tariff?.id || !data.personal_tariff.name) {
    throw new Error("Fixture personal tariff is required.");
  }
  if (!data.personal_subscription_id) {
    throw new Error("Fixture personal subscription id is required.");
  }
  if (!data.personal_booking?.date || !data.personal_booking.start_time || !data.personal_booking.end_time) {
    throw new Error("Fixture personal booking time is required.");
  }

  return data as TrainerGuestPersonalBookingFixture;
}

function isGuestVisitResponse(fixture: TrainerGuestPersonalBookingFixture) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/api/schedules/${fixture.group_schedule_id}/guest-visits/` &&
      response.request().method() === "POST"
    );
  };
}

function isPersonalBookingResponse(fixture: TrainerGuestPersonalBookingFixture) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/api/students/${fixture.personal_student.student_id}/personal-bookings/` &&
      response.request().method() === "POST"
    );
  };
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function loginAsTrainer(page: Page, fixture: TrainerGuestPersonalBookingFixture): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(fixture.trainer.email);
  await page.getByLabel("Пароль").fill(fixture.trainer.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

async function bookGuestVisit(page: Page, fixture: TrainerGuestPersonalBookingFixture): Promise<void> {
  await page.goto(`/trainer/schedule/${fixture.group_schedule_id}/checkin?date=${fixture.booking_date}`);
  await expect(page.getByRole("heading", { name: /Guest Booking Proof/ })).toBeVisible({
    timeout: 20_000,
  });

  const bookedGuest = page.getByRole("button", {
    name: new RegExp(escapeRegExp(fixture.guest_student.first_name)),
  });
  if (await bookedGuest.isVisible()) {
    await expect(bookedGuest).toBeVisible();
    await expect(page.getByText("Гость")).toBeVisible();
    return;
  }

  await page.getByRole("button", { name: "Добавить в список занятия" }).click();
  await expect(page.getByRole("dialog", { name: "Добавить гостя" })).toBeVisible();
  await page.getByPlaceholder("Имя или телефон").fill(fixture.guest_student.search);
  await page.getByRole("button", { name: /Выбрать/ }).click();
  await expect(page.getByText(fixture.guest_student.first_name).first()).toBeVisible();

  const guestResponse = page.waitForResponse(isGuestVisitResponse(fixture), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Добавить гостя" }).click();
  const response = await guestResponse;
  expect(response.ok()).toBe(true);

  await expect(
    page.getByRole("button", { name: new RegExp(escapeRegExp(fixture.guest_student.first_name)) }),
  ).toBeVisible();
  await expect(page.getByText("Гость")).toBeVisible();
}

async function bookPersonalSession(page: Page, fixture: TrainerGuestPersonalBookingFixture): Promise<void> {
  await page.goto(`/trainer/students/${fixture.personal_student.student_id}`);
  await expect(page.getByRole("heading", { name: fixture.personal_student.name })).toBeVisible({
    timeout: 20_000,
  });

  const bookingCard = page.getByLabel(
    `Персоналка ${fixture.personal_booking.date} ${fixture.personal_booking.start_time} - ${fixture.personal_booking.end_time}`,
  );
  if (await bookingCard.isVisible()) {
    await expect(bookingCard.getByText(fixture.personal_training_type.name)).toBeVisible();
    await expect(bookingCard.getByText(fixture.location.name)).toBeVisible();
    return;
  }

  await page
    .getByRole("button", { name: "Записать" })
    .first()
    .click();
  const dialog = page.getByRole("dialog", { name: "Записать персоналку" });
  await expect(dialog).toBeVisible();

  await dialog.getByLabel("Абонемент *").selectOption(String(fixture.personal_subscription_id));
  await dialog.getByLabel("Дата *").fill(fixture.personal_booking.date);
  await dialog.getByLabel("Начало *").fill(fixture.personal_booking.start_time);
  await dialog.getByLabel("Конец *").fill(fixture.personal_booking.end_time);
  await dialog.getByLabel("Тип тренировки *").selectOption(String(fixture.personal_training_type.id));
  await dialog.getByLabel("Зал *").selectOption(String(fixture.location.id));

  const personalResponse = page.waitForResponse(isPersonalBookingResponse(fixture), {
    timeout: 20_000,
  });
  await dialog.getByRole("button", { name: "Записать" }).click();
  const response = await personalResponse;
  expect(response.ok()).toBe(true);

  await expect(dialog).toBeHidden();
  await expect(bookingCard.getByText(fixture.personal_training_type.name)).toBeVisible();
  await expect(bookingCard.getByText(fixture.location.name)).toBeVisible();
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
        ["manage.py", "assert_trainer_guest_personal_booking_e2e", "--fixture", fixturePath],
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

test("real-stack trainer books a guest visit and a personal session without premature money side effects", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsTrainer(page, fixture);
  await bookGuestVisit(page, fixture);
  await bookPersonalSession(page, fixture);
  runBackendAssert(fixturePath);
});

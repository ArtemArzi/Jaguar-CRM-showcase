import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

const KIOSK_CHECKIN_PATH = "/api/checkins/kiosk/";

interface ReflectionFixture {
  kiosk_pin: string;
  phone_suffix: string;
  trainer: {
    email: string;
    password: string;
  };
  student: {
    email: string;
    password: string;
    student_id: number;
    name: string;
  };
  parent: {
    email: string;
    password: string;
  };
  attention_child: {
    student_id: number;
    name: string;
    first_name: string;
    last_name: string;
  };
  schedule_id: number;
  upcoming_schedule_id: number;
  debt_id: number;
  debt_checkin_id: number;
  checkin_date: string;
  upcoming_date: string;
  expected: {
    attendance_count_after: number;
    open_debt_count: number;
    group_name: string;
    upcoming_group_name: string;
    upcoming_start_time: string;
    upcoming_end_time: string;
    upcoming_trainer_name: string;
    upcoming_location_name: string;
    training_type_name: string;
    debt_training_type_name: string;
    debt_amount: string;
    staff_only_note: string;
    private_medical_marker: string;
    foreign_child_name: string;
    tariff_name: string;
    current_grade_name: string;
    next_grade_name: string;
    grade_trainings_to_next_after: number;
    next_grade_min_trainings: number;
    attention_grade_name: string;
    attention_child_first_name: string;
    attention_trainings_left: number;
    attention_trainings_total: number;
    trainings_left_after: number;
    trainings_total: number;
    trainer_roster_name: string;
  };
}

interface BackendAssertResponse {
  ok?: boolean;
}

interface KioskCheckinResponse {
  checkin_id?: number;
  is_debt?: boolean;
  subscription_id?: number | null;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a student/parent reflection fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): ReflectionFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<ReflectionFixture>;

  if (!data.kiosk_pin || !/^\d{6}$/.test(data.kiosk_pin)) {
    throw new Error("Fixture kiosk_pin must be a 6-digit string.");
  }
  if (!data.phone_suffix || !/^\d{4}$/.test(data.phone_suffix)) {
    throw new Error("Fixture phone_suffix must be a 4-digit string.");
  }
  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  if (!data.student?.email || !data.student.password || !data.student.student_id) {
    throw new Error("Fixture student credentials and id are required.");
  }
  if (!data.parent?.email || !data.parent.password) {
    throw new Error("Fixture parent credentials are required.");
  }
  if (!data.attention_child?.student_id || !data.attention_child.name) {
    throw new Error("Fixture attention child data is required.");
  }
  if (
    !data.schedule_id ||
    !data.upcoming_schedule_id ||
    !data.debt_id ||
    !data.debt_checkin_id ||
    !data.checkin_date ||
    !data.upcoming_date ||
    !data.expected?.group_name ||
    !data.expected.upcoming_group_name
  ) {
    throw new Error("Fixture schedule/check-in data is required.");
  }

  return data as ReflectionFixture;
}

function isKioskCheckinResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === KIOSK_CHECKIN_PATH && response.request().method() === "POST";
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function login(page: Page, email: string, password: string): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Пароль").fill(password);
  await page.getByRole("button", { name: "Войти" }).click();
}

async function completeTrainerCheckin(page: Page, fixture: ReflectionFixture): Promise<void> {
  await login(page, fixture.trainer.email, fixture.trainer.password);
  await expect(page).toHaveURL(/\/trainer\/?$/);

  await page.goto(`/trainer/schedule/${fixture.schedule_id}/checkin?date=${fixture.checkin_date}`);
  await expect(page.getByRole("heading", { name: /Reflection Proof/ })).toBeVisible();
  await expect(
    page.getByRole("button", { name: new RegExp(escapeRegExp(fixture.expected.trainer_roster_name)) }),
  ).toBeVisible();
  await expect(page.getByText("Отметку делает ученик")).toBeVisible();
  await expect(page.getByRole("button", { name: "Отметить 1 из 1" })).toHaveCount(0);
}

async function enterPin(page: Page, pin: string): Promise<void> {
  await page.goto("/kiosk/");
  for (const [index, digit] of [...pin].entries()) {
    await page.getByLabel(`PIN цифра ${index + 1}`).fill(digit);
  }
  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
}

async function completeCheckinViaKiosk(page: Page, fixture: ReflectionFixture): Promise<void> {
  await enterPin(page, fixture.kiosk_pin);
  const checkinResponse = page.waitForResponse(isKioskCheckinResponse, {
    timeout: 20_000,
  });

  for (const digit of fixture.phone_suffix) {
    await page.getByRole("button", { name: `Цифра ${digit}` }).click();
  }

  const nextStep = await Promise.race([
    checkinResponse.then((response) => ({
      type: "checkin" as const,
      response,
    })),
    page
      .getByText("Выберите тренировку")
      .waitFor({ state: "visible", timeout: 5_000 })
      .then(() => ({ type: "manual-selection" as const }))
      .catch(() => ({ type: "wait-for-checkin" as const })),
  ]);

  if (nextStep.type === "manual-selection") {
    await page
      .getByRole("button", { name: new RegExp(escapeRegExp(fixture.expected.group_name)) })
      .click();
  }

  const response = nextStep.type === "checkin" ? nextStep.response : await checkinResponse;
  expect(response.ok()).toBe(true);

  const result = (await response.json()) as KioskCheckinResponse;
  expect(result.checkin_id).toBeGreaterThan(0);
  expect(result.is_debt).toBe(false);
  expect(result.subscription_id).toBe(fixture.subscription_id);
  await expect(page.getByText("Посещение сохранено")).toBeVisible();
}

async function assertStudentSurface(page: Page, fixture: ReflectionFixture): Promise<void> {
  await login(page, fixture.student.email, fixture.student.password);
  await expect(page).toHaveURL(/\/student\/?$/);
  await expect(page.getByRole("heading", { name: "Главная" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(fixture.expected.current_grade_name).first()).toBeVisible();
  await expect(
    page.getByText(`До ${fixture.expected.next_grade_name} осталось ${fixture.expected.grade_trainings_to_next_after} тренировок.`),
  ).toBeVisible();
  await expect(
    page.getByText(
      `1 из ${fixture.expected.next_grade_min_trainings} тренировок уже закрыты по пути к следующему уровню.`,
    ),
  ).toBeVisible();
  await expect(page.getByText("Есть задолженность")).toBeVisible();
  await expect(page.getByText(fixture.expected.debt_training_type_name).first()).toBeVisible();
  await expect(page.getByText("Нет подходящего абонемента")).toBeVisible();
  await expect(page.getByText(`${fixture.expected.debt_amount} ₽`).first()).toBeVisible();
  await expect(page.getByText(fixture.expected.tariff_name).first()).toBeVisible();
  await expect(page.getByText("4 тренировки из пакета ещё доступны").first()).toBeVisible();
  await expect(page.getByText(fixture.expected.upcoming_group_name).first()).toBeVisible();
  await expect(page.getByText(/18:00[–-]19:00/).first()).toBeVisible();
  await expect(page.getByText(fixture.expected.upcoming_trainer_name).first()).toBeVisible();
  await expect(page.getByText(fixture.expected.upcoming_location_name).first()).toBeVisible();

  await page.goto("/student/attendance");
  await expect(page.getByRole("heading", { name: "Посещения" })).toBeVisible();
  await expect(page.getByText(`${fixture.expected.attendance_count_after} всего`)).toBeVisible();
  await expect(page.getByText(fixture.expected.group_name).first()).toBeVisible();

  await page.goto("/student/schedule");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible();
  const upcomingSchedule = page.getByText(fixture.expected.upcoming_group_name).first();
  if (!(await upcomingSchedule.isVisible({ timeout: 1_000 }).catch(() => false))) {
    await page.getByRole("button", { name: "Следующая неделя" }).click();
  }
  await expect(upcomingSchedule).toBeVisible();
  await expect(page.getByText(fixture.expected.staff_only_note)).not.toBeVisible();
  await expect(page.getByText(fixture.expected.private_medical_marker)).not.toBeVisible();
}

async function assertParentSurface(page: Page, fixture: ReflectionFixture): Promise<void> {
  await login(page, fixture.parent.email, fixture.parent.password);
  await expect(page).toHaveURL(/\/parent\/?$/);
  await expect(page.getByRole("heading", { name: "Мои дети" })).toBeVisible({ timeout: 20_000 });
  const primaryCard = page.getByRole("link", {
    name: new RegExp(escapeRegExp(fixture.student.name)),
  });
  const attentionCard = page.getByRole("link", {
    name: new RegExp(escapeRegExp(fixture.attention_child.name)),
  });
  await expect(primaryCard).toContainText(fixture.expected.current_grade_name);
  await expect(primaryCard).toContainText(
    `${fixture.expected.trainings_left_after} из ${fixture.expected.trainings_total} тренировок`,
  );
  await expect(primaryCard).toContainText(fixture.expected.upcoming_group_name);
  await expect(primaryCard).toContainText(fixture.expected.upcoming_trainer_name);
  await expect(attentionCard).toContainText(fixture.expected.attention_grade_name);
  await expect(attentionCard).toContainText("Последняя тренировка");
  await expect(attentionCard).toContainText(
    `Осталось ${fixture.expected.attention_trainings_left} занятие`,
  );
  await expect(attentionCard).toContainText(fixture.expected.upcoming_group_name);
  await expect(attentionCard).toContainText(fixture.expected.upcoming_trainer_name);
  await expect(page.getByRole("region", { name: "Требует внимания" })).toContainText(
    "Последняя тренировка",
  );
  await expect(page.getByRole("region", { name: "Требует внимания" })).toContainText(
    fixture.expected.attention_child_first_name,
  );
  await expect(page.getByText(fixture.expected.foreign_child_name)).not.toBeVisible();

  await page.goto(`/parent/child/${fixture.student.student_id}`);
  await expect(page.getByRole("heading", { name: fixture.student.name })).toBeVisible();
  const subscriptionRegion = page.getByRole("region", { name: "Абонемент ребёнка" });
  await expect(subscriptionRegion).toContainText("Есть задолженность");
  await expect(subscriptionRegion).toContainText(fixture.expected.debt_training_type_name);
  await expect(subscriptionRegion).toContainText("Причина: нет подходящего абонемента");
  await expect(subscriptionRegion).toContainText(`${fixture.expected.debt_amount} ₽`);
  await expect(subscriptionRegion).toContainText(`${fixture.expected.trainings_left_after}`);
  await expect(page.getByRole("region", { name: "Активность" })).toContainText(
    `${fixture.expected.attendance_count_after} посещения`,
  );
  await expect(page.getByRole("region", { name: "Занятия ребёнка" })).toContainText(
    fixture.expected.upcoming_group_name,
  );
  await expect(page.getByText(fixture.expected.staff_only_note)).not.toBeVisible();
  await expect(page.getByText(fixture.expected.private_medical_marker)).not.toBeVisible();
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
        ["manage.py", "assert_student_parent_reflection_e2e", "--fixture", fixturePath],
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

test("real-stack check-in is reflected on student and parent safe surfaces", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await completeTrainerCheckin(page, fixture);
  await completeCheckinViaKiosk(page, fixture);
  runBackendAssert(fixturePath);
  await assertStudentSurface(page, fixture);
  await assertParentSurface(page, fixture);
});

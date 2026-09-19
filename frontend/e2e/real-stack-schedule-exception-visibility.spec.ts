import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Locator, type Page, type Response } from "@playwright/test";

interface ScheduleExceptionFixture {
  trainer: {
    email: string;
    password: string;
  };
  substitute_trainer: {
    email: string;
    password: string;
    name: string;
  };
  student: {
    email: string;
    password: string;
    student_id: number;
  };
  parent: {
    email: string;
    password: string;
  };
  cancel_date: string;
  reschedule_old_date: string;
  reschedule_new_date: string;
  substitute_date: string;
  training_group_substitute: {
    training_group_id: number;
    membership_id: number;
    payment_id: number;
    schedule_id: number;
    replacement_responsible_trainer_id: number;
    rollout_mode: string;
    new_writes_enabled: boolean;
    manual_operational_admission_enabled: boolean;
  };
  expected: {
    cancel_group_name: string;
    reschedule_group_name: string;
    substitute_group_name: string;
    cancel_reason: string;
    reschedule_reason: string;
    substitute_reason: string;
    reschedule_new_start_time: string;
    reschedule_new_end_time: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a schedule exception fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): ScheduleExceptionFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<ScheduleExceptionFixture>;

  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  if (!data.substitute_trainer?.email || !data.substitute_trainer.password) {
    throw new Error("Fixture substitute trainer credentials are required.");
  }
  if (!data.student?.email || !data.student.password || !data.student.student_id) {
    throw new Error("Fixture student credentials and id are required.");
  }
  if (!data.parent?.email || !data.parent.password) {
    throw new Error("Fixture parent credentials are required.");
  }
  if (!data.cancel_date || !data.reschedule_old_date || !data.reschedule_new_date || !data.substitute_date) {
    throw new Error("Fixture schedule exception dates are required.");
  }
  if (!data.expected?.cancel_group_name || !data.expected.reschedule_group_name) {
    throw new Error("Fixture expected schedule labels are required.");
  }
  if (
    !data.training_group_substitute?.training_group_id ||
    !data.training_group_substitute.membership_id ||
    !data.training_group_substitute.payment_id ||
    !data.training_group_substitute.new_writes_enabled ||
    !data.training_group_substitute.manual_operational_admission_enabled
  ) {
    throw new Error("Fixture canonical substitute ownership evidence is required.");
  }

  return data as ScheduleExceptionFixture;
}

async function login(page: Page, email: string, password: string): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Пароль").fill(password);
  await page.getByRole("button", { name: "Войти" }).click();
}

function dayNumber(dateIso: string): string {
  return String(new Date(`${dateIso}T12:00:00`).getDate());
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function weekSunday(dateIso: string): Date {
  const date = new Date(`${dateIso}T12:00:00`);
  const day = date.getDay();
  const daysUntilSunday = day === 0 ? 0 : 7 - day;
  date.setDate(date.getDate() + daysUntilSunday);
  return date;
}

async function setDefaultBrowserDate(page: Page, date: Date): Promise<void> {
  await page.addInitScript((fixedTime) => {
    const RealDate = Date;
    window.Date = new Proxy(RealDate, {
      apply(target, thisArg, args) {
        if (args.length === 0) {
          return new target(fixedTime).toString();
        }
        return Reflect.apply(target, thisArg, args);
      },
      construct(target, args) {
        if (args.length === 0) {
          return new target(fixedTime);
        }
        return Reflect.construct(target, args);
      },
    });
  }, date.getTime());
}

async function selectTrainerDay(page: Page, dateIso: string): Promise<void> {
  await page
    .getByRole("button")
    .filter({ hasText: new RegExp(`^\\D*${dayNumber(dateIso)}$`) })
    .first()
    .click();
}

function trainingCard(page: Page, groupName: string): Locator {
  return page
    .getByRole("group", { name: new RegExp(escapeRegExp(groupName)) })
    .first();
}

function isScheduleMutation(response: Response): boolean {
  const url = new URL(response.url());
  return (
    response.request().method() === "POST" &&
    (url.pathname.endsWith("/cancel/") || url.pathname.endsWith("/reschedule/"))
  );
}

async function cancelTrainerSession(page: Page, fixture: ScheduleExceptionFixture): Promise<void> {
  await selectTrainerDay(page, fixture.cancel_date);
  const card = trainingCard(page, fixture.expected.cancel_group_name);
  await expect(card).toBeVisible();
  await card.getByLabel("Действия").click();
  await card.getByRole("menuitem", { name: "Отменить" }).click();
  await expect(page.getByRole("heading", { name: "Отменить тренировку" })).toBeVisible();
  await page.getByPlaceholder("Укажите причину отмены...").fill(fixture.expected.cancel_reason);

  const cancelResponse = page.waitForResponse(isScheduleMutation, { timeout: 20_000 });
  await page.getByRole("button", { name: "Отменить тренировку" }).click();
  expect((await cancelResponse).ok()).toBe(true);
  await expect(page.getByText(fixture.expected.cancel_group_name)).not.toBeVisible({ timeout: 20_000 });
}

async function rescheduleTrainerSession(page: Page, fixture: ScheduleExceptionFixture): Promise<void> {
  await selectTrainerDay(page, fixture.reschedule_old_date);
  const card = trainingCard(page, fixture.expected.reschedule_group_name);
  await expect(card).toBeVisible();
  await card.getByLabel("Действия").click();
  await card.getByRole("menuitem", { name: "Перенести" }).click();
  const dialog = page.getByRole("dialog", { name: "Перенести тренировку" });
  await expect(dialog).toBeVisible();
  const fields = dialog.getByRole("textbox");
  await fields.nth(0).fill(fixture.reschedule_new_date);
  await fields.nth(1).fill(fixture.expected.reschedule_new_start_time);
  await fields.nth(2).fill(fixture.expected.reschedule_new_end_time);
  await page.getByPlaceholder("Причина переноса...").fill(fixture.expected.reschedule_reason);

  const rescheduleResponse = page.waitForResponse(isScheduleMutation, { timeout: 20_000 });
  await page.getByRole("button", { name: "Перенести" }).click();
  expect((await rescheduleResponse).ok()).toBe(true);
  await expect(page.getByText(fixture.expected.reschedule_group_name)).not.toBeVisible({ timeout: 20_000 });

  await selectTrainerDay(page, fixture.reschedule_new_date);
  const movedCard = trainingCard(page, fixture.expected.reschedule_group_name);
  await expect(movedCard).toBeVisible({ timeout: 20_000 });
  await expect(movedCard.getByText("перенос")).toBeVisible();
}

async function assertSubstituteTrainerSurface(page: Page, fixture: ScheduleExceptionFixture): Promise<void> {
  await login(page, fixture.substitute_trainer.email, fixture.substitute_trainer.password);
  await expect(page).toHaveURL(/\/trainer\/?$/);
  await selectTrainerDay(page, fixture.substitute_date);
  const card = trainingCard(page, fixture.expected.substitute_group_name);
  await expect(card).toBeVisible({ timeout: 20_000 });
  await expect(card.getByText("замена")).toBeVisible();
  await expect(card.getByText(fixture.substitute_trainer.name)).toBeVisible();
  await expect(card.getByLabel("Действия")).not.toBeVisible();
}

async function assertStudentSurface(page: Page, fixture: ScheduleExceptionFixture): Promise<void> {
  await login(page, fixture.student.email, fixture.student.password);
  await expect(page).toHaveURL(/\/student\/?$/);
  await page.goto("/student/schedule");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(fixture.expected.cancel_group_name)).not.toBeVisible();
  await expect(page.getByText(fixture.expected.reschedule_group_name).first()).toBeVisible();
  await expect(page.getByText("Перенос").first()).toBeVisible();
  await expect(page.getByText(fixture.expected.substitute_group_name).first()).toBeVisible();
  await expect(page.getByText("Замена тренера").first()).toBeVisible();
  await expect(page.getByText(fixture.substitute_trainer.name).first()).toBeVisible();
  await expect(page.getByText(fixture.expected.cancel_reason)).not.toBeVisible();
  await expect(page.getByText(fixture.expected.reschedule_reason)).not.toBeVisible();
  await expect(page.getByText(fixture.expected.substitute_reason)).not.toBeVisible();
  await expect(page.getByLabel("Действия")).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Отменить тренировку" })).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Перенести" })).not.toBeVisible();
}

async function assertParentSurface(page: Page, fixture: ScheduleExceptionFixture): Promise<void> {
  await login(page, fixture.parent.email, fixture.parent.password);
  await expect(page).toHaveURL(/\/parent\/?$/);
  await page.goto(`/parent/child/${fixture.student.student_id}`);
  await expect(page.getByRole("heading", { name: "Обзор ребёнка" })).toBeVisible({ timeout: 20_000 });
  const scheduleRegion = page.getByRole("region", { name: "Занятия ребёнка" });
  await expect(scheduleRegion).toContainText(
    fixture.expected.cancel_group_name,
  );
  await expect(scheduleRegion).toContainText("Отменено");
  await expect(scheduleRegion).toContainText(
    fixture.expected.cancel_reason,
  );
  await expect(scheduleRegion).toContainText(
    fixture.expected.reschedule_group_name,
  );
  await expect(scheduleRegion).toContainText("Перенос");
  await expect(scheduleRegion).toContainText(
    fixture.expected.substitute_group_name,
  );
  await expect(scheduleRegion).toContainText("Замена тренера");
  await expect(scheduleRegion).toContainText(
    fixture.substitute_trainer.name,
  );
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
        ["manage.py", "assert_schedule_exception_visibility_e2e", "--fixture", fixturePath],
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

test("real-stack schedule exceptions are visible across trainer, student, and parent surfaces", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await setDefaultBrowserDate(page, weekSunday(fixture.cancel_date));
  await login(page, fixture.trainer.email, fixture.trainer.password);
  await expect(page).toHaveURL(/\/trainer\/?$/);
  await cancelTrainerSession(page, fixture);
  await rescheduleTrainerSession(page, fixture);

  await assertSubstituteTrainerSurface(page, fixture);
  await assertStudentSurface(page, fixture);
  await assertParentSurface(page, fixture);
  runBackendAssert(fixturePath);
});

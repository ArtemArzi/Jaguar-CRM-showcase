import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

interface TrainerBatchFixture {
  trainer: {
    email: string;
    password: string;
  };
  schedule_id: number;
  checkin_date: string;
  active_student_ids: number[];
  frozen_student_id: number;
  unclosed: {
    date: string;
    schedule_id: number;
    group_name: string;
  };
  schedule_form: {
    date: string;
    created_group_name: string;
    created_start_time: string;
    created_end_time: string;
    created_training_type_name: string;
    created_location_name: string;
    edited_group_name: string;
    edited_start_time: string;
    edited_end_time: string;
    edited_training_type_name: string;
    edited_location_name: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a trainer batch fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): TrainerBatchFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<TrainerBatchFixture>;

  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  if (!data.schedule_id || !data.checkin_date) {
    throw new Error("Fixture schedule_id and checkin_date are required.");
  }
  if (!data.active_student_ids?.length || !data.frozen_student_id) {
    throw new Error("Fixture active/frozen student ids are required.");
  }
  if (!data.unclosed?.date || !data.unclosed.schedule_id || !data.unclosed.group_name) {
    throw new Error("Fixture unclosed session expectations are required.");
  }
  if (
    !data.schedule_form?.date ||
    !data.schedule_form.created_group_name ||
    !data.schedule_form.edited_group_name
  ) {
    throw new Error("Fixture schedule form expectations are required.");
  }

  return {
    trainer: data.trainer,
    schedule_id: data.schedule_id,
    checkin_date: data.checkin_date,
    active_student_ids: data.active_student_ids,
    frozen_student_id: data.frozen_student_id,
    unclosed: data.unclosed,
    schedule_form: data.schedule_form,
  };
}

function isScheduleCreateResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === "/api/schedules/" && response.request().method() === "POST";
}

function isScheduleUpdateResponse(scheduleId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `/api/schedules/${scheduleId}/` && response.request().method() === "PUT";
  };
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function isSessionCloseResponse(response: Response): boolean {
  const url = new URL(response.url());
  return /\/api\/schedules\/\d+\/sessions\/close\/$/.test(url.pathname) && response.request().method() === "POST";
}

function trainerBatchScheduleCard(page: Page) {
  return page.getByRole("group", { name: /Тренировка .*Trainer Batch Proof/ }).first();
}

function trainingCard(page: Page, groupName: string) {
  return page.getByRole("group", { name: new RegExp(`Тренировка .*${escapeRegExp(groupName)}`) }).first();
}

function dayNumber(dateIso: string): string {
  return String(new Date(`${dateIso}T12:00:00`).getDate());
}

async function selectTrainerDay(page: Page, dateIso: string): Promise<void> {
  await page
    .getByRole("button")
    .filter({ hasText: new RegExp(`^\\D*${dayNumber(dateIso)}$`) })
    .first()
    .click();
}

async function loginAsTrainer(page: Page, fixture: TrainerBatchFixture): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill(fixture.trainer.email);
  await page.getByLabel("Пароль").fill(fixture.trainer.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
  await expect(page.getByText("Привет, Batch")).toBeVisible({ timeout: 20_000 });
}

async function assertTrainerHomeShowsOwnSchedule(page: Page): Promise<void> {
  await expect(page.getByRole("heading", { name: "Мои тренировки" })).toBeVisible();

  const scheduleCard = trainerBatchScheduleCard(page);
  await expect(scheduleCard).toBeVisible();
  await expect(scheduleCard).toContainText("Batch Hall");
  await expect(scheduleCard).toContainText("Batch Trainer");
  await expect(scheduleCard.getByRole("button", { name: "Статус" })).toBeVisible();
}

async function closeUnclosedYesterdaySession(page: Page, fixture: TrainerBatchFixture): Promise<void> {
  await expect(page.getByText("Есть незакрытые тренировки (1)")).toBeVisible({ timeout: 20_000 });
  const unclosedButton = page.getByRole("button", {
    name: new RegExp(`${escapeRegExp(fixture.unclosed.group_name)}.*проверить`),
  });
  await expect(unclosedButton).toBeVisible();

  await unclosedButton.click();
  await expect(page).toHaveURL(
    new RegExp(`/trainer/schedule/${fixture.unclosed.schedule_id}/checkin\\?date=${fixture.unclosed.date}`),
  );
  await expect(page.getByRole("heading", { name: new RegExp(fixture.unclosed.group_name) })).toBeVisible();
  await expect(page.getByText("Student Batch4")).toBeVisible();
  await expect(page.getByRole("button", { name: /Открыть контекст ученика .*Student Batch4/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Закрыть тренировку" })).toBeEnabled();

  const closeResponse = page.waitForResponse(isSessionCloseResponse, {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Закрыть тренировку" }).click();
  expect((await closeResponse).ok()).toBe(true);
  await expect(page.getByText("Итоги сохранены")).toBeVisible();

  await page.goto("/trainer");
  await expect(page.getByRole("heading", { name: "Мои тренировки" })).toBeVisible();
  await expect(page.getByText("Есть незакрытые тренировки")).toHaveCount(0);
  await expect(page.getByText(fixture.unclosed.group_name)).toHaveCount(0);
}

async function openBatchCheckinFromTrainerHome(page: Page): Promise<void> {
  await trainerBatchScheduleCard(page).getByRole("button", { name: "Статус" }).click();
  await expect(page).toHaveURL(/\/trainer\/schedule\/\d+\/checkin\?date=/);
  await expect(page.getByRole("heading", { name: /Trainer Batch Proof/ })).toBeVisible();
}

async function submitActiveStudentsOnly(page: Page): Promise<Response> {
  await expect(page.getByText("Student Batch1")).toBeVisible();
  await expect(page.getByText("Student Batch2")).toBeVisible();
  await expect(page.getByRole("button", { name: /Student Batch3.*Недоступен/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Закрыть тренировку" })).toBeEnabled();

  const closeResponse = page.waitForResponse(isSessionCloseResponse, {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Закрыть тренировку" }).click();
  return await closeResponse;
}

async function assertVisibleBatchSuccess(page: Page, batchResponse: Response): Promise<void> {
  expect(batchResponse.ok()).toBe(true);
  const result = await batchResponse.json();
  expect(result.attendee_count).toBe(2);
  await expect(page.getByText("Итоги сохранены")).toBeVisible();
  await expect(page.getByText("Student Batch1")).toBeVisible();
  await expect(page.getByText("Student Batch2")).toBeVisible();
}

async function createAndEditScheduleFromTrainerHome(
  page: Page,
  fixture: TrainerBatchFixture,
): Promise<void> {
  const form = fixture.schedule_form;

  await page.goto("/trainer");
  await expect(page.getByRole("heading", { name: "Мои тренировки" })).toBeVisible();

  await page.getByLabel("Создать тренировку").click();
  const createDialog = page.getByRole("dialog", { name: "Новая тренировка" });
  await expect(createDialog).toBeVisible();
  await createDialog.getByLabel(/Дата/).fill(form.date);
  await createDialog.getByLabel(/Начало/).fill(form.created_start_time);
  await createDialog.getByLabel(/Конец/).fill(form.created_end_time);
  await createDialog.getByLabel(/Группа/).fill(form.created_group_name);
  await createDialog.locator("#schedule-training-type").selectOption({ label: form.created_training_type_name });
  await createDialog.locator("#schedule-location").selectOption({ label: form.created_location_name });

  const createResponsePromise = page.waitForResponse(isScheduleCreateResponse, { timeout: 20_000 });
  await createDialog.getByRole("button", { name: "Создать тренировку" }).click();
  const createResponse = await createResponsePromise;
  expect(createResponse.status()).toBe(201);
  const createdSchedule = (await createResponse.json()) as { id: number };
  expect(createdSchedule.id).toBeGreaterThan(0);

  await selectTrainerDay(page, form.date);
  const createdCard = trainingCard(page, form.created_group_name);
  await expect(createdCard).toBeVisible({ timeout: 20_000 });
  await expect(createdCard).toContainText(form.created_location_name);

  await createdCard.getByLabel("Действия").click();
  await createdCard.getByRole("menuitem", { name: "Редактировать" }).click();
  const editDialog = page.getByRole("dialog", { name: "Редактировать тренировку" });
  await expect(editDialog).toBeVisible();
  await editDialog.getByLabel(/Начало/).fill(form.edited_start_time);
  await editDialog.getByLabel(/Конец/).fill(form.edited_end_time);
  await editDialog.getByLabel(/Группа/).fill(form.edited_group_name);
  await editDialog.locator("#schedule-training-type").selectOption({ label: form.edited_training_type_name });
  await editDialog.locator("#schedule-location").selectOption({ label: form.edited_location_name });

  const updateResponsePromise = page.waitForResponse(isScheduleUpdateResponse(createdSchedule.id), {
    timeout: 20_000,
  });
  await editDialog.getByRole("button", { name: "Сохранить" }).click();
  expect((await updateResponsePromise).ok()).toBe(true);

  const editedCard = trainingCard(page, form.edited_group_name);
  await expect(editedCard).toBeVisible({ timeout: 20_000 });
  await expect(editedCard).toContainText(form.edited_location_name);
  await expect(editedCard).toContainText(form.edited_start_time);
  await expect(trainingCard(page, form.created_group_name)).not.toBeVisible();
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
        ["manage.py", "assert_trainer_batch_e2e", "--fixture", fixturePath],
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

test("real-stack trainer batch check-in confirms active students and blocks frozen student", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsTrainer(page, fixture);
  await assertTrainerHomeShowsOwnSchedule(page);
  await closeUnclosedYesterdaySession(page, fixture);
  await openBatchCheckinFromTrainerHome(page);
  const batchResponse = await submitActiveStudentsOnly(page);
  await assertVisibleBatchSuccess(page, batchResponse);
  await createAndEditScheduleFromTrainerHome(page, fixture);
  runBackendAssert(fixturePath);
});

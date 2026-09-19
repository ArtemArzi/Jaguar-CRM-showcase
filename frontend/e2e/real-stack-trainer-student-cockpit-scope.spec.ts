import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

interface Credentials {
  email: string;
  password: string;
}

interface FixtureStudent {
  id: number;
  full_name: string;
  phone: string;
}

interface TrainerStudentCockpitScopeFixture {
  trainer: Credentials;
  assigned_student: FixtureStudent;
  unassigned_student: FixtureStudent;
  package_owned_student: FixtureStudent;
  other_trainer_student: FixtureStudent;
  foreign_student: FixtureStudent;
  expected: {
    assigned_feedback_question: string;
    assigned_feedback_rating_text: string;
    assigned_note_text: string;
    assigned_tariff_name: string;
    assigned_trainings_left_text: string;
    assigned_access_eligibility_text: string;
    package_owned_tariff_name: string;
    package_owned_trainings_left_text: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a trainer student cockpit scope fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): TrainerStudentCockpitScopeFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<TrainerStudentCockpitScopeFixture>;
  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  for (const key of [
    "assigned_student",
    "unassigned_student",
    "package_owned_student",
    "other_trainer_student",
    "foreign_student",
  ] as const) {
    if (!data[key]?.id || !data[key]?.full_name || !data[key]?.phone) {
      throw new Error(`Fixture ${key} data is required.`);
    }
  }
  if (!data.expected?.assigned_feedback_question || !data.expected.assigned_feedback_rating_text) {
    throw new Error("Fixture expected assigned feedback data is required.");
  }
  if (
    !data.expected.assigned_note_text ||
    !data.expected.assigned_tariff_name ||
    !data.expected.assigned_trainings_left_text ||
    !data.expected.assigned_access_eligibility_text ||
    !data.expected.package_owned_tariff_name ||
    !data.expected.package_owned_trainings_left_text
  ) {
    throw new Error("Fixture expected assigned student detail data is required.");
  }
  return data as TrainerStudentCockpitScopeFixture;
}

function isStudentListResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === "/api/students/" && response.request().method() === "GET";
}

function isStudentDetailResponse(studentId: number, status?: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    const matches = url.pathname === `/api/students/${studentId}/` && response.request().method() === "GET";
    return matches && (status === undefined || response.status() === status);
  };
}

function isFeedbackResponsesResponse(studentId: number, status?: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    const matches = url.pathname === `/api/feedback/students/${studentId}/responses/` &&
      response.request().method() === "GET";
    return matches && (status === undefined || response.status() === status);
  };
}

function isSendSurveyResponse(studentId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `/api/feedback/send-survey/${studentId}/` &&
      response.request().method() === "POST";
  };
}

async function loginAsTrainer(page: Page, credentials: Credentials): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(credentials.email);
  await page.getByLabel("Пароль").fill(credentials.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

async function expectDeniedStudentDetail(
  page: Page,
  student: FixtureStudent,
  allowedStatuses: readonly number[] = [403],
): Promise<void> {
  const deniedResponse = page.waitForResponse(isStudentDetailResponse(student.id), { timeout: 20_000 });
  await page.goto(`/trainer/students/${student.id}`);
  expect(allowedStatuses).toContain((await deniedResponse).status());
  await expect(page.getByText("Ученик не найден")).toBeVisible();
  await expect(page.getByRole("button", { name: "Назад к списку" })).toBeVisible();
  await expect(page.getByRole("heading", { name: student.full_name })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Отправить опрос" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Абонемент" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Заметки" })).toHaveCount(0);
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
        ["manage.py", "assert_trainer_student_cockpit_scope_e2e", "--fixture", fixturePath],
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

test("real-stack trainer student cockpit scope denies unassigned students", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsTrainer(page, fixture.trainer);

  const listResponse = page.waitForResponse(isStudentListResponse, { timeout: 20_000 });
  await page.goto("/trainer/students");
  expect((await listResponse).ok()).toBe(true);
  await expect(page.getByRole("heading", { name: "Ученики" })).toBeVisible();
  await expect(page.getByText(fixture.assigned_student.full_name)).toBeVisible();
  await expect(page.getByText(fixture.package_owned_student.full_name)).toBeVisible();
  await expect(page.getByText(fixture.unassigned_student.full_name)).not.toBeVisible();
  await expect(page.getByText(fixture.other_trainer_student.full_name)).not.toBeVisible();
  await expect(page.getByText(fixture.foreign_student.full_name)).not.toBeVisible();

  const assignedResponse = page.waitForResponse(isStudentDetailResponse(fixture.assigned_student.id, 200), {
    timeout: 20_000,
  });
  const assignedFeedbackResponse = page.waitForResponse(
    isFeedbackResponsesResponse(fixture.assigned_student.id, 200),
    { timeout: 20_000 },
  );
  await page.goto(`/trainer/students/${fixture.assigned_student.id}`);
  expect((await assignedResponse).ok()).toBe(true);
  expect((await assignedFeedbackResponse).ok()).toBe(true);
  await expect(page.getByRole("heading", { name: fixture.assigned_student.full_name })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Абонемент" })).toBeVisible();
  await expect(page.getByText(fixture.expected.assigned_tariff_name)).toBeVisible();
  await expect(page.getByText(fixture.expected.assigned_trainings_left_text)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Доступ" })).toBeVisible();
  await expect(page.getByText("Личный кабинет")).toBeVisible();
  await expect(page.getByText(fixture.expected.assigned_access_eligibility_text)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Опросы" })).toBeVisible();
  await expect(page.getByText(fixture.expected.assigned_feedback_question)).toBeVisible();
  await expect(page.getByText(fixture.expected.assigned_feedback_rating_text)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Заметки" })).toBeVisible();
  await expect(page.getByText(fixture.expected.assigned_note_text)).toBeVisible();

  const sendSurveyResponse = page.waitForResponse(isSendSurveyResponse(fixture.assigned_student.id), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Отправить опрос" }).click();
  expect((await sendSurveyResponse).status()).toBe(200);
  await expect(page.getByText("Опрос отправлен")).toBeVisible();

  const packageOwnedResponse = page.waitForResponse(
    isStudentDetailResponse(fixture.package_owned_student.id, 200),
    { timeout: 20_000 },
  );
  await page.goto(`/trainer/students/${fixture.package_owned_student.id}`);
  expect((await packageOwnedResponse).ok()).toBe(true);
  await expect(page.getByRole("heading", { name: fixture.package_owned_student.full_name })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Абонемент" })).toBeVisible();
  await expect(page.getByText(fixture.expected.package_owned_tariff_name)).toBeVisible();
  await expect(page.getByText(fixture.expected.package_owned_trainings_left_text)).toBeVisible();
  await expect(page.getByText("Личный кабинет")).toBeVisible();
  await expect(page.getByText(fixture.expected.assigned_access_eligibility_text)).toBeVisible();

  const packageSendSurveyResponse = page.waitForResponse(
    isSendSurveyResponse(fixture.package_owned_student.id),
    { timeout: 20_000 },
  );
  await page.getByRole("button", { name: "Отправить опрос" }).click();
  expect((await packageSendSurveyResponse).status()).toBe(200);
  await expect(page.getByText("Опрос отправлен")).toBeVisible();

  await expectDeniedStudentDetail(page, fixture.unassigned_student);
  await expectDeniedStudentDetail(page, fixture.other_trainer_student);
  await expectDeniedStudentDetail(page, fixture.foreign_student, [403, 404]);

  runBackendAssert(fixturePath);
});

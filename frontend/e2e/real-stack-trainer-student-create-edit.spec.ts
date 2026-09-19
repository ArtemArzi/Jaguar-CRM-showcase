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

interface FormStudent {
  first_name: string;
  last_name: string;
  phone: string;
  email?: string;
}

interface TrainerStudentCreateEditFixture {
  trainer: Credentials;
  assigned_student: FixtureStudent;
  conflict_student: FixtureStudent;
  new_student: FormStudent;
  existing_student_intake: FormStudent;
  duplicate_attempt: FormStudent;
  edit: {
    first_name: string;
    last_name: string;
    phone: string;
    contraindications: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a trainer student create/edit fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): TrainerStudentCreateEditFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<TrainerStudentCreateEditFixture>;
  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  for (const key of ["assigned_student", "conflict_student"] as const) {
    if (!data[key]?.id || !data[key]?.full_name || !data[key]?.phone) {
      throw new Error(`Fixture ${key} data is required.`);
    }
  }
  if (!data.new_student?.first_name || !data.new_student.phone) {
    throw new Error("Fixture new_student data is required.");
  }
  if (!data.existing_student_intake?.first_name || !data.existing_student_intake.phone) {
    throw new Error("Fixture existing_student_intake data is required.");
  }
  if (!data.duplicate_attempt?.first_name || !data.duplicate_attempt.phone) {
    throw new Error("Fixture duplicate_attempt data is required.");
  }
  if (!data.edit?.first_name || !data.edit.last_name || !data.edit.phone || !data.edit.contraindications) {
    throw new Error("Fixture edit data is required.");
  }
  return data as TrainerStudentCreateEditFixture;
}

function isIntakeResponse(status?: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    const matches = url.pathname === "/api/students/intakes/" && response.request().method() === "POST";
    return matches && (status === undefined || response.status() === status);
  };
}

function isStudentUpdateResponse(studentId: number, status?: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    const matches = url.pathname === `/api/students/${studentId}/` && response.request().method() === "PUT";
    return matches && (status === undefined || response.status() === status);
  };
}

async function loginAsTrainer(page: Page, credentials: Credentials): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(credentials.email);
  await page.getByLabel("Пароль").fill(credentials.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

async function openLeadSheet(page: Page): Promise<void> {
  await page.goto("/trainer/leads");
  await expect(page.getByRole("heading", { name: "Заявки" })).toBeVisible();
  await page.getByRole("button", { name: "Добавить ученика" }).click();
  await expect(page.getByRole("heading", { name: "Добавить ученика" })).toBeVisible();
}

async function fillLeadSheetFields(page: Page, student: FormStudent): Promise<void> {
  await page.getByLabel("Имя клиента *").fill(student.first_name);
  if (student.last_name) {
    await page.getByLabel("Фамилия").fill(student.last_name);
  }
  await page.getByLabel("Телефон клиента *").fill(student.phone);
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
        ["manage.py", "assert_trainer_student_create_edit_e2e", "--fixture", fixturePath],
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

test("real-stack trainer creates, sees, edits, and gets duplicate-phone feedback for students", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsTrainer(page, fixture.trainer);

  await openLeadSheet(page);
  await fillLeadSheetFields(page, fixture.new_student);

  const createResponse = page.waitForResponse(isIntakeResponse(201), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Создать заявку" }).click();
  expect((await createResponse).ok()).toBe(true);
  await expect(page.getByText("Заявка создана")).toBeVisible();
  await page.getByRole("button", { name: "Открыть заявку" }).click();
  await expect(page.getByRole("heading", { name: fixture.new_student.first_name })).toBeVisible();

  await openLeadSheet(page);
  await fillLeadSheetFields(page, fixture.duplicate_attempt);
  const duplicateResponse = page.waitForResponse(isIntakeResponse(409), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Создать заявку" }).click();
  expect((await duplicateResponse).status()).toBe(409);
  await expect(page.getByText("Такая заявка уже есть в свободных")).toBeVisible();

  await page.goto("/trainer/students");
  await page.getByRole("button", { name: "Добавить ученика" }).click();
  await page.getByRole("radio", { name: "Уже занимается" }).click();
  await fillLeadSheetFields(page, fixture.existing_student_intake);
  const existingResponse = page.waitForResponse(isIntakeResponse(201), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Добавить ученика" }).click();
  expect((await existingResponse).ok()).toBe(true);
  const intakeResult = page.getByRole("status");
  await expect(intakeResult.getByText("Ученик добавлен")).toBeVisible();
  await expect(intakeResult.getByText("Без абонемента в CRM")).toBeVisible();
  await page.getByRole("button", { name: "Открыть ученика" }).click();
  await expect(page.getByRole("heading", {
    name: `${fixture.existing_student_intake.first_name} ${fixture.existing_student_intake.last_name}`,
  })).toBeVisible();

  await page.goto(`/trainer/students/${fixture.assigned_student.id}`);
  await expect(page.getByRole("heading", { name: fixture.assigned_student.full_name })).toBeVisible();
  await page.getByRole("button", { name: "Редактировать ученика" }).click();
  await expect(page.getByRole("heading", { name: "Редактировать ученика" })).toBeVisible();
  await page.getByPlaceholder("Имя").fill(fixture.edit.first_name);
  await page.getByPlaceholder("Фамилия").fill(fixture.edit.last_name);
  await page.getByPlaceholder("+7 (999) 123-45-67").fill(fixture.edit.phone);
  await page.getByPlaceholder("Противопоказания, травмы, ограничения...").fill(fixture.edit.contraindications);

  const updateResponse = page.waitForResponse(isStudentUpdateResponse(fixture.assigned_student.id, 200), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Сохранить" }).click();
  expect((await updateResponse).ok()).toBe(true);
  await expect(
    page.getByRole("heading", {
      name: `${fixture.edit.first_name} ${fixture.edit.last_name}`,
    }),
  ).toBeVisible();
  await expect(page.getByText(fixture.edit.contraindications)).toBeVisible();

  runBackendAssert(fixturePath);
});

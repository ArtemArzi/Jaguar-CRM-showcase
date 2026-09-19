import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

const FEEDBACK_SUBMIT_PATH = "/api/students/me/feedback/submit/";

interface StudentFeedbackFixture {
  student: {
    email: string;
    password: string;
  };
  expected: {
    form_name: string;
    rating_question: string;
    yes_no_question: string;
    text_question: string;
    rating_value: number;
    text_value: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a student feedback fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): StudentFeedbackFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<StudentFeedbackFixture>;

  if (!data.student?.email || !data.student.password) {
    throw new Error("Fixture student credentials are required.");
  }
  if (
    !data.expected?.form_name ||
    !data.expected.rating_question ||
    !data.expected.yes_no_question ||
    !data.expected.text_question
  ) {
    throw new Error("Fixture feedback form expectations are required.");
  }

  return data as StudentFeedbackFixture;
}

function isFeedbackSubmitResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === FEEDBACK_SUBMIT_PATH && response.request().method() === "POST";
}

async function login(page: Page, email: string, password: string): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Пароль").fill(password);
  await page.getByRole("button", { name: "Войти" }).click();
}

async function submitFeedbackForm(page: Page, fixture: StudentFeedbackFixture): Promise<Response> {
  await expect(page.getByRole("heading", { name: "Обратная связь" })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByRole("heading", { name: fixture.expected.form_name })).toBeVisible();
  await expect(page.getByText(fixture.expected.rating_question)).toBeVisible();
  await expect(page.getByText(fixture.expected.yes_no_question)).toBeVisible();
  await expect(page.getByText(fixture.expected.text_question)).toBeVisible();

  await page.getByRole("button", { name: String(fixture.expected.rating_value) }).click();
  await page.getByRole("button", { name: "Да" }).click();
  await page.locator("textarea").fill(fixture.expected.text_value);

  const submitResponse = page.waitForResponse(isFeedbackSubmitResponse, {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Отправить" }).click();
  return submitResponse;
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
        ["manage.py", "assert_student_feedback_e2e", "--fixture", fixturePath],
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

function deactivateFeedbackForm(fixturePath: string): void {
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = execFileSync(
    pythonBin,
    ["manage.py", "deactivate_student_feedback_form_e2e", "--fixture", fixturePath],
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

test("real-stack student feedback submit is duplicate-safe and shows no-active-form state", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await login(page, fixture.student.email, fixture.student.password);
  await expect(page).toHaveURL(/\/student\/?$/);

  await page.goto("/student/feedback");
  const firstResponse = await submitFeedbackForm(page, fixture);
  expect(firstResponse.status()).toBe(201);
  expect((await firstResponse.json()).already_submitted).toBe(false);
  await expect(page.getByText("Спасибо, ответ сохранён")).toBeVisible();
  await expect(page.getByText("Ваш ответ передан команде клуба.")).toBeVisible();

  await page.goto("/student/feedback");
  const duplicateResponse = await submitFeedbackForm(page, fixture);
  expect(duplicateResponse.status()).toBe(200);
  expect((await duplicateResponse.json()).already_submitted).toBe(true);
  await expect(page.getByText("Ответ уже сохранён")).toBeVisible();
  await expect(page.getByText("Повторная отправка не создала новый ответ.")).toBeVisible();

  runBackendAssert(fixturePath);
  deactivateFeedbackForm(fixturePath);
  await page.goto("/student/feedback");
  await expect(page.getByText("Активного опроса сейчас нет")).toBeVisible();
  runBackendAssert(fixturePath);
});

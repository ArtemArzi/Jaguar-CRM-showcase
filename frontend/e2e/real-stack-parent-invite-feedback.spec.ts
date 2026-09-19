import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

const ACCEPT_INVITE_PATH = "/api/parents/accept-invite/";

interface ParentInviteFeedbackFixture {
  parent: {
    email: string;
    password: string;
  };
  child: {
    student_id: number;
    name: string;
  };
  invite: {
    token: string;
  };
  expected: {
    form_name: string;
    yes_no_question: string;
    text_question: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a parent invite feedback fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): ParentInviteFeedbackFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<ParentInviteFeedbackFixture>;

  if (!data.parent?.email || !data.parent.password) {
    throw new Error("Fixture parent credentials are required.");
  }
  if (!data.child?.student_id || !data.child.name) {
    throw new Error("Fixture child data is required.");
  }
  if (!data.invite?.token) {
    throw new Error("Fixture invite token is required.");
  }
  if (!data.expected?.form_name || !data.expected.yes_no_question || !data.expected.text_question) {
    throw new Error("Fixture feedback expectations are required.");
  }

  return data as ParentInviteFeedbackFixture;
}

function isAcceptInviteResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === ACCEPT_INVITE_PATH && response.request().method() === "POST";
}

function isFeedbackSubmitResponse(childId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/api/parents/children/${childId}/feedback/submit/` &&
      response.request().method() === "POST"
    );
  };
}

async function completeLoginFromInvite(page: Page, fixture: ParentInviteFeedbackFixture): Promise<Response> {
  await page.goto(`/parent-invite/${fixture.invite.token}`);
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel("Email").fill(fixture.parent.email);
  await page.getByLabel("Пароль").fill(fixture.parent.password);

  const acceptResponse = page.waitForResponse(isAcceptInviteResponse, {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Войти" }).click();
  return acceptResponse;
}

async function submitParentFeedback(page: Page, fixture: ParentInviteFeedbackFixture): Promise<Response> {
  await expect(page.getByRole("heading", { name: "Обратная связь" })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByRole("heading", { name: fixture.expected.form_name })).toBeVisible();
  await expect(page.getByText(fixture.expected.yes_no_question)).toBeVisible();
  await expect(page.getByText(fixture.expected.text_question)).toBeVisible();

  await page.getByRole("button", { name: "Да" }).click();
  await page.locator("textarea").fill(fixture.expected.text_value);

  const submitResponse = page.waitForResponse(isFeedbackSubmitResponse(fixture.child.student_id), {
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
        ["manage.py", "assert_parent_invite_feedback_e2e", "--fixture", fixturePath],
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

test("real-stack parent accepts invite and submits child feedback", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  const acceptResponse = await completeLoginFromInvite(page, fixture);
  expect(acceptResponse.ok()).toBe(true);
  await expect(page).toHaveURL(/\/parent\/?$/);
  await expect(page.getByRole("heading", { name: "Мой ребёнок" })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText(fixture.child.name).first()).toBeVisible();

  await page.getByRole("link", { name: /Опрос/ }).first().click();
  await expect(page).toHaveURL(new RegExp(`/parent/child/${fixture.child.student_id}/feedback/?$`));

  const feedbackResponse = await submitParentFeedback(page, fixture);
  expect(feedbackResponse.status()).toBe(201);
  expect((await feedbackResponse.json()).already_submitted).toBe(false);
  await expect(page.getByText("Спасибо, ответ сохранён")).toBeVisible();

  await page.goto(`/parent/child/${fixture.child.student_id}/feedback`);
  const duplicateResponse = await submitParentFeedback(page, fixture);
  expect(duplicateResponse.status()).toBe(200);
  expect((await duplicateResponse.json()).already_submitted).toBe(true);
  await expect(page.getByText("Ответ уже сохранён")).toBeVisible();
  await expect(page.getByText("Повторная отправка не создала новый ответ.")).toBeVisible();

  runBackendAssert(fixturePath);
  deactivateFeedbackForm(fixturePath);
  await page.goto(`/parent/child/${fixture.child.student_id}/feedback`);
  await expect(page.getByText("Активного опроса сейчас нет")).toBeVisible();
  runBackendAssert(fixturePath);
});

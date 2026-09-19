import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

interface RetentionTaskLifecycleFixture {
  trainer: {
    email: string;
    password: string;
  };
  target_student: {
    name: string;
  };
  other_task_id: number;
  expected: {
    comment_text: string;
    close_notes: string;
  };
}

interface TriggerResponse {
  ok?: boolean;
  task_id: number;
  trainer_queued_push_count: number;
}

interface BackendAssertResponse {
  ok?: boolean;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a retention task lifecycle fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): RetentionTaskLifecycleFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<RetentionTaskLifecycleFixture>;

  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  if (!data.target_student?.name || !data.other_task_id) {
    throw new Error("Fixture target student and privacy task are required.");
  }
  if (!data.expected?.comment_text || !data.expected.close_notes) {
    throw new Error("Fixture lifecycle expectations are required.");
  }

  return data as RetentionTaskLifecycleFixture;
}

async function login(page: Page, fixture: RetentionTaskLifecycleFixture): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(fixture.trainer.email);
  await page.getByLabel("Пароль").fill(fixture.trainer.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

function triggerRetentionScan(fixturePath: string): TriggerResponse {
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = execFileSync(
    pythonBin,
    ["manage.py", "trigger_retention_task_lifecycle_e2e", "--fixture", fixturePath],
    {
      cwd: repoRoot,
      encoding: "utf8",
      env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const result = JSON.parse(output) as TriggerResponse;
  expect(result.ok).toBe(true);
  expect(result.task_id).toBeGreaterThan(0);
  expect(result.trainer_queued_push_count).toBeGreaterThanOrEqual(1);
  return result;
}

function isTaskStatusResponse(taskId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `/api/retention/tasks/${taskId}/status/` && response.request().method() === "POST";
  };
}

function isTaskCommentResponse(taskId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `/api/retention/tasks/${taskId}/comments/` && response.request().method() === "POST";
  };
}

function isTaskSnoozeResponse(taskId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `/api/retention/tasks/${taskId}/snooze/` && response.request().method() === "POST";
  };
}

function isTaskCloseResponse(taskId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `/api/retention/tasks/${taskId}/close/` && response.request().method() === "POST";
  };
}

async function probeForeignTaskDenied(page: Page, taskId: number): Promise<void> {
  const taskResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === `/api/retention/tasks/${taskId}/` && response.request().method() === "GET";
  }, { timeout: 20_000 });

  await page.goto(`/trainer/tasks/${taskId}`);
  const response = await taskResponse;
  const body = await response.text();

  expect([403, 404]).toContain(response.status());
  expect(body).not.toContain("HiddenRetention");
  await expect(page.getByText("HiddenRetention")).not.toBeVisible();
}

async function completeTaskLifecycle(
  page: Page,
  fixture: RetentionTaskLifecycleFixture,
  taskId: number,
): Promise<void> {
  await page.goto("/trainer/tasks");
  await expect(page.getByRole("heading", { name: /Нужно обзвонить/ })).toBeVisible();
  await expect(page.getByText(fixture.target_student.name)).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("HiddenRetention")).not.toBeVisible();

  await page.getByText(fixture.target_student.name).click();
  await expect(page).toHaveURL(new RegExp(`/trainer/tasks/${taskId}`));
  await expect(page.getByText(fixture.target_student.name)).toBeVisible();
  await expect(page.getByText("Новая")).toBeVisible();

  const statusResponse = page.waitForResponse(isTaskStatusResponse(taskId), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "В работе" }).click();
  expect((await statusResponse).ok()).toBe(true);
  await expect(page.getByText("В работе")).toBeVisible();

  const commentResponse = page.waitForResponse(isTaskCommentResponse(taskId), {
    timeout: 20_000,
  });
  await page.getByPlaceholder("Добавить комментарий...").fill(fixture.expected.comment_text);
  await page.getByPlaceholder("Добавить комментарий...").press("Enter");
  expect((await commentResponse).status()).toBe(201);
  await expect(page.getByText(fixture.expected.comment_text)).toBeVisible();

  await page.getByRole("button", { name: "Отложить" }).click();
  await expect(page.getByRole("heading", { name: "Отложить задачу" })).toBeVisible();
  const snoozeResponse = page.waitForResponse(isTaskSnoozeResponse(taskId), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Отложить" }).last().click();
  expect((await snoozeResponse).ok()).toBe(true);
  await expect(page.getByText("Отложена")).toBeVisible();

  await page.getByRole("button", { name: "Закрыть" }).click();
  await expect(page.getByRole("heading", { name: "Закрыть задачу" })).toBeVisible();
  await page.getByRole("button", { name: "Позвонил, придёт" }).click();
  await page.getByPlaceholder("Заметка (необязательно)").fill(fixture.expected.close_notes);
  const closeResponse = page.waitForResponse(isTaskCloseResponse(taskId), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Закрыть задачу" }).click();
  expect((await closeResponse).ok()).toBe(true);

  await page.goto("/trainer/tasks");
  await expect(page.getByText(fixture.target_student.name)).toBeHidden();
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
        ["manage.py", "assert_retention_task_lifecycle_e2e", "--fixture", fixturePath],
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

test("real-stack retention scan creates trainer task and trainer completes lifecycle", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);
  const trigger = triggerRetentionScan(fixturePath);

  await login(page, fixture);
  await probeForeignTaskDenied(page, fixture.other_task_id);
  await completeTaskLifecycle(page, fixture, trigger.task_id);
  runBackendAssert(fixturePath);
});

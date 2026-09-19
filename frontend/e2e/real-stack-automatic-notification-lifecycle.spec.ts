import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

interface AutomaticNotificationFixture {
  student: {
    email: string;
    password: string;
    name: string;
  };
  parent: {
    email: string;
    password: string;
  };
  expected: {
    child_checkin_expected_queued_pushes: number;
    feedback_expected_queued_pushes: number;
    reminder_group_name: string;
  };
}

interface BackendResponse {
  ok?: boolean;
}

interface TriggerResponse extends BackendResponse {
  queued_push_count?: number;
  child_checkin?: {
    queued_push_count: number;
  };
  feedback_surveys?: {
    queued_push_count: number;
  };
}

interface BackendAssertResponse extends BackendResponse {
  sent_notification_types?: string[];
  parent_notification_types?: string[];
  reminder_stage_state?: Record<string, string>;
  child_checkin?: {
    recorded_suppressed_count: number;
    queued_push_expected: number;
    suppressed_notification_types: string[];
  };
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to an automatic notification fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): AutomaticNotificationFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<AutomaticNotificationFixture>;

  if (!data.student?.email || !data.student.password || !data.student.name) {
    throw new Error("Fixture student credentials are required.");
  }
  if (!data.parent?.email || !data.parent.password) {
    throw new Error("Fixture parent credentials are required.");
  }
  if (!data.expected?.reminder_group_name) {
    throw new Error("Fixture notification expectations are required.");
  }
  if (typeof data.expected.child_checkin_expected_queued_pushes !== "number") {
    throw new Error("Fixture child check-in queue expectation is required.");
  }
  if (typeof data.expected.feedback_expected_queued_pushes !== "number") {
    throw new Error("Fixture feedback survey queue expectation is required.");
  }

  return data as AutomaticNotificationFixture;
}

async function login(page: Page, email: string, password: string): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Пароль").fill(password);
  await page.getByRole("button", { name: "Войти" }).click();
}

function triggerAutomaticNotifications(fixturePath: string): TriggerResponse {
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = execFileSync(
    pythonBin,
    ["manage.py", "trigger_automatic_notification_lifecycle_e2e", "--fixture", fixturePath],
    {
      cwd: repoRoot,
      encoding: "utf8",
      env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const result = JSON.parse(output) as TriggerResponse;
  expect(result.ok).toBe(true);
  expect(result.queued_push_count ?? 0).toBeGreaterThan(0);
  return result;
}

function runBackendAssert(fixturePath: string): BackendAssertResponse {
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
        ["manage.py", "assert_automatic_notification_lifecycle_e2e", "--fixture", fixturePath],
        {
          cwd: repoRoot,
          encoding: "utf8",
          env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
          stdio: ["ignore", "pipe", "pipe"],
        },
      );

  const result = JSON.parse(output) as BackendAssertResponse;
  expect(result.ok).toBe(true);
  return result;
}

async function expectReminderScheduleVisible(page: Page, groupName: string): Promise<void> {
  const reminder = page.getByText(groupName).first();

  if (!(await reminder.isVisible())) {
    await page.getByRole("button", { name: "Следующая неделя" }).click();
  }

  await expect(reminder).toBeVisible();
}

test("real-stack automatic notification tasks send student and parent notification records", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  const trigger = triggerAutomaticNotifications(fixturePath);
  expect(trigger.child_checkin?.queued_push_count).toBe(fixture.expected.child_checkin_expected_queued_pushes);
  expect(trigger.feedback_surveys?.queued_push_count).toBe(fixture.expected.feedback_expected_queued_pushes);

  await login(page, fixture.student.email, fixture.student.password);
  await expect(page).toHaveURL(/\/student\/?$/);
  await expect(page.getByRole("heading", { name: "Главная" })).toBeVisible({ timeout: 20_000 });
  await page.goto("/student/schedule");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible();
  await expectReminderScheduleVisible(page, fixture.expected.reminder_group_name);

  await login(page, fixture.parent.email, fixture.parent.password);
  await expect(page).toHaveURL(/\/parent\/?$/);
  await expect(page.getByRole("heading", { name: "Мой ребёнок" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(fixture.student.name).first()).toBeVisible();

  const evidence = runBackendAssert(fixturePath);
  expect(evidence.sent_notification_types ?? []).toContain("parent_sub_expiry");
  expect(evidence.parent_notification_types ?? []).toContain("parent_sub_expiry");
  expect(evidence.reminder_stage_state).toEqual({
    one_hour: "queued",
    twenty_four_hour: "queued",
  });
  expect(evidence.child_checkin?.recorded_suppressed_count).toBe(0);
  expect(evidence.child_checkin?.queued_push_expected).toBe(0);
  expect(evidence.child_checkin?.suppressed_notification_types).toHaveLength(2);
});

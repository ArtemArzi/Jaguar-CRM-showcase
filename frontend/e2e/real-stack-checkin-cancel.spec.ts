import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface CheckinCancelFixture {
  owner: {
    email: string;
    password: string;
  };
  student: {
    name: string;
  };
  schedule_id: number;
  checkin_id: number;
  checkin_date: string;
  expected: {
    group_name: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a check-in cancel fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): CheckinCancelFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<CheckinCancelFixture>;

  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.student?.name || !data.schedule_id || !data.checkin_id || !data.checkin_date) {
    throw new Error("Fixture check-in cancellation ids are required.");
  }
  if (!data.expected?.group_name) {
    throw new Error("Fixture group expectation is required.");
  }

  return data as CheckinCancelFixture;
}

function compactDate(value: string): string {
  return value.replaceAll("-", "");
}

function reversedName(value: string): string {
  return value.trim().split(/\s+/).reverse().join(" ");
}

async function loginToDashboard(page: Page, fixture: CheckinCancelFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

function isSessionCheckinsResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === "/dashboard/trainers/sessions/checkins/" && response.request().method() === "GET";
}

function isCancelCheckinResponse(checkinId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `/dashboard/checkin/${checkinId}/cancel/` && response.request().method() === "POST";
  };
}

async function cancelCheckinFromDashboard(page: Page, fixture: CheckinCancelFixture): Promise<void> {
  const sessionsUrl = `/dashboard/trainers/sessions/?date_from=${fixture.checkin_date}&date_to=${fixture.checkin_date}&schedule_id=${fixture.schedule_id}`;
  await page.goto(backendUrl(sessionsUrl));
  await expect(page.getByRole("heading", { name: "ТРЕНЕРЫ" })).toBeVisible();
  const sessionGroupCell = page.getByRole("cell", {
    name: fixture.expected.group_name,
    exact: true,
  });
  await expect(sessionGroupCell).toBeVisible();

  const checkinsResponse = page.waitForResponse(isSessionCheckinsResponse, {
    timeout: 20_000,
  });
  await sessionGroupCell.click();
  expect((await checkinsResponse).ok()).toBe(true);

  const checkins = page.locator(`#checkins-${fixture.schedule_id}-${compactDate(fixture.checkin_date)}`);
  const studentName = checkins.getByText(new RegExp(`${fixture.student.name}|${reversedName(fixture.student.name)}`));
  await expect(studentName).toBeVisible();
  await checkins.getByRole("button", { name: "✕" }).click();
  await expect(checkins.getByText("Отменить?")).toBeVisible();

  const cancelResponse = page.waitForResponse(isCancelCheckinResponse(fixture.checkin_id), {
    timeout: 20_000,
  });
  await checkins.getByRole("button", { name: "да" }).click();
  expect((await cancelResponse).ok()).toBe(true);
  await expect(checkins.getByText("Нет посещений")).toBeVisible();
  await expect(studentName).toBeHidden();
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
        ["manage.py", "assert_checkin_cancel_e2e", "--fixture", fixturePath],
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

test("real-stack owner cancels mistaken check-in and reverse side effects pass", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginToDashboard(page, fixture);
  await cancelCheckinFromDashboard(page, fixture);
  runBackendAssert(fixturePath);
});

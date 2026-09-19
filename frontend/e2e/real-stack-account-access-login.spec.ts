import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

const STUDENT_ME_PATH = "/api/students/me/";
const STUDENT_FINANCIAL_STATE_PATH = "/api/students/me/financial-state/";

interface AccountAccessLoginFixture {
  trainer: {
    email: string;
    password: string;
  };
  student_id: number;
  student: {
    name: string;
  };
  child_student_id: number;
  child: {
    name: string;
    parent_phone_input: string;
    parent_username: string;
  };
}

interface AccountAccessIssueResponse {
  student_id: number;
  role: string;
  status: string;
  username: string;
  temporary_password: string | null;
  created_user: boolean;
  created_membership: boolean;
  created_access: boolean;
}

interface BackendAssertResponse {
  ok?: boolean;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to an account access login fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): AccountAccessLoginFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<AccountAccessLoginFixture>;

  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  if (!data.student_id || !data.student?.name) {
    throw new Error("Fixture student id and name are required.");
  }
  if (!data.child_student_id || !data.child?.name || !data.child.parent_phone_input || !data.child.parent_username) {
    throw new Error("Fixture child account-access data is required.");
  }

  return data as AccountAccessLoginFixture;
}

function isAccountAccessOpenResponse(studentId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/api/students/${studentId}/account-access/open/` &&
      response.request().method() === "POST"
    );
  };
}

function isAccountAccessResetResponse(studentId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/api/students/${studentId}/account-access/reset/` &&
      response.request().method() === "POST"
    );
  };
}

function isStudentMeResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === STUDENT_ME_PATH && response.request().method() === "GET";
}

function isStudentFinancialStateResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === STUDENT_FINANCIAL_STATE_PATH && response.request().method() === "GET";
}

function isParentChildrenResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === "/api/parents/children/" && response.request().method() === "GET";
}

function isParentChildResponse(childId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `/api/parents/children/${childId}/` && response.request().method() === "GET";
  };
}

async function login(page: Page, identifier: string, password: string): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel(/Email/).fill(identifier);
  await page.getByLabel("Пароль").fill(password);
  await page.getByRole("button", { name: "Войти" }).click();
}

async function pageContainsSecret(page: Page, value: string): Promise<boolean> {
  return page.locator("body").evaluate((body, expected) => {
    return body.textContent?.includes(expected) ?? false;
  }, value);
}

async function openAccountAccess(
  page: Page,
  fixture: AccountAccessLoginFixture,
): Promise<AccountAccessIssueResponse> {
  await page.goto(`/trainer/students/${fixture.student_id}`);
  await expect(page.getByRole("heading", { name: fixture.student.name })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText("Личный кабинет")).toBeVisible();

  const openButton = page.getByRole("button", { name: "Открыть кабинет" });
  const resetButton = page.getByRole("button", { name: "Сбросить пароль" });
  await expect(openButton.or(resetButton)).toBeVisible();

  let issue: AccountAccessIssueResponse;
  if (await resetButton.isVisible()) {
    const resetResponsePromise = page.waitForResponse(
      isAccountAccessResetResponse(fixture.student_id),
      { timeout: 20_000 },
    );
    await resetButton.click();
    const response = await resetResponsePromise;
    expect(response.status()).toBe(200);
    issue = (await response.json()) as AccountAccessIssueResponse;
    expect(issue.status).toBe("reset");
    expect(issue.created_user).toBe(false);
    expect(issue.created_membership).toBe(false);
    expect(issue.created_access).toBe(false);
  } else {
    await expect(page.getByText("Можно открыть кабинет")).toBeVisible();

    const openResponsePromise = page.waitForResponse(isAccountAccessOpenResponse(fixture.student_id), {
      timeout: 20_000,
    });
    await openButton.click();
    const response = await openResponsePromise;
    expect(response.status()).toBe(201);
    issue = (await response.json()) as AccountAccessIssueResponse;
    expect(issue.status).toBe("open");
    expect(issue.created_user).toBe(true);
    expect(issue.created_membership).toBe(true);
    expect(issue.created_access).toBe(true);
  }

  expect(issue.student_id).toBe(fixture.student_id);
  expect(issue.role).toBe("student");
  expect(typeof issue.username).toBe("string");
  expect(issue.username.length).toBeGreaterThan(0);
  expect(typeof issue.temporary_password).toBe("string");
  expect(issue.temporary_password?.length ?? 0).toBeGreaterThan(0);

  expect(await pageContainsSecret(page, issue.username)).toBe(true);
  expect(await pageContainsSecret(page, issue.temporary_password ?? "")).toBe(true);

  return issue;
}

async function openParentAccountAccess(
  page: Page,
  fixture: AccountAccessLoginFixture,
): Promise<AccountAccessIssueResponse> {
  await page.goto(`/trainer/students/${fixture.child_student_id}`);
  await expect(page.getByRole("heading", { name: fixture.child.name })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText("Личный кабинет")).toBeVisible();

  const openButton = page.getByRole("button", { name: "Открыть кабинет" });
  const resetButton = page.getByRole("button", { name: "Сбросить пароль" });
  await expect(openButton.or(resetButton)).toBeVisible();

  let issue: AccountAccessIssueResponse;
  if (await resetButton.isVisible()) {
    const resetResponsePromise = page.waitForResponse(
      isAccountAccessResetResponse(fixture.child_student_id),
      { timeout: 20_000 },
    );
    await resetButton.click();
    const response = await resetResponsePromise;
    expect(response.status()).toBe(200);
    issue = (await response.json()) as AccountAccessIssueResponse;
    expect(issue.status).toBe("reset");
    expect(issue.created_user).toBe(false);
    expect(issue.created_membership).toBe(false);
    expect(issue.created_access).toBe(false);
  } else {
    await expect(page.getByText("Можно открыть кабинет")).toBeVisible();
    await expect(openButton).toBeDisabled();
    await page.getByLabel("Телефон родителя").fill(fixture.child.parent_phone_input);

    const openResponsePromise = page.waitForResponse(isAccountAccessOpenResponse(fixture.child_student_id), {
      timeout: 20_000,
    });
    await openButton.click();
    const response = await openResponsePromise;
    expect(response.status()).toBe(201);
    issue = (await response.json()) as AccountAccessIssueResponse;
    expect(issue.status).toBe("open");
    expect(issue.created_user).toBe(true);
    expect(issue.created_membership).toBe(true);
    expect(issue.created_access).toBe(true);
  }

  expect(issue.student_id).toBe(fixture.child_student_id);
  expect(issue.role).toBe("parent");
  expect(issue.username).toBe(fixture.child.parent_username);
  expect(typeof issue.temporary_password).toBe("string");
  expect(issue.temporary_password?.length ?? 0).toBeGreaterThan(0);

  expect(await pageContainsSecret(page, issue.username)).toBe(true);
  expect(await pageContainsSecret(page, issue.temporary_password ?? "")).toBe(true);

  return issue;
}

async function assertStudentProfile(
  page: Page,
  fixture: AccountAccessLoginFixture,
): Promise<void> {
  const meResponsePromise = page.waitForResponse(isStudentMeResponse, {
    timeout: 20_000,
  });
  await page.goto("/student/profile");
  const response = await meResponsePromise;
  expect(response.ok()).toBe(true);

  await expect(page.getByRole("heading", { name: fixture.student.name })).toBeVisible();
  await expect(page.getByText("Активен").first()).toBeVisible();

  const financialStatePromise = page.waitForResponse(isStudentFinancialStateResponse, {
    timeout: 20_000,
  });
  await page.goto("/student");
  const financialStateResponse = await financialStatePromise;
  expect(financialStateResponse.ok()).toBe(true);
  await expect(page.getByText("Оплата ожидает подтверждения").first()).toBeVisible();

  const reloadFinancialStatePromise = page.waitForResponse(isStudentFinancialStateResponse, {
    timeout: 20_000,
  });
  await page.reload();
  expect((await reloadFinancialStatePromise).ok()).toBe(true);
  await expect(page.getByText("Оплата ожидает подтверждения").first()).toBeVisible();
}

async function assertParentChildShell(
  page: Page,
  fixture: AccountAccessLoginFixture,
): Promise<void> {
  const childrenResponsePromise = page.waitForResponse(isParentChildrenResponse, {
    timeout: 20_000,
  });
  await page.goto("/parent");
  const childrenResponse = await childrenResponsePromise;
  expect(childrenResponse.ok()).toBe(true);
  await expect(page.getByText(fixture.child.name).first()).toBeVisible();

  const childResponsePromise = page.waitForResponse(isParentChildResponse(fixture.child_student_id), {
    timeout: 20_000,
  });
  await page.goto(`/parent/child/${fixture.child_student_id}`);
  const childResponse = await childResponsePromise;
  expect(childResponse.ok()).toBe(true);
  await expect(page.getByText(fixture.child.name).first()).toBeVisible();
  await expect(page.getByText("Оплата ожидает подтверждения").first()).toBeVisible();

  const reloadChildResponsePromise = page.waitForResponse(isParentChildResponse(fixture.child_student_id), {
    timeout: 20_000,
  });
  await page.reload();
  expect((await reloadChildResponsePromise).ok()).toBe(true);
  await expect(page.getByText("Оплата ожидает подтверждения").first()).toBeVisible();
}

async function assertAnonymousRoleShellRedirectsToLogin(page: Page): Promise<void> {
  await page.goto("/trainer");
  await expect(page).toHaveURL(/\/login$/);
}

async function assertTrainerCannotOpenStudentShell(page: Page): Promise<void> {
  await page.goto("/student/profile");
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

async function assertStudentCannotOpenTrainerShell(page: Page): Promise<void> {
  await page.goto("/trainer/students");
  await expect(page).toHaveURL(/\/student\/?$/);
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
        ["manage.py", "assert_account_access_login_e2e", "--fixture", fixturePath],
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

test("real-stack trainer opens pending-manual student access and student logs in with issued credentials", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await assertAnonymousRoleShellRedirectsToLogin(page);

  await login(page, fixture.trainer.email, fixture.trainer.password);
  await expect(page).toHaveURL(/\/trainer\/?$/);
  await assertTrainerCannotOpenStudentShell(page);

  const issue = await openAccountAccess(page, fixture);
  await login(page, issue.username, issue.temporary_password ?? "");
  await expect(page).toHaveURL(/\/student\/?$/);

  await assertStudentProfile(page, fixture);
  await assertStudentCannotOpenTrainerShell(page);

  await login(page, fixture.trainer.email, fixture.trainer.password);
  await expect(page).toHaveURL(/\/trainer\/?$/);
  const parentIssue = await openParentAccountAccess(page, fixture);
  await login(page, parentIssue.username, parentIssue.temporary_password ?? "");
  await expect(page).toHaveURL(/\/parent\/?$/);
  await assertParentChildShell(page, fixture);

  runBackendAssert(fixturePath);
});

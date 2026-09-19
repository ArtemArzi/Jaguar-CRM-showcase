import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface FreezeLifecycleFixture {
  owner: {
    email: string;
    password: string;
  };
  approve_freeze_id: number;
  reject_freeze_id: number;
  unfreeze_subscription_id: number;
  expected: {
    reject_decision_reason: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a freeze lifecycle fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): FreezeLifecycleFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<FreezeLifecycleFixture>;

  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.approve_freeze_id || !data.reject_freeze_id || !data.unfreeze_subscription_id) {
    throw new Error("Fixture freeze/subscription ids are required.");
  }
  if (!data.expected?.reject_decision_reason) {
    throw new Error("Fixture expected.reject_decision_reason is required.");
  }

  return {
    owner: data.owner,
    approve_freeze_id: data.approve_freeze_id,
    reject_freeze_id: data.reject_freeze_id,
    unfreeze_subscription_id: data.unfreeze_subscription_id,
    expected: data.expected,
  };
}

function isFreezeDecisionResponse(freezeId: number, action: "approve" | "reject") {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/dashboard/billing/freezes/${freezeId}/${action}/` &&
      response.request().method() === "POST"
    );
  };
}

function isUnfreezeResponse(subscriptionId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/dashboard/billing/subscriptions/${subscriptionId}/unfreeze/` &&
      response.request().method() === "POST"
    );
  };
}

async function loginAsOwner(page: Page, fixture: FreezeLifecycleFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

async function approvePendingFreeze(page: Page, fixture: FreezeLifecycleFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/billing/subscriptions/"));
  await expect(page.getByRole("heading", { name: "Абонементы" })).toBeVisible();
  const inbox = page.locator("#freeze-approval-inbox");
  await expect(inbox.getByText("ApproveFreeze Student")).toBeVisible();
  await expect(inbox.getByText("RejectFreeze Student")).toBeVisible();
  const approveRow = inbox
    .getByText("ApproveFreeze Student")
    .locator("xpath=ancestor::div[contains(@class, 'grid')][1]");

  const decisionResponse = page.waitForResponse(isFreezeDecisionResponse(fixture.approve_freeze_id, "approve"), {
    timeout: 20_000,
  });
  await approveRow.getByRole("button", { name: "ОДОБРИТЬ" }).click();
  const response = await decisionResponse;

  expect(response.status()).toBe(204);
  await expect(page).toHaveURL(/\/dashboard\/billing\/subscriptions\/$/);
  await expect(page.locator("#freeze-approval-inbox").getByText("RejectFreeze Student")).toBeVisible();
}

async function rejectPendingFreeze(page: Page, fixture: FreezeLifecycleFixture): Promise<void> {
  const inbox = page.locator("#freeze-approval-inbox");
  await expect(inbox.getByText("RejectFreeze Student")).toBeVisible();
  const rejectRow = inbox
    .getByText("RejectFreeze Student")
    .locator("xpath=ancestor::div[contains(@class, 'grid')][1]");
  await rejectRow.getByPlaceholder("Причина").fill(fixture.expected.reject_decision_reason);

  const decisionResponse = page.waitForResponse(isFreezeDecisionResponse(fixture.reject_freeze_id, "reject"), {
    timeout: 20_000,
  });
  await rejectRow.getByRole("button", { name: "ОТКЛОНИТЬ" }).click();
  const response = await decisionResponse;

  expect(response.status()).toBe(204);
  await expect(page).toHaveURL(/\/dashboard\/billing\/subscriptions\/$/);
  await expect(page.locator("#freeze-approval-inbox").getByText("RejectFreeze Student")).toHaveCount(0);
}

async function unfreezeSubscription(page: Page, fixture: FreezeLifecycleFixture): Promise<void> {
  const row = page.locator("tr").filter({ hasText: /(^|\s)Unfreeze Student\b/ });
  await expect(row).toBeVisible();
  await row.locator('button[title="Разморозить"]').click();
  const confirmUnfreezeButton = row.locator('button[hx-post*="/unfreeze/"]');
  await expect(confirmUnfreezeButton).toBeVisible();

  const unfreezeResponse = page.waitForResponse(isUnfreezeResponse(fixture.unfreeze_subscription_id), {
    timeout: 20_000,
  });
  await confirmUnfreezeButton.click();
  const response = await unfreezeResponse;

  expect(response.status()).toBe(204);
  await expect(page).toHaveURL(/\/dashboard\/billing\/subscriptions\/$/);
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
        ["manage.py", "assert_freeze_lifecycle_e2e", "--fixture", fixturePath],
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

test("real-stack owner approves, rejects, and unfreezes subscription freezes", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsOwner(page, fixture);
  await approvePendingFreeze(page, fixture);
  await rejectPendingFreeze(page, fixture);
  await unfreezeSubscription(page, fixture);
  runBackendAssert(fixturePath);
});

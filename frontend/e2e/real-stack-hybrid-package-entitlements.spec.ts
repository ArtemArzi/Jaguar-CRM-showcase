import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface Fixture {
  owner: {
    email: string;
    password: string;
  };
  payment_id: number;
}

interface BackendAssertResponse {
  ok?: boolean;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a hybrid package fixture JSON file.");
  }
  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): Fixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<Fixture>;
  if (!data.owner?.email || !data.owner.password || !data.payment_id) {
    throw new Error("Fixture owner credentials and payment_id are required.");
  }
  return {
    owner: data.owner,
    payment_id: data.payment_id,
  };
}

function isPaymentVerifyResponse(paymentId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/dashboard/billing/payments/${paymentId}/verify/` &&
      response.request().method() === "POST"
    );
  };
}

async function loginAsOwner(page: Page, fixture: Fixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/$/);
}

async function confirmPendingPayment(page: Page, fixture: Fixture): Promise<Response> {
  await page.goto(backendUrl("/dashboard/billing/payments/"));
  await expect(page.getByRole("heading", { name: "Оплаты" })).toBeVisible();
  await page.getByRole("button", { name: "Подтвердить" }).click();
  await expect(page.getByText("Подтвердить?")).toBeVisible();

  const verifyResponse = page.waitForResponse(isPaymentVerifyResponse(fixture.payment_id), {
    timeout: 20_000,
  });
  await page
    .locator(`button[hx-post="/dashboard/billing/payments/${fixture.payment_id}/verify/"]`)
    .click();
  return await verifyResponse;
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
        ["manage.py", "assert_hybrid_package_entitlements_e2e", "--fixture", fixturePath],
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

test("real-stack hybrid package confirms payment, settles debt, and enforces component limits", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsOwner(page, fixture);
  const verifyResponse = await confirmPendingPayment(page, fixture);
  expect(verifyResponse.status()).toBe(204);
  await expect(page).toHaveURL(/\/dashboard\/billing\/payments\/$/);
  runBackendAssert(fixturePath);
});

import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Locator, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface RefundResolutionFixture {
  owner: {
    email: string;
    password: string;
  };
  full: {
    order_id: number;
    student_name: string;
    amount: string;
  };
  partial: {
    order_id: number;
    student_name: string;
    refund_amount: string;
  };
  mixed: {
    order_id: number;
    student_name: string;
    amount: string;
  };
  payroll: {
    open_date: string;
  };
  expected: {
    full_reason: string;
    partial_reason: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a refund resolution fixture JSON file.");
  }
  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): RefundResolutionFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<RefundResolutionFixture>;
  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.full?.order_id || !data.full.student_name || !data.full.amount) {
    throw new Error("Fixture full refund target is required.");
  }
  if (!data.partial?.order_id || !data.partial.student_name || !data.partial.refund_amount) {
    throw new Error("Fixture partial refund target is required.");
  }
  if (!data.mixed?.order_id || !data.mixed.student_name || !data.mixed.amount) {
    throw new Error("Fixture mixed legacy refund target is required.");
  }
  if (!data.payroll?.open_date) {
    throw new Error("Fixture open payroll date is required.");
  }
  if (!data.expected?.full_reason || !data.expected.partial_reason) {
    throw new Error("Fixture refund reasons are required.");
  }
  return data as RefundResolutionFixture;
}

async function loginToDashboard(page: Page, fixture: RefundResolutionFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/$/);
}

function reviewForm(page: Page, orderId: number): Locator {
  return page.locator(
    `form[hx-post="/dashboard/billing/bank-payment-orders/${orderId}/review/"]`,
  );
}

function isOrderReviewResponse(orderId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/dashboard/billing/bank-payment-orders/${orderId}/review/` &&
      response.request().method() === "POST"
    );
  };
}

async function postFullRefund(page: Page, fixture: RefundResolutionFixture): Promise<void> {
  const form = reviewForm(page, fixture.full.order_id);
  await expect(form).toBeVisible();
  await expect(form.locator('input[name="resolution"]')).toHaveValue("mark_refunded");
  await form.locator('select[name="entitlement_action"]').selectOption("revoke_remaining");
  await form.locator('input[name="reason"]').fill(fixture.expected.full_reason);

  const responsePromise = page.waitForResponse(isOrderReviewResponse(fixture.full.order_id), {
    timeout: 20_000,
  });
  await form.getByRole("button", { name: "ОТМЕТИТЬ ВОЗВРАТ" }).click();
  expect((await responsePromise).status()).toBe(204);
  await expect(reviewForm(page, fixture.full.order_id)).toHaveCount(0);
}

async function postPartialRefund(page: Page, fixture: RefundResolutionFixture): Promise<void> {
  const form = reviewForm(page, fixture.partial.order_id);
  await expect(form).toBeVisible();
  await expect(form.locator('input[name="resolution"]')).toHaveValue("mark_refunded_partially");
  await form.locator('input[name="refund_amount"]').fill(fixture.partial.refund_amount);
  await form.locator('input[name="reason"]').fill(fixture.expected.partial_reason);

  const responsePromise = page.waitForResponse(isOrderReviewResponse(fixture.partial.order_id), {
    timeout: 20_000,
  });
  await form.getByRole("button", { name: "ЧАСТИЧНЫЙ ВОЗВРАТ" }).click();
  expect((await responsePromise).status()).toBe(204);
  await expect(reviewForm(page, fixture.partial.order_id)).toHaveCount(0);
}

async function completePayrollAction(page: Page, fixture: RefundResolutionFixture): Promise<void> {
  const heading = page.getByRole("heading", {
    name: "Возврат проведён — нужна дата корректировки зарплаты",
  });
  await expect(heading).toBeVisible();
  const section = heading.locator("xpath=ancestor::section");
  const form = section.locator("form").filter({ hasText: fixture.partial.student_name });
  await expect(form).toBeVisible();
  const dateInput = form.locator('input[name="effective_date"]');
  await expect(dateInput).not.toHaveAttribute("min");
  await dateInput.fill(fixture.payroll.open_date);

  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      /^\/dashboard\/billing\/payment-refunds\/\d+\/complete-payroll\/$/.test(url.pathname) &&
      response.request().method() === "POST"
    );
  }, { timeout: 20_000 });
  await form.getByRole("button", { name: "СОЗДАТЬ КОРРЕКТИРОВКУ" }).click();
  expect((await responsePromise).status()).toBe(204);
  await expect(heading).toHaveCount(0);
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
        ["manage.py", "assert_refund_resolution_e2e", "--fixture", fixturePath],
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

test("real-stack owner resolves full and partial provider refunds with explicit payroll follow-up", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginToDashboard(page, fixture);
  await page.goto(backendUrl("/dashboard/billing/payments/?queue=online"));
  await expect(page.getByRole("heading", { name: "Оплаты — финансовые операции" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Проблемы онлайн-оплаты 3/ })).toBeVisible();
  await expect(page.getByText(fixture.full.student_name).first()).toBeVisible();
  await expect(page.getByText(fixture.partial.student_name).first()).toBeVisible();
  await expect(page.getByText(fixture.mixed.student_name).first()).toBeVisible();

  await postFullRefund(page, fixture);
  await postPartialRefund(page, fixture);
  await postFullRefund(page, { ...fixture, full: fixture.mixed });
  await completePayrollAction(page, fixture);
  runBackendAssert(fixturePath);
});

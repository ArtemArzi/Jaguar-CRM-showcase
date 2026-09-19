import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const fixturePath = process.env.REAL_STACK_E2E_FIXTURE;
if (!fixturePath) throw new Error("REAL_STACK_E2E_FIXTURE is required");
const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as {
  owner: { email: string; password: string };
  student: { id: number };
  subscription_id: number;
  tariff_id: number;
  tariff_name: string;
};

function persistedState(phase: "revised" | "pending" | "confirmed") {
  const override = process.env.REAL_STACK_E2E_ASSERT_COMMAND;
  const executable = override ? "bash" : process.env.PYTHON_BIN || resolve(repoRoot, ".venv/bin/python");
  const args = override
    ? ["-c", override + ' "$@"', "assert-command", "--phase", phase]
    : ["manage.py", "assert_tariff_price_revisions_e2e", "--fixture", fixturePath!, "--phase", phase];
  const output = execFileSync(executable, args, {
    cwd: repoRoot, encoding: "utf8", timeout: 45_000, env: { ...process.env },
  });
  const result = JSON.parse(output.trim()) as { ok: boolean; target_tariff_id: number; payment_id?: number };
  expect(result.ok).toBe(true);
  return result;
}

async function revisePrice(page: Page, tariffId: number, name: string, price: string) {
  await page.goto(backendUrl("/dashboard/settings/billing/"));
  const path = `/dashboard/settings/billing/tariffs/${tariffId}/price-revision/`;
  await page.locator(`button[hx-get="${path}"]`).click();
  const panel = page.locator("#slide-over");
  await panel.locator('input[name="new_name"]').fill(name);
  await panel.locator('input[name="new_price"]').fill(price);
  const response = page.waitForResponse(result => new URL(result.url()).pathname === path
    && result.request().method() === "POST");
  await panel.getByRole("button", { name: "СОХРАНИТЬ НОВУЮ ЦЕНУ", exact: true }).click();
  expect((await response).status()).toBe(204);
  await expect(page.locator(`button[hx-get="${path}"]`)).toHaveCount(0);
}

test("price archive and exact renewal survive a later price change and confirmation replay", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  page.setDefaultTimeout(20_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard\/?$/);

  await revisePrice(page, fixture.tariff_id, "Проверочный пакет — новая цена", "8500");
  const { target_tariff_id: acceptedTariffId } = persistedState("revised");
  await page.getByRole("link", { name: "Архив тарифов", exact: true }).click();
  await expect(page.getByText(fixture.tariff_name, { exact: true })).toBeVisible();

  await page.goto(backendUrl("/dashboard/students/"));
  await page.locator(`[data-student-opener][hx-get="/dashboard/students/${fixture.student.id}/card/"]:visible`).first().click();
  const panel = page.locator("#slide-over");
  await expect(panel.locator("[data-student-operations]")).toBeVisible();
  await panel.locator(`[data-subscription-id="${fixture.subscription_id}"] summary`).click();
  await panel.getByRole("button", { name: "Продлить", exact: true }).click();
  await expect(panel.locator("[data-subscription-renew]")).toContainText(/8\s?500/);
  await expect(panel.locator('input[name="expected_target_tariff_id"]')).toHaveValue(String(acceptedTariffId));
  await expect(panel.locator('input[name="expected_target_price"]')).toHaveValue(/^8500(?:\.00)?$/);
  await panel.getByLabel("Способ оплаты").selectOption("cash");
  await panel.getByRole("button", { name: "Записать продление", exact: true }).click();
  await expect(panel.getByRole("status")).toContainText("ожидает проверки");
  const { payment_id: paymentId } = persistedState("pending");
  expect(paymentId).toBeDefined();

  await revisePrice(page, acceptedTariffId, "Проверочный пакет — текущая цена", "9000");
  await page.goto(backendUrl("/dashboard/billing/payments/"));
  const verifyPath = `/dashboard/billing/payments/${paymentId}/verify/`;
  const verifyButton = page.locator(`button[hx-post="${verifyPath}"]`);
  const action = verifyButton.locator("xpath=ancestor::div[@x-data][1]");
  await action.getByRole("button", { name: "Подтвердить", exact: true }).click();
  const response = page.waitForResponse(result => new URL(result.url()).pathname === verifyPath
    && result.request().method() === "POST");
  await action.getByRole("button", { name: "Да, подтвердить", exact: true }).click();
  const confirmed = await response;
  expect(confirmed.status()).toBe(204);
  persistedState("confirmed");

  const replay = await page.request.post(confirmed.url(), {
    headers: await confirmed.request().allHeaders(),
    data: confirmed.request().postData() || "",
  });
  expect(replay.status()).toBe(204);
  persistedState("confirmed");
  await page.screenshot({ path: testInfo.outputPath("tariff-renewal-confirmed-mobile.png"), fullPage: true });
});

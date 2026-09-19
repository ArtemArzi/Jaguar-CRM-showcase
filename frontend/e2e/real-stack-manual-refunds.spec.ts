import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const fixturePath = process.env.REAL_STACK_E2E_FIXTURE;
if (!fixturePath) throw new Error("REAL_STACK_E2E_FIXTURE is required");
const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as {
  owner: { email: string; password: string };
  student: { id: number; name: string };
  foreign_student_id: number;
  subscription_id: number;
  component_id: number;
  payment_id: number;
  schedule: { id: number; date: string };
};

function backendCommand(command: string, ...args: string[]) {
  const override = command.startsWith("assert_") ? process.env.REAL_STACK_E2E_ASSERT_COMMAND : undefined;
  const executable = override ? "bash" : process.env.PYTHON_BIN || resolve(repoRoot, ".venv/bin/python");
  const commandArgs = override
    ? ["-c", override + ' "$@"', "assert-command", ...args]
    : ["manage.py", command, "--fixture", fixturePath!, ...args];
  const output = execFileSync(executable, commandArgs, {
      cwd: repoRoot, encoding: "utf8", timeout: 45_000,
      env: { ...process.env },
    });
  const result = JSON.parse(output.trim()) as { ok: boolean };
  expect(result.ok).toBe(true);
}

test("manual refund from student card preserves money and closes only remaining entitlement", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  page.setDefaultTimeout(20_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard\/?$/);
  await page.goto(backendUrl("/dashboard/students/"));
  await page.locator(`[hx-get="/dashboard/students/${fixture.student.id}/card/"]:visible`).first().click();
  const panel = page.locator("#slide-over");
  await panel.getByRole("button", { name: "Возвраты", exact: true }).click();
  await panel.getByLabel("Сумма возврата, ₽").fill("2000");
  await panel.getByLabel("Причина", { exact: true }).fill("Фактический возврат E2E");
  await panel.getByLabel("Право посещения").selectOption("revoke_remaining");
  await panel.getByRole("button", { name: "Записать возврат", exact: true }).click();
  await expect(panel.getByRole("alert")).toContainText("Частичный возврат");
  await expect(panel.getByLabel("Сумма возврата, ₽")).toHaveValue("2000");
  await panel.getByLabel("Право посещения").selectOption("kept_partial");
  await panel.getByRole("button", { name: "Записать возврат", exact: true }).click();
  await expect(panel.getByRole("status")).toContainText("Возврат записан");
  backendCommand("assert_manual_refunds_e2e", "--phase", "partial");
  await panel.getByRole("button", { name: "Возвраты", exact: true }).click();
  await expect(panel).toContainText("Можно вернуть ещё 6 000");
  await panel.getByLabel("Сумма возврата, ₽").fill("6000");
  await panel.getByLabel("Причина", { exact: true }).fill("Остаток возвращён E2E");
  await panel.getByLabel("Право посещения").selectOption("revoke_remaining");
  await panel.getByRole("button", { name: "Записать возврат", exact: true }).click();
  await expect(panel.getByRole("status")).toContainText("Возврат записан");
  backendCommand("assert_manual_refunds_e2e", "--phase", "full");
  await expect(panel).toContainText("Архив абонементов");
  for (const width of [360, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => panel.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`manual-refund-${width}.png`), fullPage: true });
  }
});

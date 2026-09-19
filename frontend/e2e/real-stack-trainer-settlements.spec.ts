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
  trainer_id: number;
  opening_on: string;
};
function backendCommand(command: string, ...args: string[]) {
  const override = command.startsWith("assert_") ? process.env.REAL_STACK_E2E_ASSERT_COMMAND : undefined;
  const executable = override ? "bash" : process.env.PYTHON_BIN || resolve(repoRoot, ".venv/bin/python");
  const commandArgs = override
    ? ["-c", override + ' "$@"', "assert-command", ...args]
    : ["manage.py", command, "--fixture", fixturePath!, ...args];
  const output = execFileSync(executable, commandArgs, {
      cwd: repoRoot, encoding: "utf8", timeout: 30_000, env: { ...process.env },
    });
  expect((JSON.parse(output.trim()) as { ok: boolean }).ok).toBe(true);
}

test("trainer actual settlement: baseline, payout, reversal and historical reconciliation", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  page.setDefaultTimeout(20_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard\/?$/);
  await page.goto(backendUrl(`/dashboard/trainers/${fixture.trainer_id}/`));
  const section = page.locator("[data-trainer-settlements]");
  const panel = page.locator("#slide-over");
  await expect(section).toContainText("Расчёты до даты перехода не сверены");
  await section.getByRole("button", { name: "Начальная сверка", exact: true }).click();
  await expect(panel.locator("[data-settlement-form]")).toBeVisible();
  await expect(panel).not.toHaveClass(/htmx-settling/);
  const refreshed = page.waitForResponse(response => response.url().endsWith("/settlements/")
    && response.request().method() === "POST");
  await panel.getByLabel("Дата записи").fill(fixture.opening_on);
  await panel.getByLabel("Дата записи").press("Tab");
  await refreshed;
  await panel.getByLabel("Долг или аванс на начало дня, ₽").fill("1000");
  await panel.getByLabel("Основание записи").fill("Начальная сверка E2E");
  await panel.getByRole("button", { name: "Подтвердить начальный остаток" }).click();
  await expect(panel.getByRole("status")).toContainText("записана");
  await panel.getByRole("button", { name: "Назад к тренеру" }).click();
  await expect(section.locator("[data-settlement-balance]")).toHaveText("1 500 ₽");

  await section.getByRole("button", { name: "Записать выплату", exact: true }).click();
  await panel.getByLabel("Сумма выплаты, ₽").fill("300");
  await panel.getByLabel("Основание записи").fill("Деньги переданы E2E");
  await panel.getByRole("button", { name: "Записать выплату", exact: true }).click();
  await expect(panel.getByRole("status")).toContainText("записана");
  backendCommand("assert_trainer_settlements_e2e", "--phase", "paid");
  await panel.getByRole("button", { name: "Назад к тренеру" }).click();
  await expect(section.locator("[data-settlement-balance]")).toHaveText("1 200 ₽");
  await section.locator("summary").click();
  await section.getByRole("button", { name: /Отменить выплату №/ }).click();
  await panel.getByLabel("Основание записи").fill("Запись выплаты ошибочна E2E");
  await panel.getByRole("button", { name: "Отменить выплату", exact: true }).click();
  await expect(panel.getByRole("status")).toContainText("записана");
  await panel.getByRole("button", { name: "Назад к тренеру" }).click();
  await expect(section.locator("[data-settlement-balance]")).toHaveText("1 500 ₽");

  backendCommand("correct_trainer_settlements_e2e");
  await page.goto(backendUrl(`/dashboard/trainers/${fixture.trainer_id}/`));
  await expect(section).toContainText("Нужно сверить исторические изменения");
  await expect(section.getByRole("button", { name: "Записать выплату", exact: true })).toHaveCount(0);
  await section.getByRole("button", { name: "Сверить изменение" }).click();
  await panel.getByLabel("Решение", { exact: true }).selectOption("adjust_opening");
  await expect(panel.getByLabel("Изменение остатка, ₽")).toHaveValue("-500.00");
  await panel.getByLabel("Основание решения").fill("Изменение не входило в начальный долг");
  await panel.getByRole("button", { name: "Сохранить решение" }).click();
  await expect(panel.getByRole("status")).toContainText("сохранено");
  await panel.getByRole("button", { name: "Назад к тренеру" }).click();
  await expect(section.locator("[data-settlement-balance]")).toHaveText("1 000 ₽");
  backendCommand("assert_trainer_settlements_e2e", "--phase", "final");
  for (const width of [360, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => section.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`trainer-settlements-${width}.png`), fullPage: true });
  }
  await section.getByRole("link", { name: "2 недели", exact: true }).click();
  await expect(page).toHaveURL(/period=two_weeks/);
  await expect(section).toContainText("Даты:");
});

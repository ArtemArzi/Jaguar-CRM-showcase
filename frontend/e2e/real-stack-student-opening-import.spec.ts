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
  workbook_path: string;
  seller_id: number;
};
function assertBackend(phase: string) {
  const override = process.env.REAL_STACK_E2E_ASSERT_COMMAND;
  const executable = override ? "bash" : process.env.PYTHON_BIN || resolve(repoRoot, ".venv/bin/python");
  const commandArgs = override
    ? ["-c", override + ' "$@"', "assert-command", "--phase", phase]
    : ["manage.py", "assert_student_opening_import_e2e", "--fixture", fixturePath!, "--phase", phase];
  const output = execFileSync(executable, commandArgs, {
      cwd: repoRoot, encoding: "utf8", timeout: 30_000, env: { ...process.env },
    });
  expect((JSON.parse(output.trim()) as { ok: boolean }).ok).toBe(true);
}

test("mixed opening workbook: lost response, partial repair, native corrections and immutable replay", async ({ page }, testInfo) => {
  test.setTimeout(180_000);
  page.setDefaultTimeout(20_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard\/?$/);
  await page.goto(backendUrl("/dashboard/students/"));
  await page.getByRole("link", { name: "Загрузить из Excel", exact: true }).click();
  await page.getByLabel("Таблица Excel").setInputFiles({
    name: "source.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: readFileSync(fixture.workbook_path),
  });
  await page.getByRole("button", { name: "Проверить таблицу", exact: true }).click();
  await expect(page.locator("[data-import-ready]")).toHaveText("4");
  await expect(page.locator("[data-import-review]")).toHaveText("1");
  assertBackend("preview");
  const batchUrl = page.url();
  const applyUrl = `${batchUrl}apply/`;
  let acceptedOnServer!: () => void;
  const serverAccepted = new Promise<void>(resolve => { acceptedOnServer = resolve; });
  await page.route(applyUrl, async route => {
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    await route.abort("failed");
    acceptedOnServer();
  });
  await page.getByRole("button", { name: "Применить 4 готовых записей" }).click();
  await serverAccepted;
  await page.unroute(applyUrl);
  await page.goto(batchUrl);
  await expect(page.locator("[data-import-applied]")).toHaveText("4", { timeout: 45_000 });
  await expect(page.getByRole("status")).toContainText("Применено частично");
  assertBackend("partial");
  const question = page.locator("[data-import-item]").filter({ hasText: "Уточнение Перенос" });
  await question.getByRole("link", { name: "Уточнить запись" }).click();
  await page.getByLabel("Заморожен", { exact: true }).selectOption("Нет");
  await page.getByRole("button", { name: "Сохранить и проверить" }).click();
  await expect(page.locator("[data-import-ready]")).toHaveText("1");
  await page.getByRole("button", { name: "Применить 1 готовых записей" }).click();
  await expect(page.locator("[data-import-applied]")).toHaveText("5", { timeout: 45_000 });
  assertBackend("complete");

  const panel = page.locator("#slide-over");
  await page.locator("[data-import-item]").filter({ hasText: "Персональный Перенос" }).getByRole("link", { name: "Открыть ученика" }).click();
  await panel.getByRole("button", { name: "Исправить", exact: true }).click();
  await panel.getByLabel("Осталось занятий").fill("9");
  await panel.locator('textarea[name="reason"]').fill("Сверка перенесённого остатка");
  await panel.getByRole("button", { name: "Сохранить исправление" }).click();
  await expect(panel.getByRole("status")).toContainText("Исправление сохранено");
  await page.goto(batchUrl);
  await page.locator("[data-import-item]").filter({ hasText: "Групповой Перенос" }).getByRole("link", { name: "Открыть ученика" }).click();
  await panel.getByRole("button", { name: "Возвраты", exact: true }).click();
  await panel.getByLabel("Сумма возврата, ₽").fill("3250");
  await panel.getByLabel("Причина", { exact: true }).fill("Фактический возврат перенесённой оплаты");
  await panel.getByRole("button", { name: "Записать возврат", exact: true }).click();
  await expect(panel.getByRole("status")).toContainText("Возврат записан");
  assertBackend("adjusted");
  await page.goto(backendUrl(`/dashboard/trainers/${fixture.seller_id}/`));
  await expect(page.locator("[data-settlement-balance]")).toHaveText("150 ₽");

  await page.goto(batchUrl);
  const downloadReady = page.waitForEvent("download");
  await page.getByRole("link", { name: "Скачать таблицу для исправления" }).click();
  const download = await downloadReady;
  const resultPath = testInfo.outputPath("synthetic-opening-result.xlsx");
  await download.saveAs(resultPath);
  await page.getByRole("link", { name: "Таблицы переноса" }).click();
  await page.getByLabel("Таблица Excel").setInputFiles({
    name: "renamed-result.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: readFileSync(resultPath),
  });
  await page.getByRole("button", { name: "Проверить таблицу", exact: true }).click();
  await expect(page.locator("[data-import-ready]")).toHaveText("5");
  await page.getByRole("button", { name: "Применить 5 готовых записей" }).click();
  await expect(page.locator("[data-import-replayed]")).toHaveText("5", { timeout: 45_000 });
  assertBackend("replay");
  for (const width of [360, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    const workspace = page.locator("[data-opening-import-batch]");
    await expect.poll(() => workspace.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`opening-import-${width}.png`), fullPage: true });
  }
});

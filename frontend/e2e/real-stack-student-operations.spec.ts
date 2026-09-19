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

test("mobile student card: correction, stale input, attendance/cancel and exact renewal", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  page.setDefaultTimeout(20_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard\/?$/);
  await page.goto(backendUrl("/dashboard/students/"));
  await page.locator(`[data-student-opener][hx-get="/dashboard/students/${fixture.student.id}/card/"]:visible`).first().click();
  const panel = page.locator("#slide-over");
  await expect(panel.locator("[data-student-operations]")).toBeVisible();
  await panel.getByRole("button", { name: "Исправить", exact: true }).click();
  await expect(panel).toHaveAttribute("role", "dialog");
  await expect(panel.getByLabel("Осталось занятий")).toBeFocused();
  await panel.getByLabel("Осталось занятий").fill("9");
  await panel.locator('textarea[name="reason"]').fill("Сверка E2E");
  await expect(panel.locator("[data-balance-diff]")).toContainText("7 → 9");
  await page.route(`**/students/${fixture.student.id}/subscriptions/${fixture.subscription_id}/correct/`, async route => {
    await new Promise(resolve => setTimeout(resolve, 500));
    await route.continue();
  }, { times: 1 });
  await panel.getByRole("button", { name: "Сохранить исправление" }).click();
  await expect(panel.getByRole("button", { name: "Сохранить исправление" })).toBeDisabled();
  await expect(panel.getByRole("status")).toContainText("Исправление сохранено");
  backendCommand("assert_student_operations_e2e", "--phase", "corrected");

  await panel.getByRole("button", { name: "Исправить", exact: true }).click();
  await panel.getByLabel("Осталось занятий").fill("10");
  await panel.locator('textarea[name="reason"]').fill("Повторная сверка E2E");
  backendCommand("consume_student_operations_e2e");
  await panel.getByRole("button", { name: "Сохранить исправление" }).click();
  await expect(panel.getByRole("alert")).toContainText("Ваш ввод сохранён");
  await expect(panel.getByLabel("Осталось занятий")).toHaveValue("10");
  await expect(panel.locator("[data-balance-diff]")).toContainText("8 → 10");
  await panel.getByRole("button", { name: "Сохранить исправление" }).click();
  await expect(panel.getByRole("status")).toContainText("Исправление сохранено");

  await panel.getByRole("button", { name: "Отметить посещение", exact: true }).click();
  await expect(panel.locator("[data-attendance-record]")).toBeVisible();
  await expect(panel).not.toHaveClass(/htmx-settling/);
  const dateResponse = page.waitForResponse(response => response.url().endsWith("/attendance/record/")
    && response.request().method() === "POST");
  await panel.getByLabel("Дата занятия").fill(fixture.schedule.date);
  await panel.getByLabel("Дата занятия").press("Tab");
  await dateResponse;
  await panel.getByLabel("Занятие", { exact: true }).selectOption(String(fixture.schedule.id));
  await panel.getByLabel("Списать занятие из").selectOption(String(fixture.component_id));
  await panel.getByLabel("Причина исправления").fill("Посещение E2E");
  await panel.getByRole("button", { name: "Проверить посещение" }).click();
  await expect(panel.locator("[data-attendance-preview]")).toContainText("Персональное начисление: 500 ₽");
  await page.route(`**/students/${fixture.student.id}/attendance/record/`, async route => {
    await new Promise(resolve => setTimeout(resolve, 500));
    await route.continue();
  }, { times: 1 });
  await panel.getByRole("button", { name: "Отметить посещение", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Проверить посещение" })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Отметить посещение", exact: true })).toBeDisabled();
  await expect(panel.getByRole("status")).toContainText("отмечено");
  await panel.getByRole("button", { name: "Отменить отметку", exact: true }).first().click();
  await panel.getByLabel("Причина отмены").fill("Отмена E2E");
  await panel.getByRole("button", { name: "Отменить отметку", exact: true }).click();
  await expect(panel.getByRole("status")).toContainText("отменена");
  await expect(panel.locator(`[data-component-id="${fixture.component_id}"]`)).toContainText("Осталось 10 занятий");
  await expect.poll(() => panel.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("student-card-mobile.png"), fullPage: true });
  for (const width of [360, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await page.keyboard.press("Escape");
    const opener = page.locator(`[data-student-opener][hx-get="/dashboard/students/${fixture.student.id}/card/"]:visible`);
    await expect(opener).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(panel.locator("[data-student-operations]")).toBeVisible();
    await expect.poll(() => panel.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    await panel.getByRole("button", { name: "История изменений", exact: true }).click();
    await expect(panel).toContainText("Повторная сверка E2E");
    const back = panel.getByRole("button", { name: "Назад к ученику" });
    await expect(back).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(panel.locator("[data-student-operations]")).toBeVisible();
    await expect(panel.getByRole("button", { name: "История изменений", exact: true })).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath(`student-card-${width}.png`), fullPage: true });
  }

  await page.keyboard.press("Escape");
  await expect(panel).toHaveClass(/translate-x-full/);
  const cardOpener = page.locator(`[data-student-opener][hx-get="/dashboard/students/${fixture.student.id}/card/"]:visible`).first();
  await expect(cardOpener).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(panel.locator("[data-student-operations]")).toBeVisible();

  const tenantResponse = await page.request.get(backendUrl(`/dashboard/students/${fixture.foreign_student_id}/history/`));
  expect(tenantResponse.status()).toBe(404);
  await panel.locator(`[data-subscription-id="${fixture.subscription_id}"] summary`).click();
  await panel.getByRole("button", { name: "Продлить", exact: true }).click();
  await panel.getByLabel("Способ оплаты").selectOption("cash");
  await panel.getByRole("button", { name: "Записать продление" }).click();
  await expect(panel.getByRole("status")).toContainText("ожидает проверки");
  backendCommand("assert_student_operations_e2e", "--phase", "final");
  await panel.getByRole("link", { name: "Принять оплату", exact: true }).click();
  await expect(page.locator("#create-sub-form")).toBeVisible();
  await expect(page.locator('select[name="student_id"]')).toHaveValue(String(fixture.student.id));
});


test("student panel waits for its own HTMX initialization before focusing Back", async ({ page }) => {
  let cardRequests = 0;
  await page.route("http://student-panel.test/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/") {
      await route.fulfill({ contentType: "text/html", body: '<div id="slide-over-backdrop"></div><div id="slide-over"></div>' });
    } else if (path === "/card") {
      cardRequests++;
      await route.fulfill({ body: '<div data-student-dialog data-student-card><h2>Card</h2><button hx-get="/history" hx-target="#slide-over">History</button></div>' });
    } else {
      await new Promise(resolve => setTimeout(resolve, 40));
      await route.fulfill({ body: '<h2 data-student-dialog>History</h2><button data-student-back hx-get="/card" hx-target="#slide-over">Back</button>' });
    }
  });
  await page.goto("http://student-panel.test/");
  await page.addScriptTag({ path: resolve(repoRoot, "static/vendor/htmx.min.js") });
  await page.addScriptTag({ path: resolve(repoRoot, "static/js/student-operation-panel.js") });
  await page.evaluate(() => {
    const htmx = (window as unknown as {
      htmx: { config: { defaultSettleDelay: number }; ajax: (method: string, url: string, options: { target: string }) => void };
    }).htmx;
    htmx.config.defaultSettleDelay = 200;
    let firstSwap = true;
    document.addEventListener("htmx:afterSwap", () => {
      if (firstSwap) {
        firstSwap = false;
        htmx.ajax("GET", "/history", { target: "#slide-over" });
      }
    });
    htmx.ajax("GET", "/card", { target: "#slide-over" });
  });
  await page.waitForFunction(() => document.activeElement?.hasAttribute("data-student-back"));
  await expect(page.getByRole("button", { name: "Back", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect.poll(() => cardRequests).toBe(2);
  await expect(page.locator("[data-student-card]")).toBeVisible();
});

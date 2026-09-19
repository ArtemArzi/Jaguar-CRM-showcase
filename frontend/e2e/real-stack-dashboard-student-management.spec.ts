import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface DashboardStudentManagementFixture {
  owner: {
    email: string;
    password: string;
  };
  manual_student: {
    first_name: string;
    last_name: string;
    full_name: string;
    phone: string;
    is_child: boolean;
    source: string;
    initial_note: string;
    edited_first_name: string;
    edited_last_name: string;
    edited_full_name: string;
    edited_email: string;
    contraindications: string;
    follow_up_note: string;
  };
  import_file: {
    filename: string;
    base64: string;
  };
  imported_student: {
    full_name: string;
    phone: string;
    preview_total: number;
    preview_errors_count: number;
    preview_valid_count: number;
  };
  control_student: {
    full_name: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a dashboard student management fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): DashboardStudentManagementFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<DashboardStudentManagementFixture>;
  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.manual_student?.full_name || !data.manual_student.phone || !data.manual_student.initial_note) {
    throw new Error("Fixture manual student data is required.");
  }
  if (data.manual_student.is_child !== true) {
    throw new Error("Fixture manual student must be a child for parent invite coverage.");
  }
  if (!data.import_file?.filename || !data.import_file.base64) {
    throw new Error("Fixture import file data is required.");
  }
  if (!data.imported_student?.full_name || !data.imported_student.phone) {
    throw new Error("Fixture imported student data is required.");
  }
  if (!Number.isFinite(data.imported_student.preview_valid_count)) {
    throw new Error("Fixture import preview expectations are required.");
  }
  if (!data.control_student?.full_name) {
    throw new Error("Fixture control student data is required.");
  }
  return data as DashboardStudentManagementFixture;
}

function responseFor(pathname: string, method = "GET"): (response: Response) => boolean {
  return (response: Response) => {
    const url = new URL(response.url());
    return url.pathname === pathname && response.request().method() === method;
  };
}

function slideOver(page: Page) {
  return page.locator("#slide-over");
}

function rowForStudent(page: Page, name: string) {
  return page.locator("tr").filter({ hasText: name }).first();
}

async function loginAsOwner(page: Page, fixture: DashboardStudentManagementFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/?$/);
}

async function createStudent(page: Page, fixture: DashboardStudentManagementFixture): Promise<void> {
  const student = fixture.manual_student;
  await page.goto(backendUrl("/dashboard/students/"));
  await expect(page.getByRole("heading", { name: "УЧЕНИКИ" })).toBeVisible();
  await expect(page.getByText(fixture.control_student.full_name)).toHaveCount(0);

  const formResponse = page.waitForResponse(responseFor("/dashboard/students/create/"), { timeout: 20_000 });
  await page.getByRole("button", { name: "ДОБАВИТЬ" }).click();
  expect((await formResponse).ok()).toBe(true);

  const panel = slideOver(page);
  await expect(panel.getByRole("heading", { name: "НОВЫЙ УЧЕНИК" })).toBeVisible();
  await panel.locator('input[name="first_name"]').fill(student.first_name);
  await panel.locator('input[name="last_name"]').fill(student.last_name);
  await panel.locator('input[name="phone"]').fill(student.phone);
  await panel.locator('select[name="source"]').selectOption(student.source);
  await panel.locator('textarea[name="note"]').fill(student.initial_note);
  await panel.locator('input[name="is_child"]').check();

  const createResponse = page.waitForResponse(responseFor("/dashboard/students/create/", "POST"), { timeout: 20_000 });
  await panel.getByRole("button", { name: "СОЗДАТЬ УЧЕНИКА" }).click();
  expect((await createResponse).ok()).toBe(true);
  await expect(rowForStudent(page, student.full_name)).toBeVisible({ timeout: 20_000 });
}

async function importStudent(page: Page, fixture: DashboardStudentManagementFixture): Promise<void> {
  const formResponse = page.waitForResponse(responseFor("/dashboard/students/import/"), { timeout: 20_000 });
  await page.getByRole("button", { name: "ИМПОРТ" }).click();
  expect((await formResponse).ok()).toBe(true);

  const panel = slideOver(page);
  await expect(panel.getByRole("heading", { name: "ИМПОРТ ИЗ EXCEL" })).toBeVisible();
  await panel.locator('input[type="file"]').setInputFiles({
    name: fixture.import_file.filename,
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: Buffer.from(fixture.import_file.base64, "base64"),
  });

  const previewResponse = page.waitForResponse(responseFor("/dashboard/students/import/", "POST"), {
    timeout: 20_000,
  });
  await panel.getByRole("button", { name: "ЗАГРУЗИТЬ И ПРЕДПРОСМОТР" }).click();
  expect((await previewResponse).ok()).toBe(true);
  await expect(panel.getByRole("heading", { name: "Предпросмотр импорта" })).toBeVisible();
  await expect(panel.getByText(`${fixture.imported_student.preview_total} строк найдено`)).toBeVisible();
  await expect(panel.getByText(`${fixture.imported_student.preview_errors_count} с ошибками`)).toBeVisible();
  await expect(panel.getByText(fixture.imported_student.full_name)).toBeVisible();
  await expect(panel.getByText("OK")).toBeVisible();
  await expect(panel.getByText("Телефон уже существует")).toBeVisible();

  const confirmResponse = page.waitForResponse(responseFor("/dashboard/students/import/confirm/", "POST"), {
    timeout: 20_000,
  });
  await panel.getByRole("button", { name: new RegExp(`Импортировать \\(${fixture.imported_student.preview_valid_count} строк`) }).click();
  expect((await confirmResponse).status()).toBe(204);
  await expect(page).toHaveURL(/\/dashboard\/students\/?$/, { timeout: 20_000 });
  await expect(rowForStudent(page, fixture.imported_student.full_name)).toBeVisible({ timeout: 20_000 });
}

async function searchAndFilterStudent(page: Page, fixture: DashboardStudentManagementFixture): Promise<void> {
  const searchResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/dashboard/students/" && url.searchParams.get("q") === fixture.manual_student.first_name;
  }, { timeout: 20_000 });
  const searchInput = page.getByPlaceholder("Поиск по имени или телефону...");
  await searchInput.click();
  await searchInput.pressSequentially(fixture.manual_student.first_name, { delay: 5 });
  expect((await searchResponse).ok()).toBe(true);
  await expect(rowForStudent(page, fixture.manual_student.full_name)).toBeVisible();
  await expect(page.getByText(fixture.imported_student.full_name)).toHaveCount(0);

  await page.goto(backendUrl(`/dashboard/students/?status=lead&q=${encodeURIComponent(fixture.manual_student.first_name)}`));
  const filteredRow = rowForStudent(page, fixture.manual_student.full_name);
  await expect(filteredRow).toBeVisible();
  await expect(filteredRow).toContainText("ЛИД");
}

async function openStudentCard(page: Page, fixture: DashboardStudentManagementFixture): Promise<void> {
  const cardResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname.startsWith("/dashboard/students/") && url.pathname.endsWith("/card/");
  }, { timeout: 20_000 });
  await rowForStudent(page, fixture.manual_student.full_name).click();
  expect((await cardResponse).ok()).toBe(true);

  const panel = slideOver(page);
  await expect(panel).toContainText(fixture.manual_student.full_name.toUpperCase());
  await expect(panel).toContainText(fixture.manual_student.phone);
  await expect(panel).toContainText("Нет активных абонементов или разовых тренировок");
  await expect(panel).toContainText(fixture.manual_student.initial_note);
  await expect(panel).toContainText("ЛИЧНЫЙ КАБИНЕТ");
  await expect(panel).toContainText("Доступ откроется после активной оплаченной подписки.");
  await expect(panel).toContainText("ДОКУМЕНТЫ");
  await expect(panel).toContainText("Для этого ученика не настроены документы");
  await expect(panel.getByRole("button", { name: "Связать родителя" })).toBeVisible();
}

async function createParentInvite(page: Page): Promise<void> {
  const panel = slideOver(page);
  const inviteResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname.startsWith("/dashboard/students/") && url.pathname.endsWith("/parent-invite/");
  }, { timeout: 20_000 });
  await panel.getByRole("button", { name: "Связать родителя" }).click();
  expect((await inviteResponse).ok()).toBe(true);
  await expect(panel.locator("#parent-invite-panel")).toContainText("Ссылка для привязки родителя");
  await expect(panel.locator("#parent-invite-panel")).toContainText("не выдаёт пароль");
  await expect(panel.locator("#parent-invite-panel")).toContainText("Действует до");
}

async function editStudent(page: Page, fixture: DashboardStudentManagementFixture): Promise<void> {
  const student = fixture.manual_student;
  const panel = slideOver(page);

  const editFormResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname.startsWith("/dashboard/students/") && url.pathname.endsWith("/edit/");
  }, { timeout: 20_000 });
  await panel.getByText("РЕДАКТИРОВАТЬ").click();
  expect((await editFormResponse).ok()).toBe(true);
  await expect(panel.getByRole("heading", { name: "РЕДАКТИРОВАТЬ УЧЕНИКА" })).toBeVisible();

  await panel.locator('input[name="first_name"]').fill(student.edited_first_name);
  await panel.locator('input[name="last_name"]').fill(student.edited_last_name);
  await panel.locator('input[name="email"]').fill(student.edited_email);
  await panel.locator('textarea[name="contraindications"]').fill(student.contraindications);

  const editResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname.startsWith("/dashboard/students/") && url.pathname.endsWith("/edit/") && response.request().method() === "POST";
  }, { timeout: 20_000 });
  await panel.getByRole("button", { name: "СОХРАНИТЬ" }).click();
  expect((await editResponse).ok()).toBe(true);
  await expect(panel).toContainText(student.edited_full_name.toUpperCase());
  await expect(panel).toContainText(student.edited_email);
  await expect(panel).toContainText(student.contraindications);
}

async function changeStatusAndAddNote(page: Page, fixture: DashboardStudentManagementFixture): Promise<void> {
  const panel = slideOver(page);
  const statusResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname.startsWith("/dashboard/students/") && url.pathname.endsWith("/status/");
  }, { timeout: 20_000 });
  const statusSelect = panel.locator('select[name="new_status"]');
  await statusSelect.selectOption("trial");
  await statusSelect.dispatchEvent("change");
  expect((await statusResponse).ok()).toBe(true);
  await expect(panel).toContainText("ПРОБНОЕ");

  const notes = panel.locator("[data-notes]");
  await notes.getByRole("button", { name: "ДОБАВИТЬ" }).click();
  await notes.locator('textarea[name="text"]').fill(fixture.manual_student.follow_up_note);

  const noteResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname.startsWith("/dashboard/students/") && url.pathname.endsWith("/note/");
  }, { timeout: 20_000 });
  await notes.getByRole("button", { name: "СОХРАНИТЬ" }).click();
  expect((await noteResponse).ok()).toBe(true);
  await expect(slideOver(page)).toContainText(fixture.manual_student.follow_up_note);
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
    : execFileSync(pythonBin, ["manage.py", "assert_dashboard_student_management_e2e", "--fixture", fixturePath], {
        cwd: repoRoot,
        encoding: "utf8",
        env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
        stdio: ["ignore", "pipe", "pipe"],
      });

  const result = JSON.parse(output) as BackendAssertResponse;
  expect(result.ok).toBe(true);
}

test("real-stack owner creates imports edits filters and annotates students", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsOwner(page, fixture);
  await createStudent(page, fixture);
  await importStudent(page, fixture);
  await searchAndFilterStudent(page, fixture);
  await openStudentCard(page, fixture);
  await createParentInvite(page);
  await editStudent(page, fixture);
  await changeStatusAndAddNote(page, fixture);

  runBackendAssert(fixturePath);
});

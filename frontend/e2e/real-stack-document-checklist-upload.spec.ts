import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface DocumentChecklistUploadFixture {
  owner: {
    email: string;
    password: string;
  };
  student: {
    student_id: number;
    name: string;
  };
  foreign_student: {
    student_id: number;
    name: string;
  };
  student_user: {
    email: string;
    password: string;
  };
  parent: {
    email: string;
    password: string;
  };
  expected: {
    document_name: string;
    student_upload_document_name: string;
    staff_note: string;
    upload_filename: string;
    upload_content: string;
    student_upload_filename: string;
    student_upload_content: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a document checklist upload fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): DocumentChecklistUploadFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<DocumentChecklistUploadFixture>;

  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.student?.student_id || !data.student.name) {
    throw new Error("Fixture student data is required.");
  }
  if (!data.foreign_student?.student_id || !data.foreign_student.name) {
    throw new Error("Fixture foreign student data is required.");
  }
  if (!data.student_user?.email || !data.student_user.password) {
    throw new Error("Fixture student credentials are required.");
  }
  if (!data.parent?.email || !data.parent.password) {
    throw new Error("Fixture parent credentials are required.");
  }
  if (
    !data.expected?.document_name ||
    !data.expected.student_upload_document_name ||
    !data.expected.staff_note ||
    !data.expected.upload_filename ||
    !data.expected.upload_content ||
    !data.expected.student_upload_filename ||
    !data.expected.student_upload_content
  ) {
    throw new Error("Fixture expected document values are required.");
  }

  return data as DocumentChecklistUploadFixture;
}

function isDocumentMarkResponse(studentId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/dashboard/students/${studentId}/documents/mark/` &&
      response.request().method() === "POST"
    );
  };
}

function isDocumentUploadResponse(studentId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/dashboard/students/${studentId}/documents/upload/` &&
      response.request().method() === "POST"
    );
  };
}

function isApiDocumentUploadResponse(studentId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return (
      url.pathname === `/api/documents/students/${studentId}/upload/` &&
      response.request().method() === "POST"
    );
  };
}

function isGetTo(pathname: string) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === pathname && response.request().method() === "GET";
  };
}

function documentBlock(page: Page) {
  return page.locator("#student-documents-block").last();
}

function dashboardDocumentItem(page: Page, fixture: DocumentChecklistUploadFixture) {
  return documentBlock(page)
    .locator(":scope > div.px-5.py-4")
    .filter({ hasText: fixture.expected.document_name })
    .first();
}

async function loginAsOwner(page: Page, fixture: DocumentChecklistUploadFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

async function loginAsStudent(page: Page, fixture: DocumentChecklistUploadFixture): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(fixture.student_user.email);
  await page.getByLabel("Пароль").fill(fixture.student_user.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/student/);
}

async function loginAsParent(page: Page, fixture: DocumentChecklistUploadFixture): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(fixture.parent.email);
  await page.getByLabel("Пароль").fill(fixture.parent.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/parent/);
}

async function openStudentCard(page: Page, fixture: DocumentChecklistUploadFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/students/"));
  await expect(page.getByRole("heading", { name: "УЧЕНИКИ" })).toBeVisible();
  const row = page.locator("tr").filter({ hasText: fixture.student.name });
  await expect(row).toBeVisible();
  await row.click();
  await expect(page.locator("#slide-over")).toContainText(fixture.student.name);
  await expect(documentBlock(page)).toContainText("ДОКУМЕНТЫ");
  await expect(documentBlock(page)).toContainText(fixture.expected.document_name);
  await expect(documentBlock(page)).toContainText("ТРЕБУЕТСЯ");
}

async function markDocumentProvided(page: Page, fixture: DocumentChecklistUploadFixture): Promise<void> {
  const item = dashboardDocumentItem(page, fixture);
  const markResponse = page.waitForResponse(isDocumentMarkResponse(fixture.student.student_id), {
    timeout: 20_000,
  });
  await item.getByRole("button", { name: /ОТМЕТИТЬ/ }).click();
  const response = await markResponse;
  expect(response.status()).toBe(200);
  await expect(item).toContainText("ПРЕДОСТАВЛЕН");
}

async function uploadDocument(page: Page, fixture: DocumentChecklistUploadFixture): Promise<void> {
  const item = dashboardDocumentItem(page, fixture);
  await item.getByRole("button", { name: /^ЗАГРУЗИТЬ$/ }).click();
  const uploadForm = item.locator("form").filter({ hasText: fixture.expected.document_name }).last();
  const fileInput = uploadForm.locator('input[type="file"]');
  await fileInput.setInputFiles({
    name: fixture.expected.upload_filename,
    mimeType: "application/pdf",
    buffer: Buffer.from(fixture.expected.upload_content),
  });

  const uploadResponse = page.waitForResponse(isDocumentUploadResponse(fixture.student.student_id), {
    timeout: 20_000,
  });
  await uploadForm.getByRole("button", { name: "ЗАГРУЗИТЬ ФАЙЛ" }).click();
  const response = await uploadResponse;
  expect(response.status()).toBe(200);
  await expect(documentBlock(page)).toContainText("ЗАГРУЖЕН");
  await expect(documentBlock(page).getByRole("link", { name: "Открыть файл" })).toBeVisible();
}

async function assertStudentProfileChecklist(
  page: Page,
  fixture: DocumentChecklistUploadFixture,
): Promise<void> {
  const checklistResponsePromise = page.waitForResponse(
    isGetTo(`/api/documents/students/${fixture.student.student_id}/checklist/`),
    { timeout: 20_000 },
  );
  await page.goto("/student/profile");
  const checklistResponse = await checklistResponsePromise;
  expect(checklistResponse.ok()).toBe(true);

  await expect(page.getByRole("heading", { name: fixture.student.name })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Документы" })).toBeVisible();
  const dashboardUploadedCard = page
    .locator("div.rounded-2xl.border")
    .filter({ hasText: fixture.expected.document_name })
    .first();
  await expect(dashboardUploadedCard).toBeVisible();
  await expect(dashboardUploadedCard.getByText("Загружен", { exact: true })).toBeVisible();
  await expect(page.getByText(fixture.expected.staff_note)).toHaveCount(0);

  const uploadCard = page
    .locator("div.rounded-2xl.border")
    .filter({ hasText: fixture.expected.student_upload_document_name })
    .first();
  await expect(uploadCard).toBeVisible();
  await expect(uploadCard.getByText("Не загружен", { exact: true })).toBeVisible();

  const fileChooserPromise = page.waitForEvent("filechooser");
  await uploadCard.getByRole("button", { name: "Загрузить" }).click();
  const fileChooser = await fileChooserPromise;
  const uploadResponsePromise = page.waitForResponse(
    isApiDocumentUploadResponse(fixture.student.student_id),
    { timeout: 20_000 },
  );
  const refreshResponsePromise = page.waitForResponse(
    isGetTo(`/api/documents/students/${fixture.student.student_id}/checklist/`),
    { timeout: 20_000 },
  );
  await fileChooser.setFiles({
    name: fixture.expected.student_upload_filename,
    mimeType: "application/pdf",
    buffer: Buffer.from(fixture.expected.student_upload_content),
  });
  const uploadResponse = await uploadResponsePromise;
  expect(uploadResponse.ok()).toBe(true);
  const refreshResponse = await refreshResponsePromise;
  expect(refreshResponse.ok()).toBe(true);
  await expect(page.getByText("Документ загружен")).toBeVisible();
  await expect(uploadCard.getByText("Загружен", { exact: true })).toBeVisible();
  await expect(uploadCard.getByRole("button", { name: "Загрузить" })).toHaveCount(0);
}

async function assertParentChildChecklist(
  page: Page,
  fixture: DocumentChecklistUploadFixture,
): Promise<void> {
  const childResponsePromise = page.waitForResponse(
    isGetTo(`/api/parents/children/${fixture.student.student_id}/`),
    { timeout: 20_000 },
  );
  await page.goto(`/parent/child/${fixture.student.student_id}`);
  const childResponse = await childResponsePromise;
  expect(childResponse.ok()).toBe(true);

  await expect(page.getByRole("heading", { name: fixture.student.name })).toBeVisible();
  const documentsSection = page.getByRole("region", { name: "Документы ребёнка" });
  await expect(documentsSection).toContainText(fixture.expected.document_name);
  await expect(documentsSection).toContainText("Файл загружен");
  await expect(documentsSection).toContainText("Обязательный");
  await expect(documentsSection.getByText(fixture.expected.staff_note)).toHaveCount(0);
  await expect(documentsSection.getByRole("button")).toHaveCount(0);
  await expect(documentsSection.getByRole("link")).toHaveCount(0);
}

async function assertParentForeignChildDenied(
  page: Page,
  fixture: DocumentChecklistUploadFixture,
): Promise<void> {
  const childResponsePromise = page.waitForResponse(
    isGetTo(`/api/parents/children/${fixture.foreign_student.student_id}/`),
    { timeout: 20_000 },
  );
  await page.goto(`/parent/child/${fixture.foreign_student.student_id}`);
  const childResponse = await childResponsePromise;
  expect(childResponse.status()).toBe(404);

  await expect(page.getByText("Не удалось загрузить профиль")).toBeVisible();
  await expect(page.getByRole("region", { name: "Документы ребёнка" })).toHaveCount(0);
  await expect(page.getByText(fixture.foreign_student.name)).toHaveCount(0);
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
        ["manage.py", "assert_document_checklist_upload_e2e", "--fixture", fixturePath],
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

test("real-stack owner uploads dashboard document and student uploads profile document", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsOwner(page, fixture);
  await openStudentCard(page, fixture);
  await markDocumentProvided(page, fixture);
  await uploadDocument(page, fixture);
  await loginAsStudent(page, fixture);
  await assertStudentProfileChecklist(page, fixture);
  runBackendAssert(fixturePath);
  await loginAsParent(page, fixture);
  await assertParentChildChecklist(page, fixture);
  await assertParentForeignChildDenied(page, fixture);
});

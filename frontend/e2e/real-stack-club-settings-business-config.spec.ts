import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface ClubSettingsBusinessConfigFixture {
  owner: {
    email: string;
    password: string;
  };
  expected: {
    club_name_display: string;
    primary_color: string;
    timezone: string;
    freeze_max_days: number;
    freeze_max_count: number;
    min_trainings_to_freeze: number;
    location_initial_name: string;
    location_initial_address: string;
    location_name: string;
    location_address: string;
    location_delete_name: string;
    location_delete_address: string;
    grade_system_name: string;
    grade_initial_name: string;
    grade_final_name: string;
    grade_final_order: number;
    grade_final_min_trainings: number;
    grade_delete_name: string;
    grade_delete_order: number;
    grade_system_delete_name: string;
    document_type_initial_name: string;
    document_type_name: string;
    document_type_description: string;
    document_type_scope: string;
    document_type_is_required: boolean;
    training_type_initial_name: string;
    training_type_name: string;
    personal_training_type_name: string;
    drop_in_price: string;
    tariff_initial_name: string;
    tariff_initial_price: string;
    tariff_initial_trainings_limit: number;
    tariff_initial_duration_days: number;
    tariff_initial_description: string;
    tariff_name: string;
    tariff_price: string;
    tariff_trainings_limit: number;
    tariff_duration_days: number;
    tariff_description: string;
    personal_tariff_name: string;
    personal_tariff_price: string;
    personal_tariff_duration_days: number;
    discount_initial_name: string;
    discount_initial_value: string;
    discount_name: string;
    discount_type: string;
    discount_value: string;
  };
}

interface BackendAssertResponse {
  ok?: boolean;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");
const logoPngBuffer = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGM4EmUNAAM/AVrgZ20NAAAAAElFTkSuQmCC",
  "base64",
);

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a club settings fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): ClubSettingsBusinessConfigFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<ClubSettingsBusinessConfigFixture>;

  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (
    !data.expected?.location_name ||
    !data.expected.grade_system_name ||
    !data.expected.document_type_name ||
    !data.expected.training_type_initial_name ||
    !data.expected.training_type_name ||
    !data.expected.tariff_name ||
    !data.expected.personal_training_type_name ||
    !data.expected.personal_tariff_name
  ) {
    throw new Error("Fixture business config expectations are required.");
  }

  return data as ClubSettingsBusinessConfigFixture;
}

function responseFor(pathname: string): (response: Response) => boolean {
  return (response: Response) => {
    const url = new URL(response.url());
    return url.pathname === pathname && response.request().method() === "POST";
  };
}

function responseMatching(pathname: RegExp): (response: Response) => boolean {
  return (response: Response) => {
    const url = new URL(response.url());
    return pathname.test(url.pathname) && response.request().method() === "POST";
  };
}

function responseMatchingMethod(pathname: RegExp, method: string): (response: Response) => boolean {
  return (response: Response) => {
    const url = new URL(response.url());
    return pathname.test(url.pathname) && response.request().method() === method;
  };
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function settingsRow(page: Page, name: string) {
  const label = page
    .locator("#content span")
    .filter({ hasText: new RegExp(`^${escapeRegExp(name)}$`) })
    .first();

  return label.locator("xpath=ancestor::div[contains(@class, 'py-3')][1]");
}

async function closeSlideOverIfOpen(page: Page): Promise<void> {
  const panel = page.locator("#slide-over");
  const className = (await panel.getAttribute("class")) ?? "";
  if (!className.includes("translate-x-full")) {
    await panel.locator("button[type='button']").first().click();
  }
}

async function loginAsOwner(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/?$/);
}

async function saveGeneralSettings(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  await page.goto(backendUrl("/dashboard/settings/general/"));
  await expect(page.getByRole("heading", { name: "Настройки" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Внешний вид клуба" })).toBeVisible();
  await expect(page.getByText("PNG, JPG или WEBP. Максимум 2 МБ.")).toBeVisible();

  await page.locator("input[name='club_name_display']").fill(expected.club_name_display);
  await page.locator("#primary-hex").fill(expected.primary_color);
  await page.locator("select[name='timezone']").selectOption(expected.timezone);

  const freezeToggle = page.locator("input[name='freeze_enabled']");
  if (!(await freezeToggle.isChecked())) {
    await freezeToggle.check();
  }
  await page.locator("input[name='freeze_max_days']").fill(String(expected.freeze_max_days));
  await page.locator("input[name='freeze_max_count']").fill(String(expected.freeze_max_count));
  await page.locator("input[name='min_trainings_to_freeze']").fill(String(expected.min_trainings_to_freeze));
  await page.locator("input[name='logo_file']").setInputFiles({
    name: "club-settings-logo.png",
    mimeType: "image/png",
    buffer: logoPngBuffer,
  });
  await expect(page.locator("#logo-filename")).toContainText("Выбран: club-settings-logo.png");

  const saveResponse = page.waitForResponse(responseFor("/dashboard/settings/general/"), {
    timeout: 20_000,
  });
  await page.locator("form[hx-post='/dashboard/settings/general/'] button[type='submit']").click();
  const uploadResponse = await saveResponse;
  expect(uploadResponse.status()).toBe(204);
  expect(uploadResponse.headers()["hx-refresh"]).toBe("true");
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await expect(page.locator("#logo-filename")).toContainText("Текущий файл:", { timeout: 20_000 });
  await expect(page.locator("input[name='logo_remove']")).toBeVisible();

  const logoSrc = await page.locator("#logo-preview").getAttribute("src");
  expect(logoSrc).toContain("/media/club_logos/");
  const logoUrl = new URL(logoSrc ?? "", backendUrl("/")).toString();
  expect((await page.request.get(logoUrl)).status()).toBe(200);

  await page.locator("input[name='logo_remove']").check();
  const removeResponse = page.waitForResponse(responseFor("/dashboard/settings/general/"), {
    timeout: 20_000,
  });
  await page.locator("form[hx-post='/dashboard/settings/general/'] button[type='submit']").click();
  const removeResult = await removeResponse;
  expect(removeResult.status()).toBe(204);
  expect(removeResult.headers()["hx-refresh"]).toBe("true");
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await expect(page.locator("#logo-filename")).toContainText("PNG, JPG или WEBP. Максимум 2 МБ.", {
    timeout: 20_000,
  });
  await expect(page.locator("input[name='logo_remove']")).toHaveCount(0);
  expect((await page.request.get(logoUrl)).status()).toBe(404);
}

async function createLocation(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  await page.goto(backendUrl("/dashboard/settings/catalog/"));
  await expect(page.getByRole("heading", { name: "Залы" })).toBeVisible();

  await page.getByRole("button", { name: /Добавить зал/ }).click();
  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "НОВЫЙ ЗАЛ" })).toBeVisible();
  await panel.locator("input[name='name']").fill(expected.location_initial_name);
  await panel.locator("textarea[name='address']").fill(expected.location_initial_address);

  const createResponse = page.waitForResponse(responseFor("/dashboard/settings/catalog/locations/form/"), {
    timeout: 20_000,
  });
  await panel.getByRole("button", { name: "СОЗДАТЬ ЗАЛ" }).click();
  expect((await createResponse).status()).toBe(204);
  await expect(page.getByText(expected.location_initial_name)).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(expected.location_initial_address)).toBeVisible();
}

async function editLocation(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  const locationRow = settingsRow(page, expected.location_initial_name);
  await expect(locationRow).toBeVisible();

  const openEditResponse = page.waitForResponse(
    responseMatchingMethod(/^\/dashboard\/settings\/catalog\/locations\/\d+\/form\/$/, "GET"),
    { timeout: 20_000 },
  );
  await locationRow.getByTitle("Редактировать").click();
  expect((await openEditResponse).ok()).toBe(true);

  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "РЕДАКТИРОВАТЬ ЗАЛ" })).toBeVisible();
  await panel.locator("input[name='name']").fill(expected.location_name);
  await panel.locator("textarea[name='address']").fill(expected.location_address);

  const editResponse = page.waitForResponse(
    responseMatching(/^\/dashboard\/settings\/catalog\/locations\/\d+\/form\/$/),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "СОХРАНИТЬ" }).click();
  expect((await editResponse).status()).toBe(204);
  await expect(page.getByText(expected.location_name)).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(expected.location_address)).toBeVisible();
  await expect(page.getByText(expected.location_initial_name)).toHaveCount(0);
}

async function createAndDeleteTemporaryLocation(
  page: Page,
  fixture: ClubSettingsBusinessConfigFixture,
): Promise<void> {
  const expected = fixture.expected;
  await page.getByRole("button", { name: /Добавить зал/ }).click();
  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "НОВЫЙ ЗАЛ" })).toBeVisible();
  await panel.locator("input[name='name']").fill(expected.location_delete_name);
  await panel.locator("textarea[name='address']").fill(expected.location_delete_address);

  const createResponse = page.waitForResponse(responseFor("/dashboard/settings/catalog/locations/form/"), {
    timeout: 20_000,
  });
  await panel.getByRole("button", { name: "СОЗДАТЬ ЗАЛ" }).click();
  expect((await createResponse).status()).toBe(204);
  await expect(page.getByText(expected.location_delete_name)).toBeVisible({ timeout: 20_000 });

  const temporaryLocationRow = settingsRow(page, expected.location_delete_name);
  const deleteResponse = page.waitForResponse(
    responseMatching(/^\/dashboard\/settings\/catalog\/locations\/\d+\/delete\/$/),
    { timeout: 20_000 },
  );
  await temporaryLocationRow.getByTitle("Удалить").click();
  await confirmAction(page);
  expect((await deleteResponse).status()).toBe(204);
  await expect(page.getByText(expected.location_delete_name)).toHaveCount(0);
}

async function createGradeSystem(page: Page, discipline: string): Promise<void> {
  await page.goto(backendUrl("/dashboard/settings/catalog/"));
  await expect(page.getByRole("heading", { name: "Системы аттестации" })).toBeVisible();

  await page.getByRole("button", { name: /Добавить систему/ }).click();
  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "НОВАЯ СИСТЕМА АТТЕСТАЦИИ" })).toBeVisible();
  await panel.locator("input[name='discipline']").fill(discipline);

  const createResponse = page.waitForResponse(responseFor("/dashboard/settings/catalog/grades/form/"), {
    timeout: 20_000,
  });
  await panel.getByRole("button", { name: "СОЗДАТЬ СИСТЕМУ" }).click();
  expect((await createResponse).status()).toBe(204);
  await expect(page.getByText(discipline).first()).toBeVisible({ timeout: 20_000 });
}

async function createGrade(
  page: Page,
  discipline: string,
  name: string,
  order: number,
  minTrainings: number,
): Promise<void> {
  const systemCard = page.locator("div.mb-4.border").filter({ hasText: discipline });
  await expect(systemCard).toBeVisible();
  await systemCard.getByRole("button", { name: "Уровень" }).click();

  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "НОВЫЙ УРОВЕНЬ" })).toBeVisible();
  await panel.locator("input[name='name']").fill(name);
  await panel.locator("input[name='order']").fill(String(order));
  await panel.locator("input[name='min_trainings']").fill(String(minTrainings));

  const createResponse = page.waitForResponse(
    responseMatching(/^\/dashboard\/settings\/catalog\/grades\/\d+\/grade\/form\/$/),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "СОЗДАТЬ УРОВЕНЬ" }).click();
  expect((await createResponse).status()).toBe(204);
  await expect(page.getByText(name).first()).toBeVisible({ timeout: 20_000 });
}

async function editGrade(
  page: Page,
  fromName: string,
  toName: string,
  order: number,
  minTrainings: number,
): Promise<void> {
  const gradeRow = page.locator("div.flex.items-center.justify-between").filter({ hasText: fromName }).first();
  await expect(gradeRow).toBeVisible();
  await gradeRow.getByTitle("Редактировать").click();

  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "РЕДАКТИРОВАТЬ УРОВЕНЬ" })).toBeVisible();
  await panel.locator("input[name='name']").fill(toName);
  await panel.locator("input[name='order']").fill(String(order));
  await panel.locator("input[name='min_trainings']").fill(String(minTrainings));

  const editResponse = page.waitForResponse(
    responseMatching(/^\/dashboard\/settings\/catalog\/grades\/\d+\/grade\/\d+\/form\/$/),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "СОХРАНИТЬ" }).click();
  expect((await editResponse).status()).toBe(204);
  await expect(page.getByText(toName).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(`${minTrainings} тренировок`).first()).toBeVisible();
  await expect(page.getByText(fromName)).toHaveCount(0);
}

async function deleteGrade(page: Page, name: string): Promise<void> {
  const gradeRow = page.locator("div.flex.items-center.justify-between").filter({ hasText: name }).first();
  await expect(gradeRow).toBeVisible();

  const deleteResponse = page.waitForResponse(
    responseMatching(/^\/dashboard\/settings\/catalog\/grades\/\d+\/grade\/\d+\/delete\/$/),
    { timeout: 20_000 },
  );
  await gradeRow.getByTitle("Удалить").click();
  await confirmAction(page);
  expect((await deleteResponse).status()).toBe(204);
  await expect(page.getByText(name)).toHaveCount(0);
}

async function deleteGradeSystem(page: Page, discipline: string): Promise<void> {
  const systemCard = page.locator("div.mb-4.border").filter({ hasText: discipline });
  await expect(systemCard).toBeVisible();

  const deleteResponse = page.waitForResponse(
    responseMatching(/^\/dashboard\/settings\/catalog\/grades\/\d+\/delete\/$/),
    { timeout: 20_000 },
  );
  await systemCard.getByTitle("Удалить систему").click();
  await confirmAction(page);
  expect((await deleteResponse).status()).toBe(204);
  await expect(page.getByText(discipline)).toHaveCount(0);
}

async function manageGradeCatalog(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  await createGradeSystem(page, expected.grade_system_name);
  await createGrade(
    page,
    expected.grade_system_name,
    expected.grade_initial_name,
    expected.grade_final_order,
    0,
  );
  await editGrade(
    page,
    expected.grade_initial_name,
    expected.grade_final_name,
    expected.grade_final_order,
    expected.grade_final_min_trainings,
  );
  await createGrade(
    page,
    expected.grade_system_name,
    expected.grade_delete_name,
    expected.grade_delete_order,
    0,
  );
  await deleteGrade(page, expected.grade_delete_name);
  await createGradeSystem(page, expected.grade_system_delete_name);
  await deleteGradeSystem(page, expected.grade_system_delete_name);
}

async function manageDocumentTypes(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  await page.goto(backendUrl("/dashboard/settings/documents/"));
  await expect(page.getByRole("heading", { name: "Документы клуба" })).toBeVisible();

  const openCreateResponse = page.waitForResponse(
    responseMatchingMethod(/^\/dashboard\/settings\/documents\/types\/form\/$/, "GET"),
    { timeout: 20_000 },
  );
  await page.getByRole("button", { name: /Добавить документ/ }).click();
  expect((await openCreateResponse).ok()).toBe(true);

  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "НОВЫЙ ТИП ДОКУМЕНТА" })).toBeVisible();
  await panel.locator("input[name='name']").fill(expected.document_type_initial_name);
  await panel.locator("textarea[name='description']").fill("Initial club settings E2E document");
  await panel.locator("select[name='scope']").selectOption("all");
  const requiredToggle = panel.locator("input[name='is_required']");
  if (!(await requiredToggle.isChecked())) {
    await requiredToggle.check();
  }

  const createResponse = page.waitForResponse(responseFor("/dashboard/settings/documents/types/form/"), {
    timeout: 20_000,
  });
  await panel.getByRole("button", { name: "СОЗДАТЬ ДОКУМЕНТ" }).click();
  expect((await createResponse).ok()).toBe(true);
  await expect(page.getByText(expected.document_type_initial_name).first()).toBeVisible({ timeout: 20_000 });
  await closeSlideOverIfOpen(page);

  const initialRow = settingsRow(page, expected.document_type_initial_name);
  await expect(initialRow).toBeVisible();
  const openEditResponse = page.waitForResponse(
    responseMatchingMethod(/^\/dashboard\/settings\/documents\/types\/\d+\/form\/$/, "GET"),
    { timeout: 20_000 },
  );
  await initialRow.getByRole("button", { name: "Изменить" }).click();
  expect((await openEditResponse).ok()).toBe(true);
  await expect(panel.getByRole("heading", { name: "РЕДАКТИРОВАТЬ ДОКУМЕНТ" })).toBeVisible();

  await panel.locator("input[name='name']").fill(expected.document_type_name);
  await panel.locator("textarea[name='description']").fill(expected.document_type_description);
  await panel.locator("select[name='scope']").selectOption(expected.document_type_scope);
  if (expected.document_type_is_required) {
    await requiredToggle.check();
  } else {
    await requiredToggle.uncheck();
  }

  const editResponse = page.waitForResponse(
    responseMatching(/^\/dashboard\/settings\/documents\/types\/\d+\/form\/$/),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "СОХРАНИТЬ" }).click();
  expect((await editResponse).ok()).toBe(true);
  await expect(page.getByText(expected.document_type_name).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(expected.document_type_initial_name)).toHaveCount(0);
  await closeSlideOverIfOpen(page);

  const finalRow = settingsRow(page, expected.document_type_name);
  await expect(finalRow.getByText("Только детям")).toBeVisible();
  await expect(finalRow.getByText("Необязательный")).toBeVisible();
  const toggleResponse = page.waitForResponse(
    responseMatching(/^\/dashboard\/settings\/documents\/types\/\d+\/toggle\/$/),
    { timeout: 20_000 },
  );
  await finalRow.getByRole("button", { name: "Выключить" }).click();
  await confirmAction(page);
  expect((await toggleResponse).ok()).toBe(true);
  const inactiveRow = settingsRow(page, expected.document_type_name);
  await expect(inactiveRow.getByText("Неактивен")).toBeVisible({ timeout: 20_000 });
  await expect(inactiveRow.getByRole("button", { name: "Включить" })).toHaveAttribute("aria-pressed", "false");
}

async function createTrainingType(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  await page.goto(backendUrl("/dashboard/settings/billing/"));
  await expect(page.getByRole("heading", { name: "Типы тренировок" })).toBeVisible();

  await page.getByRole("button", { name: /Добавить тип/ }).click();
  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "НОВЫЙ ТИП ТРЕНИРОВКИ" })).toBeVisible();
  await panel.locator("input[name='name']").fill(expected.training_type_initial_name);

  const createResponse = page.waitForResponse(responseFor("/dashboard/settings/billing/training-types/form/"), {
    timeout: 20_000,
  });
  await panel.getByRole("button", { name: "СОЗДАТЬ" }).click();
  expect((await createResponse).status()).toBe(204);
  await expect(page.getByText(expected.training_type_initial_name).first()).toBeVisible({ timeout: 20_000 });
}

async function editTrainingType(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  const trainingTypeRow = settingsRow(page, expected.training_type_initial_name);
  await expect(trainingTypeRow).toBeVisible();

  const openEditResponse = page.waitForResponse(
    responseMatchingMethod(/^\/dashboard\/settings\/billing\/training-types\/\d+\/form\/$/, "GET"),
    { timeout: 20_000 },
  );
  await trainingTypeRow.getByRole("button", { name: "Изменить" }).click();
  expect((await openEditResponse).ok()).toBe(true);

  const panel = page.locator("#slide-over");
  await expect(
    panel.getByRole("heading", { name: `ПЕРЕИМЕНОВАТЬ: ${expected.training_type_initial_name}` }),
  ).toBeVisible();
  await panel.locator("input[name='name']").fill(expected.training_type_name);

  const editResponse = page.waitForResponse(
    responseMatching(/^\/dashboard\/settings\/billing\/training-types\/\d+\/form\/$/),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "СОХРАНИТЬ" }).click();
  expect((await editResponse).status()).toBe(204);
  await expect(page.getByText(expected.training_type_name).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(expected.training_type_initial_name)).toHaveCount(0);
}

async function createTariff(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  await page.getByRole("button", { name: /Добавить тариф/ }).click();
  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "НОВЫЙ ТАРИФ" })).toBeVisible();

  await panel.locator("input[name='name']").fill(expected.tariff_initial_name);
  await panel.locator("select[name='training_type_id']").selectOption({ label: expected.training_type_name });
  await panel.locator("input[name='price']").fill(expected.tariff_initial_price.split(".")[0]);
  await panel.locator("input[name='trainings_limit']").fill(String(expected.tariff_initial_trainings_limit));
  await panel.locator("input[name='duration_days']").fill(String(expected.tariff_initial_duration_days));
  await panel.locator("select[name='scope']").selectOption("location");
  await expect(panel.locator("#location-field")).toBeVisible();
  await panel.locator("select[name='location_id']").selectOption({ label: expected.location_name });
  await panel.locator("textarea[name='description']").fill(expected.tariff_initial_description);

  const createResponse = page.waitForResponse(responseFor("/dashboard/settings/billing/tariffs/form/"), {
    timeout: 20_000,
  });
  await panel.getByRole("button", { name: "СОЗДАТЬ ТАРИФ" }).click();
  expect((await createResponse).status()).toBe(204);
  await expect(page.getByText(expected.tariff_initial_name).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(expected.location_name).first()).toBeVisible();
}

async function editTariff(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  const tariffRow = settingsRow(page, expected.tariff_initial_name);
  await expect(tariffRow).toBeVisible();

  const openEditResponse = page.waitForResponse(
    responseMatchingMethod(/^\/dashboard\/settings\/billing\/tariffs\/\d+\/form\/$/, "GET"),
    { timeout: 20_000 },
  );
  await tariffRow.getByRole("button", { name: "Изменить", exact: true }).click();
  expect((await openEditResponse).ok()).toBe(true);

  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "РЕДАКТИРОВАТЬ ТАРИФ" })).toBeVisible();
  await expect(panel.locator("select[name='training_type_id']")).toBeDisabled();
  await panel.locator("input[name='name']").fill(expected.tariff_name);
  await panel.locator("input[name='price']").fill(expected.tariff_price.split(".")[0]);
  await panel.locator("input[name='trainings_limit']").fill(String(expected.tariff_trainings_limit));
  await panel.locator("input[name='duration_days']").fill(String(expected.tariff_duration_days));
  await panel.locator("textarea[name='description']").fill(expected.tariff_description);

  const editResponse = page.waitForResponse(
    responseMatching(/^\/dashboard\/settings\/billing\/tariffs\/\d+\/form\/$/),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "СОХРАНИТЬ" }).click();
  expect((await editResponse).status()).toBe(204);
  await expect(page.getByText(expected.tariff_name).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(expected.tariff_initial_name)).toHaveCount(0);
  await expect(settingsRow(page, expected.tariff_name).getByText(`${expected.tariff_trainings_limit} занятий`)).toBeVisible();
}

async function createPersonalBookingDefaultTariff(
  page: Page,
  fixture: ClubSettingsBusinessConfigFixture,
): Promise<void> {
  const expected = fixture.expected;
  await page.getByRole("button", { name: /Добавить тариф/ }).click();
  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "НОВЫЙ ТАРИФ" })).toBeVisible();

  await panel.locator("input[name='name']").fill(expected.personal_tariff_name);
  await panel.locator("select[name='training_type_id']").selectOption({ label: expected.personal_training_type_name });
  await panel.locator("input[name='price']").fill(expected.personal_tariff_price.split(".")[0]);
  await panel.locator("input[name='trainings_limit']").fill("1");
  await panel.locator("input[name='duration_days']").fill(String(expected.personal_tariff_duration_days));
  await panel.locator("select[name='trainer_payout_policy']").selectOption("on_checkin");
  await panel.locator("input[name='is_personal_booking_default']").check();
  await panel.locator("input[name='use_components']").check();

  const components = panel.locator("#tariff-components");
  await expect(components).toBeVisible();
  await components.locator("input[name='component_name_0']").fill(expected.personal_tariff_name);
  await components
    .locator("select[name='component_training_type_id_0']")
    .selectOption({ label: expected.personal_training_type_name });
  await components.locator("select[name='component_entitlement_kind_0']").selectOption("finite_credits");
  await components.locator("input[name='component_paid_amount_basis_0']").fill(expected.personal_tariff_price.split(".")[0]);
  await components.locator("input[name='component_credits_total_0']").fill("1");
  await components
    .locator("select[name='component_trainer_payout_policy_0']")
    .selectOption("on_checkin");

  const createResponse = page.waitForResponse(responseFor("/dashboard/settings/billing/tariffs/form/"), {
    timeout: 20_000,
  });
  await panel.getByRole("button", { name: "СОЗДАТЬ ТАРИФ" }).click();
  expect((await createResponse).status()).toBe(204);

  const tariffRow = settingsRow(page, expected.personal_tariff_name);
  await expect(tariffRow).toBeVisible({ timeout: 20_000 });
  await expect(tariffRow.getByText("Текущая цена персоналки", { exact: true })).toBeVisible();

  await page.reload();
  const reloadedTariffRow = settingsRow(page, expected.personal_tariff_name);
  await expect(reloadedTariffRow).toBeVisible({ timeout: 20_000 });
  await expect(reloadedTariffRow.getByText("Текущая цена персоналки", { exact: true })).toBeVisible();

  const openEditResponse = page.waitForResponse(
    responseMatchingMethod(/^\/dashboard\/settings\/billing\/tariffs\/\d+\/form\/$/, "GET"),
    { timeout: 20_000 },
  );
  await reloadedTariffRow.getByRole("button", { name: "Изменить", exact: true }).click();
  expect((await openEditResponse).ok()).toBe(true);
  await expect(panel.locator("input[name='is_personal_booking_default']")).toBeChecked();
  await closeSlideOverIfOpen(page);
}

async function createDiscount(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  await page.getByRole("button", { name: /Добавить скидку/ }).click();
  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "НОВАЯ СКИДКА" })).toBeVisible();

  await panel.locator("input[name='name']").fill(expected.discount_initial_name);
  await panel.locator("select[name='discount_type']").selectOption(expected.discount_type);
  await panel.locator("input[name='value']").fill(expected.discount_initial_value);

  const createResponse = page.waitForResponse(responseFor("/dashboard/settings/billing/discounts/form/"), {
    timeout: 20_000,
  });
  await panel.getByRole("button", { name: "СОЗДАТЬ СКИДКУ" }).click();
  expect((await createResponse).status()).toBe(204);
  await expect(page.getByText(expected.discount_initial_name).first()).toBeVisible({ timeout: 20_000 });
}

async function editDiscount(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  const discountRow = settingsRow(page, expected.discount_initial_name);
  await expect(discountRow).toBeVisible();

  const openEditResponse = page.waitForResponse(
    responseMatchingMethod(/^\/dashboard\/settings\/billing\/discounts\/\d+\/form\/$/, "GET"),
    { timeout: 20_000 },
  );
  await discountRow.getByRole("button", { name: "Изменить" }).click();
  expect((await openEditResponse).ok()).toBe(true);

  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "РЕДАКТИРОВАТЬ СКИДКУ" })).toBeVisible();
  await panel.locator("input[name='name']").fill(expected.discount_name);
  await panel.locator("select[name='discount_type']").selectOption(expected.discount_type);
  await panel.locator("input[name='value']").fill(expected.discount_value);

  const editResponse = page.waitForResponse(
    responseMatching(/^\/dashboard\/settings\/billing\/discounts\/\d+\/form\/$/),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "СОХРАНИТЬ" }).click();
  expect((await editResponse).status()).toBe(204);
  await expect(page.getByText(expected.discount_name).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(expected.discount_initial_name)).toHaveCount(0);
}

async function saveDropInSettings(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  await page.locator("input[name^='tt_drop_in_price_']").first().fill(expected.drop_in_price.split(".")[0]);

  const trialFree = page.locator("input[name^='tt_trial_free_']").first();
  if (await trialFree.isChecked()) {
    await trialFree.uncheck();
  }

  const saveResponse = page.waitForResponse(responseFor("/dashboard/settings/billing/"), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Сохранить", exact: true }).click();
  expect((await saveResponse).ok()).toBe(true);
  await expect(page.getByText("Настройки сохранены")).toBeVisible({ timeout: 20_000 });
}

async function confirmAction(page: Page): Promise<void> {
  const confirmButton = page.getByRole("button", { name: "Подтвердить", exact: true });
  await expect(confirmButton).toBeVisible({ timeout: 5_000 });
  await confirmButton.click();
}

async function toggleBillingItemsInactive(page: Page, fixture: ClubSettingsBusinessConfigFixture): Promise<void> {
  const expected = fixture.expected;
  const tariffToggleResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname.match(/\/dashboard\/settings\/billing\/tariffs\/\d+\/toggle\/$/)
      !== null && response.request().method() === "POST",
    { timeout: 20_000 },
  );
  await page
    .getByLabel(new RegExp(`Тариф «${escapeRegExp(expected.tariff_name)}».*активен.*выключить`))
    .click();
  await confirmAction(page);
  expect((await tariffToggleResponse).ok()).toBe(true);
  await expect(settingsRow(page, expected.tariff_name)).toHaveCount(0);
  await page.getByRole("link", { name: "Архив тарифов", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Архив тарифов", exact: true })).toBeVisible();
  const archivedTariffRow = settingsRow(page, expected.tariff_name);
  await expect(archivedTariffRow).toBeVisible();
  await expect(archivedTariffRow.getByText("В архиве", { exact: true })).toBeVisible();
  await expect(archivedTariffRow.getByText(`${expected.tariff_trainings_limit} занятий`, { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "К текущим тарифам", exact: true }).click();

  const discountToggleResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname.match(/\/dashboard\/settings\/billing\/discounts\/\d+\/toggle\/$/)
      !== null && response.request().method() === "POST",
    { timeout: 20_000 },
  );
  await page
    .getByLabel(new RegExp(`Скидка «${escapeRegExp(expected.discount_name)}».*активна.*выключить`))
    .click();
  await confirmAction(page);
  expect((await discountToggleResponse).ok()).toBe(true);
  await expect(
    page.getByLabel(new RegExp(`Скидка «${escapeRegExp(expected.discount_name)}».*неактивна.*включить`)),
  ).toBeVisible({ timeout: 20_000 });

  const trainingTypeToggleResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname.match(/\/dashboard\/settings\/billing\/training-types\/\d+\/toggle\/$/)
      !== null && response.request().method() === "POST",
    { timeout: 20_000 },
  );
  await page
    .getByLabel(new RegExp(`Тип тренировки «${escapeRegExp(expected.training_type_name)}».*активен.*выключить`))
    .click();
  await confirmAction(page);
  expect((await trainingTypeToggleResponse).ok()).toBe(true);
  await expect(
    page.getByLabel(new RegExp(`Тип тренировки «${escapeRegExp(expected.training_type_name)}».*неактивен.*включить`)),
  ).toBeVisible({ timeout: 20_000 });
}

function runBackendAssert(fixturePath: string, mode = "final"): void {
  const command = process.env.REAL_STACK_E2E_ASSERT_COMMAND;
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const assertArgs = ["manage.py", "assert_club_settings_business_config_e2e", "--fixture", fixturePath];
  if (mode !== "final") {
    assertArgs.push("--mode", mode);
  }
  const output = command
    ? execSync(command, {
        cwd: repoRoot,
        encoding: "utf8",
        env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath, REAL_STACK_E2E_ASSERT_MODE: mode },
        stdio: ["ignore", "pipe", "pipe"],
      })
    : execFileSync(
        pythonBin,
        assertArgs,
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

test("real-stack owner can configure club settings, catalog and billing primitives", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsOwner(page, fixture);
  await saveGeneralSettings(page, fixture);
  await createLocation(page, fixture);
  await editLocation(page, fixture);
  await createAndDeleteTemporaryLocation(page, fixture);
  await manageGradeCatalog(page, fixture);
  await manageDocumentTypes(page, fixture);
  await createTrainingType(page, fixture);
  await editTrainingType(page, fixture);
  await createTariff(page, fixture);
  await editTariff(page, fixture);
  await createPersonalBookingDefaultTariff(page, fixture);
  await createDiscount(page, fixture);
  await editDiscount(page, fixture);
  await saveDropInSettings(page, fixture);
  runBackendAssert(fixturePath, "active-consumption");
  await toggleBillingItemsInactive(page, fixture);

  runBackendAssert(fixturePath);
});

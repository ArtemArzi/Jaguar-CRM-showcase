import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Locator, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface OwnerDashboardBusinessFixture {
  owner: {
    email: string;
    password: string;
  };
  admin: {
    email: string;
    password: string;
  };
  student: {
    student_id: number;
    name: string;
  };
  expected: {
    confirmed_amount: string;
    pending_amount: string;
    debt_amount: string;
    dashboard_revenue: string;
    dashboard_salary: string;
    pnl_income: string;
    manual_expense: string;
    pnl_margin: string;
    group_name: string;
    debt_group_name: string;
    tariff_name: string;
    expense_name: string;
  };
  report_range: {
    date_from: string;
    date_to: string;
  };
  browser_expense: {
    name: string;
    amount: string;
    date: string;
    category: string;
  };
  retention_admin: {
    task_id: number;
    comment_id: number;
    comment_text: string;
    student_name: string;
    trainer_id: number;
    trainer_name: string;
    status: string;
    level: string;
    due_date: string;
  };
  enrollment_admin: {
    student_id: number;
    student_name: string;
    source_schedule_id: number;
    source_group_name: string;
    source_week_offset: number;
    session_date: string;
    target_schedule_id: number;
    target_group_name: string;
    target_cancel_week_offset: number;
    transfer_date: string;
    cancel_date: string;
  };
  enrollment_consistency: {
    student_id: number;
    student_name: string;
    schedule_id: number;
    group_name: string;
    week_offset: number;
    session_date: string;
    transferred_enrollment_id: number;
    active_enrollment_id: number;
  };
  schedule_admin: {
    week_offset: number;
    create: {
      date: string;
      created_group_name: string;
      edited_group_name: string;
      created_start_time: string;
      created_end_time: string;
      edited_start_time: string;
      edited_end_time: string;
      created_training_type_id: number;
      edited_training_type_id: number;
      trainer_id: number;
      location_id: number;
    };
    cancel: {
      schedule_id: number;
      group_name: string;
      date: string;
      reason: string;
    };
    reschedule: {
      schedule_id: number;
      group_name: string;
      old_date: string;
      new_date: string;
      new_start_time: string;
      new_end_time: string;
    };
    substitute: {
      schedule_id: number;
      group_name: string;
      date: string;
      substitute_trainer_id: number;
      substitute_trainer_name: string;
    };
    revert: {
      schedule_id: number;
      group_name: string;
      date: string;
      week_offset: number;
      original_trainer_id: number;
      substitute_trainer_name: string;
    };
  };
  training_group_reconciliation: {
    schedule_ids: number[];
    canonical_name: string;
    responsible_trainer_id: number;
    source_student_id: number;
    source_enrollment_id: number;
    source_schedule_id: number;
    projection_schedule_id: number;
    initial_rollout_mode: string;
    new_writes_enabled: boolean;
    manual_operational_admission_enabled: boolean;
    rationale: string;
    idempotency_key: string;
  };
}

interface BackendAssertResponse {
  ok?: boolean;
}

interface KioskPrivacyEvidence {
  pin: string;
  token: string;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to an owner dashboard business fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): OwnerDashboardBusinessFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<OwnerDashboardBusinessFixture>;

  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.admin?.email || !data.admin.password) {
    throw new Error("Fixture admin credentials are required.");
  }
  if (!data.student?.student_id || !data.student.name) {
    throw new Error("Fixture student data is required.");
  }
  if (!data.expected?.confirmed_amount || !data.expected.pending_amount || !data.expected.debt_amount) {
    throw new Error("Fixture business expectations are required.");
  }
  if (!data.report_range?.date_from || !data.report_range.date_to) {
    throw new Error("Fixture report range is required.");
  }
  if (!data.browser_expense?.name || !data.browser_expense.amount || !data.browser_expense.date) {
    throw new Error("Fixture browser expense is required.");
  }
  if (
    !data.retention_admin?.task_id ||
    !data.retention_admin.comment_id ||
    !data.retention_admin.comment_text ||
    !data.retention_admin.student_name ||
    !data.retention_admin.trainer_id ||
    !data.retention_admin.trainer_name
  ) {
    throw new Error("Fixture retention admin data is required.");
  }
  if (
    !data.enrollment_admin?.student_id ||
    !data.enrollment_admin.student_name ||
    !data.enrollment_admin.source_schedule_id ||
    !data.enrollment_admin.target_schedule_id ||
    !data.enrollment_admin.session_date ||
    !data.enrollment_admin.transfer_date ||
    !data.enrollment_admin.cancel_date
  ) {
    throw new Error("Fixture enrollment admin data is required.");
  }
  if (
    !data.enrollment_consistency?.student_id ||
    !data.enrollment_consistency.student_name ||
    !data.enrollment_consistency.schedule_id ||
    !data.enrollment_consistency.group_name ||
    !data.enrollment_consistency.session_date ||
    !data.enrollment_consistency.transferred_enrollment_id ||
    !data.enrollment_consistency.active_enrollment_id
  ) {
    throw new Error("Fixture enrollment consistency data is required.");
  }
  if (
    !data.schedule_admin?.create?.created_group_name ||
    !data.schedule_admin.create.edited_group_name ||
    !data.schedule_admin.cancel?.schedule_id ||
    !data.schedule_admin.reschedule?.schedule_id ||
    !data.schedule_admin.substitute?.schedule_id ||
    !data.schedule_admin.revert?.schedule_id
  ) {
    throw new Error("Fixture schedule admin data is required.");
  }
  if (
    !data.training_group_reconciliation?.schedule_ids?.length ||
    !data.training_group_reconciliation.canonical_name ||
    !data.training_group_reconciliation.responsible_trainer_id ||
    data.training_group_reconciliation.initial_rollout_mode !== "off" ||
    data.training_group_reconciliation.new_writes_enabled !== true ||
    data.training_group_reconciliation.manual_operational_admission_enabled !== true ||
    !data.training_group_reconciliation.rationale ||
    !data.training_group_reconciliation.idempotency_key
  ) {
    throw new Error("Fixture training-group reconciliation data is required.");
  }

  return data as OwnerDashboardBusinessFixture;
}

function money(value: string): RegExp {
  const [whole] = value.split(".");
  return new RegExp(`${whole.split("").join("\\s*")}\\s*₽`);
}

function amount(value: string): RegExp {
  const [whole] = value.split(".");
  return new RegExp(`^\\s*${whole.split("").join("\\s*")}\\s*$`);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function loginToDashboard(page: Page, credentials: { email: string; password: string }): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(credentials.email);
  await page.getByLabel("Пароль").fill(credentials.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/?$/);
}

async function loginAsOwner(page: Page, fixture: OwnerDashboardBusinessFixture): Promise<void> {
  await loginToDashboard(page, fixture.owner);
}

async function loginOwnerForAccessToken(
  page: Page,
  fixture: OwnerDashboardBusinessFixture,
): Promise<string> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  const loginResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/_allauth/app/v1/auth/login" && response.request().method() === "POST";
  }, { timeout: 20_000 });
  await page.getByRole("button", { name: "Войти" }).click();
  const response = await loginResponse;
  expect(response.ok()).toBe(true);
  const body = await response.json() as { meta?: { access_token?: unknown } };
  expect(typeof body.meta?.access_token).toBe("string");
  return body.meta?.access_token as string;
}

async function loginAsAdmin(page: Page, fixture: OwnerDashboardBusinessFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/logout/"));
  await expect(page).toHaveURL(/\/dashboard\/login\/?$/);
  await loginToDashboard(page, fixture.admin);
}

async function assertDashboardHome(page: Page, fixture: OwnerDashboardBusinessFixture): Promise<void> {
  await expect(page.getByRole("heading", { name: "МОЙ ДЕНЬ" })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText("ВЫРУЧКА")).toBeVisible();
  await expect(page.getByText(money(fixture.expected.dashboard_revenue)).first()).toBeVisible();
  await expect(page.getByText("ЗАРПЛАТА ТРЕНЕРОВ")).toBeVisible();
  await expect(page.getByText(money(fixture.expected.dashboard_salary)).first()).toBeVisible();
  await expect(page.getByText(fixture.expected.group_name).first()).toBeVisible();
}

async function assertPayments(page: Page, fixture: OwnerDashboardBusinessFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/billing/payments/"));
  await expect(page.getByRole("heading", { name: "Оплаты" })).toBeVisible();
  await expect(page.getByText(fixture.student.name).first()).toBeVisible();
  await expect(page.getByText(money(fixture.expected.pending_amount)).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /Требуют подтверждения 1/ })).toBeVisible();

  await page.getByRole("link", { name: "История" }).click();
  await expect(page).toHaveURL(/queue=history/);
  await expect(page.getByText(money(fixture.expected.confirmed_amount)).first()).toBeVisible();
  await expect(page.getByText("Подтверждён")).toBeVisible();
}

async function assertDebtorsAndStudentCard(page: Page, fixture: OwnerDashboardBusinessFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/billing/"));
  await expect(page.getByRole("heading", { name: "ДОЛЖНИКИ" })).toBeVisible();
  const debtRow = page.getByRole("row", { name: new RegExp(escapeRegExp(fixture.student.name)) });
  await expect(debtRow).toBeVisible();
  await expect(debtRow.getByText(money(fixture.expected.debt_amount))).toBeVisible();
  await expect(debtRow.getByText("Нет абонемента")).toBeVisible();

  const exportResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/dashboard/billing/debtors/export/" && response.status() === 200;
  });
  await page.getByRole("link", { name: /ЭКСПОРТ/ }).click();
  const response = await exportResponse;
  expect(response.headers()["content-type"]).toContain(
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  );

  await page.goto(backendUrl(`/dashboard/students/${fixture.student.student_id}/card/`));
  await expect(page.getByRole("heading", { name: fixture.student.name.toUpperCase() })).toBeVisible();
  await expect(page.getByText(fixture.expected.tariff_name).first()).toBeVisible();
  await expect(page.getByText("3 из 4").first()).toBeVisible();
}

async function assertRetentionQueue(page: Page, fixture: OwnerDashboardBusinessFixture): Promise<void> {
  const retention = fixture.retention_admin;
  await page.goto(backendUrl("/dashboard/retention/"));
  await expect(page.getByRole("heading", { name: "RETENTION TASKS" })).toBeVisible();

  const taskRow = page.getByRole("row", { name: new RegExp(escapeRegExp(retention.student_name)) });
  await expect(taskRow).toBeVisible();
  await expect(taskRow).toContainText(retention.trainer_name);
  await expect(taskRow).toContainText("Новая");

  await page.goto(backendUrl(`/dashboard/retention/?status=open&trainer_id=${retention.trainer_id}`));
  await expect(page.getByRole("heading", { name: "RETENTION TASKS" })).toBeVisible();
  const filteredTaskRow = page.getByRole("row", { name: new RegExp(escapeRegExp(retention.student_name)) });
  await expect(filteredTaskRow).toBeVisible();
  await expect(filteredTaskRow).toContainText(retention.trainer_name);

  const commentsResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === `/dashboard/retention/${retention.task_id}/comments/` && response.status() === 200;
  });
  await filteredTaskRow.locator(`[hx-get="/dashboard/retention/${retention.task_id}/comments/"]`).click();
  expect((await commentsResponse).ok()).toBe(true);
  await expect(page.locator(`#comments-${retention.task_id}`).getByText(retention.comment_text)).toBeVisible();
}

async function assertPnl(page: Page, fixture: OwnerDashboardBusinessFixture): Promise<void> {
  const reportPath = `/dashboard/reports/?date_from=${fixture.report_range.date_from}&date_to=${fixture.report_range.date_to}`;
  await page.goto(backendUrl(reportPath));
  await expect(page.getByRole("heading", { name: "ФИНАНСЫ" })).toBeVisible();
  await expect(page.locator("input[name='date_from']")).toHaveValue(fixture.report_range.date_from);
  await expect(page.locator("input[name='date_to']")).toHaveValue(fixture.report_range.date_to);
  await expect(page.getByText("ДОХОД ПОСЛЕ ВОЗВРАТОВ", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(amount(fixture.expected.pnl_income)).first()).toBeVisible();
  await expect(page.getByText("РАСХОДЫ", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(amount(fixture.expected.manual_expense)).first()).toBeVisible();
  await expect(page.getByText("МАРЖА", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(amount(fixture.expected.pnl_margin)).first()).toBeVisible();
  await expect(page.getByText(fixture.expected.expense_name).first()).toBeVisible();

  const exportResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/dashboard/reports/export/" && response.status() === 200;
  });
  await page.getByRole("link", { name: /Excel/ }).click();
  const response = await exportResponse;
  expect(response.headers()["content-type"]).toContain(
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  );
  expect(response.headers()["content-disposition"]).toContain(
    `pnl_${fixture.report_range.date_from.replaceAll("-", "")}_${fixture.report_range.date_to.replaceAll("-", "")}.xlsx`,
  );

  await page.goto(backendUrl(reportPath));
  await page.getByRole("button", { name: /ДОБАВИТЬ РАСХОД/ }).click();
  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "НОВЫЙ РАСХОД" })).toBeVisible();
  await expect(panel.locator("input[name='date_from']")).toHaveValue(fixture.report_range.date_from);
  await expect(panel.locator("input[name='date_to']")).toHaveValue(fixture.report_range.date_to);
  await panel.locator("input[name='name']").fill(fixture.browser_expense.name);
  await panel.locator("input[name='amount']").fill(fixture.browser_expense.amount);
  await panel.locator("input[name='date']").fill(fixture.browser_expense.date);
  await panel.locator("input[name='category']").fill(fixture.browser_expense.category);

  const createResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/dashboard/reports/expense/create/" &&
      response.request().method() === "POST" &&
      response.status() === 204
    );
  });
  await panel.getByRole("button", { name: /ДОБАВИТЬ РАСХОД/ }).click();
  expect((await createResponse).headers()["hx-redirect"]).toContain(
    `/dashboard/reports/?date_from=${fixture.report_range.date_from}&date_to=${fixture.report_range.date_to}`,
  );
  await expect(page).toHaveURL(new RegExp(`date_from=${fixture.report_range.date_from}.*date_to=${fixture.report_range.date_to}`));
  await expect(page.locator("input[name='date_from']")).toHaveValue(fixture.report_range.date_from);
  await expect(page.getByText(fixture.browser_expense.name, { exact: true })).toBeVisible();

  const expenseRow = page
    .getByText(fixture.browser_expense.name, { exact: true })
    .locator("xpath=ancestor::div[contains(@class, 'justify-between')][1]");
  const deleteResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname.includes("/dashboard/reports/expense/") &&
      url.pathname.endsWith("/delete/") &&
      response.request().method() === "POST" &&
      response.status() === 204
    );
  });
  await expenseRow.locator("button[hx-post*='/delete/']").click();
  const confirmDialog = page.getByRole("dialog", { name: "Подтверждение" });
  await expect(confirmDialog).toBeVisible();
  await confirmDialog.getByRole("button", { name: "Подтвердить" }).click();
  expect((await deleteResponse).headers()["hx-redirect"]).toContain(
    `/dashboard/reports/?date_from=${fixture.report_range.date_from}&date_to=${fixture.report_range.date_to}`,
  );
  await expect(page).toHaveURL(new RegExp(`date_from=${fixture.report_range.date_from}.*date_to=${fixture.report_range.date_to}`));
  await expect(page.getByText(fixture.browser_expense.name, { exact: true })).not.toBeVisible();
  await expect(page.getByText(fixture.expected.expense_name).first()).toBeVisible();
}

function assertSensitiveValueAbsent(haystack: string, value: string, label: string): void {
  if (haystack.includes(value)) {
    throw new Error(`${label} unexpectedly exposed kiosk activation secret.`);
  }
}

async function assertKioskStatusHasNoSecrets(
  page: Page,
  evidence: KioskPrivacyEvidence,
  label: string,
): Promise<void> {
  const html = await page.locator("#kiosk-status").evaluateAll((nodes) =>
    nodes.map((node) => node.innerHTML).join("\n"),
  );
  assertSensitiveValueAbsent(html, evidence.pin, label);
  assertSensitiveValueAbsent(html, evidence.token, label);
}

async function assertKioskControls(page: Page): Promise<KioskPrivacyEvidence> {
  await page.goto(backendUrl("/dashboard/settings/kiosk/"));
  await expect(page.getByRole("heading", { name: "Настройки" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Киоск" })).toBeVisible();
  await expect(page.getByText("Неактивен", { exact: true })).toBeVisible();

  const generateResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/dashboard/settings/kiosk/generate-pin/" &&
      response.request().method() === "POST" &&
      response.status() === 200
    );
  });
  await page.getByRole("button", { name: /Сгенерировать PIN/ }).click();
  await generateResponse;

  const kioskStatus = page.locator("#kiosk-status");
  await expect(kioskStatus.getByText("PIN-код для активации киоска:")).toBeVisible();
  const pin = await kioskStatus.locator("button[data-pin]").getAttribute("data-pin");
  if (!pin || !/^\d{6}$/.test(pin)) {
    throw new Error("Dashboard did not expose a valid one-time kiosk PIN.");
  }

  await page.goto("/kiosk/");
  for (const [index, digit] of [...pin].entries()) {
    await page.getByLabel(`PIN цифра ${index + 1}`).fill(digit);
  }
  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
  const token = await page.evaluate(() => localStorage.getItem("kiosk_device_token"));
  if (!token || !/^[a-f0-9]{64}$/i.test(token)) {
    throw new Error("Kiosk activation did not persist a valid device token.");
  }
  const evidence = { pin, token };

  await page.goto(backendUrl("/dashboard/settings/kiosk/"));
  await expect(page.getByText("Активен", { exact: true })).toBeVisible();
  await assertKioskStatusHasNoSecrets(page, evidence, "Active kiosk status");
  const deactivateResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/dashboard/settings/kiosk/deactivate/" &&
      response.request().method() === "POST" &&
      response.status() === 200
    );
  });
  await page.getByRole("button", { name: /Деактивировать киоск/ }).click();
  const confirmDialog = page.getByRole("dialog", { name: "Подтверждение" });
  await expect(confirmDialog).toBeVisible();
  await confirmDialog.getByRole("button", { name: "Подтвердить" }).click();
  await deactivateResponse;
  await expect(page.getByText("Неактивен", { exact: true })).toBeVisible();
  await assertKioskStatusHasNoSecrets(page, evidence, "Inactive kiosk status");
  return evidence;
}

function isDashboardPostResponse(matchesPath: (pathname: string) => boolean) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return response.request().method() === "POST" && response.status() === 200 && matchesPath(url.pathname);
  };
}

function isDashboardPostStatusResponse(matchesPath: (pathname: string) => boolean, status: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return response.request().method() === "POST" && response.status() === status && matchesPath(url.pathname);
  };
}

function scheduleCard(page: Page, groupName: string): Locator {
  return page.locator("[hx-get][hx-target='#slide-over']:visible").filter({ hasText: groupName });
}

async function openScheduleDetail(
  page: Page,
  options: {
    scheduleId: number;
    groupName: string;
    weekOffset: number;
  },
): Promise<Locator> {
  const { scheduleId, groupName, weekOffset } = options;

  await page.goto(backendUrl(`/dashboard/schedule/?week_offset=${weekOffset}`));
  await expect(page.getByRole("heading", { name: "РАСПИСАНИЕ" })).toBeVisible();

  const detailResponse = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return url.pathname === `/dashboard/schedule/${scheduleId}/detail/` && response.status() === 200;
    },
    { timeout: 20_000 },
  );
  await page.locator("[hx-get][hx-target='#slide-over']:visible").filter({ hasText: groupName }).first().click();
  expect((await detailResponse).ok()).toBe(true);

  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "ДЕТАЛИ ЗАНЯТИЯ" })).toBeVisible({
    timeout: 20_000,
  });
  await expect(panel.getByText(groupName).first()).toBeVisible();
  return panel;
}

async function openScheduleDetailByGroup(
  page: Page,
  options: {
    groupName: string;
    weekOffset: number;
  },
): Promise<{ panel: Locator; scheduleId: number }> {
  const { groupName, weekOffset } = options;

  await page.goto(backendUrl(`/dashboard/schedule/?week_offset=${weekOffset}`));
  await expect(page.getByRole("heading", { name: "РАСПИСАНИЕ" })).toBeVisible();

  const card = scheduleCard(page, groupName).first();
  await expect(card).toBeVisible({ timeout: 20_000 });
  const hxGet = await card.getAttribute("hx-get");
  const scheduleId = Number(hxGet?.match(/\/dashboard\/schedule\/(\d+)\/detail\//)?.[1]);
  if (!Number.isInteger(scheduleId)) {
    throw new Error(`Could not parse schedule id for ${groupName}.`);
  }

  const detailResponse = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return url.pathname === `/dashboard/schedule/${scheduleId}/detail/` && response.status() === 200;
    },
    { timeout: 20_000 },
  );
  await card.click();
  expect((await detailResponse).ok()).toBe(true);

  const panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "ДЕТАЛИ ЗАНЯТИЯ" })).toBeVisible({
    timeout: 20_000,
  });
  await expect(panel.getByText(groupName).first()).toBeVisible();
  return { panel, scheduleId };
}

function scheduleEnrollmentRow(panel: Locator, studentName: string): Locator {
  return panel
    .locator("p")
    .filter({ hasText: studentName })
    .locator("xpath=ancestor::div[contains(@class, 'p-3')][1]");
}

async function assertEnrollmentAdmin(page: Page, fixture: OwnerDashboardBusinessFixture): Promise<void> {
  const enrollment = fixture.enrollment_admin;
  let panel = await openScheduleDetail(page, {
    scheduleId: enrollment.source_schedule_id,
    groupName: enrollment.source_group_name,
    weekOffset: enrollment.source_week_offset,
  });

  await expect(panel.getByText(enrollment.student_name)).not.toBeVisible();
  await panel.locator("select[name='student_id']").selectOption(String(enrollment.student_id));
  await panel.locator("select[name='status']").selectOption("active");

  const enrollResponse = page.waitForResponse(
    isDashboardPostResponse((pathname) => pathname === `/dashboard/schedule/${enrollment.source_schedule_id}/enroll/`),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "ЗАПИСАТЬ УЧЕНИКА" }).click();
  expect((await enrollResponse).ok()).toBe(true);

  let row = scheduleEnrollmentRow(panel, enrollment.student_name);
  await expect(row).toContainText("Активен");

  const freezeResponse = page.waitForResponse(
    isDashboardPostResponse((pathname) => pathname.includes("/dashboard/schedule/enrollments/") && pathname.endsWith("/freeze/")),
    { timeout: 20_000 },
  );
  await row.getByRole("button", { name: "ПАУЗА" }).click();
  expect((await freezeResponse).ok()).toBe(true);
  row = scheduleEnrollmentRow(panel, enrollment.student_name);
  await expect(row).toContainText("Заморожен");
  await expect(row).toContainText("Нельзя отметить");

  const unfreezeResponse = page.waitForResponse(
    isDashboardPostResponse((pathname) => pathname.includes("/dashboard/schedule/enrollments/") && pathname.endsWith("/unfreeze/")),
    { timeout: 20_000 },
  );
  await row.getByRole("button", { name: "ВЕРНУТЬ" }).click();
  expect((await unfreezeResponse).ok()).toBe(true);
  row = scheduleEnrollmentRow(panel, enrollment.student_name);
  await expect(row).toContainText("Активен");

  await row.getByRole("button", { name: "ПЕРЕВЕСТИ" }).click();
  const transferForm = panel.locator("form[hx-post*='/transfer/']");
  await expect(transferForm).toBeVisible();
  await transferForm.locator("select[name='target_schedule_id']").selectOption(String(enrollment.target_schedule_id));
  await transferForm.locator("input[name='transfer_date']").fill(enrollment.transfer_date);

  const transferResponse = page.waitForResponse(
    isDashboardPostResponse((pathname) => pathname.includes("/dashboard/schedule/enrollments/") && pathname.endsWith("/transfer/")),
    { timeout: 20_000 },
  );
  await transferForm.getByRole("button", { name: "ПЕРЕВЕСТИ" }).click();
  const confirmDialog = page.getByRole("dialog", { name: "Подтверждение" });
  await expect(confirmDialog).toBeVisible();
  await confirmDialog.getByRole("button", { name: "Подтвердить" }).click();
  expect((await transferResponse).ok()).toBe(true);
  await expect(panel.getByText(enrollment.student_name)).not.toBeVisible();

  panel = await openScheduleDetail(page, {
    scheduleId: enrollment.target_schedule_id,
    groupName: enrollment.target_group_name,
    weekOffset: enrollment.target_cancel_week_offset,
  });
  row = scheduleEnrollmentRow(panel, enrollment.student_name);
  await expect(row).toContainText("Активен");

  const cancelResponse = page.waitForResponse(
    isDashboardPostResponse((pathname) => pathname.includes("/dashboard/schedule/enrollments/") && pathname.endsWith("/cancel/")),
    { timeout: 20_000 },
  );
  await row.getByRole("button", { name: "СНЯТЬ" }).click();
  expect((await cancelResponse).ok()).toBe(true);
  await expect(panel.getByText(enrollment.student_name)).not.toBeVisible();
}

async function assertEnrollmentConsistency(page: Page, fixture: OwnerDashboardBusinessFixture): Promise<void> {
  const consistency = fixture.enrollment_consistency;
  const panel = await openScheduleDetail(page, {
    scheduleId: consistency.schedule_id,
    groupName: consistency.group_name,
    weekOffset: consistency.week_offset,
  });
  const row = scheduleEnrollmentRow(panel, consistency.student_name);

  await expect(row).toContainText("Активен");
  await expect(row).not.toContainText("transferred");
  await expect(
    row.locator(
      `form[hx-post='/dashboard/schedule/enrollments/${consistency.active_enrollment_id}/freeze/']`,
    ),
  ).toHaveCount(1);
  await expect(
    row.locator(
      `form[hx-post='/dashboard/schedule/enrollments/${consistency.active_enrollment_id}/cancel/']`,
    ),
  ).toHaveCount(1);
  await expect(row.locator(`[hx-post*='/${consistency.transferred_enrollment_id}/']`)).toHaveCount(0);

  await row.getByRole("button", { name: "ПЕРЕВЕСТИ" }).click();
  const transferForm = panel.locator("form[hx-post$='/transfer/']");
  await expect(transferForm).toHaveCount(1);
  await expect(transferForm).toHaveAttribute(
    "hx-post",
    `/dashboard/schedule/enrollments/${consistency.active_enrollment_id}/transfer/`,
  );
}

async function assertScheduleCrud(page: Page, fixture: OwnerDashboardBusinessFixture): Promise<void> {
  const scheduleAdmin = fixture.schedule_admin;
  const create = scheduleAdmin.create;
  await page.goto(backendUrl(`/dashboard/schedule/?week_offset=${scheduleAdmin.week_offset}`));
  await expect(page.getByRole("heading", { name: "РАСПИСАНИЕ" })).toBeVisible();

  const createFormResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/dashboard/schedule/create/" && response.status() === 200;
  });
  await page.locator("button[hx-get='/dashboard/schedule/create/']").click();
  expect((await createFormResponse).ok()).toBe(true);

  let panel = page.locator("#slide-over");
  await expect(panel.getByRole("heading", { name: "НОВОЕ ЗАНЯТИЕ" })).toBeVisible();
  await panel.locator("input[name='group_name']").fill(create.created_group_name);
  await panel.locator("select[name='training_type_id']").selectOption(String(create.created_training_type_id));
  await panel.getByRole("button", { name: "Разовое" }).click();
  await expect(panel.locator("input[name='one_time_date']")).toBeVisible();
  await panel.locator("input[name='one_time_date']").fill(create.date);
  await panel.locator("input[name='start_time']").fill(create.created_start_time);
  await panel.locator("input[name='end_time']").fill(create.created_end_time);
  await panel.locator("select[name='trainer_id']").selectOption(String(create.trainer_id));
  await panel.locator("select[name='location_id']").selectOption(String(create.location_id));

  const createResponse = page.waitForResponse(
    isDashboardPostStatusResponse((pathname) => pathname === "/dashboard/schedule/create/", 204),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "ДОБАВИТЬ В РАСПИСАНИЕ" }).click();
  expect((await createResponse).ok()).toBe(true);

  const opened = await openScheduleDetailByGroup(page, {
    groupName: create.created_group_name,
    weekOffset: scheduleAdmin.week_offset,
  });
  panel = opened.panel;

  const editFormResponse = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return url.pathname === `/dashboard/schedule/${opened.scheduleId}/edit/` && response.status() === 200;
    },
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "РЕДАКТИРОВАТЬ РАСПИСАНИЕ" }).click();
  expect((await editFormResponse).ok()).toBe(true);

  await expect(panel.getByRole("heading", { name: "РЕДАКТИРОВАТЬ" })).toBeVisible();
  await panel.locator("input[name='group_name']").fill(create.edited_group_name);
  await panel.locator("select[name='training_type_id']").selectOption(String(create.edited_training_type_id));
  await panel.locator("input[name='start_time']").fill(create.edited_start_time);
  await panel.locator("input[name='end_time']").fill(create.edited_end_time);

  const editResponse = page.waitForResponse(
    isDashboardPostStatusResponse((pathname) => pathname === `/dashboard/schedule/${opened.scheduleId}/edit/`, 204),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "СОХРАНИТЬ ИЗМЕНЕНИЯ" }).click();
  expect((await editResponse).ok()).toBe(true);

  await page.goto(backendUrl(`/dashboard/schedule/?week_offset=${scheduleAdmin.week_offset}`));
  await expect(scheduleCard(page, create.edited_group_name).first()).toBeVisible();
  await expect(scheduleCard(page, create.created_group_name)).toHaveCount(0);
}

async function assertScheduleExceptionsAsAdmin(page: Page, fixture: OwnerDashboardBusinessFixture): Promise<void> {
  const scheduleAdmin = fixture.schedule_admin;
  await loginAsAdmin(page, fixture);

  const cancel = scheduleAdmin.cancel;
  let panel = await openScheduleDetail(page, {
    scheduleId: cancel.schedule_id,
    groupName: cancel.group_name,
    weekOffset: scheduleAdmin.week_offset,
  });
  await panel.locator("input[name='reason']").fill(cancel.reason);
  const cancelResponse = page.waitForResponse(
    isDashboardPostResponse((pathname) => pathname === `/dashboard/schedule/${cancel.schedule_id}/cancel/`),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "ОТМЕНИТЬ ЗАНЯТИЕ" }).click();
  expect((await cancelResponse).ok()).toBe(true);
  await expect(panel.getByText("Занятие отменено")).toBeVisible();
  await expect(panel.getByText(cancel.reason)).toBeVisible();

  await page.goto(backendUrl(`/dashboard/schedule/?week_offset=${scheduleAdmin.week_offset}`));
  await expect(scheduleCard(page, cancel.group_name)).toHaveCount(0);

  const reschedule = scheduleAdmin.reschedule;
  panel = await openScheduleDetail(page, {
    scheduleId: reschedule.schedule_id,
    groupName: reschedule.group_name,
    weekOffset: scheduleAdmin.week_offset,
  });
  await panel.locator("input[name='new_date']").fill(reschedule.new_date);
  await panel.locator("input[name='new_time']").fill(reschedule.new_start_time);
  const rescheduleResponse = page.waitForResponse(
    isDashboardPostResponse((pathname) => pathname === `/dashboard/schedule/${reschedule.schedule_id}/reschedule/`),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "ПЕРЕНЕСТИ ЗАНЯТИЕ" }).click();
  expect((await rescheduleResponse).ok()).toBe(true);
  await expect(panel.getByText("Перенесено на")).toBeVisible();
  await expect(panel.getByText(reschedule.new_start_time)).toBeVisible();

  await page.goto(backendUrl(`/dashboard/schedule/?week_offset=${scheduleAdmin.week_offset}`));
  const rescheduledCard = scheduleCard(page, reschedule.group_name).first();
  await expect(rescheduledCard).toBeVisible();
  await expect(rescheduledCard).toContainText("ПЕРЕНОС");
  await expect(rescheduledCard).toContainText(new RegExp(`${reschedule.new_start_time}\\s*[–-]\\s*${reschedule.new_end_time}`));

  const substitute = scheduleAdmin.substitute;
  panel = await openScheduleDetail(page, {
    scheduleId: substitute.schedule_id,
    groupName: substitute.group_name,
    weekOffset: scheduleAdmin.week_offset,
  });
  await panel.locator("select[name='substitute_trainer_id']").selectOption(String(substitute.substitute_trainer_id));
  const substituteResponse = page.waitForResponse(
    isDashboardPostResponse((pathname) => pathname === `/dashboard/schedule/${substitute.schedule_id}/substitute/`),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "ЗАМЕНИТЬ ТРЕНЕРА" }).click();
  expect((await substituteResponse).ok()).toBe(true);
  await expect(panel.getByText("Замена тренера")).toBeVisible();
  await expect(panel.getByText(substitute.substitute_trainer_name, { exact: true })).toBeVisible();

  await page.goto(backendUrl(`/dashboard/schedule/?week_offset=${scheduleAdmin.week_offset}`));
  const substituteCard = scheduleCard(page, substitute.group_name).first();
  await expect(substituteCard).toBeVisible();
  await expect(substituteCard).toContainText("ЗАМЕНА");
  await expect(substituteCard).toContainText(substitute.substitute_trainer_name);

  const revert = scheduleAdmin.revert;
  panel = await openScheduleDetail(page, {
    scheduleId: revert.schedule_id,
    groupName: revert.group_name,
    weekOffset: revert.week_offset,
  });
  await expect(panel.getByText("Замена тренера")).toBeVisible();
  await expect(panel.getByText(revert.substitute_trainer_name, { exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "ОТМЕНИТЬ ИЗМЕНЕНИЕ" }).click();
  await expect(panel.getByText("Вернуть расписание как было?")).toBeVisible();

  const revertResponse = page.waitForResponse(
    isDashboardPostResponse((pathname) => pathname === `/dashboard/schedule/${revert.schedule_id}/revert/`),
    { timeout: 20_000 },
  );
  await panel.getByRole("button", { name: "ДА, ОТМЕНИТЬ" }).click();
  expect((await revertResponse).ok()).toBe(true);
  await expect(panel.getByText("Замена тренера")).not.toBeVisible();
  await expect(panel.getByRole("button", { name: "ОТМЕНИТЬ ЗАНЯТИЕ" })).toBeVisible();

  await page.goto(backendUrl(`/dashboard/schedule/?week_offset=${revert.week_offset}`));
  const revertedCard = scheduleCard(page, revert.group_name).first();
  await expect(revertedCard).toBeVisible();
  await expect(revertedCard).not.toContainText("ЗАМЕНА");
}

type ReconciliationApplyResult = {
  training_group_id: number;
  status: string;
  preview_digest: string;
  selected_schedule_ids: number[];
  membership_count: number;
  projection_count: number;
  linked_payment_count: number;
  roster_delta_digest: string;
  rollout_gate_digest: string;
  batch_id: string;
};

function isReconciliationPost(response: Response): boolean {
  const url = new URL(response.url());
  return (
    url.pathname === "/dashboard/training-groups/reconciliation/" &&
    response.request().method() === "POST" &&
    response.status() === 200
  );
}

function writeReconciliationRuntime(
  fixturePath: string,
  uiPreviewDigest: string,
  apply: ReconciliationApplyResult,
): void {
  const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as Record<string, unknown>;
  const runtime = (fixture.runtime ?? {}) as Record<string, unknown>;
  writeFileSync(
    fixturePath,
    `${JSON.stringify({
      ...fixture,
      runtime: {
        ...runtime,
        training_group_reconciliation: {
          ui_preview_digest: uiPreviewDigest,
          apply,
        },
      },
    }, null, 2)}\n`,
    "utf8",
  );
}

async function assertOwnerTrainingGroupReconciliation(
  page: Page,
  fixturePath: string,
  fixture: OwnerDashboardBusinessFixture,
  ownerAccessToken: string,
): Promise<void> {
  const reconciliation = fixture.training_group_reconciliation;
  await loginAsOwner(page, fixture);
  await page.goto(backendUrl("/dashboard/training-groups/reconciliation/"));
  await expect(page.getByRole("heading", { name: "Сверка тренировочных групп" })).toBeVisible();

  const form = page.locator("form[hx-post='/dashboard/training-groups/reconciliation/']");
  await expect(form).toBeVisible();
  for (const scheduleId of reconciliation.schedule_ids) {
    await form.locator(`input[name='schedule_ids'][value='${scheduleId}']`).check();
  }
  await form.locator("input[name='canonical_name']").fill(reconciliation.canonical_name);
  await form
    .locator("select[name='responsible_trainer_id']")
    .selectOption(String(reconciliation.responsible_trainer_id));
  const previewResponse = page.waitForResponse(isReconciliationPost, { timeout: 20_000 });
  await form.getByRole("button", { name: "Проверить выбранные ID" }).click();
  expect((await previewResponse).ok()).toBe(true);
  const preview = page.locator("#training-group-preview");
  await expect(preview.getByText(/^Предпросмотр #/)).toBeVisible();
  const uiPreviewDigest = await form.locator("input[name='preview_digest']").inputValue();
  expect(uiPreviewDigest).toMatch(/^[a-f0-9]{64}$/);

  const headers = { Authorization: `Bearer ${ownerAccessToken}` };
  const transitionToReconciling = await page.request.post(
    backendUrl("/api/schedules/training-group-rollout/transition/"),
    {
      headers,
      data: {
        target_mode: "reconciling",
        rationale: "Owner browser reconciliation transition.",
        idempotency_key: `${reconciliation.idempotency_key}-reconciling`,
        rollout_gate_digest: "",
      },
    },
  );
  expect(transitionToReconciling.status()).toBe(200);
  expect((await transitionToReconciling.json()).mode).toBe("reconciling");

  await form.locator("input[name='rationale']").fill(reconciliation.rationale);
  await form.locator("input[name='idempotency_key']").fill(reconciliation.idempotency_key);
  const applyResponse = page.waitForResponse(isReconciliationPost, { timeout: 20_000 });
  await form.getByRole("button", { name: "Подтвердить и применить digest" }).click();
  expect((await applyResponse).ok()).toBe(true);
  await expect(preview.getByText(/Сверка применена: группа #/)).toBeVisible();

  const applyPayload = {
    schedule_ids: reconciliation.schedule_ids,
    canonical_name: reconciliation.canonical_name,
    responsible_trainer_id: reconciliation.responsible_trainer_id,
    start_dates: [],
    preview_digest: uiPreviewDigest,
    rationale: reconciliation.rationale,
    idempotency_key: reconciliation.idempotency_key,
  };
  const retryResponse = await page.request.post(
    backendUrl("/api/schedules/training-group-reconciliation/apply/"),
    { headers, data: applyPayload },
  );
  expect(retryResponse.status()).toBe(200);
  const applied = await retryResponse.json() as ReconciliationApplyResult;
  expect(applied.preview_digest).toBe(uiPreviewDigest);
  expect(applied.selected_schedule_ids).toEqual([...reconciliation.schedule_ids].sort((a, b) => a - b));
  expect(applied.membership_count).toBe(1);
  expect(applied.projection_count).toBe(1);

  for (const targetMode of ["shadow", "active"] as const) {
    const transition = await page.request.post(
      backendUrl("/api/schedules/training-group-rollout/transition/"),
      {
        headers,
        data: {
          target_mode: targetMode,
          rationale: `Owner browser reconciliation ${targetMode} transition.`,
          idempotency_key: `${reconciliation.idempotency_key}-${targetMode}`,
          rollout_gate_digest: applied.rollout_gate_digest,
        },
      },
    );
    expect(transition.status()).toBe(200);
    expect((await transition.json()).mode).toBe(targetMode);
  }
  writeReconciliationRuntime(fixturePath, uiPreviewDigest, applied);
}

function runBackendAssert(fixturePath: string, kioskEvidence?: KioskPrivacyEvidence): void {
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
        ["manage.py", "assert_owner_dashboard_business_e2e", "--fixture", fixturePath],
        {
          cwd: repoRoot,
          encoding: "utf8",
          env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
          stdio: ["ignore", "pipe", "pipe"],
        },
      );

  if (kioskEvidence) {
    assertSensitiveValueAbsent(output, kioskEvidence.pin, "Backend assertion output");
    assertSensitiveValueAbsent(output, kioskEvidence.token, "Backend assertion output");
  }
  const result = JSON.parse(output) as BackendAssertResponse;
  expect(result.ok).toBe(true);
}

test("real-stack owner sees business picture and dashboard controls", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  const ownerAccessToken = await loginOwnerForAccessToken(page, fixture);
  await loginAsOwner(page, fixture);
  await assertDashboardHome(page, fixture);
  await assertPayments(page, fixture);
  await assertDebtorsAndStudentCard(page, fixture);
  await assertRetentionQueue(page, fixture);
  await assertPnl(page, fixture);
  const kioskEvidence = await assertKioskControls(page);
  await assertEnrollmentConsistency(page, fixture);
  await assertEnrollmentAdmin(page, fixture);
  await assertScheduleCrud(page, fixture);
  await assertScheduleExceptionsAsAdmin(page, fixture);
  await assertOwnerTrainingGroupReconciliation(page, fixturePath, fixture, ownerAccessToken);
  runBackendAssert(fixturePath, kioskEvidence);
});

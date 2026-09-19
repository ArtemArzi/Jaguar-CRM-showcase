import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface DashboardAccessSubscriptionsFixture {
  owner: {
    email: string;
    password: string;
  };
  admin: {
    email: string;
    password: string;
    role: string;
  };
  denied_users: Record<
    "trainer" | "student" | "parent",
    {
      email: string;
      password: string;
      role: string;
    }
  >;
  student: {
    student_id: number;
    name: string;
  };
  sale_student: {
    student_id: number;
    name: string;
  };
  tariff_id: number;
  expected: {
    direct_payment_method: "transfer";
    direct_payment_method_label: string;
    freeze_days: number;
    paid_amount: string;
    tariff_name: string;
    trainings_left: number;
    trainings_limit: number;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a dashboard access subscriptions fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): DashboardAccessSubscriptionsFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<DashboardAccessSubscriptionsFixture>;

  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.admin?.email || !data.admin.password || !data.admin.role) {
    throw new Error("Fixture admin credentials are required.");
  }
  if (!data.denied_users?.trainer || !data.denied_users.student || !data.denied_users.parent) {
    throw new Error("Fixture denied user credentials are required.");
  }
  if (!data.student?.name) {
    throw new Error("Fixture student data is required.");
  }
  if (!data.sale_student?.student_id || !data.sale_student.name || !data.tariff_id) {
    throw new Error("Fixture transfer sale data is required.");
  }
  if (
    !data.expected?.tariff_name ||
    !data.expected.paid_amount ||
    data.expected.direct_payment_method !== "transfer" ||
    !data.expected.direct_payment_method_label
  ) {
    throw new Error("Fixture subscription expectations are required.");
  }

  return data as DashboardAccessSubscriptionsFixture;
}

function money(value: string): RegExp {
  const [whole] = value.split(".");
  return new RegExp(`${whole.split("").join("\\s*")}\\s*₽`);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function loginToDashboard(page: Page, email: string, password: string): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(email);
  await page.getByLabel("Пароль").fill(password);
  await page.getByRole("button", { name: "Войти" }).click();
}

async function openLoginFromAppEntry(page: Page): Promise<void> {
  await openResetLogin(page);
  await expect(page.getByRole("heading", { name: "Вход в кабинет" })).toBeVisible();
  await page.goto("/app");
  await expect(page.getByRole("heading", { name: "Кабинет клуба" })).toBeVisible();
  await page.getByRole("link", { name: /Войти в кабинет/ }).click();
  await expect(page).toHaveURL(/\/login$/);
}

async function loginThroughAppEntry(page: Page, email: string, password: string): Promise<void> {
  await openLoginFromAppEntry(page);
  await page.getByLabel(/Email/).fill(email);
  await page.getByLabel("Пароль").fill(password);
  await page.getByRole("button", { name: "Войти" }).click();
}

async function assertManagementAppEntryHandoff(
  page: Page,
  user: { email: string; password: string },
): Promise<void> {
  await loginThroughAppEntry(page, user.email, user.password);
  await expect(page).toHaveURL(/\/app$/);
  await expect(page.getByRole("heading", { name: "Панель управления клубом" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Открыть панель управления/ })).toHaveAttribute(
    "href",
    "/dashboard/login/",
  );
}

async function assertRoleAppEntryRedirect(
  page: Page,
  user: { email: string; password: string },
  expectedPath: RegExp,
): Promise<void> {
  await loginThroughAppEntry(page, user.email, user.password);
  await expect(page).toHaveURL(expectedPath);
}

async function assertUnifiedAppEntryRoleMatrix(
  page: Page,
  fixture: DashboardAccessSubscriptionsFixture,
): Promise<void> {
  await assertManagementAppEntryHandoff(page, fixture.owner);
  await assertManagementAppEntryHandoff(page, fixture.admin);
  await assertRoleAppEntryRedirect(page, fixture.denied_users.trainer, /\/trainer\/?$/);
  await assertRoleAppEntryRedirect(page, fixture.denied_users.student, /\/student\/?$/);
  await assertRoleAppEntryRedirect(page, fixture.denied_users.parent, /\/parent\/?$/);
}

async function loginAsOwner(page: Page, fixture: DashboardAccessSubscriptionsFixture): Promise<void> {
  await loginToDashboard(page, fixture.owner.email, fixture.owner.password);
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

async function loginAsAdmin(page: Page, fixture: DashboardAccessSubscriptionsFixture): Promise<void> {
  await loginToDashboard(page, fixture.admin.email, fixture.admin.password);
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

async function assertSubscriptionsAndFreezeInbox(
  page: Page,
  fixture: DashboardAccessSubscriptionsFixture,
): Promise<void> {
  await page.goto(backendUrl("/dashboard/billing/subscriptions/"));
  await expect(page.getByRole("heading", { name: "Абонементы" })).toBeVisible();
  const subscriptionRow = page.getByRole("row", { name: new RegExp(escapeRegExp(fixture.student.name)) });
  await expect(subscriptionRow).toBeVisible();
  await expect(subscriptionRow.getByText(fixture.expected.tariff_name)).toBeVisible();
  await expect(subscriptionRow.getByText(money(fixture.expected.paid_amount))).toBeVisible();
  await expect(
    subscriptionRow.getByText(`${fixture.expected.trainings_left} из ${fixture.expected.trainings_limit}`),
  ).toBeVisible();

  const inbox = page.locator("#freeze-approval-inbox");
  await expect(inbox.getByRole("heading", { name: "Заявки на заморозку" })).toBeVisible();
  await expect(inbox.getByText("1 заявок")).toBeVisible();
  await expect(inbox.getByText(fixture.student.name).first()).toBeVisible();
  await expect(inbox.getByText(`${fixture.expected.freeze_days} дн.`)).toBeVisible();
  await expect(inbox.getByText("ЗАПРОСИЛ")).toBeVisible();
  await expect(inbox.getByRole("button", { name: "ОДОБРИТЬ" })).toBeVisible();
  await expect(inbox.getByRole("button", { name: "ОТКЛОНИТЬ" })).toBeVisible();
  await expect(inbox.getByPlaceholder("Причина")).toBeVisible();
}

async function assertTransferDirectSubscription(
  page: Page,
  fixture: DashboardAccessSubscriptionsFixture,
): Promise<void> {
  await page.goto(backendUrl("/dashboard/billing/subscriptions/"));
  let saleRow = page.getByRole("row", {
    name: new RegExp(escapeRegExp(fixture.sale_student.name)),
  });

  if ((await saleRow.count()) === 0) {
    await page.getByRole("button", { name: "Новый абонемент" }).click();
    const form = page.locator("#create-sub-form");
    await expect(form).toBeVisible();
    await form.locator('select[name="student_id"]').selectOption(String(fixture.sale_student.student_id));
    await form.locator('select[name="tariff_id"]').selectOption(String(fixture.tariff_id));
    await form.locator('select[name="payment_method"]').selectOption(fixture.expected.direct_payment_method);
    await form.getByRole("button", { name: "СОЗДАТЬ" }).click();
    await expect(page).toHaveURL(/\/dashboard\/billing\/subscriptions\/$/);
    saleRow = page.getByRole("row", {
      name: new RegExp(escapeRegExp(fixture.sale_student.name)),
    });
  }

  await expect(saleRow).toBeVisible();
  await page.goto(backendUrl("/dashboard/billing/payments/?status=confirmed"));
  await expect(page.getByRole("heading", { name: "Оплаты" })).toBeVisible();
  const paymentDetails = page.getByText(fixture.sale_student.name, { exact: true }).locator("..").locator("..");
  await expect(paymentDetails.getByText(fixture.expected.direct_payment_method_label, { exact: true })).toBeVisible();
}

async function assertDeniedDashboardAccess(page: Page, fixture: DashboardAccessSubscriptionsFixture): Promise<void> {
  for (const role of ["trainer", "student", "parent"] as const) {
    await page.goto(backendUrl("/dashboard/logout/"));
    const deniedUser = fixture.denied_users[role];
    await loginToDashboard(page, deniedUser.email, deniedUser.password);
    await expect(page).toHaveURL(/\/dashboard\/$/);
    await expect(page.getByText("Access denied: insufficient role")).toBeVisible();
  }
}

async function assertDashboardLogoutClearsSession(page: Page): Promise<void> {
  await page.goto(backendUrl("/dashboard/logout/"));
  await expect(page).toHaveURL(/\/dashboard\/login\/(?:\?.*)?$/);

  await page.goto(backendUrl("/dashboard/"));
  await expect(page).toHaveURL(/\/dashboard\/login\/(?:\?.*)?$/);
  await expect(page.getByRole("button", { name: "Войти" })).toBeVisible();
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
        ["manage.py", "assert_dashboard_access_subscriptions_e2e", "--fixture", fixturePath],
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

test("real-stack owner sees subscriptions freeze inbox and non-management roles are denied", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await assertUnifiedAppEntryRoleMatrix(page, fixture);
  await loginAsOwner(page, fixture);
  await assertSubscriptionsAndFreezeInbox(page, fixture);
  await assertTransferDirectSubscription(page, fixture);
  await assertDashboardLogoutClearsSession(page);
  await loginAsAdmin(page, fixture);
  await assertSubscriptionsAndFreezeInbox(page, fixture);
  await assertDashboardLogoutClearsSession(page);
  await assertDeniedDashboardAccess(page, fixture);
  runBackendAssert(fixturePath);
});

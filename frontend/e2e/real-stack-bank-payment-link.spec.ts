import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Locator, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface Credentials {
  email: string;
  password: string;
}

interface BankPaymentLinkFixture {
  owner: Credentials;
  owner_create_student: {
    id: number;
    name: string;
  };
  owner_manual_review: {
    student_id: number;
    student_name: string;
    order_id: number;
    private_error_code: string;
    private_error_message: string;
    provider_name: string;
    provider_status_label: string;
    provider_reference_suffix: string;
  };
  finance_workspace: {
    manual_payment_id: number;
    manual_student_name: string;
    confirmed_online_payment_id: number;
    confirmed_online_student_name: string;
  };
  trainer: Credentials;
  trainer_student: {
    id: number;
    name: string;
    debt_id: number;
    debt_checkin_id: number;
    order_id: number;
  };
  target_group: {
    schedule_id: number;
    name: string;
    start_date: string;
    training_group_id: number;
    rollout_mode: string;
    second_schedule_id: number;
    second_start_date: string;
    new_writes_enabled: boolean;
    manual_operational_admission_enabled: boolean;
  };
  student: Credentials & {
    student_id: number;
    name: string;
    order_id: number;
  };
  parent: Credentials & {
    child_id: number;
    child_name: string;
    order_id: number;
  };
  tariff: {
    id: number;
    name: string;
    price: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a bank payment link fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): BankPaymentLinkFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<BankPaymentLinkFixture>;
  if (
    !data.owner?.email ||
    !data.owner.password ||
    !data.owner_create_student?.id ||
    !data.owner_create_student.name ||
    !data.owner_manual_review?.order_id ||
    !data.owner_manual_review.student_name ||
    !data.owner_manual_review.private_error_code ||
    !data.owner_manual_review.private_error_message ||
    !data.owner_manual_review.provider_name ||
    !data.owner_manual_review.provider_status_label ||
    !data.owner_manual_review.provider_reference_suffix ||
    !data.finance_workspace?.manual_payment_id ||
    !data.finance_workspace.manual_student_name ||
    !data.finance_workspace.confirmed_online_payment_id ||
    !data.finance_workspace.confirmed_online_student_name
  ) {
    throw new Error("Fixture owner credentials and payment operations are required.");
  }
  if (
    !data.trainer?.email ||
    !data.trainer.password ||
    !data.trainer_student?.id ||
    !data.trainer_student.debt_id ||
    !data.trainer_student.debt_checkin_id ||
    !data.target_group?.schedule_id ||
    !data.target_group.name ||
    !data.target_group.start_date ||
    !data.target_group.training_group_id ||
    data.target_group.rollout_mode !== "active" ||
    !data.target_group.second_schedule_id ||
    !data.target_group.second_start_date ||
    data.target_group.new_writes_enabled !== true ||
    data.target_group.manual_operational_admission_enabled !== true
  ) {
    throw new Error("Fixture trainer credentials, student target, and debt target are required.");
  }
  if (!data.student?.email || !data.student.password || !data.student.student_id) {
    throw new Error("Fixture student credentials and renewal target are required.");
  }
  if (!data.parent?.email || !data.parent.password || !data.parent.child_id) {
    throw new Error("Fixture parent credentials and child target are required.");
  }
  if (!data.tariff?.id || !data.tariff.name) {
    throw new Error("Fixture tariff data is required.");
  }
  return data as BankPaymentLinkFixture;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function formatFixtureDate(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${value}T00:00:00Z`));
}

async function login(page: Page, credentials: Credentials, expectedPath: RegExp): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(credentials.email);
  await page.getByLabel("Пароль").fill(credentials.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(expectedPath);
}

async function loginToDashboard(page: Page, credentials: Credentials): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(credentials.email);
  await page.getByLabel("Пароль").fill(credentials.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/?$/);
}

function paymentPanel(page: Page, title: string): Locator {
  return page.getByRole("region", { name: title }).first();
}

function isPostTo(pathname: string | RegExp) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    const matches = typeof pathname === "string" ? url.pathname === pathname : pathname.test(url.pathname);
    return matches && response.request().method() === "POST";
  };
}

async function expectQrCollapsed(panel: Locator): Promise<void> {
  await expect(panel.getByRole("button", { name: "Показать QR" })).toHaveAttribute(
    "aria-expanded",
    "false",
  );
  await expect(panel.getByRole("img", { name: "QR-код ссылки на оплату" })).toHaveCount(0);
}

async function expandQr(panel: Locator): Promise<void> {
  const toggle = panel.getByRole("button", { name: "Показать QR" });
  await toggle.focus();
  await expect(toggle).toBeFocused();
  await toggle.press("Enter");
  const expandedToggle = panel.getByRole("button", { name: "Скрыть QR" });
  await expect(expandedToggle).toBeFocused();
  await expect(expandedToggle).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await expect(panel.getByRole("img", { name: "QR-код ссылки на оплату" })).toBeVisible();
  await expect(panel.locator('[aria-label="QR-код ссылки на оплату"] svg')).toBeVisible();
}

async function expectPaymentPanelResponsiveAtAcceptanceWidths(
  page: Page,
  panel: Locator,
): Promise<void> {
  const assertNoHorizontalOverflow = async (): Promise<void> => {
    const metrics = await panel.evaluate((element) => {
      const documentElement = document.documentElement;
      const panelRect = element.getBoundingClientRect();
      return {
        documentClientWidth: documentElement.clientWidth,
        documentScrollWidth: documentElement.scrollWidth,
        panelLeft: panelRect.left,
        panelRight: panelRect.right,
      };
    });
    expect(metrics.documentScrollWidth).toBeLessThanOrEqual(metrics.documentClientWidth + 1);
    expect(metrics.panelLeft).toBeGreaterThanOrEqual(-1);
    expect(metrics.panelRight).toBeLessThanOrEqual(metrics.documentClientWidth + 1);
  };

  await page.setViewportSize({ width: 320, height: 720 });
  await assertNoHorizontalOverflow();

  await page.setViewportSize({ width: 640, height: 900 });
  await page.evaluate(() => {
    document.documentElement.style.zoom = "2";
  });
  await assertNoHorizontalOverflow();
  await page.evaluate(() => {
    document.documentElement.style.zoom = "";
  });
  await page.setViewportSize({ width: 320, height: 720 });
}

async function expectStaffPaymentActions(panel: Locator): Promise<void> {
  await expect(panel.getByRole("button", { name: "Отправить ссылку" })).toBeVisible();
  await expect(panel.getByRole("button", { name: "Скопировать ссылку" })).toBeVisible();
  await expect(panel.getByRole("link", { name: "Открыть предпросмотр" })).toBeVisible();
  await expect(panel).not.toContainText(/https?:\/\//);
}

async function expectSelfServicePaymentAction(panel: Locator): Promise<void> {
  const pay = panel.getByRole("link", { name: "Оплатить через СБП" });
  await expect(pay).toBeVisible();
  await expect(pay).not.toHaveAttribute("target", "_blank");
  await expect(panel.getByRole("button", { name: "Показать QR" })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Отправить ссылку" })).toHaveCount(0);
  await expect(panel).not.toContainText(/https?:\/\//);
}

async function assertOwnerDashboardResponsiveAtAcceptanceWidths(page: Page): Promise<void> {
  const assertNoHorizontalOverflow = async (): Promise<void> => {
    const metrics = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth + 1);
  };

  await page.setViewportSize({ width: 320, height: 720 });
  await assertNoHorizontalOverflow();
  await page.setViewportSize({ width: 640, height: 900 });
  await page.evaluate(() => {
    document.documentElement.style.zoom = "2";
  });
  await assertNoHorizontalOverflow();
  await page.evaluate(() => {
    document.documentElement.style.zoom = "";
  });
  await page.setViewportSize({ width: 320, height: 720 });
}

async function verifyOwnerCreationAndRecovery(
  page: Page,
  fixture: BankPaymentLinkFixture,
): Promise<void> {
  await page.setViewportSize({ width: 320, height: 720 });
  await loginToDashboard(page, fixture.owner);
  await page.goto(backendUrl("/dashboard/billing/payments/"));
  await expect(page.getByRole("heading", { name: "Оплаты" })).toBeVisible();
  const manualPayment = page.locator(`#payment-${fixture.finance_workspace.manual_payment_id}`);
  await expect(
    manualPayment.getByText(fixture.finance_workspace.manual_student_name, { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(fixture.owner_manual_review.student_name, { exact: true })).toHaveCount(0);
  await expect(
    page.getByText(fixture.finance_workspace.confirmed_online_student_name, { exact: true }),
  ).toHaveCount(0);
  await manualPayment.getByRole("button", { name: "Подтвердить" }).click();
  const confirmResponse = page.waitForResponse(
    isPostTo(`/dashboard/billing/payments/${fixture.finance_workspace.manual_payment_id}/verify/`),
    { timeout: 20_000 },
  );
  await manualPayment.getByRole("button", { name: "Да, подтвердить" }).click();
  expect((await confirmResponse).status()).toBe(204);
  await expect(page).toHaveURL(/\/dashboard\/billing\/payments\/$/);

  await page.getByRole("link", { name: "История" }).click();
  await expect(page).toHaveURL(/\/dashboard\/billing\/payments\/\?queue=history/);
  const confirmedManual = page.locator(`#payment-${fixture.finance_workspace.manual_payment_id}`);
  const confirmedOnline = page.locator(
    `#payment-${fixture.finance_workspace.confirmed_online_payment_id}`,
  );
  await expect(confirmedManual.getByText("Подтверждён", { exact: true })).toBeVisible();
  await expect(confirmedOnline.getByText("Подтверждён", { exact: true })).toBeVisible();
  await expect(confirmedOnline).toContainText("Групповое обучение");
  await expect(confirmedOnline).toContainText(fixture.target_group.name);
  await expect(confirmedOnline.getByRole("button", { name: "Подтвердить" })).toHaveCount(0);

  await page.getByRole("link", { name: /Проблемы онлайн-оплаты/ }).click();
  await expect(page).toHaveURL(/\/dashboard\/billing\/payments\/\?queue=online/);
  const reviewQueue = page.getByRole("region", { name: "Спорные онлайн-оплаты" });
  await expect(reviewQueue).toContainText(fixture.owner_manual_review.student_name);
  await expect(reviewQueue).not.toContainText(
    fixture.finance_workspace.confirmed_online_student_name,
  );
  const providerReviewOrder = reviewQueue.locator(
    `[data-manual-review-order-id="${fixture.owner_manual_review.order_id}"]`,
  );
  const providerEvidence = providerReviewOrder.locator('[aria-label="Данные провайдера"]');
  await expect(providerReviewOrder).toContainText("Групповое обучение");
  await expect(providerReviewOrder).toContainText(fixture.target_group.name);
  await expect(providerReviewOrder).toContainText(formatFixtureDate(fixture.target_group.start_date));
  await expect(providerEvidence).toContainText(`Провайдер: ${fixture.owner_manual_review.provider_name}`);
  await expect(providerEvidence).toContainText(
    `Статус банка: ${fixture.owner_manual_review.provider_status_label}`,
  );
  await expect(providerEvidence).toContainText(fixture.owner_manual_review.provider_reference_suffix);
  await expect(providerReviewOrder.getByRole("button", { name: "ПОДТВЕРДИТЬ" })).toBeVisible();
  await expect(providerReviewOrder.getByRole("button", { name: "ОТКЛОНИТЬ" })).toBeVisible();

  await page.goto(backendUrl("/dashboard/billing/subscriptions/"));
  await expect(page.getByRole("heading", { name: "Абонементы" })).toBeVisible();
  await page.getByRole("button", { name: "Новый абонемент" }).click();

  const form = page.locator("#create-sub-form");
  await expect(form).toBeVisible();
  await form.getByLabel("Ученик").selectOption(String(fixture.owner_create_student.id));
  await form.getByLabel("Тариф").selectOption(String(fixture.tariff.id));
  await form.getByLabel("Способ оплаты").selectOption("online");
  const createButton = form.getByRole("button", { name: "СОЗДАТЬ ССЫЛКУ СБП" });
  await expect(createButton).toBeEnabled();

  const createResponse = page.waitForResponse(
    isPostTo("/dashboard/billing/subscriptions/create/"),
    { timeout: 20_000 },
  );
  await createButton.click();
  expect((await createResponse).ok()).toBe(true);
  await expect(page).toHaveURL(/\/dashboard\/billing\/payments\/(?:\?bank_order=\d+)?$/);

  const activeLinks = page.getByRole("region", { name: "Активные ссылки СБП" });
  await expect(activeLinks).toBeVisible();
  const ownerOrder = activeLinks.locator("[data-bank-payment-order-id]").filter({
    hasText: fixture.owner_create_student.name,
  });
  await expect(ownerOrder).toHaveCount(1);
  await expect(ownerOrder.getByRole("button", { name: "ОТПРАВИТЬ ССЫЛКУ" })).toBeVisible();
  await expect(ownerOrder.getByRole("button", { name: "СКОПИРОВАТЬ ССЫЛКУ" })).toBeVisible();
  const qrToggle = ownerOrder.getByRole("button", { name: "ПОКАЗАТЬ QR" });
  await expect(qrToggle).toHaveAttribute("aria-expanded", "false");
  await qrToggle.focus();
  await expect(qrToggle).toBeFocused();
  await qrToggle.press("Enter");
  const expandedQrToggle = ownerOrder.getByRole("button", { name: "СКРЫТЬ QR" });
  await expect(expandedQrToggle).toBeFocused();
  await expect(expandedQrToggle).toHaveAttribute("aria-expanded", "true");
  const qrImage = ownerOrder.getByRole("img", { name: "QR-код ссылки на оплату" });
  await expect(qrImage).toBeVisible();
  await expect(ownerOrder.locator('[aria-label="QR-код ссылки на оплату"] svg')).toBeVisible();
  await expect
    .poll(() => qrImage.evaluate((element) => element.getBoundingClientRect().top))
    .toBeGreaterThanOrEqual(-1);
  await expect
    .poll(() => qrImage.evaluate((element) => element.getBoundingClientRect().bottom))
    .toBeLessThanOrEqual(721);
  const previewLink = ownerOrder.getByRole("link", { name: "ОТКРЫТЬ ПРЕДПРОСМОТР" });
  await expect(previewLink).toBeVisible();
  await expect(previewLink).toHaveAttribute("target", "_blank");
  await expect(ownerOrder).not.toContainText(/https?:\/\//);

  const manualReview = page.getByRole("region", { name: "Спорные онлайн-оплаты" });
  await expect(page.getByRole("link", { name: /Проблемы онлайн-оплаты/ })).toBeVisible();
  await expect(manualReview).toBeVisible();
  const manualReviewOrder = manualReview.locator(
    `[data-manual-review-order-id="${fixture.owner_manual_review.order_id}"]`,
  );
  await expect(manualReviewOrder).toContainText(fixture.owner_manual_review.student_name);
  await expect(manualReviewOrder).toContainText("Групповое обучение");
  await expect(manualReviewOrder).toContainText(fixture.target_group.name);
  await expect(manualReviewOrder.getByRole("button", { name: "СВЕРИТЬ С БАНКОМ" })).toHaveCount(0);
  await expect(manualReviewOrder.getByRole("button", { name: "ПОДТВЕРДИТЬ" })).toBeVisible();
  await expect(manualReviewOrder.getByRole("button", { name: "ОТКЛОНИТЬ" })).toBeVisible();
  const evidence = manualReviewOrder.locator('[aria-label="Данные провайдера"]');
  await expect(evidence).toContainText(`Провайдер: ${fixture.owner_manual_review.provider_name}`);
  await expect(evidence).toContainText(
    `Статус банка: ${fixture.owner_manual_review.provider_status_label}`,
  );
  await expect(evidence).toContainText(fixture.owner_manual_review.provider_reference_suffix);
  await expect(page.locator("body")).not.toContainText(fixture.owner_manual_review.private_error_code);
  await expect(page.locator("body")).not.toContainText(fixture.owner_manual_review.private_error_message);
  await assertOwnerDashboardResponsiveAtAcceptanceWidths(page);
}

async function cancelPaymentPanel(
  page: Page,
  panel: Locator,
  cancelLabel: string,
  cancelPath: string | RegExp,
): Promise<void> {
  await panel.getByRole("button", { name: cancelLabel }).click();
  const cancelSheet = page.getByRole("dialog").filter({ hasText: "Отменить оплату?" });
  await expect(cancelSheet).toBeVisible();

  const cancelResponse = page.waitForResponse(isPostTo(cancelPath), { timeout: 20_000 });
  await cancelSheet.getByRole("button", { name: cancelLabel }).click();
  expect((await cancelResponse).ok()).toBe(true);
  await expect(cancelSheet).toHaveCount(0);
}

async function verifyTrainerCreationReadinessAndCancellation(
  page: Page,
  fixture: BankPaymentLinkFixture,
): Promise<void> {
  await login(page, fixture.trainer, /\/trainer\/?$/);
  await page.goto(`/trainer/students/${fixture.trainer_student.id}`);
  await expect(page.getByRole("heading", { name: fixture.trainer_student.name })).toBeVisible({
    timeout: 20_000,
  });
  const panel = paymentPanel(page, "Онлайн-оплата");
  await expect(panel).toBeVisible({ timeout: 20_000 });
  await expectStaffPaymentActions(panel);
  await expectQrCollapsed(panel);
  await expandQr(panel);
  await expectPaymentPanelResponsiveAtAcceptanceWidths(page, panel);

  await cancelPaymentPanel(
    page,
    panel,
    "Отменить оплату",
    `/api/billing/bank-payment-orders/${fixture.trainer_student.order_id}/cancel/`,
  );
  const cancelledPanel = paymentPanel(page, "Последняя онлайн-оплата");
  await expect(cancelledPanel).toBeVisible();
  await expect(cancelledPanel).toContainText("Отменена");
  await expect(cancelledPanel.getByRole("link", { name: "Открыть предпросмотр" })).toHaveCount(0);

  const paymentAction = page.getByRole("button", { name: "Принять оплату", exact: true });
  await expect(paymentAction).toBeVisible();
  await paymentAction.click();
  await page.getByRole("button", { name: new RegExp(escapeRegExp(fixture.tariff.name)) }).click();
  const targetGroup = page.getByRole("radio", {
    name: new RegExp(escapeRegExp(fixture.target_group.name)),
  });
  await expect(targetGroup).toHaveCount(1);
  await targetGroup.click();
  const occurrences = page
    .getByRole("radiogroup", { name: "Выбор первого занятия" })
    .getByRole("radio");
  const primary = occurrences.filter({ hasText: formatFixtureDate(fixture.target_group.start_date) });
  const secondary = occurrences.filter({ hasText: formatFixtureDate(fixture.target_group.second_start_date) });
  await expect(primary).toHaveCount(1);
  await expect(secondary).toHaveCount(1);
  await primary.click();
  await page.getByLabel(new RegExp(`Долг #${fixture.trainer_student.debt_checkin_id}`)).check();
  await expect(page.getByText("1/1")).toBeVisible();
  await expect(page.getByRole("button", { name: "СБП" })).toBeEnabled();
}

async function verifyStudentAutomaticReturn(
  page: Page,
  fixture: BankPaymentLinkFixture,
): Promise<void> {
  await login(page, fixture.student, /\/student\/?$/);
  await page.goto("/student");
  await expect(page.getByText(fixture.tariff.name, { exact: true })).toBeVisible({
    timeout: 20_000,
  });

  await expect(page.getByRole("button", { name: "Продлить через СБП" })).toHaveCount(0);
  const panel = paymentPanel(page, "Продление ожидает оплаты");
  await expect(panel).toBeVisible();
  await expectSelfServicePaymentAction(panel);

  await panel.getByRole("link", { name: "Оплатить через СБП" }).click();
  await expect(page.getByRole("heading", { name: "Тестовая оплата СБП" })).toBeVisible({
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Оплатить тестовый заказ" }).click();
  await expect(page.getByRole("heading", { name: "Оплата подтверждена" })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page).not.toHaveURL(/(?:\?|&)state=/);

  await page.reload();
  await expect(page.getByRole("heading", { name: "Оплата подтверждена" })).toBeVisible();

  await page.goBack();
  await expect(page.getByRole("heading", { name: "Тестовая оплата СБП" })).toBeVisible();
  await page.goForward();
  await expect(page.getByRole("heading", { name: "Оплата подтверждена" })).toBeVisible();

  const reopened = await page.context().newPage();
  await reopened.goto("/payments/return");
  await expect(reopened.getByRole("heading", { name: "Оплата подтверждена" })).toBeVisible();
  await reopened.close();
}

async function verifyParentDisabledCreationAndCancellation(
  page: Page,
  fixture: BankPaymentLinkFixture,
): Promise<void> {
  await login(page, fixture.parent, /\/parent\/?$/);
  await page.goto(`/parent/child/${fixture.parent.child_id}`);
  await expect(page.getByRole("heading", { name: fixture.parent.child_name })).toBeVisible({
    timeout: 20_000,
  });

  await expect(page.getByRole("button", { name: "Продлить через СБП" })).toHaveCount(0);
  const panel = paymentPanel(page, "Продление ожидает оплаты");
  await expect(panel).toBeVisible();
  await expectSelfServicePaymentAction(panel);

  await cancelPaymentPanel(
    page,
    panel,
    "Отменить продление",
    `/api/parents/children/${fixture.parent.child_id}/bank-payment-orders/${fixture.parent.order_id}/cancel/`,
  );
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
        ["manage.py", "assert_bank_payment_link_e2e", "--fixture", fixturePath],
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

test("real-stack bank payment links cancel safely and complete an automatic mock return", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 720 });
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await verifyOwnerCreationAndRecovery(page, fixture);
  await verifyTrainerCreationReadinessAndCancellation(page, fixture);
  await verifyStudentAutomaticReturn(page, fixture);
  await verifyParentDisabledCreationAndCancellation(page, fixture);
  runBackendAssert(fixturePath);
});

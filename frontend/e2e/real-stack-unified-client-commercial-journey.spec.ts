import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

interface FixturePerson {
  id: number;
  first_name: string;
}

interface UnifiedCommercialFixture {
  trainer: { email: string; password: string; user_id: number };
  trial_lead: FixturePerson;
  contact_lead: FixturePerson;
  archived_lead: FixturePerson;
  student: FixturePerson;
  cash_group_lead: FixturePerson;
  sbp_group_lead: FixturePerson;
  checkin_group_lead: FixturePerson;
  reject_group_lead: FixturePerson;
  commercial: {
    protocol_version: "v2";
    tariff_id: number;
    tariff_name: string;
    tariff_price: string;
    tariff_trainings_limit: number;
    tariff_duration_days: number;
    training_group_id: number;
    schedule_id: number;
    group_name: string;
    start_date: string;
    renewal_student_id: number;
    renewal_source_subscription_id: number;
    renewal_membership_id: number;
  };
  expected: { trial_display: string };
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function fixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) throw new Error("REAL_STACK_E2E_FIXTURE is required.");
  const path = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(path)) throw new Error("REAL_STACK_E2E_FIXTURE does not exist.");
  return path;
}

function readFixture(path: string): UnifiedCommercialFixture {
  const fixture = JSON.parse(readFileSync(path, "utf8")) as UnifiedCommercialFixture;
  if (!fixture.trainer?.email || !fixture.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  for (const person of [
    fixture.trial_lead,
    fixture.contact_lead,
    fixture.archived_lead,
    fixture.student,
    fixture.cash_group_lead,
    fixture.sbp_group_lead,
    fixture.checkin_group_lead,
    fixture.reject_group_lead,
  ]) {
    if (!person?.id || !person.first_name) throw new Error("Fixture person is incomplete.");
  }
  if (!fixture.expected?.trial_display) throw new Error("Fixture trial display is required.");
  if (
    fixture.commercial?.protocol_version !== "v2" ||
    !fixture.commercial.tariff_id ||
    !fixture.commercial.tariff_name ||
    !/^\d+\.\d{2}$/.test(fixture.commercial.tariff_price) ||
    !Number.isSafeInteger(fixture.commercial.tariff_trainings_limit) ||
    fixture.commercial.tariff_trainings_limit <= 0 ||
    !Number.isSafeInteger(fixture.commercial.tariff_duration_days) ||
    fixture.commercial.tariff_duration_days <= 0 ||
    !fixture.commercial.training_group_id ||
    !fixture.commercial.schedule_id ||
    !fixture.commercial.group_name ||
    !/^\d{4}-\d{2}-\d{2}$/.test(fixture.commercial.start_date) ||
    !fixture.commercial.renewal_student_id ||
    !fixture.commercial.renewal_source_subscription_id ||
    !fixture.commercial.renewal_membership_id
  ) {
    throw new Error("Fixture commercial context is incomplete.");
  }
  return fixture;
}

async function login(page: Page, fixture: UnifiedCommercialFixture): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill(fixture.trainer.email);
  await page.getByLabel("Пароль").fill(fixture.trainer.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

function isPost(path: string) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === path && response.request().method() === "POST";
  };
}

function postData(response: Response): Record<string, unknown> {
  const data = response.request().postDataJSON();
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("Expected an object request payload.");
  }
  return data as Record<string, unknown>;
}

async function searchAndOpen(page: Page, person: FixturePerson): Promise<void> {
  await page.goto("/trainer/students");
  const search = page.getByPlaceholder("Поиск по имени или телефону");
  await search.fill(person.first_name);
  const resultName = page.getByText(person.first_name, { exact: true });
  await expect(resultName).toBeVisible();
  await resultName.locator("..").getByRole("button", { name: "Открыть" }).click();
  await expect(page.getByRole("heading", { name: person.first_name })).toBeVisible();
}

function runBackendAssert(path: string): void {
  const command = process.env.REAL_STACK_E2E_ASSERT_COMMAND;
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = command
    ? execSync(command, {
        cwd: repoRoot,
        encoding: "utf8",
        env: { ...process.env, REAL_STACK_E2E_FIXTURE: path },
        stdio: ["ignore", "pipe", "pipe"],
      })
    : execFileSync(
        pythonBin,
        ["manage.py", "assert_unified_client_commercial_journey_e2e", "--fixture", path],
        {
          cwd: repoRoot,
          encoding: "utf8",
          env: { ...process.env, REAL_STACK_E2E_FIXTURE: path },
          stdio: ["ignore", "pipe", "pipe"],
        },
      );
  expect((JSON.parse(output) as { ok?: boolean }).ok).toBe(true);
}

function formattedRubles(price: string): string {
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(Number(price))} ₽`;
}

function v2GroupCommandPayload({
  payload,
  fixture,
  studentId,
  paymentMethod,
}: {
  payload: Record<string, unknown>;
  fixture: UnifiedCommercialFixture;
  studentId: number;
  paymentMethod?: "cash" | "transfer";
}): void {
  const expectedFields = [
    "expected_offer_digest",
    "idempotency_key",
    "protocol_version",
    "student_id",
    "tariff_id",
    "target_schedule_id",
    "target_start_date",
    "target_training_group_id",
    ...(paymentMethod ? ["payment_method"] : []),
  ].sort();
  expect(Object.keys(payload).sort()).toEqual(expectedFields);
  expect(payload).toMatchObject({
    protocol_version: "v2",
    student_id: studentId,
    tariff_id: fixture.commercial.tariff_id,
    target_training_group_id: fixture.commercial.training_group_id,
    target_schedule_id: fixture.commercial.schedule_id,
    target_start_date: fixture.commercial.start_date,
    ...(paymentMethod ? { payment_method: paymentMethod } : {}),
  });
  expect(payload.expected_offer_digest).toEqual(expect.stringMatching(/^v2\.[a-f0-9]{64}$/));
  expect(payload.idempotency_key).toEqual(expect.stringMatching(/^.{1,120}$/));
}

async function createBrowserV2GroupSale({
  page,
  fixture,
  person,
  paymentMethod,
}: {
  page: Page;
  fixture: UnifiedCommercialFixture;
  person: FixturePerson;
  paymentMethod: "cash" | "sbp";
}): Promise<Response> {
  await page.goto(`/trainer/leads?lead=${person.id}`);
  await expect(page.getByRole("heading", { name: person.first_name })).toBeVisible();
  const primaryEnrollment = page.getByRole("button", { name: "Оформить обучение" });
  if (await primaryEnrollment.isVisible()) {
    await primaryEnrollment.click();
  } else {
    await page.getByRole("button", { name: "Ещё" }).click();
    await page.getByRole("button", { name: /^Оформить .*группу$/ }).click();
  }
  await expect(page.getByRole("heading", { name: "Оформить обучение" })).toBeVisible();
  const tariffButton = page.getByRole("button", { name: new RegExp(fixture.commercial.tariff_name) });
  await expect(tariffButton).toContainText(formattedRubles(fixture.commercial.tariff_price));
  await expect(tariffButton).toContainText(`${fixture.commercial.tariff_trainings_limit} занятий`);
  await expect(tariffButton).toContainText(`${fixture.commercial.tariff_duration_days} дней`);
  await tariffButton.click();
  const groupButton = page.getByRole("button", { name: new RegExp(fixture.commercial.group_name) });
  await expect(groupButton).toBeVisible();
  await groupButton.click();
  await page.getByRole("button", { name: fixture.commercial.start_date, exact: true }).click();
  await page.getByRole("button", { name: "Проверить условия" }).click();
  await expect(page.getByRole("heading", { name: "Проверка перед оформлением" })).toBeVisible();
  await expect(page.getByText(fixture.commercial.group_name, { exact: true })).toBeVisible();
  await expect(page.getByText(fixture.commercial.start_date, { exact: true })).toBeVisible();
  await expect(page.getByText(formattedRubles(fixture.commercial.tariff_price), { exact: true })).toBeVisible();
  await expect(
    page.getByText(
      `${fixture.commercial.tariff_name} · ${fixture.commercial.tariff_trainings_limit} занятий · ${fixture.commercial.tariff_duration_days} дней`,
      { exact: true },
    ),
  ).toBeVisible();
  if (paymentMethod === "sbp") {
    await page.getByRole("button", { name: "СБП" }).click();
  }
  const route =
    paymentMethod === "sbp"
      ? "/api/billing/v2/group-sales/bank-orders/"
      : "/api/billing/v2/group-sales/manual/";
  const responsePromise = page.waitForResponse(isPost(route));
  await page
    .getByRole("button", {
      name:
        paymentMethod === "sbp"
          ? new RegExp("^Создать ссылку СБП")
          : new RegExp("^Зафиксировать наличные"),
    })
    .click();
  return responsePromise;
}

async function replayExactBrowserCommand({
  page,
  response,
}: {
  page: Page;
  response: Response;
}) {
  const authorization = response.request().headers()["authorization"];
  if (!authorization) throw new Error("The frontend command did not authenticate its API request.");
  return page.context().request.post(response.url(), {
    data: postData(response),
    headers: { authorization },
  });
}

test("real-stack unified workspaces, search, CTA, contact, and reopen pass", async ({ page }) => {
  test.setTimeout(120_000);
  const path = fixturePath();
  const fixture = readFixture(path);
  await login(page, fixture);

  await page.goto("/trainer/students");
  await expect(page.getByRole("button", { name: "Без абонемента 2" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Заявки" })).toHaveCount(0);
  await expect(page.getByText(fixture.student.first_name)).toBeVisible();
  await expect(page.getByText("Без абонемента в CRM")).toHaveCount(2);

  await searchAndOpen(page, fixture.trial_lead);
  await expect(page).toHaveURL(new RegExp(`/trainer/leads\\?lead=${fixture.trial_lead.id}`));
  await expect(page.getByText(fixture.expected.trial_display)).toBeVisible();
  await expect(page.getByRole("button", { name: "Открыть пробную" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Пробная прошла" })).toHaveCount(0);
  await expect(page.locator(".ui-brand-action")).toHaveCount(1);
  await page.getByRole("button", { name: "Открыть пробную" }).click();
  await expect(page).toHaveURL(new RegExp(`/trainer/leads\\?lead=${fixture.trial_lead.id}`));
  await expect(page.getByRole("heading", { name: fixture.trial_lead.first_name })).toBeVisible();

  await searchAndOpen(page, fixture.contact_lead);
  await page.getByRole("button", { name: "Связаться" }).click();
  const contactResponse = page.waitForResponse(
    isPost(`/api/leads/${fixture.contact_lead.id}/contact-outcomes/`),
  );
  await page.getByRole("button", { name: "Сохранить результат" }).click();
  expect((await contactResponse).ok()).toBe(true);

  await page.goto("/trainer/students");
  await page
    .getByPlaceholder("Поиск по имени или телефону")
    .fill(fixture.archived_lead.first_name);
  const reopenResponse = page.waitForResponse(
    isPost(`/api/leads/${fixture.archived_lead.id}/reopen-and-claim`),
  );
  await page.getByRole("button", { name: "Вернуть и забрать" }).click();
  expect((await reopenResponse).ok()).toBe(true);
  await expect(page).toHaveURL(new RegExp(`/trainer/leads\\?lead=${fixture.archived_lead.id}`));
  await expect(page.getByRole("heading", { name: fixture.archived_lead.first_name })).toBeVisible();

  const cashResponse = await createBrowserV2GroupSale({
    page,
    fixture,
    person: fixture.cash_group_lead,
    paymentMethod: "cash",
  });
  expect(cashResponse.status()).toBe(201);
  const cashPayload = postData(cashResponse);
  v2GroupCommandPayload({
    payload: cashPayload,
    fixture,
    studentId: fixture.cash_group_lead.id,
    paymentMethod: "cash",
  });

  const cashReplay = await replayExactBrowserCommand({ page, response: cashResponse });
  expect(cashReplay.status()).toBe(200);
  const cashResult = (await cashResponse.json()) as {
    payment_id?: number;
    workspace_state?: string;
    finance_state?: string;
  };
  const cashReplayResult = (await cashReplay.json()) as {
    payment_id?: number;
    command_replayed?: boolean;
    workspace_state?: string;
    finance_state?: string;
  };
  expect(cashResult.workspace_state).toBe("student");
  expect(cashResult.finance_state).toBe("pending_manual");
  expect(cashReplayResult.payment_id).toBe(cashResult.payment_id);
  expect(cashReplayResult.command_replayed).toBe(true);
  expect(cashReplayResult.workspace_state).toBe("student");
  expect(cashReplayResult.finance_state).toBe("pending_manual");

  await expect(page).toHaveURL(new RegExp(`/trainer/students/${fixture.cash_group_lead.id}`));
  const cashReceipt = page.getByRole("region", { name: "Коммерческий контекст" });
  await expect(cashReceipt).toContainText("Групповое обучение");
  await expect(cashReceipt).toContainText(fixture.commercial.group_name);
  await expect(cashReceipt).toContainText("Оплата ожидает подтверждения владельцем");
  await page.reload();
  await expect(page.getByRole("region", { name: "Коммерческий контекст" })).toContainText(
    "Оплата ожидает подтверждения владельцем",
  );
  const cashAccountAccessResponse = page.waitForResponse(
    isPost(`/api/students/${fixture.cash_group_lead.id}/account-access/open/`),
  );
  await page.getByRole("button", { name: "Открыть кабинет" }).click();
  const cashAccountAccess = await cashAccountAccessResponse;
  expect(cashAccountAccess.status()).toBe(201);
  const cashAccountAccessResult = (await cashAccountAccess.json()) as {
    student_id?: number;
    role?: string;
    status?: string;
    created_access?: boolean;
  };
  expect(cashAccountAccessResult).toMatchObject({
    student_id: fixture.cash_group_lead.id,
    role: "student",
    status: "open",
    created_access: true,
  });

  const sbpResponse = await createBrowserV2GroupSale({
    page,
    fixture,
    person: fixture.sbp_group_lead,
    paymentMethod: "sbp",
  });
  expect(sbpResponse.status()).toBe(201);
  const sbpPayload = postData(sbpResponse);
  v2GroupCommandPayload({
    payload: sbpPayload,
    fixture,
    studentId: fixture.sbp_group_lead.id,
  });
  const sbpResult = (await sbpResponse.json()) as {
    payment_id?: number;
    bank_payment_order_id?: number;
    workspace_state?: string;
    finance_state?: string;
  };
  expect(sbpResult.workspace_state).toBe("lead");
  expect(sbpResult.finance_state).toBe("provider_pending");
  expect(sbpResult.bank_payment_order_id).toEqual(expect.any(Number));
  await expect(page.getByRole("status")).toContainText(
    "Ссылка создана. Ученик будет оформлен после подтверждения оплаты",
  );

  const sbpPendingReplay = await replayExactBrowserCommand({ page, response: sbpResponse });
  expect(sbpPendingReplay.status()).toBe(200);
  const sbpPendingReplayResult = (await sbpPendingReplay.json()) as {
    payment_id?: number;
    bank_payment_order_id?: number;
    command_replayed?: boolean;
    workspace_state?: string;
    finance_state?: string;
  };
  expect(sbpPendingReplayResult).toMatchObject({
    payment_id: sbpResult.payment_id,
    bank_payment_order_id: sbpResult.bank_payment_order_id,
    command_replayed: true,
    workspace_state: "lead",
    finance_state: "provider_pending",
  });

  const renewalPayload = {
    student_id: fixture.commercial.renewal_student_id,
    tariff_id: fixture.commercial.tariff_id,
    discount_ids: [],
    debt_ids: [],
    target_training_group_id: fixture.commercial.training_group_id,
    target_schedule_id: fixture.commercial.schedule_id,
    target_start_date: fixture.commercial.start_date,
    renewed_from_subscription_id: fixture.commercial.renewal_source_subscription_id,
    idempotency_key: `slice6-browser-renewal-${fixture.commercial.renewal_source_subscription_id}`,
  };
  const authorization = cashResponse.request().headers()["authorization"];
  if (!authorization) throw new Error("The frontend session is missing authorization.");
  const renewalOrder = await page.context().request.post("/api/billing/bank-payment-orders/", {
    data: renewalPayload,
    headers: { authorization },
  });
  expect(renewalOrder.status()).toBe(201);
  const renewalReplay = await page.context().request.post("/api/billing/bank-payment-orders/", {
    data: renewalPayload,
    headers: { authorization },
  });
  expect(renewalReplay.status()).toBe(200);
  const renewalResult = (await renewalOrder.json()) as { id?: number; payment_id?: number; command_replayed?: boolean };
  const renewalReplayResult = (await renewalReplay.json()) as { id?: number; payment_id?: number; command_replayed?: boolean };
  expect(renewalReplayResult.id).toBe(renewalResult.id);
  expect(renewalReplayResult.payment_id).toBe(renewalResult.payment_id);
  expect(renewalReplayResult.command_replayed).toBe(true);

  await page.goto(`/trainer/students/${fixture.commercial.renewal_student_id}`);
  const renewalReceipt = page.getByRole("region", { name: "Коммерческий контекст" });
  await expect(renewalReceipt).toContainText("Продление абонемента");
  await expect(renewalReceipt).toContainText("Ожидает оплаты через СБП");
  await expect(renewalReceipt).toContainText("Ссылка на оплату СБП");
  await page.reload();
  await expect(page.getByRole("region", { name: "Коммерческий контекст" })).toContainText(
    "Ожидает оплаты через СБП",
  );

  runBackendAssert(path);

  const sbpConfirmedReplay = await replayExactBrowserCommand({ page, response: sbpResponse });
  expect(sbpConfirmedReplay.status()).toBe(200);
  const sbpConfirmedResult = (await sbpConfirmedReplay.json()) as {
    payment_id?: number;
    bank_payment_order_id?: number;
    command_replayed?: boolean;
    workspace_state?: string;
    finance_state?: string;
  };
  expect(sbpConfirmedResult).toMatchObject({
    payment_id: sbpResult.payment_id,
    bank_payment_order_id: sbpResult.bank_payment_order_id,
    command_replayed: true,
    workspace_state: "student",
    finance_state: "confirmed",
  });
  await page.goto(`/trainer/students/${fixture.sbp_group_lead.id}`);
  await expect(page.getByRole("heading", { name: fixture.sbp_group_lead.first_name })).toBeVisible();
});

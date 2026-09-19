import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

interface Credentials {
  email: string;
  password: string;
}

interface ContainmentFixture {
  trainer: Credentials;
  trainer_student: {
    id: number;
    name: string;
    order_id: number;
  };
  target_group: {
    name: string;
    training_group_id: number;
    rollout_mode: string;
    new_writes_enabled: boolean;
    manual_operational_admission_enabled: boolean;
  };
  tariff: {
    name: string;
  };
  containment: {
    existing_order_id: number;
    existing_order_training_group_id: number;
    new_intent_selection_mode: string;
  };
}

interface PaymentCapabilities {
  training_group_rollout_mode?: string;
  training_group_payment_selection_mode?: string;
  canonical_group_selection_enabled?: boolean;
}

interface BankPaymentOrder {
  id: number;
  target_training_group_id?: number | null;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a containment fixture JSON file.");
  }
  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): ContainmentFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<ContainmentFixture>;
  if (
    !data.trainer?.email ||
    !data.trainer.password ||
    !data.trainer_student?.id ||
    !data.trainer_student.name ||
    !data.target_group?.name ||
    !data.target_group.training_group_id ||
    data.target_group.rollout_mode !== "containment" ||
    data.target_group.new_writes_enabled !== true ||
    data.target_group.manual_operational_admission_enabled !== true ||
    !data.tariff?.name ||
    !data.containment?.existing_order_id ||
    data.containment.existing_order_training_group_id !== data.target_group.training_group_id ||
    data.containment.new_intent_selection_mode !== "disabled"
  ) {
    throw new Error("Fixture must provide containment capability and existing canonical order data.");
  }
  return data as ContainmentFixture;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function isGet(pathname: string) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === pathname && response.request().method() === "GET";
  };
}

async function login(page: Page, credentials: Credentials): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(credentials.email);
  await page.getByLabel("Пароль").fill(credentials.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

function ordersFromPayload(payload: unknown): BankPaymentOrder[] {
  if (Array.isArray(payload)) return payload as BankPaymentOrder[];
  if (payload && typeof payload === "object" && Array.isArray((payload as { items?: unknown }).items)) {
    return (payload as { items: BankPaymentOrder[] }).items;
  }
  throw new Error("Trainer payment-order readback did not return an order list.");
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
        ["manage.py", "assert_training_group_containment_e2e", "--fixture", fixturePath],
        {
          cwd: repoRoot,
          encoding: "utf8",
          env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
          stdio: ["ignore", "pipe", "pipe"],
        },
      );
  expect((JSON.parse(output) as { ok?: boolean }).ok).toBe(true);
}

test("real-stack containment blocks new group intent while existing canonical orders remain manageable", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await login(page, fixture.trainer);
  const capabilitiesResponse = page.waitForResponse(isGet("/api/billing/payment-capabilities/"));
  const ordersResponse = page.waitForResponse(isGet("/api/billing/bank-payment-orders/"));
  await page.goto(`/trainer/students/${fixture.trainer_student.id}`);
  await expect(page.getByRole("heading", { name: fixture.trainer_student.name })).toBeVisible({
    timeout: 20_000,
  });

  const capabilities = (await (await capabilitiesResponse).json()) as PaymentCapabilities;
  expect(capabilities).toMatchObject({
    training_group_rollout_mode: "containment",
    training_group_payment_selection_mode: "disabled",
    canonical_group_selection_enabled: false,
  });
  const orders = ordersFromPayload(await (await ordersResponse).json());
  expect(orders).toContainEqual(
    expect.objectContaining({
      id: fixture.containment.existing_order_id,
      target_training_group_id: fixture.target_group.training_group_id,
    }),
  );

  const panel = page.getByRole("region", { name: "Онлайн-оплата" }).first();
  await expect(panel).toBeVisible();
  await expect(panel.getByRole("link", { name: "Открыть предпросмотр" })).toBeVisible();
  await panel.getByRole("button", { name: "Показать QR" }).click();
  await expect(panel.getByRole("img", { name: "QR-код ссылки на оплату" })).toBeVisible();

  await panel.getByRole("button", { name: "Отменить оплату" }).click();
  const cancelDialog = page.getByRole("dialog").filter({ hasText: "Отменить оплату?" });
  await expect(cancelDialog).toBeVisible();
  const cancelResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === `/api/billing/bank-payment-orders/${fixture.containment.existing_order_id}/cancel/` &&
      response.request().method() === "POST"
    );
  });
  await cancelDialog.getByRole("button", { name: "Отменить оплату" }).click();
  expect((await cancelResponse).ok()).toBe(true);
  const cancelledPanel = page.getByRole("region", { name: "Последняя онлайн-оплата" });
  await expect(cancelledPanel).toBeVisible();
  await expect(cancelledPanel).toContainText("Отменена");
  await expect(
    cancelledPanel.getByRole("link", { name: "Открыть предпросмотр" }),
  ).toHaveCount(0);

  const paymentAction = page.getByRole("button", { name: "Принять оплату", exact: true });
  await expect(paymentAction).toBeVisible();
  await paymentAction.click();
  await page.getByRole("button", { name: new RegExp(escapeRegExp(fixture.tariff.name)) }).click();
  await expect(
    page.getByRole("radio", { name: new RegExp(escapeRegExp(fixture.target_group.name)) }),
  ).toHaveCount(0);
  await expect(page.getByText(/Новые оплаты в группу временно отключены на сервере/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Принять оплату" })).toBeDisabled();
  await page.getByRole("button", { name: "СБП" }).click();
  await expect(page.getByRole("button", { name: "Создать ссылку СБП" })).toBeDisabled();

  runBackendAssert(fixturePath);
});

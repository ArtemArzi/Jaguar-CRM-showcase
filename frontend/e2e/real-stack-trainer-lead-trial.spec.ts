import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

const LEADS_PATH = "/api/leads/";
const KIOSK_CHECKIN_PATH = "/api/checkins/kiosk/";

interface TrainerLeadTrialFixture {
  kiosk_pin: string;
  phone_suffix: string;
  trainer: {
    email: string;
    password: string;
  };
  hidden_lead_id: number;
  schedule_id: number;
  training_type_id: number;
  new_lead: {
    first_name: string;
    last_name?: string;
    phone: string;
  };
  trial: {
    checkin_date: string;
    time: string;
  };
  expected: {
    group_name: string;
  };
}

interface CreatedLeadResponse {
  id: number;
}

interface BackendAssertResponse {
  ok?: boolean;
}

interface KioskCheckinResponse {
  checkin_id?: number;
  is_debt?: boolean;
  subscription_id?: number | null;
  post_trial_task_queued?: boolean;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a trainer lead/trial fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): TrainerLeadTrialFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<TrainerLeadTrialFixture>;

  if (!data.kiosk_pin || !/^\d{6}$/.test(data.kiosk_pin)) {
    throw new Error("Fixture kiosk_pin must be a 6-digit string.");
  }
  if (!data.phone_suffix || !/^\d{4}$/.test(data.phone_suffix)) {
    throw new Error("Fixture phone_suffix must be a 4-digit string.");
  }
  if (!data.trainer?.email || !data.trainer.password) {
    throw new Error("Fixture trainer credentials are required.");
  }
  if (!data.schedule_id || !data.training_type_id || !data.hidden_lead_id) {
    throw new Error("Fixture schedule/training type/hidden lead ids are required.");
  }
  if (!data.new_lead?.first_name || !data.new_lead.phone) {
    throw new Error("Fixture new lead data is required.");
  }
  if (!data.trial?.checkin_date || !data.trial.time) {
    throw new Error("Fixture trial date/time are required.");
  }
  if (!data.expected?.group_name) {
    throw new Error("Fixture expected group name is required.");
  }

  return data as TrainerLeadTrialFixture;
}

function isCreateLeadResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === LEADS_PATH && response.request().method() === "POST";
}

function isLeadStatusResponse(leadId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `${LEADS_PATH}${leadId}/status` && response.request().method() === "POST";
  };
}

function isLeadListResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === LEADS_PATH && response.request().method() === "GET";
}

function isBookTrialResponse(leadId: number) {
  return (response: Response): boolean => {
    const url = new URL(response.url());
    return url.pathname === `${LEADS_PATH}${leadId}/book-trial` && response.request().method() === "POST";
  };
}

async function loginAsTrainer(page: Page, fixture: TrainerLeadTrialFixture): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill(fixture.trainer.email);
  await page.getByLabel("Пароль").fill(fixture.trainer.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/trainer\/?$/);
}

async function createLeadFromTrainerUi(page: Page, fixture: TrainerLeadTrialFixture): Promise<number> {
  await page.goto("/trainer/leads");
  await expect(page.getByRole("heading", { name: "Заявки" })).toBeVisible();
  await expect(page.getByText("Hidden Lead")).not.toBeVisible();

  await page.getByRole("button", { name: "Новая заявка" }).click();
  await page.getByRole("textbox", { name: "Имя клиента *" }).fill(fixture.new_lead.first_name);
  await page.getByRole("textbox", { name: "Телефон клиента *" }).fill(fixture.new_lead.phone);

  const createResponsePromise = page.waitForResponse(isCreateLeadResponse, {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Создать заявку" }).click();
  const createResponse = await createResponsePromise;
  expect(createResponse.ok()).toBe(true);
  const created = (await createResponse.json()) as CreatedLeadResponse;

  await expect(page.getByRole("button", { name: /LeadTrial/ })).toBeVisible();
  await expect(page.getByText("Hidden Lead")).not.toBeVisible();
  return created.id;
}

async function moveLeadToContacted(page: Page, leadId: number): Promise<void> {
  await page.getByRole("button", { name: /LeadTrial/ }).click();
  await expect(page.getByRole("heading", { name: "LeadTrial" })).toBeVisible();

  const statusResponse = page.waitForResponse(isLeadStatusResponse(leadId), {
    timeout: 20_000,
  });
  const listRefresh = page.waitForResponse(isLeadListResponse, {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Связался" }).click();
  const response = await statusResponse;
  expect(response.ok()).toBe(true);
  await listRefresh;

  await expect(page.getByRole("dialog", { name: "LeadTrial" })).toBeHidden();
  await expect(page.getByRole("button", { name: /LeadTrial/ })).toBeVisible();
}

async function bookTrial(page: Page, fixture: TrainerLeadTrialFixture, leadId: number): Promise<void> {
  await page.getByRole("button", { name: /LeadTrial/ }).click();
  await expect(page.getByRole("dialog", { name: "LeadTrial" }).getByText("Связались")).toBeVisible();
  await page.getByRole("button", { name: "Записать на пробную" }).click();
  await page.getByLabel("Дата пробной *").fill(fixture.trial.checkin_date);
  await expect(page.getByLabel("Время пробной *")).toHaveCount(0);

  const scheduleSelect = page.getByLabel("Тренировка *");
  await expect(scheduleSelect).toBeEnabled({ timeout: 20_000 });
  await scheduleSelect.selectOption(String(fixture.schedule_id));

  const bookResponse = page.waitForResponse(isBookTrialResponse(leadId), {
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "Сохранить пробную" }).click();
  const response = await bookResponse;
  expect(response.ok()).toBe(true);
}

async function completeTrialCheckin(page: Page, fixture: TrainerLeadTrialFixture): Promise<void> {
  await page.goto(`/trainer/schedule/${fixture.schedule_id}/checkin?date=${fixture.trial.checkin_date}`);
  await expect(page.getByRole("heading", { name: /Lead Trial Proof/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /LeadTrial/ })).toBeVisible();
  await expect(page.getByText("Отметку делает ученик")).toBeVisible();
  await expect(page.getByRole("button", { name: "Отметить 1 из 1" })).toHaveCount(0);
}

async function enterPin(page: Page, pin: string): Promise<void> {
  await page.goto("/kiosk/");
  for (const [index, digit] of [...pin].entries()) {
    await page.getByLabel(`PIN цифра ${index + 1}`).fill(digit);
  }
  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
}

async function completeTrialCheckinViaKiosk(
  page: Page,
  fixture: TrainerLeadTrialFixture,
  leadId: number,
): Promise<void> {
  await enterPin(page, fixture.kiosk_pin);
  const response = await page.evaluate(
    async ({ path, studentId, scheduleId, trainingTypeId, checkinDate }) => {
      const token = localStorage.getItem("kiosk_device_token");
      if (!token) throw new Error("Kiosk activation did not persist a device token.");
      const result = await fetch(path, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Kiosk-Token": token,
        },
        body: JSON.stringify({
          student_id: studentId,
          schedule_id: scheduleId,
          training_type_id: trainingTypeId,
          checkin_date: checkinDate,
        }),
      });
      return { ok: result.ok, status: result.status, payload: await result.json() };
    },
    {
      path: KIOSK_CHECKIN_PATH,
      studentId: leadId,
      scheduleId: fixture.schedule_id,
      trainingTypeId: fixture.training_type_id,
      checkinDate: fixture.trial.checkin_date,
    },
  );
  expect(response.ok, `kiosk check-in returned ${response.status}`).toBe(true);

  const result = response.payload as KioskCheckinResponse;
  expect(result.checkin_id).toBeGreaterThan(0);
  expect(result.is_debt).toBe(false);
  expect(result.subscription_id ?? null).toBeNull();
  expect(result.post_trial_task_queued).toBe(true);
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
        ["manage.py", "assert_trainer_lead_trial_e2e", "--fixture", fixturePath],
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

test("real-stack trainer creates lead, books trial, completes check-in, and backend side effects pass", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsTrainer(page, fixture);
  const leadId = await createLeadFromTrainerUi(page, fixture);
  await moveLeadToContacted(page, leadId);
  await bookTrial(page, fixture, leadId);
  await completeTrialCheckin(page, fixture);
  await completeTrialCheckinViaKiosk(page, fixture, leadId);
  runBackendAssert(fixturePath);
});

import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface DashboardOnboardingFixture {
  owner: {
    email: string;
    password: string;
  };
  expected: {
    completed_current_step: number;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a dashboard onboarding fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): DashboardOnboardingFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<DashboardOnboardingFixture>;
  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!Number.isFinite(data.expected?.completed_current_step)) {
    throw new Error("Fixture onboarding expectations are required.");
  }
  return data as DashboardOnboardingFixture;
}

function responseFor(pathname: string): (response: Response) => boolean {
  return (response: Response) => {
    const url = new URL(response.url());
    return url.pathname === pathname && response.request().method() === "POST";
  };
}

async function loginAsOwner(page: Page, fixture: DashboardOnboardingFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/?$/);
}

async function assertStep(page: Page, heading: string): Promise<void> {
  const wizardContent = page.locator("#wizard-content");
  await expect(wizardContent).toBeVisible();
  await expect(wizardContent.getByRole("heading", { name: heading })).toBeVisible();
}

async function skipStep(page: Page, step: number, nextHeading: string): Promise<void> {
  const responsePromise = page.waitForResponse(responseFor(`/dashboard/onboarding/skip/${step}/`), {
    timeout: 20_000,
  });
  await page.locator("#wizard-content").getByRole("button", { name: "Пропустить" }).click();
  expect((await responsePromise).ok()).toBe(true);
  await assertStep(page, nextHeading);
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
    : execFileSync(pythonBin, ["manage.py", "assert_dashboard_onboarding_e2e", "--fixture", fixturePath], {
        cwd: repoRoot,
        encoding: "utf8",
        env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
        stdio: ["ignore", "pipe", "pipe"],
      });

  const result = JSON.parse(output) as BackendAssertResponse;
  expect(result.ok).toBe(true);
}

test("real-stack owner recovers an invalid trainer reference and finishes exact onboarding once", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsOwner(page, fixture);

  await page.goto(backendUrl("/dashboard/onboarding/"));
  await expect(page.getByRole("heading", { name: /Добро пожаловать/ })).toBeVisible();
  await assertStep(page, "Шаг 1: Грейды и дисциплины");

  await skipStep(page, 1, "Шаг 2: Тренеры");
  await page.goto(backendUrl("/dashboard/onboarding/"));
  await assertStep(page, "Шаг 2: Тренеры");

  const trainerStep = page.locator("#wizard-content");
  await trainerStep.locator('input[name="trainers[0].first_name"]').fill("Draft");
  await trainerStep.locator('input[name="trainers[0].last_name"]').fill("Coach");
  await trainerStep.locator('input[name="trainers[0].phone"]').fill("+79000000001");
  const trainerResponse = page.waitForResponse(responseFor("/dashboard/onboarding/step/2/"));
  await trainerStep.getByRole("button", { name: "Далее" }).click();
  expect((await trainerResponse).ok()).toBe(true);
  await assertStep(page, "Шаг 3: Расписание");

  const invalidTrainerRef = "00000000-0000-0000-0000-000000000099";
  const scheduleStep = page.locator("#wizard-content");
  const trainerSelect = scheduleStep.locator('select[name="schedules[0].trainer_choice"]');
  await trainerSelect.evaluate((element, value) => {
    const option = document.createElement("option");
    option.value = `draft:${value}`;
    option.textContent = "Unavailable trainer";
    element.append(option);
  }, invalidTrainerRef);
  await trainerSelect.selectOption(`draft:${invalidTrainerRef}`);
  await scheduleStep.locator('input[name="schedules[0].group"]').fill("Adults E2E");
  await scheduleStep.locator('select[name="schedules[0].location_id"]').selectOption({ index: 1 });
  const scheduleResponse = page.waitForResponse(responseFor("/dashboard/onboarding/step/3/"));
  await scheduleStep.getByRole("button", { name: "Далее" }).click();
  expect((await scheduleResponse).ok()).toBe(true);
  await assertStep(page, "Шаг 4: Ученики");

  await skipStep(page, 4, "Шаг 5: Тарифы");
  const failedFinishResponse = page.waitForResponse(responseFor("/dashboard/onboarding/skip/5/"));
  await page.locator("#wizard-content").getByRole("button", { name: "Пропустить" }).click();
  expect((await failedFinishResponse).ok()).toBe(true);
  await assertStep(page, "Шаг 3: Расписание");
  await expect(
    page.locator('[data-error-code="onboarding_schedule_trainer_unresolved"]'),
  ).toBeVisible();
  await expect(page.locator('input[name="schedules[0].group"]')).toHaveValue("Adults E2E");

  const draftId = await page
    .locator("#wizard-content [data-onboarding-draft-id]")
    .getAttribute("data-onboarding-draft-id");
  const csrfToken = await page.locator('#wizard-content input[name="csrfmiddlewaretoken"]').inputValue();
  expect(draftId).toBeTruthy();
  expect(csrfToken).toBeTruthy();

  const recoveredTrainerSelect = page.locator('select[name="schedules[0].trainer_choice"]');
  const recoveredTrainerValue = await recoveredTrainerSelect
    .locator("option")
    .filter({ hasText: /Draft Coach · новый/ })
    .getAttribute("value");
  expect(recoveredTrainerValue).toBeTruthy();
  await recoveredTrainerSelect.selectOption(recoveredTrainerValue!);
  const recoveredScheduleResponse = page.waitForResponse(responseFor("/dashboard/onboarding/step/3/"));
  await page.locator("#wizard-content").getByRole("button", { name: "Далее" }).click();
  expect((await recoveredScheduleResponse).ok()).toBe(true);
  await assertStep(page, "Шаг 4: Ученики");
  await skipStep(page, 4, "Шаг 5: Тарифы");

  const finishResponse = page.waitForResponse(responseFor("/dashboard/onboarding/skip/5/"));
  await page.locator("#wizard-content").getByRole("button", { name: "Пропустить" }).click();
  expect((await finishResponse).ok()).toBe(true);
  await expect(page).toHaveURL(/\/dashboard\/?$/, { timeout: 20_000 });
  await expect(page.getByText("Дашборд").first()).toBeVisible();
  await expect(page.locator("#wizard-content")).toHaveCount(0);

  const retry = await page.context().request.post(backendUrl("/dashboard/onboarding/finish/"), {
    form: { draft_id: draftId! },
    headers: { "X-CSRFToken": csrfToken },
    maxRedirects: 0,
  });
  expect(retry.status()).toBe(302);

  runBackendAssert(fixturePath);
});

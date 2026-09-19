import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface OwnerTrainerRateGridFixture {
  owner: {
    email: string;
    password: string;
  };
  location: {
    location_id: number;
    name: string;
  };
  training_types: {
    group: {
      training_type_id: number;
      name: string;
    };
    personal: {
      training_type_id: number;
      name: string;
    };
  };
  seeded_trainer: {
    trainer_id: number;
    name: string;
  };
  expected: {
    new_trainer_first_name: string;
    new_trainer_last_name: string;
    new_trainer_group_rate: string;
    new_trainer_personal_rate: string;
    seeded_personal_rate: string;
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to an owner trainer rate grid fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): OwnerTrainerRateGridFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<OwnerTrainerRateGridFixture>;
  if (!data.owner?.email || !data.owner.password) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.location?.location_id || !data.training_types?.group?.training_type_id) {
    throw new Error("Fixture rate-grid data is required.");
  }
  return data as OwnerTrainerRateGridFixture;
}

function responseFor(pathname: string): (response: Response) => boolean {
  return (response: Response) => {
    const url = new URL(response.url());
    return url.pathname === pathname && response.request().method() === "POST";
  };
}

function percentTextPattern(value: string): RegExp {
  return new RegExp(`${value.replace(".", "[,.]")}%`);
}

async function loginAsOwner(page: Page, fixture: OwnerTrainerRateGridFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.owner.email);
  await page.getByLabel("Пароль").fill(fixture.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/?$/);
}

async function createTrainerWithRates(page: Page, fixture: OwnerTrainerRateGridFixture): Promise<void> {
  const panel = page.locator("#slide-over");
  const locationId = fixture.location.location_id;
  const groupTypeId = fixture.training_types.group.training_type_id;
  const personalTypeId = fixture.training_types.personal.training_type_id;

  await page.goto(backendUrl("/dashboard/trainers/"));
  await expect(page.getByRole("heading", { name: "ТРЕНЕРЫ" })).toBeVisible();
  await page.getByRole("button", { name: /ДОБАВИТЬ/ }).click();
  await expect(panel.getByRole("heading", { name: "НОВЫЙ ТРЕНЕР" })).toBeVisible();

  await panel.locator("input[name='first_name']").fill(fixture.expected.new_trainer_first_name);
  await panel.locator("input[name='last_name']").fill(fixture.expected.new_trainer_last_name);
  await panel.locator(`input[name='location_${locationId}']`).check();
  await panel
    .locator(`input[name='percent_${locationId}_${groupTypeId}']`)
    .fill(fixture.expected.new_trainer_group_rate);
  await panel
    .locator(`input[name='percent_${locationId}_${personalTypeId}']`)
    .fill(fixture.expected.new_trainer_personal_rate);

  const createResponse = page.waitForResponse(responseFor("/dashboard/trainers/create/"), {
    timeout: 20_000,
  });
  await panel.getByRole("button", { name: "СОЗДАТЬ ТРЕНЕРА" }).click();
  expect((await createResponse).ok()).toBe(true);
  const fullName = `${fixture.expected.new_trainer_first_name} ${fixture.expected.new_trainer_last_name}`;
  await expect(page.locator("tbody tr").filter({ hasText: fullName }).first()).toBeVisible({
    timeout: 20_000,
  });
}

async function fillSeededTrainerMissingRate(page: Page, fixture: OwnerTrainerRateGridFixture): Promise<void> {
  const panel = page.locator("#slide-over");
  const locationId = fixture.location.location_id;
  const personalTypeId = fixture.training_types.personal.training_type_id;

  await page.goto(backendUrl(`/dashboard/trainers/${fixture.seeded_trainer.trainer_id}/`));
  await expect(page.getByRole("heading", { name: fixture.seeded_trainer.name })).toBeVisible();
  await expect(page.getByText("Не заполнены ставки (1)")).toBeVisible();
  await expect(page.getByText(`${fixture.location.name} — ${fixture.training_types.personal.name}`)).toBeVisible();

  await page.getByRole("button", { name: "ЗАПОЛНИТЬ СТАВКИ" }).click();
  await expect(panel.getByRole("heading", { name: "СТАВКИ" })).toBeVisible();
  await panel
    .locator(`input[name='percent_${locationId}_${personalTypeId}']`)
    .fill(fixture.expected.seeded_personal_rate);

  const detailRefreshResponse = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return (
        url.pathname === `/dashboard/trainers/${fixture.seeded_trainer.trainer_id}/` &&
        response.request().method() === "GET"
      );
    },
    { timeout: 20_000 },
  );
  const saveResponse = page.waitForResponse(responseFor(`/dashboard/trainers/${fixture.seeded_trainer.trainer_id}/rates/`), {
    timeout: 20_000,
  });
  await panel.getByRole("button", { name: "СОХРАНИТЬ СТАВКИ" }).click();
  expect((await saveResponse).ok()).toBe(true);
  await expect(panel.getByText("Ставки сохранены")).toBeVisible({ timeout: 20_000 });
  expect((await detailRefreshResponse).ok()).toBe(true);

  await expect(page.getByText("Не заполнены ставки")).not.toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(fixture.training_types.personal.name).first()).toBeVisible();
  await expect(page.getByText(percentTextPattern(fixture.expected.seeded_personal_rate)).first()).toBeVisible();
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
        ["manage.py", "assert_owner_trainer_rate_grid_e2e", "--fixture", fixturePath],
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

test("real-stack owner configures trainer rate grid and salary attribution", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsOwner(page, fixture);
  await createTrainerWithRates(page, fixture);
  await fillSeededTrainerMissingRate(page, fixture);
  runBackendAssert(fixturePath);
});

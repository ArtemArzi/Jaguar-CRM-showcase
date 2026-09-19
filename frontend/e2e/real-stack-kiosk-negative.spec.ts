import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

const CHECKIN_PATH = "/api/checkins/kiosk/";

interface KioskNegativeFixture {
  kiosk_pin: string;
  shared_guardian: {
    phone_suffix: string;
    child_a: SharedGuardianChild;
    child_b: SharedGuardianChild;
  };
  frozen_phone_suffix: string;
  blocked_phone_suffix: string;
}

interface SharedGuardianChild {
  student_id: number;
  subscription_id: number;
  schedule_id: number;
  training_group_id: number;
  group_name: string;
  name: string;
}

interface KioskCheckinResponse {
  checkin_id?: number;
  created?: boolean;
  duplicate?: boolean;
  subscription_effect?: string;
}

interface BackendAssertResponse {
  ok?: boolean;
  duplicate?: {
    checkin_id?: number;
  };
  shared_guardian?: {
    child_a?: {
      checkin_id?: number | null;
    };
    child_b?: {
      checkin_id?: number | null;
    };
  };
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a kiosk negative fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): KioskNegativeFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<KioskNegativeFixture>;

  for (const key of ["frozen_phone_suffix", "blocked_phone_suffix"] as const) {
    if (!data[key] || !/^\d{4}$/.test(data[key])) {
      throw new Error(`Fixture ${key} must be a 4-digit string.`);
    }
  }
  const sharedGuardian = data.shared_guardian;
  if (!sharedGuardian || typeof sharedGuardian !== "object") {
    throw new Error("Fixture shared guardian data is required.");
  }
  if (!/^\d{4}$/.test(String(sharedGuardian.phone_suffix ?? ""))) {
    throw new Error("Fixture shared guardian suffix must be a 4-digit string.");
  }
  for (const childKey of ["child_a", "child_b"] as const) {
    const child = sharedGuardian[childKey];
    if (
      !child ||
      typeof child !== "object" ||
      !Number.isInteger(child.student_id) ||
      !Number.isInteger(child.subscription_id) ||
      !Number.isInteger(child.schedule_id) ||
      !Number.isInteger(child.training_group_id) ||
      !child.group_name ||
      !child.name
    ) {
      throw new Error(`Fixture shared guardian ${childKey} data is incomplete.`);
    }
  }
  if (!data.kiosk_pin || !/^\d{6}$/.test(data.kiosk_pin)) {
    throw new Error("Fixture kiosk_pin must be a 6-digit string.");
  }

  return data as KioskNegativeFixture;
}

function isKioskCheckinResponse(response: Response): boolean {
  const url = new URL(response.url());
  return url.pathname === CHECKIN_PATH && response.request().method() === "POST";
}

async function enterPin(page: Page, pin: string): Promise<void> {
  await page.goto("/kiosk/");

  for (const [index, digit] of [...pin].entries()) {
    await page.getByLabel(`PIN цифра ${index + 1}`).fill(digit);
  }

  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
}

async function enterPhoneSuffix(page: Page, phoneSuffix: string): Promise<void> {
  for (const digit of phoneSuffix) {
    await page.getByRole("button", { name: `Цифра ${digit}` }).click();
  }
}

async function returnToPhoneEntry(page: Page): Promise<void> {
  const backButton = page.getByRole("button", { name: "Вернуться к вводу" });
  const retryButton = page.getByRole("button", { name: "Попробовать отметиться снова" });

  if (await backButton.isVisible()) {
    await backButton.click();
  } else {
    await retryButton.click();
  }

  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
}

async function waitForCheckinResponseOrVisible(
  page: Page,
  visibleText: string,
  action: () => Promise<void>,
): Promise<Response | null> {
  const visibleResult = page.getByText(visibleText);
  const checkinResponse = page
    .waitForResponse(isKioskCheckinResponse, { timeout: 20_000 })
    .catch(() => null);

  await action();

  const response = await Promise.race([
    checkinResponse,
    visibleResult.waitFor({ state: "visible", timeout: 20_000 }).then(() => null),
  ]);
  await expect(visibleResult).toBeVisible();
  return response;
}

async function selectSharedGuardianChildFromMultipleMatches(
  page: Page,
  fixture: KioskNegativeFixture,
  child: SharedGuardianChild,
  expectedFeedback: string,
): Promise<Response | null> {
  await expect(page.getByText("Найдено несколько совпадений")).toBeVisible();
  const childAButton = page.getByRole("button", { name: fixture.shared_guardian.child_a.name });
  const childBButton = page.getByRole("button", { name: fixture.shared_guardian.child_b.name });
  await expect(childAButton).toContainText(fixture.shared_guardian.child_a.group_name);
  await expect(childBButton).toContainText(fixture.shared_guardian.child_b.group_name);

  return await waitForCheckinResponseOrVisible(page, expectedFeedback, () =>
    page.getByRole("button", { name: child.name }).click(),
  );
}

async function assertSharedChildBCheckin(
  page: Page,
  response: Response | null,
  fixturePath: string,
): Promise<number> {
  await expect(page.getByText("Посещение сохранено")).toBeVisible();
  await expect(page.getByText("Абонемент списан")).toBeVisible();

  let checkinId: number | undefined;
  if (response) {
    expect(response.ok()).toBe(true);
    const result = (await response.json()) as KioskCheckinResponse;
    expect(result.created).toBe(true);
    expect(result.duplicate).toBe(false);
    expect(result.subscription_effect).toBe("deducted");
    expect(result.checkin_id).toBeTruthy();
    checkinId = result.checkin_id;
  }

  const evidence = runBackendAssert(fixturePath);
  const assertedCheckinId = evidence.shared_guardian?.child_b?.checkin_id;
  expect(assertedCheckinId).toBeTruthy();
  if (checkinId) {
    expect(assertedCheckinId).toBe(checkinId);
  }
  await returnToPhoneEntry(page);
  return assertedCheckinId!;
}

async function assertDuplicateCheckin(
  page: Page,
  response: Response | null,
  firstCheckinId: number,
  fixturePath: string,
): Promise<void> {
  await expect(page.getByText("Вы уже отмечены на этой тренировке")).toBeVisible();

  if (response) {
    expect(response.ok()).toBe(true);
    const result = (await response.json()) as KioskCheckinResponse;
    expect(result.created).toBe(false);
    expect(result.duplicate).toBe(true);
    expect(result.subscription_effect).toBe("none");
    expect(result.checkin_id).toBe(firstCheckinId);
  } else {
    const evidence = runBackendAssert(fixturePath);
    expect(evidence.duplicate?.checkin_id).toBe(firstCheckinId);
  }

  await returnToPhoneEntry(page);
}

async function assertBusinessError(page: Page, phoneSuffix: string, expectedMessage: string): Promise<void> {
  const response = await waitForCheckinResponseOrVisible(page, expectedMessage, () =>
    enterPhoneSuffix(page, phoneSuffix),
  );
  if (response) {
    expect(response.ok()).toBe(false);
  }
  await expect(page.getByText(expectedMessage)).toBeVisible();
  await returnToPhoneEntry(page);
}

async function assertSharedChildAExactCheckin(
  page: Page,
  response: Response | null,
  fixturePath: string,
): Promise<void> {
  await expect(page.getByText("Посещение сохранено")).toBeVisible();
  await expect(page.getByText("Абонемент списан")).toBeVisible();

  if (response) {
    expect(response.ok()).toBe(true);
    const result = (await response.json()) as KioskCheckinResponse;
    expect(result.created).toBe(true);
    expect(result.duplicate).toBe(false);
    expect(result.subscription_effect).toBe("deducted");
    expect(result.checkin_id).toBeTruthy();
  }

  const evidence = runBackendAssert(fixturePath, true);
  expect(evidence.shared_guardian?.child_a?.checkin_id).toBeTruthy();
  await returnToPhoneEntry(page);
}

function runBackendAssert(
  fixturePath: string,
  expectSharedChildACheckin = false,
): BackendAssertResponse {
  const command = process.env.REAL_STACK_E2E_ASSERT_COMMAND;
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const assertionEnv = {
    ...process.env,
    REAL_STACK_E2E_FIXTURE: fixturePath,
    REAL_STACK_E2E_EXPECT_SHARED_CHILD_A_CHECKIN: expectSharedChildACheckin ? "1" : "0",
  };
  const output = command
    ? execSync(command, {
        cwd: repoRoot,
        encoding: "utf8",
        env: assertionEnv,
        stdio: ["ignore", "pipe", "pipe"],
      })
    : execFileSync(
        pythonBin,
        [
          "manage.py",
          "assert_kiosk_negative_e2e",
          "--fixture",
          fixturePath,
          ...(expectSharedChildACheckin ? ["--expect-shared-child-a-checkin"] : []),
        ],
        {
          cwd: repoRoot,
          encoding: "utf8",
          env: assertionEnv,
          stdio: ["ignore", "pipe", "pipe"],
        },
      );

  const result = JSON.parse(output) as BackendAssertResponse;
  expect(result.ok).toBe(true);
  return result;
}

function deactivateKiosk(fixturePath: string): void {
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = execFileSync(
    pythonBin,
    ["manage.py", "deactivate_kiosk_negative_e2e", "--fixture", fixturePath],
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

async function assertRevokedTokenReturnsToPin(page: Page, fixturePath: string): Promise<void> {
  deactivateKiosk(fixturePath);
  await page.goto("/kiosk/");
  await expect(page.getByText("Активация киоска")).toBeVisible({
    timeout: 20_000,
  });
}

test("real-stack kiosk keeps shared-guardian children in their exact canonical groups", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await enterPin(page, fixture.kiosk_pin);

  await enterPhoneSuffix(page, fixture.shared_guardian.phone_suffix);
  const firstResponse = await selectSharedGuardianChildFromMultipleMatches(
    page,
    fixture,
    fixture.shared_guardian.child_b,
    "Посещение сохранено",
  );
  const firstCheckinId = await assertSharedChildBCheckin(page, firstResponse, fixturePath);

  await enterPhoneSuffix(page, fixture.shared_guardian.phone_suffix);
  const duplicateResponse = await selectSharedGuardianChildFromMultipleMatches(
    page,
    fixture,
    fixture.shared_guardian.child_b,
    "Вы уже отмечены на этой тренировке",
  );
  await assertDuplicateCheckin(page, duplicateResponse, firstCheckinId, fixturePath);

  await assertBusinessError(page, fixture.frozen_phone_suffix, "Абонемент заморожен");
  await assertBusinessError(
    page,
    fixture.blocked_phone_suffix,
    "Сейчас нельзя отметиться. Обратитесь к тренеру",
  );
  runBackendAssert(fixturePath);

  await enterPhoneSuffix(page, fixture.shared_guardian.phone_suffix);
  const childAResponse = await selectSharedGuardianChildFromMultipleMatches(
    page,
    fixture,
    fixture.shared_guardian.child_a,
    "Посещение сохранено",
  );
  await assertSharedChildAExactCheckin(page, childAResponse, fixturePath);

  await assertRevokedTokenReturnsToPin(page, fixturePath);
});

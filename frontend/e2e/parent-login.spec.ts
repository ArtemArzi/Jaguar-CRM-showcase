import { openResetLogin } from "./auth-helpers";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

interface ParentLoginFixture {
  parent: {
    email: string;
    password: string;
  };
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function readFixture(): ParentLoginFixture {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a parent login fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }

  const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<ParentLoginFixture>;
  if (!fixture.parent?.email || !fixture.parent.password) {
    throw new Error("Fixture parent credentials are required.");
  }

  return fixture as ParentLoginFixture;
}

test("parent can log in, open own child, and see parent-safe details", async ({ page }) => {
  const fixture = readFixture();

  await openResetLogin(page);

  await page.getByLabel("Email").fill(fixture.parent.email);
  await page.getByLabel("Пароль").fill(fixture.parent.password);
  await page.getByRole("button", { name: "Войти" }).click();

  await expect(page).toHaveURL(/\/parent/);
  await expect(page.getByText("Connection error")).toHaveCount(0);

  const singleChildRegion = page.getByRole("region", {
    name: "Ребёнок",
    exact: true,
  });
  const mainChildRegion = page.getByRole("region", {
    name: "Главный ребёнок",
    exact: true,
  });
  const childRegion = page
    .locator('section[aria-label="Ребёнок"], section[aria-label="Главный ребёнок"]')
    .first();

  await expect(childRegion).toBeVisible();

  if (await singleChildRegion.isVisible()) {
    await expect(mainChildRegion).toHaveCount(0);
  } else {
    await expect(mainChildRegion).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Остальные дети", exact: true }),
    ).toBeVisible();
  }

  await expect(childRegion).toBeVisible();
  await expect(childRegion.getByText("Нет активного абонемента")).toHaveCount(0);
  await childRegion.getByRole("link").first().click();

  await expect(page).toHaveURL(/\/parent\/child\/\d+/);
  await expect(page.getByRole("region", { name: "Абонемент ребёнка" })).toBeVisible();
  await expect(page.getByText("Нет активного абонемента")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Прогресс по грейду" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Занятия ребёнка" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Активность" })).toBeVisible();
  await expect(page.getByText("Connection error")).toHaveCount(0);
});

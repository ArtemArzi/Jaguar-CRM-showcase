import { expect, test } from "@playwright/test";
import {
  authenticateAs,
  failUnhandledApi,
} from "./support/mock-api";

test("login page renders the form", async ({ page }) => {
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: "Вход в кабинет" })).toBeVisible();
  await expect(page.getByLabel("Email")).toBeVisible();
  await expect(page.getByLabel("Пароль")).toBeVisible();
  await expect(page.getByRole("button", { name: "Войти" })).toBeVisible();
});

test("app entry sends anonymous users to login", async ({ page }) => {
  await page.goto("/app");

  await expect(page.getByRole("heading", { name: "Кабинет клуба" })).toBeVisible();
  await page.getByRole("link", { name: /Войти в кабинет/ }).click();
  await expect(page).toHaveURL(/\/login$/);
});

for (const path of ["/trainer", "/student", "/parent"]) {
  test(`protected route ${path} redirects anonymous users to login`, async ({
    page,
  }) => {
    await page.goto(path);

    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole("heading", { name: "Вход в кабинет" })).toBeVisible();
  });
}

for (const role of ["owner", "admin"] as const) {
  test(`app entry offers dashboard link for ${role}`, async ({ page }) => {
    await failUnhandledApi(page);
    await authenticateAs(page, role);

    await page.goto("/app");

    await expect(page.getByRole("heading", { name: "Панель управления клубом" })).toBeVisible();
    await expect(page.getByText(/HTMX|PWA/)).toHaveCount(0);
    await expect(page.getByRole("link", { name: /Открыть панель управления/ })).toHaveAttribute(
      "href",
      "/dashboard/login/",
    );
  });
}

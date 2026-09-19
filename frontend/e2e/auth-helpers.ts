import { expect, type Page, type Response } from "@playwright/test";

const LOGOUT_PATH = "/api/auth/logout/";

function isResetLogout(response: Response): boolean {
  const url = new URL(response.url());
  return (
    url.pathname === LOGOUT_PATH &&
    response.request().method() === "POST"
  );
}

export async function openResetLogin(page: Page): Promise<void> {
  const logoutResponse = page.waitForResponse(isResetLogout, {
    timeout: 20_000,
  });
  await page.goto("/login?reset=1");
  expect((await logoutResponse).status()).toBe(204);
  await page.waitForLoadState("networkidle");
  await expect(page).toHaveURL(/\/login\/?$/);
}

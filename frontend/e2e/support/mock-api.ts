import type { Page } from "@playwright/test";

export type MockRole = "trainer" | "student" | "parent" | "owner" | "admin";

const CLUB_ID = 7;

function base64Url(value: unknown): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeAccessToken(role: MockRole): string {
  const now = Math.floor(Date.now() / 1000);
  return [
    base64Url({ alg: "none", typ: "JWT" }),
    base64Url({
      exp: now + 60 * 60,
      sub: `${role}-device-user`,
      role,
      club_id: CLUB_ID,
    }),
    "synthetic-signature",
  ].join(".");
}

export async function failUnhandledApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }
    throw new Error(`Unhandled API request in smoke test: ${route.request().url()}`);
  });
}

export async function authenticateAs(page: Page, role: MockRole) {
  await page.addInitScript(
    ({ clubId, roleName }) => {
      window.localStorage.setItem(
        "jaguar-auth",
        JSON.stringify({
          state: {
            refreshToken: "__jaguar_server_refresh_cookie__",
            role: roleName,
            clubId,
            trainerId: roleName === "trainer" ? 201 : null,
            studentId: roleName === "student" ? 301 : null,
            isAuthenticated: true,
          },
          version: 0,
        }),
      );
    },
    { clubId: CLUB_ID, roleName: role },
  );

  await page.route("**/api/auth/refresh/", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ access_token: fakeAccessToken(role) }),
    });
  });
}

export async function mockBranding(page: Page) {
  await mockJson(page, "**/api/billing/settings/", {
    primary_color: "#111111",
    accent_color: "#2563eb",
    club_name_display: "Jaguar Gym",
    logo_url: "",
  });
}

export async function mockJson(page: Page, pattern: string, body: unknown) {
  await page.route(pattern, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}

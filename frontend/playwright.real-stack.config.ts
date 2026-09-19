import { defineConfig, devices } from "@playwright/test";

const testMatch = process.env.REAL_STACK_E2E_TEST_MATCH || "real-stack-kiosk.spec.ts";

export default defineConfig({
  testDir: "./e2e",
  testMatch,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:4173",
    trace: "off",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});

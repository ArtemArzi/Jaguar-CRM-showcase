import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface TenantNegativeMatrixFixture {
  club_a: {
    owner: {
      email: string;
      password: string;
    };
    parent: {
      email: string;
      password: string;
    };
    child_id: number;
    markers: {
      student_name: string;
      group_name: string;
      training_type_name: string;
      document_type_name: string;
    };
  };
  club_b: {
    child_id: number;
    markers: {
      student_name: string;
      group_name: string;
      training_type_name: string;
      document_type_name: string;
    };
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
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a tenant negative matrix fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): TenantNegativeMatrixFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<TenantNegativeMatrixFixture>;

  if (!data.club_a?.owner?.email || !data.club_a.owner.password) {
    throw new Error("Fixture club A owner credentials are required.");
  }
  if (!data.club_a?.parent?.email || !data.club_a.parent.password) {
    throw new Error("Fixture club A parent credentials are required.");
  }
  if (!data.club_a?.markers?.student_name || !data.club_b?.markers?.student_name) {
    throw new Error("Fixture tenant markers are required.");
  }
  if (!data.club_a.child_id || !data.club_b.child_id) {
    throw new Error("Fixture child ids are required.");
  }

  return data as TenantNegativeMatrixFixture;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function loginAsDashboardOwner(page: Page, fixture: TenantNegativeMatrixFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/login/"));
  await page.getByLabel("Эл. почта").fill(fixture.club_a.owner.email);
  await page.getByLabel("Пароль").fill(fixture.club_a.owner.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/dashboard\/$/);
  await expect(page.getByText("Дашборд").first()).toBeVisible();
}

async function assertDashboardDoesNotLeakClubB(page: Page, fixture: TenantNegativeMatrixFixture): Promise<void> {
  await page.goto(backendUrl("/dashboard/students/"));
  await expect(
    page.getByRole("row", { name: new RegExp(escapeRegExp(fixture.club_a.markers.student_name)) }),
  ).toBeVisible();
  await expect(
    page.getByRole("row", { name: new RegExp(escapeRegExp(fixture.club_b.markers.student_name)) }),
  ).toHaveCount(0);

  await page.goto(backendUrl(`/dashboard/students/${fixture.club_b.child_id}/card/`));
  await expect(page.getByText(fixture.club_b.markers.student_name)).not.toBeVisible();

  await page.goto(backendUrl("/dashboard/schedule/"));
  await expect(
    page.getByRole("row", { name: new RegExp(escapeRegExp(fixture.club_a.markers.group_name)) }),
  ).toBeVisible();
  await expect(
    page.getByRole("row", { name: new RegExp(escapeRegExp(fixture.club_b.markers.group_name)) }),
  ).toHaveCount(0);

  await page.goto(backendUrl("/dashboard/billing/"));
  await expect(
    page.getByRole("row", { name: new RegExp(escapeRegExp(fixture.club_a.markers.student_name)) }),
  ).toBeVisible();
  await expect(
    page.getByRole("row", { name: new RegExp(escapeRegExp(fixture.club_b.markers.student_name)) }),
  ).toHaveCount(0);

  await page.goto(backendUrl("/dashboard/settings/documents/"));
  await expect(page.getByText(fixture.club_a.markers.document_type_name).first()).toBeVisible();
  await expect(page.getByText(fixture.club_b.markers.document_type_name)).not.toBeVisible();
}

async function loginAsParent(page: Page, fixture: TenantNegativeMatrixFixture): Promise<void> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(fixture.club_a.parent.email);
  await page.getByLabel("Пароль").fill(fixture.club_a.parent.password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).toHaveURL(/\/parent\/?$/);
  await expect(page.getByRole("heading", { name: "Мой ребёнок" })).toBeVisible({ timeout: 20_000 });
}

async function assertParentDoesNotLeakClubB(page: Page, fixture: TenantNegativeMatrixFixture): Promise<void> {
  await expect(page.getByText(fixture.club_a.markers.student_name).first()).toBeVisible();
  await expect(page.getByText(fixture.club_b.markers.student_name)).not.toBeVisible();

  await page.goto(`/parent/child/${fixture.club_b.child_id}`);
  await expect(page.getByText(fixture.club_b.markers.student_name)).not.toBeVisible();
  await expect(page.getByText("Не удалось загрузить профиль")).toBeVisible();
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
        ["manage.py", "assert_tenant_negative_matrix_e2e", "--fixture", fixturePath],
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

test("real-stack tenant negative matrix hides foreign club data across owner and parent surfaces", async ({
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await loginAsDashboardOwner(page, fixture);
  await assertDashboardDoesNotLeakClubB(page, fixture);
  await loginAsParent(page, fixture);
  await assertParentDoesNotLeakClubB(page, fixture);
  runBackendAssert(fixturePath);
});

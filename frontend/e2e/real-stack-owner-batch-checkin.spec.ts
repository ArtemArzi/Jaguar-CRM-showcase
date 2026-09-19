import { openResetLogin } from "./auth-helpers";
import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface Credentials {
  email: string;
  password: string;
  user_id: number;
}

interface BatchTarget {
  schedule_id: number;
  student_id: number;
  date: string;
}

interface OwnerBatchCheckinFixture {
  owner: Credentials;
  admin: Credentials;
  trainer: Credentials;
  training_type_id: number;
  early: BatchTarget;
  finished: BatchTarget;
  expected: {
    owner_topic_tags: string[];
    owner_notes: string;
    admin_topic_tags: string[];
    admin_notes: string;
  };
}

interface SessionDetail {
  group_session_id: number;
  closed_at: string;
  closed_by_id: number;
  close_source: string;
}

interface BackendAssertResponse {
  ok?: boolean;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to an owner batch check-in fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): OwnerBatchCheckinFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<OwnerBatchCheckinFixture>;
  if (!data.owner?.email || !data.owner.password || !data.owner.user_id) {
    throw new Error("Fixture owner credentials are required.");
  }
  if (!data.admin?.email || !data.admin.password || !data.admin.user_id) {
    throw new Error("Fixture admin credentials are required.");
  }
  if (!data.trainer?.email || !data.trainer.password || !data.trainer.user_id) {
    throw new Error("Fixture trainer credentials are required.");
  }
  if (!data.training_type_id || !data.early?.schedule_id || !data.finished?.schedule_id) {
    throw new Error("Fixture batch targets are required.");
  }
  if (!data.expected?.owner_notes || !data.expected.admin_notes) {
    throw new Error("Fixture correction expectations are required.");
  }
  return data as OwnerBatchCheckinFixture;
}

async function loginForAccessToken(page: Page, credentials: Credentials): Promise<string> {
  await openResetLogin(page);
  await page.getByLabel("Email").fill(credentials.email);
  await page.getByLabel("Пароль").fill(credentials.password);
  const loginResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/_allauth/app/v1/auth/login" && response.request().method() === "POST";
  }, { timeout: 20_000 });
  await page.getByRole("button", { name: "Войти" }).click();
  const response = await loginResponse;
  expect(response.ok()).toBe(true);
  const body = await response.json() as { meta?: { access_token?: unknown } };
  expect(typeof body.meta?.access_token).toBe("string");
  return body.meta?.access_token as string;
}

function batchPayload(
  fixture: OwnerBatchCheckinFixture,
  target: BatchTarget,
  topicTags: string[],
  notes: string,
) {
  return {
    schedule_id: target.schedule_id,
    date: target.date,
    present_student_ids: [target.student_id],
    training_type_id: fixture.training_type_id,
    topic_tags: topicTags,
    notes,
  };
}

async function getSessionDetail(
  page: Page,
  accessToken: string,
  target: BatchTarget,
): Promise<SessionDetail> {
  const response = await page.request.get(
    backendUrl(`/api/schedules/${target.schedule_id}/session-detail/?date=${target.date}`),
    { headers: { Authorization: `Bearer ${accessToken}` } },
  );
  expect(response.status()).toBe(200);
  return await response.json() as SessionDetail;
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
        ["manage.py", "assert_owner_batch_checkin_e2e", "--fixture", fixturePath],
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

test("real-stack owner batch close rejects early state and preserves provenance on admin retry", async ({ page }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  const ownerToken = await loginForAccessToken(page, fixture.owner);
  const earlyResponse = await page.request.post(backendUrl("/api/checkins/batch/"), {
    headers: { Authorization: `Bearer ${ownerToken}` },
    data: batchPayload(fixture, fixture.early, [], "Too early"),
  });
  expect(earlyResponse.status()).toBe(400);
  expect((await earlyResponse.json()).code).toBe("session_close_not_allowed_yet");

  const ownerResponse = await page.request.post(backendUrl("/api/checkins/batch/"), {
    headers: { Authorization: `Bearer ${ownerToken}` },
    data: batchPayload(
      fixture,
      fixture.finished,
      fixture.expected.owner_topic_tags,
      fixture.expected.owner_notes,
    ),
  });
  expect(ownerResponse.status()).toBe(200);
  const ownerResult = await ownerResponse.json();
  expect(ownerResult.checkins).toHaveLength(1);
  expect(ownerResult.checkins[0].created).toBe(true);
  const firstDetail = await getSessionDetail(page, ownerToken, fixture.finished);
  expect(firstDetail.closed_by_id).toBe(fixture.owner.user_id);
  expect(firstDetail.close_source).toBe("batch");

  const adminToken = await loginForAccessToken(page, fixture.admin);
  const adminResponse = await page.request.post(backendUrl("/api/checkins/batch/"), {
    headers: { Authorization: `Bearer ${adminToken}` },
    data: batchPayload(
      fixture,
      fixture.finished,
      fixture.expected.admin_topic_tags,
      fixture.expected.admin_notes,
    ),
  });
  expect(adminResponse.status()).toBe(200);
  const adminResult = await adminResponse.json();
  expect(adminResult.checkins).toHaveLength(1);
  expect(adminResult.checkins[0].created).toBe(false);
  expect(adminResult.group_session_id).toBe(firstDetail.group_session_id);

  const retryDetail = await getSessionDetail(page, adminToken, fixture.finished);
  expect(retryDetail.group_session_id).toBe(firstDetail.group_session_id);
  expect(retryDetail.closed_at).toBe(firstDetail.closed_at);
  expect(retryDetail.closed_by_id).toBe(firstDetail.closed_by_id);
  expect(retryDetail.close_source).toBe(firstDetail.close_source);

  const trainerToken = await loginForAccessToken(page, fixture.trainer);
  const trainerResponse = await page.request.post(backendUrl("/api/checkins/batch/"), {
    headers: { Authorization: `Bearer ${trainerToken}` },
    data: batchPayload(fixture, fixture.finished, [], "Forbidden trainer retry"),
  });
  expect(trainerResponse.status()).toBe(403);

  runBackendAssert(fixturePath);
});

import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type Response,
} from "@playwright/test";

const ROSTER_PATH = "/api/checkins/kiosk/roster/";
const SCHEDULES_PATH = "/api/checkins/kiosk/schedules/today/";
const SYNC_PATH = "/api/checkins/sync/";
const PUBLIC_CACHEABLE_API_PATHS = new Set(["/api/health/", "/api/notifications/vapid-key/"]);
const FORBIDDEN_IDB_FIELD_NAMES = new Set([
  "authorization",
  "email",
  "headers",
  "kiosk_token",
  "phone",
  "request",
  "token",
  "x-kiosk-token",
  "x_kiosk_token",
]);
const EMAIL_LIKE_VALUE = /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/i;
const JWT_LIKE_VALUE = /\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/;
const HEX_TOKEN_LIKE_VALUE = /\b[a-f0-9]{64}\b/i;
const ALLOWED_PENDING_CHECKIN_FIELDS = new Set([
  "checkin_date",
  "client_id",
  "created_at",
  "id",
  "idempotency_key",
  "schedule_id",
  "student_id",
  "training_type_id",
]);
const ALLOWED_REJECTED_CHECKIN_FIELDS = new Set([
  "checkin_date",
  "error_code",
  "queued_at",
  "rejected_at",
  "schedule_id",
  "stable_key",
  "student_id",
  "training_type_id",
]);

interface OfflineKioskSyncFixture {
  kiosk_pin: string;
  phone_suffix: string;
  student_id: number;
  schedule_id: number;
  training_type_id: number;
  checkin_date: string;
  terminal: {
    student_id: number;
    phone_suffix: string;
    enrollment_id: number;
    schedule_id: number;
    subscription_id: number;
    checkin_date: string;
  };
}

interface SyncResponsePayload {
  synced: number;
  failed: number;
  results: Array<{
    success: boolean;
    checkin_id: number | null;
    duplicate: boolean;
    client_id?: string | null;
    idempotency_key?: string | null;
    error?: string | null;
    retryable: boolean;
  }>;
}

interface BackendAssertResponse {
  ok?: boolean;
}

interface EnrollmentStatusResponse {
  ok?: boolean;
  enrollment?: {
    previous_status?: string;
    status?: string;
  };
}

interface RolloutStatusResponse {
  ok?: boolean;
  mode?: string;
}

interface KioskDbSnapshot {
  students: Array<Record<string, unknown>>;
  schedules: Array<Record<string, unknown>>;
  pendingCheckins: Array<Record<string, unknown>>;
  rejectedCheckins: Array<Record<string, unknown>>;
}

interface CacheRequestSnapshot {
  cacheName: string;
  method: string;
  pathname: string;
  url: string;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to an offline kiosk sync fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): OfflineKioskSyncFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<OfflineKioskSyncFixture>;

  if (!data.kiosk_pin || !/^\d{6}$/.test(data.kiosk_pin)) {
    throw new Error("Fixture kiosk_pin must be a 6-digit string.");
  }
  if (!data.phone_suffix || !/^\d{4}$/.test(data.phone_suffix)) {
    throw new Error("Fixture phone_suffix must be a 4-digit string.");
  }
  if (!data.student_id || !data.schedule_id || !data.training_type_id || !data.checkin_date) {
    throw new Error("Fixture offline check-in ids and date are required.");
  }
  if (
    !data.terminal?.student_id ||
    !data.terminal.phone_suffix ||
    !/^\d{4}$/.test(data.terminal.phone_suffix) ||
    !data.terminal.enrollment_id ||
    !data.terminal.schedule_id ||
    !data.terminal.subscription_id ||
    !data.terminal.checkin_date
  ) {
    throw new Error("Fixture terminal offline check-in identity is required.");
  }

  return data as OfflineKioskSyncFixture;
}

function isPathResponse(response: Response, path: string, method = "GET"): boolean {
  const url = new URL(response.url());
  return url.pathname === path && response.request().method() === method;
}

function isSyncResponse(response: Response): boolean {
  return isPathResponse(response, SYNC_PATH, "POST");
}

function normalizePath(pathname: string): string {
  return pathname.endsWith("/") ? pathname : `${pathname}/`;
}

function assertNoForbiddenIdbFields(value: unknown, path = "idb"): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertNoForbiddenIdbFields(item, `${path}[${index}]`));
    return;
  }

  if (!value || typeof value !== "object") {
    return;
  }

  for (const [key, child] of Object.entries(value)) {
    expect(FORBIDDEN_IDB_FIELD_NAMES.has(key.toLowerCase()), `${path}.${key} must not be persisted`).toBe(false);
    assertNoForbiddenIdbFields(child, `${path}.${key}`);
  }
}

function assertNoForbiddenIdbStringValues(value: unknown, path = "idb"): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertNoForbiddenIdbStringValues(item, `${path}[${index}]`));
    return;
  }

  if (typeof value === "string") {
    const normalized = value.trim();
    const digitCount = normalized.replace(/\D/g, "").length;
    const isMasked = normalized.includes("*") || normalized.includes("•");
    const hasOnlyPhoneCharacters = /^[+\d\s().-]+$/.test(normalized);

    expect(EMAIL_LIKE_VALUE.test(normalized), `${path} must not persist email-like values`).toBe(false);
    expect(
      digitCount >= 10 && hasOnlyPhoneCharacters && !isMasked,
      `${path} must not persist full phone-like values`,
    ).toBe(false);
    expect(JWT_LIKE_VALUE.test(normalized), `${path} must not persist JWT-like values`).toBe(false);
    expect(HEX_TOKEN_LIKE_VALUE.test(normalized), `${path} must not persist token-like hex values`).toBe(false);
    return;
  }

  if (!value || typeof value !== "object") {
    return;
  }

  for (const [key, child] of Object.entries(value)) {
    assertNoForbiddenIdbStringValues(child, `${path}.${key}`);
  }
}

function assertPendingCheckinShape(records: Array<Record<string, unknown>>): void {
  expect(records).toHaveLength(1);
  const [record] = records;
  for (const key of Object.keys(record)) {
    expect(ALLOWED_PENDING_CHECKIN_FIELDS.has(key), `pending check-in field ${key} must be kiosk-safe`).toBe(true);
  }
  expect(record.student_id).toBeTruthy();
  expect(record.schedule_id).toBeTruthy();
  expect(record.training_type_id).toBeTruthy();
  expect(record.checkin_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  expect(record.client_id).toBe(record.idempotency_key);
  expect(typeof record.created_at).toBe("string");
}

function assertRejectedCheckinShape(records: Array<Record<string, unknown>>): void {
  expect(records).toHaveLength(1);
  const [record] = records;
  for (const key of Object.keys(record)) {
    expect(
      ALLOWED_REJECTED_CHECKIN_FIELDS.has(key),
      `rejected check-in field ${key} must be kiosk-safe`,
    ).toBe(true);
  }
  expect(record.stable_key).toBe(
    `${record.student_id}_${record.schedule_id}_${record.checkin_date}`,
  );
  expect(record.error_code).toBe("enrollment_frozen");
  expect(typeof record.queued_at).toBe("string");
  expect(typeof record.rejected_at).toBe("string");
}

async function readKioskDbSnapshot(page: Page): Promise<KioskDbSnapshot> {
  return await page.evaluate(async () => {
    const openDatabase = (): Promise<IDBDatabase> =>
      new Promise((resolveOpen, rejectOpen) => {
        const request = indexedDB.open("kiosk-db");
        request.onerror = () => rejectOpen(request.error ?? new Error("Could not open kiosk IndexedDB."));
        request.onsuccess = () => resolveOpen(request.result);
      });

    const readStore = (db: IDBDatabase, storeName: string): Promise<Array<Record<string, unknown>>> =>
      new Promise((resolveStore, rejectStore) => {
        const tx = db.transaction(storeName, "readonly");
        const request = tx.objectStore(storeName).getAll();
        request.onerror = () => rejectStore(request.error ?? new Error(`Could not read ${storeName}.`));
        request.onsuccess = () => resolveStore(request.result as Array<Record<string, unknown>>);
      });

    const db = await openDatabase();
    try {
      const [students, schedules, pendingCheckins, rejectedCheckins] = await Promise.all([
        readStore(db, "students"),
        readStore(db, "schedules"),
        readStore(db, "pendingCheckins"),
        readStore(db, "rejectedCheckins"),
      ]);
      return { students, schedules, pendingCheckins, rejectedCheckins };
    } finally {
      db.close();
    }
  });
}

async function waitForKioskDbSnapshot(
  page: Page,
  predicate: (snapshot: KioskDbSnapshot) => boolean,
): Promise<KioskDbSnapshot> {
  await expect
    .poll(async () => {
      const snapshot = await readKioskDbSnapshot(page);
      return predicate(snapshot);
    }, { timeout: 20_000 })
    .toBe(true);
  return await readKioskDbSnapshot(page);
}

async function readCacheRequests(page: Page): Promise<CacheRequestSnapshot[]> {
  return await page.evaluate(async () => {
    if (!("caches" in window)) {
      return [];
    }

    const cacheNames = await caches.keys();
    const entries: CacheRequestSnapshot[] = [];
    for (const cacheName of cacheNames) {
      const cache = await caches.open(cacheName);
      const requests = await cache.keys();
      for (const request of requests) {
        entries.push({
          cacheName,
          method: request.method,
          pathname: new URL(request.url).pathname,
          url: request.url,
        });
      }
    }
    return entries;
  });
}

async function assertNoPrivateApiCache(page: Page): Promise<void> {
  const entries = await readCacheRequests(page);
  for (const entry of entries) {
    if (!entry.pathname.startsWith("/api/")) {
      continue;
    }

    expect(
      PUBLIC_CACHEABLE_API_PATHS.has(normalizePath(entry.pathname)),
      `${entry.method} ${entry.pathname} must not be persisted in ${entry.cacheName}`,
    ).toBe(true);
  }
}

async function activateAndWarmOfflineCache(page: Page, fixture: OfflineKioskSyncFixture): Promise<string> {
  await page.goto("/kiosk/");

  const rosterResponse = page.waitForResponse((response) => isPathResponse(response, ROSTER_PATH), {
    timeout: 20_000,
  });
  const schedulesResponse = page.waitForResponse((response) => isPathResponse(response, SCHEDULES_PATH), {
    timeout: 20_000,
  });

  for (const [index, digit] of [...fixture.kiosk_pin].entries()) {
    await page.getByLabel(`PIN цифра ${index + 1}`).fill(digit);
  }

  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
  await expect((await rosterResponse).ok()).toBe(true);
  await expect((await schedulesResponse).ok()).toBe(true);

  const token = await page.evaluate(() => localStorage.getItem("kiosk_device_token"));
  if (!token) {
    throw new Error("Kiosk activation did not persist a device token.");
  }
  return token;
}

async function queueCheckinWhileOffline(
  page: Page,
  context: BrowserContext,
  phoneSuffix: string,
): Promise<void> {
  await context.setOffline(true);
  await expect(page.getByText("Нет подключения")).toBeVisible({ timeout: 10_000 });

  for (const digit of phoneSuffix) {
    await page.getByRole("button", { name: `Цифра ${digit}` }).click();
  }

  await expect(page.getByText("Сохранено в очереди")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Сохранён в очереди")).toBeVisible();
  await expect(page.getByText("Синхронизируется при подключении")).toBeVisible();
}

async function reconnectAndWaitForSync(
  page: Page,
  context: BrowserContext,
): Promise<SyncResponsePayload> {
  const syncResponse = page.waitForResponse(isSyncResponse, {
    timeout: 20_000,
  });

  await context.setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));

  const response = await syncResponse;
  expect(response.ok()).toBe(true);
  return (await response.json()) as SyncResponsePayload;
}

async function triggerOnlineSync(page: Page): Promise<SyncResponsePayload> {
  const syncResponse = page.waitForResponse(isSyncResponse, { timeout: 20_000 });
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  const response = await syncResponse;
  expect(response.ok()).toBe(true);
  return (await response.json()) as SyncResponsePayload;
}

async function replaySyncedPayload(
  page: Page,
  fixture: OfflineKioskSyncFixture,
  kioskToken: string,
): Promise<SyncResponsePayload> {
  return await page.evaluate(
    async ({ token, payload }) => {
      const response = await fetch("/api/checkins/sync/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Kiosk-Token": token,
        },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        throw new Error(`offline replay failed with status ${response.status}`);
      }
      return response.json();
    },
    {
      token: kioskToken,
      payload: {
        checkins: [
          {
            student_id: fixture.student_id,
            schedule_id: fixture.schedule_id,
            training_type_id: fixture.training_type_id,
            checkin_date: fixture.checkin_date,
            client_id: "offline-kiosk-sync-browser-replay-1",
            idempotency_key: "offline-kiosk-sync-browser-replay-1",
          },
        ],
      },
    },
  );
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
        ["manage.py", "assert_offline_kiosk_sync_e2e", "--fixture", fixturePath],
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

function setFixtureEnrollmentStatus(
  fixturePath: string,
  status: "active" | "frozen",
  target: "primary" | "terminal" = "primary",
): EnrollmentStatusResponse {
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = execFileSync(
    pythonBin,
    [
      "manage.py",
      "set_offline_kiosk_sync_enrollment_status_e2e",
      "--fixture",
      fixturePath,
      "--status",
      status,
      "--target",
      target,
    ],
    {
      cwd: repoRoot,
      encoding: "utf8",
      env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const result = JSON.parse(output) as EnrollmentStatusResponse;
  expect(result.ok).toBe(true);
  expect(result.enrollment?.status).toBe(status);
  return result;
}

function setFixtureRolloutMode(
  fixturePath: string,
  mode: "reconciling" | "shadow",
): RolloutStatusResponse {
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = execFileSync(
    pythonBin,
    ["manage.py", "set_offline_kiosk_sync_rollout_e2e", "--fixture", fixturePath, "--mode", mode],
    {
      cwd: repoRoot,
      encoding: "utf8",
      env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const result = JSON.parse(output) as RolloutStatusResponse;
  expect(result.ok).toBe(true);
  expect(result.mode).toBe(mode);
  return result;
}

function runTerminalFailureAssert(fixturePath: string): void {
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = execFileSync(
    pythonBin,
    ["manage.py", "assert_offline_kiosk_terminal_failure_e2e", "--fixture", fixturePath, "--target", "terminal"],
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

test("real-stack kiosk offline queue reconnects, syncs once, and duplicate replay is safe", async ({
  context,
  page,
}) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  const kioskToken = await activateAndWarmOfflineCache(page, fixture);
  const warmedSnapshot = await waitForKioskDbSnapshot(
    page,
    (snapshot) => snapshot.students.length > 0 && snapshot.schedules.length > 0,
  );
  expect(warmedSnapshot.pendingCheckins).toHaveLength(0);
  expect(warmedSnapshot.rejectedCheckins).toHaveLength(0);
  assertNoForbiddenIdbFields(warmedSnapshot.students, "students");
  assertNoForbiddenIdbStringValues(warmedSnapshot.students, "students");
  assertNoForbiddenIdbFields(warmedSnapshot.schedules, "schedules");
  assertNoForbiddenIdbStringValues(warmedSnapshot.schedules, "schedules");
  await assertNoPrivateApiCache(page);

  await queueCheckinWhileOffline(page, context, fixture.phone_suffix);
  const queuedSnapshot = await waitForKioskDbSnapshot(page, (snapshot) => snapshot.pendingCheckins.length === 1);
  assertNoForbiddenIdbFields(queuedSnapshot.pendingCheckins, "pendingCheckins");
  assertNoForbiddenIdbStringValues(queuedSnapshot.pendingCheckins, "pendingCheckins");
  assertPendingCheckinShape(queuedSnapshot.pendingCheckins);
  await assertNoPrivateApiCache(page);

  setFixtureRolloutMode(fixturePath, "reconciling");
  const retryablePayload = await reconnectAndWaitForSync(page, context);
  expect(retryablePayload.synced).toBe(0);
  expect(retryablePayload.failed).toBe(1);
  expect(retryablePayload.results[0]).toMatchObject({
    success: false,
    error: "training_group_reconciling",
    retryable: true,
  });
  await waitForKioskDbSnapshot(page, (snapshot) => snapshot.pendingCheckins.length === 1);

  setFixtureRolloutMode(fixturePath, "shadow");
  const shadowPayload = await triggerOnlineSync(page);
  expect(shadowPayload.synced).toBe(1);
  expect(shadowPayload.failed).toBe(0);
  expect(shadowPayload.results[0]).toMatchObject({
    success: true,
    duplicate: false,
    retryable: false,
  });
  await waitForKioskDbSnapshot(page, (snapshot) => snapshot.pendingCheckins.length === 0);

  await page.reload();
  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
  setFixtureEnrollmentStatus(fixturePath, "frozen", "terminal");
  await queueCheckinWhileOffline(page, context, fixture.terminal.phone_suffix);
  const terminalPayload = await reconnectAndWaitForSync(page, context);
  expect(terminalPayload.synced).toBe(0);
  expect(terminalPayload.failed).toBe(1);
  expect(terminalPayload.results).toHaveLength(1);
  expect(terminalPayload.results[0]).toMatchObject({
    success: false,
    duplicate: false,
    checkin_id: null,
    error: "enrollment_frozen",
    retryable: false,
  });
  expect(terminalPayload.results[0].idempotency_key).toBe(
    `${fixture.terminal.student_id}_${fixture.schedule_id}_${fixture.terminal.checkin_date}`,
  );
  const terminalSnapshot = await waitForKioskDbSnapshot(
    page,
    (snapshot) =>
      snapshot.pendingCheckins.length === 0 &&
      snapshot.rejectedCheckins.length === 1,
  );
  assertNoForbiddenIdbFields(terminalSnapshot.students, "students");
  assertNoForbiddenIdbStringValues(terminalSnapshot.students, "students");
  assertNoForbiddenIdbFields(terminalSnapshot.schedules, "schedules");
  assertNoForbiddenIdbStringValues(terminalSnapshot.schedules, "schedules");
  assertNoForbiddenIdbFields(
    terminalSnapshot.rejectedCheckins,
    "rejectedCheckins",
  );
  assertNoForbiddenIdbStringValues(
    terminalSnapshot.rejectedCheckins,
    "rejectedCheckins",
  );
  assertRejectedCheckinShape(terminalSnapshot.rejectedCheckins);
  await expect(page.getByText("1 посещение не записано")).toBeVisible();
  await expect(page.getByText("Абонемент заморожен")).toBeVisible();
  await expect(
    page.getByText(
      "Попросите администратора исправить причину, затем подтвердите сообщение.",
    ),
  ).toBeVisible();
  runTerminalFailureAssert(fixturePath);

  await page.reload();
  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
  await expect(page.getByText("1 посещение не записано")).toBeVisible();
  const reloadedTerminalSnapshot = await readKioskDbSnapshot(page);
  assertRejectedCheckinShape(reloadedTerminalSnapshot.rejectedCheckins);

  setFixtureEnrollmentStatus(fixturePath, "active", "terminal");
  const firstSuccessfulPayload = await replaySyncedPayload(
    page,
    fixture,
    kioskToken,
  );
  expect(firstSuccessfulPayload.synced).toBe(1);
  expect(firstSuccessfulPayload.failed).toBe(0);
  expect(firstSuccessfulPayload.results[0]).toMatchObject({
    success: true,
    duplicate: true,
    retryable: false,
  });
  expect(firstSuccessfulPayload.results[0].checkin_id).toBe(
    shadowPayload.results[0].checkin_id,
  );

  await page.reload();
  await expect(page.getByText("Введите последние 4 цифры телефона")).toBeVisible();
  await expect(page.getByText("1 посещение не записано")).toBeVisible();
  expect((await readKioskDbSnapshot(page)).rejectedCheckins).toHaveLength(1);

  await page
    .getByRole("alert")
    .getByRole("button", { name: "Подтвердить" })
    .click();
  await waitForKioskDbSnapshot(
    page,
    (snapshot) => snapshot.rejectedCheckins.length === 0,
  );
  await expect(page.getByText("1 посещение не записано")).toBeHidden();

  await queueCheckinWhileOffline(page, context, fixture.phone_suffix);
  const queuedSuccessSnapshot = await waitForKioskDbSnapshot(
    page,
    (snapshot) => snapshot.pendingCheckins.length === 1,
  );
  assertNoForbiddenIdbFields(queuedSuccessSnapshot.pendingCheckins, "pendingCheckins");
  assertNoForbiddenIdbStringValues(queuedSuccessSnapshot.pendingCheckins, "pendingCheckins");
  assertPendingCheckinShape(queuedSuccessSnapshot.pendingCheckins);
  await assertNoPrivateApiCache(page);

  const syncPayload = await reconnectAndWaitForSync(page, context);
  expect(syncPayload.synced).toBe(1);
  expect(syncPayload.failed).toBe(0);
  expect(syncPayload.results).toHaveLength(1);
  expect(syncPayload.results[0].success).toBe(true);
  expect(syncPayload.results[0].duplicate).toBe(true);
  expect(syncPayload.results[0].retryable).toBe(false);
  const syncedSnapshot = await waitForKioskDbSnapshot(page, (snapshot) => snapshot.pendingCheckins.length === 0);
  assertNoForbiddenIdbFields(syncedSnapshot.students, "students");
  assertNoForbiddenIdbStringValues(syncedSnapshot.students, "students");
  assertNoForbiddenIdbFields(syncedSnapshot.schedules, "schedules");
  assertNoForbiddenIdbStringValues(syncedSnapshot.schedules, "schedules");
  await assertNoPrivateApiCache(page);

  const replayPayload = await replaySyncedPayload(page, fixture, kioskToken);
  expect(replayPayload.synced).toBe(1);
  expect(replayPayload.failed).toBe(0);
  expect(replayPayload.results).toHaveLength(1);
  expect(replayPayload.results[0].success).toBe(true);
  expect(replayPayload.results[0].duplicate).toBe(true);
  expect(replayPayload.results[0].retryable).toBe(false);
  expect(replayPayload.results[0].checkin_id).toBe(
    firstSuccessfulPayload.results[0].checkin_id,
  );
  expect(syncPayload.results[0].checkin_id).toBe(
    firstSuccessfulPayload.results[0].checkin_id,
  );

  runBackendAssert(fixturePath);
});

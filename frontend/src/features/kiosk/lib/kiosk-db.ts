import { openDB, type DBSchema, type IDBPDatabase } from "idb";
import type { StudentMatch, Schedule } from "./kiosk-api";
import { toISODate } from "@/lib/utils";

// ── IndexedDB Schema ──────────────────────────────

interface KioskDB extends DBSchema {
  students: {
    key: number;
    value: {
      id: number;
      first_name: string;
      last_name: string;
      lookup_suffix: string;
      lookup_suffixes: string[];
      masked_phone?: string;
      group_name: string;
      grade_name?: string;
      subscription_name?: string;
      subscription_status?: string;
      trainings_left?: number | null;
    };
    indexes: { "by-lookup-suffix": string };
  };
  pendingCheckins: {
    key: number;
    value: {
      id?: number;
      student_id: number;
      schedule_id: number;
      training_type_id: number;
      checkin_date: string;
      client_id: string;
      idempotency_key: string;
      created_at: string;
    };
    indexes: { "by-idempotency-key": string };
  };
  schedules: {
    key: number;
    value: {
      schedule_id: number;
      effective_date: string;
      start_time: string;
      end_time: string;
      group_name: string;
      trainer_name: string;
      location_name: string;
      training_type_id: number | null;
      training_type_name: string;
    };
  };
  rejectedCheckins: {
    key: string;
    value: RejectedCheckin;
  };
}

export interface PendingCheckin {
  id?: number;
  student_id: number;
  schedule_id: number;
  training_type_id: number;
  checkin_date: string;
  client_id: string;
  idempotency_key: string;
  created_at: string;
}

export interface RejectedCheckin {
  stable_key: string;
  student_id: number;
  schedule_id: number;
  training_type_id: number;
  checkin_date: string;
  error_code: string;
  queued_at: string;
  rejected_at: string;
}

export interface TerminalCheckinRejection {
  pending_id: number;
  stable_key: string;
  error_code: string;
}

// ── Singleton DB connection ───────────────────────

let dbPromise: Promise<IDBPDatabase<KioskDB>> | null = null;
const KIOSK_DB_VERSION = 6;

function createKioskStores(db: IDBPDatabase<KioskDB>) {
  const studentStore = db.createObjectStore("students", {
    keyPath: "id",
  });
  studentStore.createIndex("by-lookup-suffix", "lookup_suffixes", {
    multiEntry: true,
  });

  const pcStore = db.createObjectStore("pendingCheckins", {
    keyPath: "id",
    autoIncrement: true,
  });
  pcStore.createIndex("by-idempotency-key", "idempotency_key", {
    unique: true,
  });

  db.createObjectStore("schedules", { keyPath: "schedule_id" });
  db.createObjectStore("rejectedCheckins", { keyPath: "stable_key" });
}

export function getDB(): Promise<IDBPDatabase<KioskDB>> {
  if (!dbPromise) {
    dbPromise = openDB<KioskDB>("kiosk-db", KIOSK_DB_VERSION, {
      upgrade(db, oldVersion, _newVersion, transaction) {
        if (oldVersion < 3) {
          for (const storeName of [
            "students",
            "pendingCheckins",
            "schedules",
            "rejectedCheckins",
          ] as const) {
            if (db.objectStoreNames.contains(storeName)) {
              db.deleteObjectStore(storeName);
            }
          }
          createKioskStores(db);
        }
        // v4 adds optional kiosk-safe student summary fields to cached values.
        // v5 moves suffix lookup to a multi-entry array index.
        // Legacy records are still handled by lookup fallback until the next roster sync.
        if (oldVersion >= 3 && oldVersion < 5 && db.objectStoreNames.contains("students")) {
          const studentStore = transaction.objectStore("students");
          if (studentStore.indexNames.contains("by-lookup-suffix")) {
            studentStore.deleteIndex("by-lookup-suffix");
          }
          studentStore.createIndex("by-lookup-suffix", "lookup_suffixes", {
            multiEntry: true,
          });
        }
        // v6 keeps deterministic backend rejections out of the retry queue while
        // preserving a kiosk-safe, explicitly acknowledged operator ledger.
        if (
          oldVersion >= 3 &&
          oldVersion < 6 &&
          !db.objectStoreNames.contains("rejectedCheckins")
        ) {
          db.createObjectStore("rejectedCheckins", { keyPath: "stable_key" });
        }
      },
    });
  }
  return dbPromise;
}

// ── Student cache ─────────────────────────────────

export async function cacheStudents(students: StudentMatch[]): Promise<void> {
  const db = await getDB();
  const tx = db.transaction("students", "readwrite");
  await tx.store.clear();
  await Promise.all(
    students.flatMap((s) => {
      const lookupSuffixes = normalizeLookupSuffixes(s.lookup_suffixes ?? [s.lookup_suffix]);
      const lookupSuffix = lookupSuffixes[0];
      if (!lookupSuffix) return [];

      return tx.store.put({
        id: s.id,
        first_name: s.first_name,
        last_name: s.last_name,
        lookup_suffix: lookupSuffix,
        lookup_suffixes: lookupSuffixes,
        masked_phone: s.masked_phone,
        group_name: s.group_name,
        grade_name: s.grade_name,
        subscription_name: s.subscription_name,
        subscription_status: s.subscription_status,
        trainings_left: s.trainings_left,
      });
    }),
  );
  await tx.done;
}

function normalizeLookupSuffixes(values: Array<string | undefined>): string[] {
  const suffixes: string[] = [];
  for (const value of values) {
    if (typeof value !== "string" || !/^\d{4}$/.test(value)) {
      continue;
    }
    if (!suffixes.includes(value)) {
      suffixes.push(value);
    }
  }
  return suffixes;
}

/**
 * Uses the backend-provided lookup suffix index for fast kiosk phone lookup.
 */
export async function lookupStudentsOffline(
  phoneSuffix: string,
): Promise<StudentMatch[]> {
  if (!/^\d{4}$/.test(phoneSuffix)) {
    return [];
  }

  const db = await getDB();
  const tx = db.transaction("students", "readonly");
  const indexedResults = await tx.store.index("by-lookup-suffix").getAll(phoneSuffix);
  if (indexedResults.length > 0) {
    return indexedResults;
  }

  const allStudents = await tx.store.getAll();
  return allStudents.filter(
    (student) =>
      student.lookup_suffix === phoneSuffix ||
      (Array.isArray(student.lookup_suffixes) &&
        student.lookup_suffixes.includes(phoneSuffix)),
  );
}

// ── Schedule cache ────────────────────────────────

export async function cacheSchedules(schedules: Schedule[]): Promise<void> {
  const db = await getDB();
  const tx = db.transaction("schedules", "readwrite");
  await tx.store.clear();
  await Promise.all(
    schedules.map((s) =>
      tx.store.put({
        schedule_id: s.schedule_id,
        effective_date: s.effective_date,
        start_time: s.start_time,
        end_time: s.end_time,
        group_name: s.group_name,
        trainer_name: s.trainer_name,
        location_name: s.location_name,
        training_type_id: s.training_type_id,
        training_type_name: s.training_type_name,
      }),
    ),
  );
  await tx.done;
}

export async function getTodaySchedulesOffline(): Promise<Schedule[]> {
  const db = await getDB();
  const all = await db.getAll("schedules");
  const today = toISODate();
  return all.filter((schedule) => schedule.effective_date === today);
}

// ── Pending check-ins queue ───────────────────────

export async function addPendingCheckin(checkin: {
  student_id: number;
  schedule_id: number;
  training_type_id: number;
  checkin_date?: string;
}): Promise<void> {
  const checkin_date = checkin.checkin_date ?? toISODate();
  const idempotency_key = `${checkin.student_id}_${checkin.schedule_id}_${checkin_date}`;
  const client_id = idempotency_key;

  const db = await getDB();

  const rejectedTx = db.transaction("rejectedCheckins", "readonly");
  const rejected = await rejectedTx.store.get(idempotency_key);
  await rejectedTx.done;
  if (rejected) return;

  // Deduplicate via IDB index (O(1) lookup instead of full scan)
  const existing = await db.getFromIndex(
    "pendingCheckins",
    "by-idempotency-key",
    idempotency_key,
  );
  if (existing) return;

  await db.add("pendingCheckins", {
    student_id: checkin.student_id,
    schedule_id: checkin.schedule_id,
    training_type_id: checkin.training_type_id,
    checkin_date,
    client_id,
    idempotency_key,
    created_at: new Date().toISOString(),
  });
}

export async function getPendingCheckins(): Promise<
  (PendingCheckin & { id: number })[]
> {
  const db = await getDB();
  const all = await db.getAll("pendingCheckins");
  return all as (PendingCheckin & { id: number })[];
}

export async function removePendingCheckins(keys: number[]): Promise<void> {
  const db = await getDB();
  const tx = db.transaction("pendingCheckins", "readwrite");
  for (const key of keys) {
    await tx.store.delete(key);
  }
  await tx.done;
}

export async function getPendingCount(): Promise<number> {
  const db = await getDB();
  return db.count("pendingCheckins");
}

const SAFE_ERROR_CODE = /^[a-z0-9_]{1,80}$/;

export async function movePendingCheckinsToRejected(
  rejections: TerminalCheckinRejection[],
): Promise<void> {
  if (rejections.length === 0) return;

  const db = await getDB();
  const tx = db.transaction(
    ["pendingCheckins", "rejectedCheckins"],
    "readwrite",
  );
  const pendingStore = tx.objectStore("pendingCheckins");
  const rejectedStore = tx.objectStore("rejectedCheckins");

  for (const rejection of rejections) {
    if (!SAFE_ERROR_CODE.test(rejection.error_code)) continue;

    const pending = await pendingStore.get(rejection.pending_id);
    if (
      !pending ||
      (pending.idempotency_key !== rejection.stable_key &&
        pending.client_id !== rejection.stable_key)
    ) {
      continue;
    }

    await rejectedStore.put({
      stable_key: rejection.stable_key,
      student_id: pending.student_id,
      schedule_id: pending.schedule_id,
      training_type_id: pending.training_type_id,
      checkin_date: pending.checkin_date,
      error_code: rejection.error_code,
      queued_at: pending.created_at,
      rejected_at: new Date().toISOString(),
    });
    await pendingStore.delete(rejection.pending_id);
  }

  await tx.done;
}

export async function getRejectedCheckins(): Promise<RejectedCheckin[]> {
  const db = await getDB();
  const rejected = await db.getAll("rejectedCheckins");
  return rejected.sort((left, right) =>
    left.rejected_at.localeCompare(right.rejected_at),
  );
}

export async function getRejectedCount(): Promise<number> {
  const db = await getDB();
  return db.count("rejectedCheckins");
}

export async function acknowledgeRejectedCheckins(
  stableKeys: string[],
): Promise<void> {
  if (stableKeys.length === 0) return;

  const db = await getDB();
  const tx = db.transaction("rejectedCheckins", "readwrite");
  for (const stableKey of new Set(stableKeys)) {
    await tx.store.delete(stableKey);
  }
  await tx.done;
}

export async function clearKioskOfflineData(
  options: { preservePending?: boolean } = {},
): Promise<void> {
  const db = await getDB();
  const storeNames = options.preservePending
    ? (["students", "schedules"] as const)
    : (["students", "schedules", "pendingCheckins"] as const);

  for (const storeName of storeNames) {
    const tx = db.transaction(storeName, "readwrite");
    await tx.store.clear();
    await tx.done;
  }
}

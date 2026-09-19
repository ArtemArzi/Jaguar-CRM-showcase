import { beforeEach, describe, expect, it, vi } from "vitest";

type StoreRecord = Record<string, unknown>;
type StoreName = "students" | "schedules" | "pendingCheckins" | "rejectedCheckins";

const stores = vi.hoisted(() => ({
  students: new Map<number, StoreRecord>(),
  schedules: new Map<number, StoreRecord>(),
  pendingCheckins: new Map<number, StoreRecord>(),
  rejectedCheckins: new Map<string, StoreRecord>(),
  addKey: 1,
  upgradeSpy: vi.fn(),
}));

vi.mock("idb", () => ({
  openDB: vi.fn((_name: string, version: number, options: { upgrade?: (...args: unknown[]) => void }) => {
    stores.upgradeSpy(version, options);
    return Promise.resolve(createMockDb());
  }),
}));

function createStore(name: StoreName) {
  const map = stores[name] as Map<number | string, StoreRecord>;
  return {
    clear: vi.fn(async () => map.clear()),
    put: vi.fn(async (value: StoreRecord) => {
      map.set((value.stable_key ?? value.id ?? value.schedule_id) as number | string, value);
    }),
    get: vi.fn(async (key: number | string) => map.get(key)),
    getAll: vi.fn(async () => Array.from(map.values())),
    delete: vi.fn(async (key: number) => {
      map.delete(key);
    }),
    index: vi.fn(() => ({
      getAll: vi.fn(async (value: string) =>
        Array.from(map.values()).filter((record) => {
          const suffixes = record.lookup_suffixes;
          return Array.isArray(suffixes) ? suffixes.includes(value) : false;
        }),
      ),
    })),
    createIndex: vi.fn(),
    deleteIndex: vi.fn(),
    indexNames: {
      contains: vi.fn((indexName: string) => indexName === "by-lookup-suffix"),
    },
  };
}

function createMockDb() {
  return {
    createObjectStore: vi.fn((name: StoreName) => createStore(name)),
    deleteObjectStore: vi.fn((name: StoreName) => {
      (stores[name] as Map<number | string, StoreRecord>).clear();
    }),
    objectStoreNames: {
      contains: vi.fn((name: string) =>
        ["students", "pendingCheckins", "schedules", "rejectedCheckins"].includes(name),
      ),
    },
    transaction: vi.fn((names: StoreName | StoreName[]) => {
      const firstName = Array.isArray(names) ? names[0] : names;
      return {
        store: createStore(firstName),
        objectStore: vi.fn((storeName: StoreName) => createStore(storeName)),
        done: Promise.resolve(),
      };
    }),
    add: vi.fn(async (_name: "pendingCheckins", value: StoreRecord) => {
      const key = stores.addKey++;
      stores.pendingCheckins.set(key, { ...value, id: key });
      return key;
    }),
    count: vi.fn(
      async (name: StoreName) =>
        (stores[name] as Map<number | string, StoreRecord>).size,
    ),
    getAll: vi.fn(async (name: StoreName) =>
      Array.from((stores[name] as Map<number | string, StoreRecord>).values()),
    ),
    getFromIndex: vi.fn(async (_name: "pendingCheckins", _index: string, value: string) =>
      Array.from(stores.pendingCheckins.values()).find((record) => record.idempotency_key === value),
    ),
  };
}

describe("kiosk IndexedDB offline store", () => {
  beforeEach(() => {
    vi.resetModules();
    stores.students.clear();
    stores.schedules.clear();
    stores.pendingCheckins.clear();
    stores.rejectedCheckins.clear();
    stores.addKey = 1;
    stores.upgradeSpy.mockClear();
    vi.setSystemTime(new Date("2026-06-03T18:20:00"));
  });

  it("stores queued check-ins with original date and stable client id", async () => {
    const { addPendingCheckin, getPendingCheckins } = await import("./kiosk-db");

    await addPendingCheckin({
      student_id: 10,
      schedule_id: 42,
      training_type_id: 7,
      checkin_date: "2026-06-03",
    });

    expect(await getPendingCheckins()).toMatchObject([
      {
        student_id: 10,
        schedule_id: 42,
        training_type_id: 7,
        checkin_date: "2026-06-03",
        client_id: "10_42_2026-06-03",
        idempotency_key: "10_42_2026-06-03",
      },
    ]);
  });

  it("stores pending check-ins as app-owned payloads without request headers", async () => {
    const { addPendingCheckin } = await import("./kiosk-db");

    await addPendingCheckin({
      student_id: 10,
      schedule_id: 42,
      training_type_id: 7,
      checkin_date: "2026-06-03",
    });

    const [storedCheckin] = Array.from(stores.pendingCheckins.values());
    expect(storedCheckin).toEqual({
      id: 1,
      student_id: 10,
      schedule_id: 42,
      training_type_id: 7,
      checkin_date: "2026-06-03",
      client_id: "10_42_2026-06-03",
      idempotency_key: "10_42_2026-06-03",
      created_at: expect.any(String),
    });
    expect(storedCheckin).not.toHaveProperty("headers");
    expect(storedCheckin).not.toHaveProperty("request");
    expect(JSON.stringify(storedCheckin)).not.toContain("X-Kiosk-Token");
    expect(JSON.stringify(storedCheckin)).not.toContain("Authorization");
  });

  it("caches students without raw phone or email fields", async () => {
    const { cacheStudents, lookupStudentsOffline } = await import("./kiosk-db");
    const incomingStudents = [
      {
        id: 10,
        first_name: "Ada",
        last_name: "Lovelace",
        phone: "+1 555 010 2233",
        email: "ada@example.test",
        masked_phone: "+1 *** ** 22-33",
        lookup_suffix: "2233",
        lookup_suffixes: ["2233"],
        group_name: "Kids",
      },
    ];

    await cacheStudents(incomingStudents);

    const [storedStudent] = Array.from(stores.students.values());
    expect(storedStudent).not.toHaveProperty("phone");
    expect(storedStudent).not.toHaveProperty("email");
    expect(storedStudent).toMatchObject({ lookup_suffixes: ["2233"] });
    await expect(lookupStudentsOffline("2233")).resolves.toHaveLength(1);
  });

  it("indexes all backend-provided lookup suffixes for one cached student", async () => {
    const { cacheStudents, lookupStudentsOffline } = await import("./kiosk-db");

    await cacheStudents([
      {
        id: 10,
        first_name: "Ada",
        last_name: "Lovelace",
        masked_phone: "+***2233",
        lookup_suffix: "2233",
        lookup_suffixes: ["2233", "4567"],
        group_name: "Kids",
      },
    ]);

    expect(Array.from(stores.students.values())).toEqual([
      expect.objectContaining({
        lookup_suffix: "2233",
        lookup_suffixes: ["2233", "4567"],
      }),
    ]);
    await expect(lookupStudentsOffline("2233")).resolves.toMatchObject([
      { id: 10 },
    ]);
    await expect(lookupStudentsOffline("4567")).resolves.toMatchObject([
      { id: 10 },
    ]);
  });

  it("falls back to the legacy single suffix field for pre-v5 cached students", async () => {
    const { lookupStudentsOffline } = await import("./kiosk-db");
    stores.students.set(10, {
      id: 10,
      first_name: "Ada",
      last_name: "Lovelace",
      lookup_suffix: "2233",
      group_name: "Kids",
    });

    await expect(lookupStudentsOffline("2233")).resolves.toMatchObject([
      { id: 10 },
    ]);
  });

  it("preserves kiosk-safe student summary fields for offline lookup", async () => {
    const { cacheStudents, lookupStudentsOffline } = await import("./kiosk-db");

    await cacheStudents([
      {
        id: 10,
        first_name: "Ada",
        last_name: "Lovelace",
        lookup_suffix: "2233",
        lookup_suffixes: ["2233"],
        masked_phone: "+1 *** ** 22-33",
        group_name: "Kids",
        grade_name: "Yellow belt",
        subscription_name: "Monthly",
        subscription_status: "active",
        trainings_left: 7,
      },
    ]);

    expect(Array.from(stores.students.values())).toEqual([
      expect.objectContaining({
        grade_name: "Yellow belt",
        subscription_name: "Monthly",
        subscription_status: "active",
        trainings_left: 7,
      }),
    ]);
    await expect(lookupStudentsOffline("2233")).resolves.toMatchObject([
      {
        id: 10,
        group_name: "Kids",
        grade_name: "Yellow belt",
        subscription_name: "Monthly",
        subscription_status: "active",
        trainings_left: 7,
      },
    ]);
  });

  it("rebuilds v2 stores so key paths and indexes match the v3 contract", async () => {
    const { getDB } = await import("./kiosk-db");
    await getDB();

    const [, options] = stores.upgradeSpy.mock.calls[0];
    expect(stores.upgradeSpy.mock.calls[0][0]).toBe(6);
    const mockDb = {
      createObjectStore: vi.fn((name: StoreName) => createStore(name)),
      deleteObjectStore: vi.fn(),
      objectStoreNames: {
        contains: vi.fn((name: string) =>
          ["students", "pendingCheckins", "schedules"].includes(name),
        ),
      },
    };

    options.upgrade(mockDb, 2, 6);

    expect(mockDb.deleteObjectStore).toHaveBeenCalledWith("students");
    expect(mockDb.deleteObjectStore).toHaveBeenCalledWith("pendingCheckins");
    expect(mockDb.deleteObjectStore).toHaveBeenCalledWith("schedules");
    expect(mockDb.createObjectStore).toHaveBeenCalledWith("students", {
      keyPath: "id",
    });
    expect(mockDb.createObjectStore).toHaveBeenCalledWith("pendingCheckins", {
      keyPath: "id",
      autoIncrement: true,
    });
    expect(mockDb.createObjectStore).toHaveBeenCalledWith("schedules", {
      keyPath: "schedule_id",
    });
    expect(mockDb.createObjectStore).toHaveBeenCalledWith("rejectedCheckins", {
      keyPath: "stable_key",
    });
  });

  it("keeps v3 stores and recreates the lookup suffix index during v5 migration", async () => {
    const { getDB } = await import("./kiosk-db");
    await getDB();

    const [, options] = stores.upgradeSpy.mock.calls[0];
    const studentStore = createStore("students");
    const mockDb = {
      createObjectStore: vi.fn((name: StoreName) => createStore(name)),
      deleteObjectStore: vi.fn(),
      objectStoreNames: {
        contains: vi.fn((name: string) =>
          ["students", "pendingCheckins", "schedules"].includes(name),
        ),
      },
    };
    const mockTransaction = {
      objectStore: vi.fn(() => studentStore),
    };

    options.upgrade(mockDb, 3, 6, mockTransaction);

    expect(mockDb.deleteObjectStore).not.toHaveBeenCalled();
    expect(mockDb.createObjectStore).toHaveBeenCalledTimes(1);
    expect(mockTransaction.objectStore).toHaveBeenCalledWith("students");
    expect(studentStore.deleteIndex).toHaveBeenCalledWith("by-lookup-suffix");
    expect(studentStore.createIndex).toHaveBeenCalledWith(
      "by-lookup-suffix",
      "lookup_suffixes",
      { multiEntry: true },
    );
    expect(mockDb.createObjectStore).toHaveBeenCalledWith("rejectedCheckins", {
      keyPath: "stable_key",
    });
  });

  it("moves one terminal result from pending to a kiosk-safe rejected ledger atomically", async () => {
    stores.pendingCheckins.set(1, {
      id: 1,
      student_id: 10,
      schedule_id: 42,
      training_type_id: 7,
      checkin_date: "2026-06-03",
      client_id: "10_42_2026-06-03",
      idempotency_key: "10_42_2026-06-03",
      created_at: "2026-06-03T12:00:00.000Z",
    });
    stores.pendingCheckins.set(2, {
      id: 2,
      student_id: 11,
      schedule_id: 43,
      training_type_id: 8,
      checkin_date: "2026-06-03",
      client_id: "11_43_2026-06-03",
      idempotency_key: "11_43_2026-06-03",
      created_at: "2026-06-03T12:01:00.000Z",
    });

    const {
      addPendingCheckin,
      getRejectedCheckins,
      movePendingCheckinsToRejected,
    } = await import("./kiosk-db");
    await movePendingCheckinsToRejected([
      {
        pending_id: 1,
        stable_key: "10_42_2026-06-03",
        error_code: "payroll_period_closed",
      },
    ]);

    expect(stores.pendingCheckins.has(1)).toBe(false);
    expect(stores.pendingCheckins.has(2)).toBe(true);
    expect(await getRejectedCheckins()).toEqual([
      {
        stable_key: "10_42_2026-06-03",
        student_id: 10,
        schedule_id: 42,
        training_type_id: 7,
        checkin_date: "2026-06-03",
        error_code: "payroll_period_closed",
        queued_at: "2026-06-03T12:00:00.000Z",
        rejected_at: expect.any(String),
      },
    ]);

    await addPendingCheckin({
      student_id: 10,
      schedule_id: 42,
      training_type_id: 7,
      checkin_date: "2026-06-03",
    });
    expect(Array.from(stores.pendingCheckins.keys())).toEqual([2]);
  });

  it("acknowledges only selected rejected results", async () => {
    stores.rejectedCheckins.set("first", { stable_key: "first" });
    stores.rejectedCheckins.set("second", { stable_key: "second" });

    const { acknowledgeRejectedCheckins, getRejectedCount } = await import("./kiosk-db");
    await acknowledgeRejectedCheckins(["first"]);

    expect(await getRejectedCount()).toBe(1);
    expect(stores.rejectedCheckins.has("first")).toBe(false);
    expect(stores.rejectedCheckins.has("second")).toBe(true);
  });

  it("clears volatile kiosk offline stores but preserves unacknowledged rejections", async () => {
    stores.students.set(10, { id: 10 });
    stores.schedules.set(20, { schedule_id: 20 });
    stores.pendingCheckins.set(30, { id: 30 });
    stores.rejectedCheckins.set("terminal", { stable_key: "terminal" });

    const { clearKioskOfflineData } = await import("./kiosk-db");
    await clearKioskOfflineData();

    expect(stores.students.size).toBe(0);
    expect(stores.schedules.size).toBe(0);
    expect(stores.pendingCheckins.size).toBe(0);
    expect(stores.rejectedCheckins.size).toBe(1);
  });

  it("can clear kiosk cache while preserving pending check-ins", async () => {
    stores.students.set(10, { id: 10 });
    stores.schedules.set(20, { schedule_id: 20 });
    stores.pendingCheckins.set(30, { id: 30 });

    const { clearKioskOfflineData } = await import("./kiosk-db");
    await clearKioskOfflineData({ preservePending: true });

    expect(stores.students.size).toBe(0);
    expect(stores.schedules.size).toBe(0);
    expect(stores.pendingCheckins.size).toBe(1);
  });

  it("returns only schedules cached for the current day", async () => {
    const { cacheSchedules, getTodaySchedulesOffline } = await import("./kiosk-db");

    await cacheSchedules([
      {
        schedule_id: 1,
        effective_date: "2026-06-02",
        start_time: "10:00",
        end_time: "11:00",
        group_name: "Yesterday",
        trainer_name: "Old Trainer",
        location_name: "Old Gym",
        training_type_id: 7,
        training_type_name: "Boxing",
      },
      {
        schedule_id: 2,
        effective_date: "2026-06-03",
        start_time: "12:00",
        end_time: "13:00",
        group_name: "Today",
        trainer_name: "Current Trainer",
        location_name: "Main Gym",
        training_type_id: 8,
        training_type_name: "Muay Thai",
      },
    ]);

    await expect(getTodaySchedulesOffline()).resolves.toMatchObject([
      {
        schedule_id: 2,
        group_name: "Today",
      },
    ]);
  });
});

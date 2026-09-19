import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useOfflineSync } from "./use-offline-sync";

const db = vi.hoisted(() => ({
  cacheStudents: vi.fn(),
  cacheSchedules: vi.fn(),
  getPendingCheckins: vi.fn(),
  removePendingCheckins: vi.fn(),
  getPendingCount: vi.fn(),
  getRejectedCheckins: vi.fn(),
  movePendingCheckinsToRejected: vi.fn(),
  acknowledgeRejectedCheckins: vi.fn(),
}));

const api = vi.hoisted(() => ({
  fetchKioskRoster: vi.fn(),
  fetchTodaySchedules: vi.fn(),
  lookupStudents: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/features/kiosk/lib/kiosk-db", () => db);
vi.mock("@/features/kiosk/lib/kiosk-api", () => ({
  fetchKioskRoster: api.fetchKioskRoster,
  fetchTodaySchedules: api.fetchTodaySchedules,
  lookupStudents: api.lookupStudents,
  kioskApi: { post: api.post },
}));

describe("useOfflineSync kiosk data contracts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    db.cacheStudents.mockResolvedValue(undefined);
    db.cacheSchedules.mockResolvedValue(undefined);
    db.getPendingCheckins.mockResolvedValue([]);
    db.removePendingCheckins.mockResolvedValue(undefined);
    db.getPendingCount.mockResolvedValue(0);
    db.getRejectedCheckins.mockResolvedValue([]);
    db.movePendingCheckinsToRejected.mockResolvedValue(undefined);
    db.acknowledgeRejectedCheckins.mockResolvedValue(undefined);
    api.fetchKioskRoster.mockResolvedValue([]);
    api.fetchTodaySchedules.mockResolvedValue([]);
    api.lookupStudents.mockResolvedValue([]);
    api.post.mockResolvedValue({ data: { results: [] } });
    Object.defineProperty(navigator, "onLine", {
      value: true,
      configurable: true,
    });
  });

  it("warms the offline roster from an explicit kiosk roster endpoint", async () => {
    renderHook(() => useOfflineSync(true));

    await waitFor(() => {
      expect(api.fetchKioskRoster).toHaveBeenCalled();
    });
    expect(api.lookupStudents).not.toHaveBeenCalledWith("");
  });

  it("clears offline roster and schedules when refresh returns empty lists", async () => {
    api.fetchKioskRoster.mockResolvedValue([]);
    api.fetchTodaySchedules.mockResolvedValue([]);

    renderHook(() => useOfflineSync(true));

    await waitFor(() => {
      expect(db.cacheStudents).toHaveBeenCalledWith([]);
      expect(db.cacheSchedules).toHaveBeenCalledWith([]);
    });
  });

  it("removes terminal duplicate sync results by stable client id", async () => {
    db.getPendingCheckins.mockResolvedValue([
      {
        id: 1,
        student_id: 10,
        schedule_id: 42,
        training_type_id: 7,
        checkin_date: "2026-06-03",
        client_id: "10_42_2026-06-03",
        idempotency_key: "10_42_2026-06-03",
        created_at: "2026-06-03T12:00:00.000Z",
      },
    ]);
    api.post.mockResolvedValue({
      data: {
        results: [
          {
            client_id: "10_42_2026-06-03",
            success: true,
            duplicate: true,
          },
        ],
      },
    });

    const { result } = renderHook(() => useOfflineSync(false));
    await act(async () => {
      await result.current.syncPending();
    });

    expect(api.post).toHaveBeenCalledWith("/checkins/sync/", {
      checkins: [
        expect.objectContaining({
          checkin_date: "2026-06-03",
          client_id: "10_42_2026-06-03",
          idempotency_key: "10_42_2026-06-03",
        }),
      ],
    });
    expect(db.removePendingCheckins).toHaveBeenCalledWith([1]);
  });

  it.each([undefined, true])(
    "keeps a known error retryable when backend retryable is %s",
    async (retryable) => {
    db.getPendingCheckins.mockResolvedValue([
      {
        id: 1,
        student_id: 10,
        schedule_id: 42,
        training_type_id: 7,
        checkin_date: "2026-06-03",
        client_id: "10_42_2026-06-03",
        idempotency_key: "10_42_2026-06-03",
        created_at: "2026-06-03T12:00:00.000Z",
      },
    ]);
    db.getPendingCount.mockResolvedValue(1);
    api.post.mockResolvedValue({
      data: {
        results: [
          {
            client_id: "10_42_2026-06-03",
            success: false,
            error: "enrollment_frozen",
            ...(retryable === undefined ? {} : { retryable }),
          },
        ],
      },
    });

    const { result } = renderHook(() => useOfflineSync(false));
    await act(async () => {
      await result.current.syncPending();
    });

    expect(db.removePendingCheckins).not.toHaveBeenCalled();
    expect(db.movePendingCheckinsToRejected).not.toHaveBeenCalled();
    expect(result.current.syncError).toContain("1");
    expect(result.current.pendingCount).toBe(1);
    },
  );

  it("keeps thrown sync failures retryable and hides raw error details", async () => {
    db.getPendingCheckins.mockResolvedValue([
      {
        id: 1,
        student_id: 10,
        schedule_id: 42,
        training_type_id: 7,
        checkin_date: "2026-06-03",
        client_id: "10_42_2026-06-03",
        idempotency_key: "10_42_2026-06-03",
        created_at: "2026-06-03T12:00:00.000Z",
      },
    ]);
    db.getPendingCount.mockResolvedValue(1);
    api.post.mockRejectedValue(
      new Error("Private backend detail call +7 900 000-00-00"),
    );

    const { result } = renderHook(() => useOfflineSync(false));
    await act(async () => {
      await result.current.syncPending();
    });

    expect(db.removePendingCheckins).not.toHaveBeenCalled();
    expect(result.current.syncError).toBe(
      "Не удалось синхронизировать. Повторим позже",
    );
    expect(result.current.syncError).not.toContain("+7 900");
    expect(result.current.pendingCount).toBe(1);
  });

  it.each([
    "subscription_component_limit_exceeded",
    "subscription_component_credits_exhausted",
    "payroll_period_closed",
    "future_backend_terminal_code",
  ])("moves backend-terminal sync failure %s into the rejected ledger", async (code) => {
    db.getPendingCheckins.mockResolvedValue([
      {
        id: 1,
        student_id: 10,
        schedule_id: 42,
        training_type_id: 7,
        checkin_date: "2026-06-03",
        client_id: "10_42_2026-06-03",
        idempotency_key: "10_42_2026-06-03",
        created_at: "2026-06-03T12:00:00.000Z",
      },
    ]);
    db.getPendingCount.mockResolvedValue(0);
    db.getRejectedCheckins.mockResolvedValue([
      {
        stable_key: "10_42_2026-06-03",
        student_id: 10,
        schedule_id: 42,
        training_type_id: 7,
        checkin_date: "2026-06-03",
        error_code: code,
        queued_at: "2026-06-03T12:00:00.000Z",
        rejected_at: "2026-06-03T12:05:00.000Z",
      },
    ]);
    api.post.mockResolvedValue({
      data: {
        results: [
          {
            client_id: "10_42_2026-06-03",
            idempotency_key: "10_42_2026-06-03",
            success: false,
            error: code,
            retryable: false,
          },
        ],
      },
    });

    const { result } = renderHook(() => useOfflineSync(false));
    await act(async () => {
      await result.current.syncPending();
    });

    expect(db.movePendingCheckinsToRejected).toHaveBeenCalledWith([
      {
        pending_id: 1,
        stable_key: "10_42_2026-06-03",
        error_code: code,
      },
    ]);
    expect(db.removePendingCheckins).not.toHaveBeenCalled();
    expect(result.current.syncError).toBeNull();
    expect(result.current.pendingCount).toBe(0);
    expect(result.current.rejectedCount).toBe(1);
  });

  it("matches shuffled partial results by stable key and leaves unmatched work pending", async () => {
    db.getPendingCheckins.mockResolvedValue([
      {
        id: 1,
        student_id: 10,
        schedule_id: 42,
        training_type_id: 7,
        checkin_date: "2026-06-03",
        client_id: "10_42_2026-06-03",
        idempotency_key: "10_42_2026-06-03",
        created_at: "2026-06-03T12:00:00.000Z",
      },
      {
        id: 2,
        student_id: 11,
        schedule_id: 43,
        training_type_id: 8,
        checkin_date: "2026-06-03",
        client_id: "11_43_2026-06-03",
        idempotency_key: "11_43_2026-06-03",
        created_at: "2026-06-03T12:01:00.000Z",
      },
      {
        id: 3,
        student_id: 12,
        schedule_id: 44,
        training_type_id: 9,
        checkin_date: "2026-06-03",
        client_id: "12_44_2026-06-03",
        idempotency_key: "12_44_2026-06-03",
        created_at: "2026-06-03T12:02:00.000Z",
      },
    ]);
    db.getPendingCount.mockResolvedValue(1);
    api.post.mockResolvedValue({
      data: {
        results: [
          {
            idempotency_key: "12_44_2026-06-03",
            success: true,
            duplicate: false,
            retryable: false,
          },
          {
            idempotency_key: "11_43_2026-06-03",
            success: false,
            error: "payroll_period_closed",
            retryable: false,
          },
        ],
      },
    });

    const { result } = renderHook(() => useOfflineSync(false));
    await act(async () => {
      await result.current.syncPending();
    });

    expect(db.movePendingCheckinsToRejected).toHaveBeenCalledWith([
      {
        pending_id: 2,
        stable_key: "11_43_2026-06-03",
        error_code: "payroll_period_closed",
      },
    ]);
    expect(db.removePendingCheckins).toHaveBeenCalledWith([3]);
    expect(result.current.pendingCount).toBe(1);
    expect(result.current.syncError).toContain("1");
  });

  it("keeps prior rejected work visible after a later successful sync", async () => {
    db.getPendingCheckins.mockResolvedValue([
      {
        id: 1,
        student_id: 10,
        schedule_id: 42,
        training_type_id: 7,
        checkin_date: "2026-06-03",
        client_id: "10_42_2026-06-03",
        idempotency_key: "10_42_2026-06-03",
        created_at: "2026-06-03T12:00:00.000Z",
      },
    ]);
    db.getRejectedCheckins.mockResolvedValue([
      {
        stable_key: "older",
        student_id: 20,
        schedule_id: 50,
        training_type_id: 9,
        checkin_date: "2026-06-02",
        error_code: "subscription_component_credits_exhausted",
        queued_at: "2026-06-02T12:00:00.000Z",
        rejected_at: "2026-06-02T12:05:00.000Z",
      },
    ]);
    api.post.mockResolvedValue({
      data: {
        results: [
          {
            idempotency_key: "10_42_2026-06-03",
            success: true,
            duplicate: false,
            retryable: false,
          },
        ],
      },
    });

    const { result } = renderHook(() => useOfflineSync(false));
    await act(async () => {
      await result.current.syncPending();
    });

    expect(db.removePendingCheckins).toHaveBeenCalledWith([1]);
    expect(result.current.rejectedCount).toBe(1);
    expect(result.current.rejectedCheckins[0].stable_key).toBe("older");
  });

  it("acknowledges only selected rejected work and refreshes the ledger", async () => {
    const rejected = {
      stable_key: "older",
      student_id: 20,
      schedule_id: 50,
      training_type_id: 9,
      checkin_date: "2026-06-02",
      error_code: "payroll_period_closed",
      queued_at: "2026-06-02T12:00:00.000Z",
      rejected_at: "2026-06-02T12:05:00.000Z",
    };
    db.getRejectedCheckins
      .mockResolvedValueOnce([rejected])
      .mockResolvedValue([]);

    const { result } = renderHook(() => useOfflineSync(true));
    await waitFor(() => expect(result.current.rejectedCount).toBe(1));

    await act(async () => {
      await result.current.acknowledgeRejected(["older"]);
    });

    expect(db.acknowledgeRejectedCheckins).toHaveBeenCalledWith(["older"]);
    expect(result.current.rejectedCount).toBe(0);
  });
});

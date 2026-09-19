import { useState, useEffect, useCallback, useRef } from "react";
import {
  cacheStudents,
  cacheSchedules,
  getPendingCheckins,
  removePendingCheckins,
  getPendingCount,
  getRejectedCheckins,
  movePendingCheckinsToRejected,
  acknowledgeRejectedCheckins,
  type RejectedCheckin,
} from "@/features/kiosk/lib/kiosk-db";
import {
  fetchKioskRoster,
  fetchTodaySchedules,
  kioskApi,
} from "@/features/kiosk/lib/kiosk-api";

// ── Cache refresh interval (5 min) ───────────────
const CACHE_REFRESH_MS = 5 * 60 * 1000;

const SAFE_ERROR_CODE = /^[a-z0-9_]{1,80}$/;

export type SyncStatus = "online" | "offline" | "syncing";

export function useOfflineSync(isActivated: boolean) {
  const [isOnline, setIsOnline] = useState(navigator.onLine);
  const [pendingCount, setPendingCount] = useState(0);
  const [isSyncing, setIsSyncing] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [rejectedCheckins, setRejectedCheckins] = useState<RejectedCheckin[]>([]);
  const syncRef = useRef(false);

  // Derived status for indicator
  const status: SyncStatus = isSyncing
    ? "syncing"
    : isOnline
      ? "online"
      : "offline";

  // ── Refresh pending count ───────────────────────
  const refreshPendingCount = useCallback(async () => {
    const count = await getPendingCount();
    setPendingCount(count);
  }, []);

  const refreshRejectedCheckins = useCallback(async () => {
    const rejected = await getRejectedCheckins();
    setRejectedCheckins(rejected);
  }, []);

  // ── Batch sync to /api/checkins/sync/ ───────────
  const syncPending = useCallback(async () => {
    // Prevent concurrent syncs
    if (syncRef.current) return;
    syncRef.current = true;
    setIsSyncing(true);

    try {
      const pending = await getPendingCheckins();
      if (pending.length === 0) {
        setSyncError(null);
        return;
      }

      const response = await kioskApi.post("/checkins/sync/", {
        checkins: pending.map((p) => ({
          student_id: p.student_id,
          schedule_id: p.schedule_id,
          training_type_id: p.training_type_id,
          checkin_date: p.checkin_date,
          client_id: p.client_id,
          idempotency_key: p.idempotency_key,
        })),
      });

      // Remove successfully synced from IndexedDB
      type OfflineSyncResult = {
        success: boolean;
        duplicate?: boolean;
        client_id?: string | null;
        idempotency_key?: string | null;
        code?: string;
        error?: string;
        retryable?: boolean;
      };
      const results: OfflineSyncResult[] = Array.isArray(response.data?.results)
        ? response.data.results
        : [];
      const pendingByStableKey = new Map<string, (typeof pending)[number]>();
      for (const item of pending) {
        pendingByStableKey.set(item.idempotency_key, item);
        pendingByStableKey.set(item.client_id, item);
      }

      const resolvedPendingIds = new Set<number>();
      const successfulPendingIds: number[] = [];
      const terminalRejections: Array<{
        pending_id: number;
        stable_key: string;
        error_code: string;
      }> = [];

      for (const result of results) {
        const responseStableKey = result.idempotency_key ?? result.client_id;
        if (typeof responseStableKey !== "string" || !responseStableKey) {
          continue;
        }

        const matched = pendingByStableKey.get(responseStableKey);
        if (!matched || resolvedPendingIds.has(matched.id)) continue;

        if (result.success || result.duplicate) {
          successfulPendingIds.push(matched.id);
          resolvedPendingIds.add(matched.id);
          continue;
        }

        const errorCode = result.error ?? result.code;
        if (
          result.retryable === false &&
          typeof errorCode === "string" &&
          SAFE_ERROR_CODE.test(errorCode)
        ) {
          terminalRejections.push({
            pending_id: matched.id,
            stable_key: matched.idempotency_key,
            error_code: errorCode,
          });
          resolvedPendingIds.add(matched.id);
        }
      }

      if (terminalRejections.length > 0) {
        await movePendingCheckinsToRejected(terminalRejections);
      }
      if (successfulPendingIds.length > 0) {
        await removePendingCheckins(successfulPendingIds);
      }

      const unresolvedCount = pending.filter(
        (item) => !resolvedPendingIds.has(item.id),
      ).length;
      setSyncError(
        unresolvedCount > 0
          ? `Не удалось синхронизировать ${unresolvedCount} посещ.`
          : null,
      );
    } catch {
      setSyncError("Не удалось синхронизировать. Повторим позже");
    } finally {
      await Promise.all([refreshPendingCount(), refreshRejectedCheckins()]);
      setIsSyncing(false);
      syncRef.current = false;
    }
  }, [refreshPendingCount, refreshRejectedCheckins]);

  const acknowledgeRejected = useCallback(
    async (stableKeys: string[]) => {
      await acknowledgeRejectedCheckins(stableKeys);
      await refreshRejectedCheckins();
    },
    [refreshRejectedCheckins],
  );

  // ── Refresh student + schedule cache ────────────
  const refreshCache = useCallback(async () => {
    if (!navigator.onLine) return;

    try {
      const students = await fetchKioskRoster();
      await cacheStudents(students);
    } catch {
      // Cache refresh failed -- stale cache still available
    }

    try {
      const schedules = await fetchTodaySchedules();
      await cacheSchedules(schedules);
    } catch {
      // Schedule cache refresh failed
    }
  }, []);

  // ── Online/offline listeners ────────────────────
  useEffect(() => {
    if (!isActivated) return;

    const handleOnline = () => {
      setIsOnline(true);
      // Auto-sync when coming back online
      syncPending();
    };

    const handleOffline = () => {
      setIsOnline(false);
    };

    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);

    // Initial count + sync if online with pending items
    refreshPendingCount();
    if (navigator.onLine) {
      syncPending();
    } else {
      refreshRejectedCheckins();
    }

    return () => {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, [
    syncPending,
    refreshPendingCount,
    refreshRejectedCheckins,
    isActivated,
  ]);

  // ── Periodic cache refresh (every 5 min) ───────
  useEffect(() => {
    if (!isActivated) return;

    // Initial cache refresh
    refreshCache();

    const interval = setInterval(() => {
      refreshCache();
    }, CACHE_REFRESH_MS);

    return () => clearInterval(interval);
  }, [refreshCache, isActivated]);

  return {
    isOnline,
    pendingCount,
    isSyncing,
    status,
    syncPending,
    syncError,
    rejectedCheckins,
    rejectedCount: rejectedCheckins.length,
    acknowledgeRejected,
    refreshPendingCount,
    refreshRejectedCheckins,
    refreshCache,
  };
}

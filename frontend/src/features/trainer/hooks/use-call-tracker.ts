import { useState, useEffect, useCallback } from "react";

interface UseCallTrackerReturn {
  readonly pendingTaskId: number | null;
  readonly showResult: boolean;
  readonly startCall: (taskId: number, phone: string) => void;
  readonly dismissResult: () => void;
}

export function useCallTracker(): UseCallTrackerReturn {
  const [pendingTaskId, setPendingTaskId] = useState<number | null>(null);
  const [showResult, setShowResult] = useState(false);

  const startCall = useCallback((taskId: number, phone: string) => {
    const sanitized = phone.replace(/[^\d+\-\s()]/g, "");
    if (!sanitized) return;
    setPendingTaskId(taskId);
    window.location.href = `tel:${sanitized}`;
  }, []);

  const dismissResult = useCallback(() => {
    setShowResult(false);
    setPendingTaskId(null);
  }, []);

  useEffect(() => {
    function onVisibilityChange() {
      if (document.visibilityState === "visible" && pendingTaskId !== null) {
        setTimeout(() => setShowResult(true), 500);
      }
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () =>
      document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [pendingTaskId]);

  return { pendingTaskId, showResult, startCall, dismissResult };
}

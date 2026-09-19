import { Loader2 } from "lucide-react";
import type { SyncStatus } from "@/features/kiosk/hooks/use-offline-sync";
import type { RejectedCheckin } from "@/features/kiosk/lib/kiosk-db";
import { getKioskCheckinErrorMessage } from "@/features/kiosk/lib/checkin-error";

/**
 * Sync indicator for kiosk mode (K-06 / D-13).
 * Fixed bottom-right, minimal and non-distracting.
 *
 * - Online: small green dot (8px)
 * - Offline: red dot + "N в очереди" label, gentle pulse every 5s
 * - Syncing: spinner + "Синхронизация..." label
 */
interface SyncIndicatorProps {
  status: SyncStatus;
  pendingCount: number;
  syncError?: string | null;
  rejectedCheckins?: RejectedCheckin[];
  onAcknowledgeRejected?: (stableKeys: string[]) => void;
}

function rejectedTitle(count: number): string {
  const remainder10 = count % 10;
  const remainder100 = count % 100;
  if (remainder10 === 1 && remainder100 !== 11) {
    return `${count} посещение не записано`;
  }
  if (
    remainder10 >= 2 &&
    remainder10 <= 4 &&
    (remainder100 < 12 || remainder100 > 14)
  ) {
    return `${count} посещения не записаны`;
  }
  return `${count} посещений не записано`;
}

export function SyncIndicator({
  status,
  pendingCount,
  syncError,
  rejectedCheckins = [],
  onAcknowledgeRejected,
}: SyncIndicatorProps) {
  const rejectionMessages = Array.from(
    new Set(
      rejectedCheckins.map((rejection) =>
        getKioskCheckinErrorMessage({ code: rejection.error_code }),
      ),
    ),
  );

  return (
    <div
      className="fixed right-4 bottom-6 z-30 flex max-w-[calc(100vw-2rem)] flex-col items-end gap-2"
      role="status"
      aria-live="polite"
    >
      {rejectedCheckins.length > 0 && (
        <div
          className="w-full max-w-sm rounded-xl border border-red-400/40 bg-red-950/95 p-4 text-left shadow-xl"
          role="alert"
        >
          <p className="font-semibold text-red-100">
            {rejectedTitle(rejectedCheckins.length)}
          </p>
          {rejectionMessages.map((message) => (
            <p className="mt-2 text-sm leading-relaxed text-red-100" key={message}>
              {message}
            </p>
          ))}
          <p className="mt-2 text-sm leading-relaxed text-red-100">
            Попросите администратора исправить причину, затем подтвердите сообщение.
          </p>
          {onAcknowledgeRejected && (
            <button
              className="mt-3 min-h-16 w-full rounded-lg bg-white px-5 py-3 font-semibold text-neutral-950"
              type="button"
              onClick={() =>
                onAcknowledgeRejected(
                  rejectedCheckins.map((rejection) => rejection.stable_key),
                )
              }
            >
              Подтвердить
            </button>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-end gap-2">
        {status === "online" && (
          <>
          <span
            className="block h-2 w-2 rounded-full bg-[oklch(0.65_0.2_145)]"
            aria-label="Онлайн"
          />
          {pendingCount > 0 && (
            <span className="text-sm text-neutral-400">
              {pendingCount} в очереди
            </span>
          )}
          </>
        )}

        {status === "offline" && (
          <>
          <span
            className="block h-2 w-2 rounded-full bg-red-500 motion-safe:animate-[pulse_5s_ease-in-out_infinite]"
            aria-hidden="true"
          />
          <span className="text-sm text-neutral-400">
            {pendingCount > 0 ? `${pendingCount} в очереди` : "Нет подключения"}
          </span>
          </>
        )}

        {status === "syncing" && (
          <>
          <Loader2 className="h-4 w-4 animate-spin text-neutral-400" />
          <span className="text-sm text-neutral-400">Синхронизация...</span>
          </>
        )}

        {syncError && (
          <span className="max-w-[18rem] rounded-md border border-amber-400/30 bg-amber-950/70 px-2 py-1 text-right text-xs leading-snug text-amber-200">
            {syncError}
          </span>
        )}
      </div>
    </div>
  );
}

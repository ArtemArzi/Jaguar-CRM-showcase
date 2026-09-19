import { useEffect, useMemo, useState } from "react";
import { CheckCircle, XCircle } from "lucide-react";
import type {
  StudentMatch,
  CheckinResult,
} from "@/features/kiosk/lib/kiosk-api";

interface CascadeFeedbackProps {
  student: StudentMatch;
  result: CheckinResult | null;
  goToNumpad: () => void;
  error?: string | null;
}

const AUTO_RETURN_MS = 4000;

const OFFLINE_ROWS = ["Сохранён в очереди", "Синхронизируется при подключении"];

export default function CascadeFeedback({
  student,
  result,
  goToNumpad,
  error = null,
}: CascadeFeedbackProps) {
  const [visibleRows, setVisibleRows] = useState(0);
  const [showCheck, setShowCheck] = useState(false);

  const isOffline = result?.checkin_id === -1;
  const isDuplicate =
    result?.duplicate || result?.alerts?.some((a) => a.type === "duplicate") || false;
  const feedback = useMemo(() => {
    if (error || !result) {
      return {
        tone: "error" as const,
        title: error || "Ошибка чек-ина",
        rows: [] as string[],
      };
    }

    if (isOffline) {
      return {
        tone: "success" as const,
        title: "Сохранено в очереди",
        rows: OFFLINE_ROWS,
      };
    }

    if (isDuplicate) {
      return {
        tone: "warning" as const,
        title: "Уже отмечен на этой тренировке",
        rows: [] as string[],
      };
    }

    const rows: string[] = [];
    if (result.subscription_effect === "deducted") {
      rows.push("Абонемент списан");
    }
    if (result.debt_effect === "created" || result.is_debt) {
      rows.push("Занятие в долг");
    }

    return {
      tone: "success" as const,
      title: "Посещение сохранено",
      rows,
    };
  }, [error, isDuplicate, isOffline, result]);

  const isError = feedback.tone === "error";
  const isCheckVisible = isError || showCheck;

  // Animated checkmark appearance
  useEffect(() => {
    if (isError) return;

    const checkTimer = setTimeout(() => setShowCheck(true), 100);
    return () => clearTimeout(checkTimer);
  }, [isError]);

  // Staggered info rows
  useEffect(() => {
    if (isError || !showCheck) return;

    const timers: ReturnType<typeof setTimeout>[] = [];
    feedback.rows.forEach((_, i) => {
      const timer = setTimeout(
        () => setVisibleRows((prev) => Math.max(prev, i + 1)),
        500 + i * 100 + i * 150,
      );
      timers.push(timer);
    });

    return () => timers.forEach(clearTimeout);
  }, [feedback.rows, isError, showCheck]);

  // D-08: Auto-return to numpad after 4 seconds
  useEffect(() => {
    if (isError) return;

    const timer = setTimeout(goToNumpad, AUTO_RETURN_MS);
    return () => clearTimeout(timer);
  }, [isError, goToNumpad]);

  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-4">
      {/* Checkmark / Error icon */}
      <div
        className={`mb-6 transition-transform duration-[400ms] ease-out motion-reduce:transition-none ${
          isCheckVisible ? "scale-100 opacity-100" : "scale-50 opacity-0"
        }`}
      >
        {isError ? (
          <XCircle className="h-20 w-20 text-red-500" strokeWidth={1.5} />
        ) : (
          <CheckCircle
            className={`h-20 w-20 ${
              feedback.tone === "warning" ? "text-amber-400" : "text-green-500"
            }`}
            strokeWidth={1.5}
            style={{
              strokeDasharray: 200,
              strokeDashoffset: isCheckVisible ? 0 : 200,
              transition:
                "stroke-dashoffset 400ms ease-out, transform 400ms ease-out",
            }}
          />
        )}
      </div>

      {/* Student name */}
      <h1 className="mb-3 text-center text-4xl font-semibold uppercase text-white">
        {student.first_name} {student.last_name}
      </h1>

      <p
        className={`text-center text-2xl font-semibold ${
          isError
            ? "text-red-400"
            : feedback.tone === "warning"
              ? "text-amber-300"
              : "text-white"
        }`}
        aria-live="polite"
      >
        {feedback.title}
      </p>

      {/* Error state */}
      {isError && (
        <div className="mt-4 text-center">
          <p className="mt-1 text-sm text-neutral-500">
            Попробуйте снова или обратитесь к тренеру
          </p>
          <button
            type="button"
            onClick={goToNumpad}
            className="mt-6 min-h-16 rounded-lg bg-[var(--branding-accent,#ff6b00)] px-6 py-3 text-base font-semibold text-white transition-transform active:scale-95 motion-reduce:active:scale-100"
          >
            Попробовать отметиться снова
          </button>
        </div>
      )}

      {/* Success: cascade info rows */}
      {!isError && (
        <div className="mt-6 flex flex-col items-center gap-3">
          {feedback.rows.map((text, i) => (
            <div
              key={text}
              className={`text-base text-neutral-300 transition-all duration-150 ease-out motion-reduce:transition-none ${
                i < visibleRows
                  ? "translate-y-0 opacity-100"
                  : "translate-y-2 opacity-0"
              }`}
            >
              {text}
            </div>
          ))}

          {/* Subscription info */}
          {result?.subscription_id && visibleRows >= 1 && (
            <div className="mt-2 text-sm text-neutral-500">
              {result.is_debt
                ? "Занятие в долг"
                : "Занятие списано с абонемента"}
            </div>
          )}

          {/* Manual return button */}
          <button
            type="button"
            onClick={goToNumpad}
            className="mt-6 min-h-16 rounded-lg border border-neutral-700 px-6 py-3 text-base text-neutral-300 transition-transform active:scale-95 motion-reduce:active:scale-100"
          >
            Вернуться к вводу
          </button>
        </div>
      )}
    </div>
  );
}

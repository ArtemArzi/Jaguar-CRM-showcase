import { useState, useEffect, useCallback, useRef } from "react";
import { InputDisplay } from "@/features/kiosk/components/input-display";
import { Numpad } from "@/features/kiosk/components/numpad";
import {
  lookupStudents,
  fetchTodaySchedules,
  fetchKioskOptions,
  autoDetectTraining,
} from "@/features/kiosk/lib/kiosk-api";
import type {
  StudentMatch,
  CheckinResult,
  KioskScheduleOption,
  Schedule,
} from "@/features/kiosk/lib/kiosk-api";
import {
  lookupStudentsOffline,
  getTodaySchedulesOffline,
} from "@/features/kiosk/lib/kiosk-db";
import {
  isNetworkCheckinFailure,
  submitKioskCheckinWithOfflineQueue,
} from "@/features/kiosk/lib/checkin-submit";
import { getKioskOptionDecision } from "@/features/kiosk/lib/kiosk-option-decision";

interface NumpadHomeProps {
  logoUrl: string;
  clubName: string;
  goToMatches: (matches: StudentMatch[], phoneSuffix: string) => void;
  goToResult: (
    student: StudentMatch,
    kioskOptions?: KioskScheduleOption[] | null,
  ) => void;
  goToFeedback: (result: CheckinResult, student: StudentMatch) => void;
  goToErrorFeedback: (error: unknown, student: StudentMatch) => void;
  schedules: Schedule[];
  setSchedules: (schedules: Schedule[]) => void;
}

const PHONE_DIGITS = 4;

export default function NumpadHome({
  logoUrl,
  clubName,
  goToMatches,
  goToResult,
  goToFeedback,
  goToErrorFeedback,
  schedules,
  setSchedules,
}: NumpadHomeProps) {
  const [digits, setDigits] = useState<string[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Fetch today's schedules on mount (online -> API, offline -> IndexedDB)
  useEffect(() => {
    if (navigator.onLine) {
      fetchTodaySchedules()
        .then((data) => setSchedules(data))
        .catch(() => {
          // Fallback to cached schedules
          getTodaySchedulesOffline().then((cached) => {
            if (cached.length > 0) setSchedules(cached);
          });
        });
    } else {
      getTodaySchedulesOffline().then((cached) => setSchedules(cached));
    }
  }, [setSchedules]);

  const performSearch = useCallback(
    async (phoneSuffix: string) => {
      // Cancel any in-flight search (race condition prevention)
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      setIsSearching(true);
      setError(null);

      try {
        // Online: API lookup. Offline: IndexedDB cache.
        const matches = navigator.onLine
          ? await lookupStudents(phoneSuffix, controller.signal)
          : await lookupStudentsOffline(phoneSuffix);

        if (matches.length === 0) {
          setError("Ученик не найден");
          setDigits([]);
          return;
        }

        let availableSchedules = schedules;
        if (availableSchedules.length === 0) {
          try {
            const refreshed = navigator.onLine
              ? await fetchTodaySchedules()
              : await getTodaySchedulesOffline();
            if (refreshed.length > 0) {
              availableSchedules = refreshed;
              setSchedules(refreshed);
            }
          } catch {
            // Keep the no-schedule path below.
          }
        }

        if (matches.length === 1 && navigator.onLine) {
          try {
            const personalized = await fetchKioskOptions(matches[0].id);
            const decision = getKioskOptionDecision(personalized.options);
            if (decision.kind === "auto") {
              const result = await submitKioskCheckinWithOfflineQueue({
                studentId: matches[0].id,
                scheduleId: decision.option.schedule_id,
                trainingTypeId: decision.option.training_type_id,
                checkinDate: decision.option.effective_date,
                online: true,
              });
              goToFeedback(result, matches[0]);
            } else if (decision.kind === "blocked") {
              goToErrorFeedback(
                {
                  data: {
                    code: decision.reasonCode,
                    detail: "Подойдите к тренеру",
                  },
                },
                matches[0],
              );
            } else {
              goToResult(matches[0], personalized.options);
            }
            return;
          } catch (error) {
            if (!isNetworkCheckinFailure(error)) {
              goToErrorFeedback(error, matches[0]);
              return;
            }
          }
        }

        const detectedTraining = autoDetectTraining(availableSchedules);

        // Offline fallback: use the cached club schedule when personalized
        // eligibility cannot be refreshed.
        if (
          matches.length === 1 &&
          detectedTraining &&
          detectedTraining.training_type_id !== null
        ) {
          try {
            const result = await submitKioskCheckinWithOfflineQueue({
              studentId: matches[0].id,
              scheduleId: detectedTraining.schedule_id,
              trainingTypeId: detectedTraining.training_type_id,
              checkinDate: detectedTraining.effective_date,
              online: false,
            });
            goToFeedback(result, matches[0]);
          } catch (error) {
            goToErrorFeedback(error, matches[0]);
          }
          return;
        }

        if (matches.length === 1) {
          goToResult(matches[0]);
          return;
        }

        goToMatches(matches, phoneSuffix);
      } catch (e) {
        // Silently ignore aborted requests (user typed again before response)
        if (e instanceof DOMException && e.name === "AbortError") return;
        // If online failed, try offline cache as fallback
        if (navigator.onLine) {
          try {
            const offlineMatches = await lookupStudentsOffline(phoneSuffix);
            if (offlineMatches.length > 0) {
              if (offlineMatches.length === 1) {
                goToResult(offlineMatches[0]);
              } else {
                goToMatches(offlineMatches, phoneSuffix);
              }
              return;
            }
          } catch {
            // IndexedDB also failed
          }
        }
        setError("Ошибка поиска. Попробуйте ещё раз");
        setDigits([]);
      } finally {
        setIsSearching(false);
      }
    },
    [
      schedules,
      setSchedules,
      goToFeedback,
      goToErrorFeedback,
      goToResult,
      goToMatches,
    ],
  );

  const handleDigit = useCallback(
    (digit: string) => {
      if (digits.length >= PHONE_DIGITS || isSearching) return;

      const newDigits = [...digits, digit];
      setDigits(newDigits);
      setError(null);

      // Auto-search on 4th digit (D-05)
      if (newDigits.length === PHONE_DIGITS) {
        performSearch(newDigits.join(""));
      }
    },
    [digits, isSearching, performSearch],
  );

  const handleBackspace = useCallback(() => {
    if (digits.length === 0 || isSearching) return;
    setDigits((prev) => prev.slice(0, -1));
    setError(null);
  }, [digits.length, isSearching]);

  const handleConfirm = useCallback(() => {
    if (digits.length === PHONE_DIGITS && !isSearching) {
      performSearch(digits.join(""));
    }
  }, [digits, isSearching, performSearch]);

  return (
    <div className="flex min-h-screen flex-col items-center px-6 pb-8 pt-8">
      {/* Header: logo + club name */}
      <div className="mb-8 flex items-center gap-3">
        {logoUrl ? (
          <img
            src={logoUrl}
            alt={clubName}
            className="h-12 w-12 rounded-lg object-contain"
            loading="eager"
          />
        ) : (
          <div className="flex h-12 w-12 items-center justify-center rounded-lg bg-[var(--branding-accent,#ff6b00)]">
            <span className="text-xl font-semibold text-white">
              {clubName.charAt(0) || "K"}
            </span>
          </div>
        )}
        <div>
          <h1 className="text-xl font-semibold uppercase tracking-wide text-white">
            {clubName}
          </h1>
        </div>
      </div>

      {/* Label */}
      <p className="mb-6 text-center text-base text-neutral-400">
        Введите последние 4 цифры телефона
      </p>

      {/* Input display */}
      <div className="mb-8">
        <InputDisplay digits={digits} maxLength={PHONE_DIGITS} />
      </div>

      {/* Loading indicator */}
      {isSearching && (
        <div
          className="mb-4 flex flex-col items-center gap-2"
          aria-live="polite"
        >
          <div className="h-2 w-32 animate-pulse rounded-full bg-neutral-700" />
          <p className="text-center text-[14px] text-neutral-400 animate-pulse">
            Поиск...
          </p>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="mb-4 text-center" aria-live="polite">
          <p className="text-base font-medium text-red-400">{error}</p>
          {error === "Ученик не найден" && (
            <p className="mt-1 text-sm text-neutral-500">
              Проверьте последние 4 цифры телефона
            </p>
          )}
        </div>
      )}

      {/* Numpad */}
      <div className="w-full max-w-sm">
        <Numpad
          onDigit={handleDigit}
          onBackspace={handleBackspace}
          onConfirm={handleConfirm}
          disabled={isSearching}
        />
      </div>

    </div>
  );
}

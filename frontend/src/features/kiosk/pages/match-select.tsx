import { useCallback, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { StudentMatchList } from "@/features/kiosk/components/student-match-list";
import {
  autoDetectTraining,
  fetchKioskOptions,
} from "@/features/kiosk/lib/kiosk-api";
import type {
  StudentMatch,
  CheckinResult,
  KioskScheduleOption,
  Schedule,
} from "@/features/kiosk/lib/kiosk-api";
import {
  isNetworkCheckinFailure,
  submitKioskCheckinWithOfflineQueue,
} from "@/features/kiosk/lib/checkin-submit";
import { getKioskOptionDecision } from "@/features/kiosk/lib/kiosk-option-decision";

interface MatchSelectProps {
  matches: StudentMatch[];
  phoneSuffix: string;
  schedules: Schedule[];
  goToNumpad: () => void;
  goToResult: (
    student: StudentMatch,
    kioskOptions?: KioskScheduleOption[] | null,
  ) => void;
  goToFeedback: (result: CheckinResult, student: StudentMatch) => void;
  goToErrorFeedback: (error: unknown, student: StudentMatch) => void;
}

export default function MatchSelect({
  matches,
  phoneSuffix,
  schedules,
  goToNumpad,
  goToResult,
  goToFeedback,
  goToErrorFeedback,
}: MatchSelectProps) {
  const [isChecking, setIsChecking] = useState(false);
  const maskedPhone = `** ** ${phoneSuffix}`;
  const handleSelect = useCallback(
    async (student: StudentMatch) => {
      if (isChecking) return;

      if (navigator.onLine) {
        setIsChecking(true);
        try {
          const personalized = await fetchKioskOptions(student.id);
          const decision = getKioskOptionDecision(personalized.options);
          if (decision.kind === "auto") {
            const result = await submitKioskCheckinWithOfflineQueue({
              studentId: student.id,
              scheduleId: decision.option.schedule_id,
              trainingTypeId: decision.option.training_type_id,
              checkinDate: decision.option.effective_date,
              online: true,
            });
            goToFeedback(result, student);
          } else if (decision.kind === "blocked") {
            goToErrorFeedback(
              {
                data: {
                  code: decision.reasonCode,
                  detail: "Подойдите к тренеру",
                },
              },
              student,
            );
          } else {
            goToResult(student, personalized.options);
          }
          return;
        } catch (error) {
          if (!isNetworkCheckinFailure(error)) {
            goToErrorFeedback(error, student);
            return;
          }
        } finally {
          setIsChecking(false);
        }
      }

      const detectedTraining = autoDetectTraining(schedules);

      // Offline fallback: use the cached club schedule.
      if (detectedTraining && detectedTraining.training_type_id !== null) {
        setIsChecking(true);
        try {
          const result = await submitKioskCheckinWithOfflineQueue({
            studentId: student.id,
            scheduleId: detectedTraining.schedule_id,
            trainingTypeId: detectedTraining.training_type_id,
            checkinDate: detectedTraining.effective_date,
            online: false,
          });
          goToFeedback(result, student);
        } catch (error) {
          goToErrorFeedback(error, student);
        } finally {
          setIsChecking(false);
        }
        return;
      }

      // No single training detected -> result screen for training selection
      goToResult(student);
    },
    [schedules, isChecking, goToFeedback, goToErrorFeedback, goToResult],
  );

  // Empty state guard -- shouldn't happen normally, but defensive
  if (matches.length === 0) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center px-4">
        <p className="text-xl font-semibold text-white">Ученик не найден</p>
        <p className="mt-2 text-base text-neutral-400">
          Проверьте последние 4 цифры телефона
        </p>
        <button
          type="button"
          onClick={goToNumpad}
          className="mx-auto mt-6 flex min-h-16 items-center gap-2 rounded-lg border border-neutral-600 px-6 py-3 text-base text-neutral-300 transition-transform active:scale-95 motion-reduce:active:scale-100"
        >
          <ArrowLeft className="h-4 w-4" />
          Ввести заново
        </button>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col px-4 pb-8 pt-8">
      {/* Header */}
      <div className="mb-6 text-center">
        <h2 className="text-xl font-semibold text-white">
          Найдено несколько совпадений
        </h2>
        <p className="mt-1 text-base text-neutral-400">{maskedPhone}</p>
      </div>

      {/* Subheading */}
      <p className="mb-4 text-center text-sm font-semibold uppercase tracking-wider text-neutral-400">
        Выберите себя
      </p>

      {/* Student list */}
      <div
        className={`flex-1 transition-opacity ${isChecking ? "opacity-50" : ""}`}
      >
        <StudentMatchList matches={matches} onSelect={handleSelect} />
      </div>

      {/* Back button */}
      <button
        type="button"
        onClick={goToNumpad}
        disabled={isChecking}
        className="mx-auto mt-6 flex min-h-16 items-center gap-2 rounded-lg border border-neutral-600 px-6 py-3 text-base text-neutral-300 transition-transform active:scale-95 disabled:opacity-50 motion-reduce:active:scale-100"
      >
        <ArrowLeft className="h-4 w-4" />
        Ввести заново
      </button>
    </div>
  );
}

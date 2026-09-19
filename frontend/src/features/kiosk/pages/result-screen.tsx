import { useState, useCallback, useEffect } from "react";
import { Clock3, Users, User } from "lucide-react";
import type {
  StudentMatch,
  CheckinResult,
  KioskScheduleOption,
  Schedule,
} from "@/features/kiosk/lib/kiosk-api";
import { submitKioskCheckinWithOfflineQueue } from "@/features/kiosk/lib/checkin-submit";
import { getKioskCheckinErrorMessage } from "@/features/kiosk/lib/checkin-error";
import {
  formatKioskCheckinOpening,
  getKioskOptionDecision,
} from "@/features/kiosk/lib/kiosk-option-decision";

interface ResultScreenProps {
  student: StudentMatch;
  schedules: Schedule[];
  kioskOptions?: KioskScheduleOption[] | null;
  goToFeedback: (result: CheckinResult, student: StudentMatch) => void;
  goToNumpad: () => void;
}

// Training type buttons when we have schedule data
interface TrainingOption {
  id: number;
  training_type_id: number | null;
  label: string;
  time: string;
  meta: string;
  effective_date: string;
  icon: "group" | "personal";
}

const WAITING_AUTO_RETURN_MS = 8_000;

function buildTrainingOptions(schedules: Schedule[]): TrainingOption[] {
  return schedules.map((s) => ({
    id: s.schedule_id,
    training_type_id: s.training_type_id,
    label: s.group_name || "Тренировка",
    time: `${s.start_time.slice(0, 5)}-${s.end_time.slice(0, 5)}`,
    meta: [s.training_type_name, s.trainer_name, s.location_name]
      .filter(Boolean)
      .join(" · "),
    effective_date: s.effective_date,
    icon: "group" as const,
  }));
}

function buildPersonalizedTrainingOptions(
  options: KioskScheduleOption[],
): TrainingOption[] {
  return options.map((option) => ({
    id: option.schedule_id,
    training_type_id: option.training_type_id,
    label: option.group_name || "Тренировка",
    time: `${option.start_time.slice(0, 5)}-${option.end_time.slice(0, 5)}`,
    meta: [option.training_type_name, option.trainer_name, option.location_name]
      .filter(Boolean)
      .join(" · "),
    effective_date: option.effective_date,
    icon: option.training_type_name.toLocaleLowerCase().includes("персон")
      ? "personal"
      : "group",
  }));
}

function formatSubscription(student: StudentMatch): string {
  if (!student.subscription_name) return "Нет активного";
  if (student.trainings_left === null || student.trainings_left === undefined) {
    return `${student.subscription_name} · Безлимит`;
  }
  return `${student.subscription_name} · ${student.trainings_left} тр.`;
}

export default function ResultScreen({
  student,
  schedules,
  kioskOptions = null,
  goToFeedback,
  goToNumpad,
}: ResultScreenProps) {
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const personalizedDecision =
    kioskOptions === null ? null : getKioskOptionDecision(kioskOptions);
  const waitingOptions =
    personalizedDecision?.kind === "wait" ? personalizedDecision.options : [];
  const personalizedSelectableOptions =
    personalizedDecision?.kind === "select"
      ? personalizedDecision.options
      : personalizedDecision?.kind === "auto"
        ? [personalizedDecision.option]
        : [];
  const trainingOptions =
    kioskOptions === null
      ? buildTrainingOptions(schedules)
      : buildPersonalizedTrainingOptions(personalizedSelectableOptions);

  useEffect(() => {
    if (waitingOptions.length === 0) return;
    const timer = window.setTimeout(goToNumpad, WAITING_AUTO_RETURN_MS);
    return () => window.clearTimeout(timer);
  }, [goToNumpad, waitingOptions.length]);

  const handleTrainingSelect = useCallback(
    async (option: TrainingOption) => {
      if (isLoading || option.training_type_id === null) return;
      setIsLoading(true);
      setError(null);

      try {
        const result = await submitKioskCheckinWithOfflineQueue({
          studentId: student.id,
          scheduleId: option.id,
          trainingTypeId: option.training_type_id,
          checkinDate: option.effective_date,
          online: navigator.onLine,
        });
        goToFeedback(result, student);
      } catch (err) {
        setError(getKioskCheckinErrorMessage(err));
        setIsLoading(false);
      }
    },
    [student, isLoading, goToFeedback],
  );

  return (
    <div className="flex min-h-screen flex-col items-center px-6 pb-8 pt-10">
      {/* Greeting */}
      <p className="mb-2 text-base text-neutral-400">Привет!</p>

      {/* Student name - Display size, uppercase */}
      <h1 className="mb-8 text-center text-5xl font-semibold uppercase leading-tight text-white">
        {student.last_name}
        <br />
        {student.first_name}
      </h1>

      {/* Info cards row */}
      <div className="mb-8 flex w-full max-w-2xl gap-3">
        {/* Grade card */}
        <div className="flex-1 rounded-xl bg-[#2A2A2A] p-4">
          <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-neutral-400">
            Грейд
          </p>
          <p className="break-words text-base font-semibold text-white">
            {student.grade_name || "Без грейда"}
          </p>
        </div>

        {/* Subscription card */}
        <div className="flex-1 rounded-xl bg-[#2A2A2A] p-4">
          <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-neutral-400">
            Абонемент
          </p>
          <p className="break-words text-base font-semibold text-white">
            {formatSubscription(student)}
          </p>
        </div>
      </div>

      {waitingOptions.length > 0 ? (
        <div
          className="w-full max-w-2xl"
          aria-live="polite"
        >
          <p className="mb-4 text-center text-sm font-semibold uppercase tracking-wider text-neutral-400">
            Ваша тренировка
          </p>
          <div className="grid gap-3">
            {waitingOptions.map((option) => (
              <div
                key={option.schedule_id}
                className="rounded-xl border border-amber-500/40 bg-[#242424] px-5 py-5"
              >
                <div className="flex items-start gap-4">
                  <Clock3 className="mt-0.5 h-6 w-6 shrink-0 text-amber-400" />
                  <div className="min-w-0">
                    <p className="text-lg font-semibold uppercase text-white">
                      {option.group_name || "Тренировка"}
                    </p>
                    <p className="mt-1 text-base text-neutral-300">
                      {option.start_time.slice(0, 5)}-
                      {option.end_time.slice(0, 5)}
                    </p>
                    <p className="mt-3 text-lg font-semibold text-amber-300">
                      Чек-ин откроется в{" "}
                      {formatKioskCheckinOpening(option)}
                    </p>
                    <p className="mt-1 text-sm text-neutral-400">
                      Введите номер снова после этого времени
                    </p>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : trainingOptions.length === 0 ? (
        <div className="w-full max-w-2xl rounded-lg border border-neutral-700 bg-[#242424] px-5 py-6 text-center">
          <p className="text-lg font-semibold text-white">
            Нет доступных тренировок сейчас
          </p>
          <p className="mt-2 text-base text-neutral-400">
            Обратитесь к тренеру
          </p>
        </div>
      ) : (
        <div className="w-full max-w-2xl">
          <p className="mb-4 text-center text-sm font-semibold uppercase tracking-wider text-neutral-400">
            Выберите тренировку
          </p>
          <div
            className={`grid gap-3 transition-opacity ${isLoading ? "opacity-50" : ""}`}
          >
            {trainingOptions.map((option, i) => (
              <button
                key={option.id}
                type="button"
                onClick={() => handleTrainingSelect(option)}
                disabled={isLoading || option.training_type_id === null}
                className={`flex min-h-20 items-center gap-4 rounded-lg px-5 py-4 text-left transition-transform duration-[60ms] ease-out active:scale-[0.98] disabled:pointer-events-none disabled:opacity-50 motion-reduce:transition-none motion-reduce:active:scale-100 ${
                  i === 0
                    ? "bg-[var(--branding-accent,#ff6b00)] text-white"
                    : "border border-neutral-600 bg-[#242424] text-white"
                }`}
              >
                {option.icon === "group" ? (
                  <Users className="h-6 w-6 shrink-0" />
                ) : (
                  <User className="h-6 w-6 shrink-0" />
                )}
                <div className="min-w-0">
                  <span className="block text-lg font-semibold uppercase">
                    {option.label}
                  </span>
                  <span className="mt-1 block text-base opacity-80">
                    {option.time}
                    {option.meta ? ` · ${option.meta}` : ""}
                  </span>
                </div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Error */}
      {error && (
        <p
          className="mt-4 text-center text-sm text-red-400"
          aria-live="assertive"
        >
          {error}
        </p>
      )}

      {/* Back button */}
      <button
        type="button"
        onClick={goToNumpad}
        disabled={isLoading}
        className="mt-8 min-h-16 rounded-lg border border-neutral-700 px-6 py-3 text-base text-neutral-300 disabled:opacity-50"
      >
        Ввести номер заново
      </button>
    </div>
  );
}

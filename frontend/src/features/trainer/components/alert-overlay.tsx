import { useEffect, useMemo } from "react";
import { cn } from "@/lib/utils";
import { AlertBadge } from "./alert-badge";
import type { CheckinResultOut, SubmittedStudentWithCheckin } from "../types";

interface AlertOverlayProps {
  readonly students: readonly SubmittedStudentWithCheckin[];
  readonly onDismiss: () => void;
}

interface CascadeBadge {
  readonly key: string;
  readonly label: string;
  readonly className: string;
}

function buildCascadeBadges(checkin: CheckinResultOut): CascadeBadge[] {
  const badges: CascadeBadge[] = [];

  if (checkin.duplicate) {
    badges.push({
      key: "duplicate",
      label: "Уже отмечен",
      className: "bg-neutral-200 text-neutral-800",
    });
  }

  if (checkin.subscription_effect === "deducted" || checkin.subscription_effect === "used") {
    badges.push({
      key: "subscription",
      label: "Абонемент",
      className: "bg-emerald-100 text-emerald-800",
    });
  }

  if (checkin.debt_effect === "created") {
    badges.push({
      key: "debt",
      label: "Долг",
      className: "bg-red-100 text-red-800",
    });
  }

  if (checkin.salary_queued) {
    badges.push({
      key: "salary",
      label: "ЗП",
      className: "bg-sky-100 text-sky-800",
    });
  }

  if (checkin.parent_notification_queued || checkin.trainings_left_push_queued) {
    badges.push({
      key: "parent",
      label: "Родитель",
      className: "bg-blue-100 text-blue-800",
    });
  }

  if (checkin.grade_progress_queued) {
    badges.push({
      key: "grade",
      label: "Прогресс",
      className: "bg-violet-100 text-violet-800",
    });
  }

  if (checkin.group_analytics_queued) {
    badges.push({
      key: "analytics",
      label: "Аналитика",
      className: "bg-stone-200 text-stone-800",
    });
  }

  if (checkin.retention_auto_close_queued || checkin.post_trial_task_queued) {
    badges.push({
      key: "tasks",
      label: "Задачи",
      className: "bg-amber-100 text-amber-900",
    });
  }

  return badges;
}

function hasFeedback(student: SubmittedStudentWithCheckin): boolean {
  return student.alerts.length > 0 || buildCascadeBadges(student.checkin).length > 0;
}

export function AlertOverlay({ students, onDismiss }: AlertOverlayProps) {
  const studentsWithFeedback = useMemo(
    () => students.filter(hasFeedback),
    [students],
  );

  // Don't auto-dismiss when any alert is critical (contraindication, last_training)
  const hasCritical = useMemo(
    () =>
      studentsWithFeedback.some((s) =>
        s.alerts.some(
          (a) => a.type === "contraindications" || a.type === "last_training",
        ),
      ),
    [studentsWithFeedback],
  );

  useEffect(() => {
    if (studentsWithFeedback.length === 0) {
      onDismiss();
      return;
    }
    if (hasCritical) return; // Critical alerts require manual dismiss
    const timer = setTimeout(onDismiss, 10_000);
    return () => clearTimeout(timer);
  }, [onDismiss, hasCritical, studentsWithFeedback.length]);

  if (studentsWithFeedback.length === 0) {
    return null;
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 animate-in fade-in duration-200"
      onClick={onDismiss}
    >
      <div
        className="mx-4 w-full max-w-sm rounded-2xl bg-white p-6 animate-in slide-in-from-bottom-4 duration-200"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="mb-4 text-[20px] font-semibold text-foreground">
          Сводка по группе
        </h2>

        <div className="flex flex-col gap-3 max-h-[60vh] overflow-y-auto">
          {studentsWithFeedback.map((student) => (
            <div key={student.id} className="flex flex-col gap-1">
              <span className="ui-title-16">
                {student.last_name} {student.first_name}
              </span>
              <div className="flex flex-wrap gap-1">
                {student.alerts.map((alert, i) => (
                  <AlertBadge key={`${alert.type}-${i}`} type={alert.type} />
                ))}
                {buildCascadeBadges(student.checkin).map((badge) => (
                  <span
                    key={badge.key}
                    className={cn(
                      "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium whitespace-nowrap",
                      badge.className,
                    )}
                  >
                    {badge.label}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>

        <button
          onClick={onDismiss}
          className="mt-6 w-full rounded-lg bg-neutral-100 py-3 text-[16px] font-semibold text-foreground active:bg-neutral-200 transition-colors"
        >
          Закрыть сводку
        </button>
      </div>
    </div>
  );
}

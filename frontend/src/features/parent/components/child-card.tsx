import { Card, CardContent } from "@/components/ui/card";
import { formatDateRu } from "@/lib/locale";
import { cn } from "@/lib/utils";
import { CalendarDays, ChevronRight, Trophy } from "lucide-react";
import { Link } from "react-router";
import { getParentSubscriptionAlert } from "../lib/subscription-alerts";

interface ChildCardProps {
  id: number;
  firstName: string;
  lastName: string;
  gradeName: string | null;
  subscriptionRemaining: number | null;
  subscriptionTotal: number | null;
  subscriptionStatus?: string | null;
  subscriptionFreezeStatus?: string | null;
  lastVisitDate: string | null;
  nextTrainingDayOfWeek?: number | null;
  nextTrainingStartTime?: string | null;
  nextTrainingGroupName?: string | null;
  nextTrainingTrainerName?: string | null;
  nextTrainingIsRescheduled?: boolean | null;
  nextTrainingIsSubstitute?: boolean | null;
}

const DAY_NAMES = [
  "Понедельник",
  "Вторник",
  "Среда",
  "Четверг",
  "Пятница",
  "Суббота",
  "Воскресенье",
];

export function ChildCard({
  id,
  firstName,
  lastName,
  gradeName,
  subscriptionRemaining,
  subscriptionTotal,
  subscriptionStatus = null,
  subscriptionFreezeStatus = null,
  lastVisitDate,
  nextTrainingDayOfWeek = null,
  nextTrainingStartTime = null,
  nextTrainingGroupName = null,
  nextTrainingTrainerName = null,
  nextTrainingIsRescheduled = false,
  nextTrainingIsSubstitute = false,
}: ChildCardProps) {
  const alert = getParentSubscriptionAlert({
    hasSubscription:
      subscriptionStatus !== null ||
      subscriptionFreezeStatus !== null ||
      subscriptionRemaining !== null ||
      subscriptionTotal !== null,
    remaining: subscriptionRemaining,
    total: subscriptionTotal,
    status: subscriptionStatus,
    freezeStatus: subscriptionFreezeStatus,
  });
  const isAttention = alert.requiresAttention;
  const subscriptionToneClass = {
    danger: "border-red-200 bg-red-50/85",
    warning: "border-amber-200 bg-amber-50/85",
    success: "border-emerald-100 bg-emerald-50/75",
    neutral: "border-black/5 bg-neutral-50",
  }[alert.tone];
  const subscriptionLabelClass = {
    danger: "text-red-700/75",
    warning: "text-amber-800/75",
    success: "text-emerald-700/70",
    neutral: "text-neutral-500",
  }[alert.tone];
  const subscriptionValueClass = {
    danger: "text-red-900",
    warning: "text-amber-950",
    success: "text-emerald-950",
    neutral: "text-neutral-600",
  }[alert.tone];
  const nextTrainingDay =
    nextTrainingDayOfWeek !== null
      ? DAY_NAMES[nextTrainingDayOfWeek] ?? null
      : null;
  const nextTrainingLabel =
    nextTrainingDay && nextTrainingStartTime
      ? `${nextTrainingDay}, ${nextTrainingStartTime.slice(0, 5)}`
      : null;

  return (
    <Link
      to={`/parent/child/${id}`}
      className="group block rounded-2xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--branding-accent)] focus-visible:ring-offset-2"
    >
      <Card
        className={cn(
          "parent-card-lift relative min-h-[172px] overflow-hidden border-0 bg-white/90 shadow-sm ring-1 ring-black/6 backdrop-blur-sm",
          isAttention && "ring-2",
        )}
        style={isAttention ? { borderColor: "var(--branding-accent)" } : undefined}
      >
        <div
          className="absolute inset-x-0 top-0 h-1"
          style={{ backgroundColor: "var(--branding-accent)" }}
        />
        <CardContent className="space-y-3.5 py-4">
          <div className="ui-row-between">
            <div className="min-w-0">
              <p className="parent-balanced-title text-[17px] font-semibold leading-tight">
                {firstName} {lastName}
              </p>
              <div className="mt-1 flex min-h-6 flex-wrap items-center gap-2">
                {gradeName ? (
                  <span className="inline-flex items-center gap-1 rounded-full bg-black/[0.04] px-2.5 py-1 text-[12px] font-medium text-neutral-700">
                    <Trophy className="h-3.5 w-3.5 text-amber-500" />
                    {gradeName}
                  </span>
                ) : null}
              </div>
            </div>
            {isAttention ? (
              <p
                className={cn(
                  "shrink-0 rounded-full px-2.5 py-1 text-[11px] font-semibold shadow-sm",
                  alert.tone === "danger"
                    ? "bg-red-600 text-white"
                    : "bg-amber-300 text-amber-950",
                )}
              >
                {alert.badge}
              </p>
            ) : (
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-neutral-100 text-neutral-500 transition-[background-color,color,transform] duration-150 ease-out group-hover:bg-[var(--branding-accent)] group-hover:text-black group-hover:translate-x-0.5">
                <ChevronRight className="h-4.5 w-4.5" />
              </span>
            )}
          </div>

          <div className="grid grid-cols-1 gap-2">
            <div
              className={cn(
                "rounded-xl border px-3 py-2.5",
                subscriptionToneClass,
              )}
            >
              <p
                className={cn(
                  "text-[11px] font-semibold uppercase",
                  subscriptionLabelClass,
                )}
              >
                Абонемент
              </p>
              <div className="space-y-0.5">
                <p
                  className={cn(
                    "parent-number text-[15px] font-semibold",
                    subscriptionValueClass,
                  )}
                >
                  {isAttention ? alert.title : alert.remainingText}
                </p>
                {isAttention ? (
                  <p className={cn("text-[12px] leading-4", subscriptionValueClass)}>
                    {alert.remainingText ?? alert.description}
                  </p>
                ) : null}
              </div>
            </div>

            {nextTrainingLabel ? (
              <div className="rounded-xl border border-black/6 bg-black/[0.02] px-3 py-2.5">
                <div className="flex items-center gap-2 text-[13px] font-semibold text-neutral-800">
                  <span
                    className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full"
                    style={{
                      background:
                        "linear-gradient(0deg, rgba(255,255,255,0.9), rgba(255,255,255,0.9)), var(--branding-accent)",
                    }}
                  >
                    <CalendarDays
                      className="h-4 w-4"
                      style={{ color: "var(--branding-accent)" }}
                    />
                  </span>
                  <span className="parent-number">{nextTrainingLabel}</span>
                </div>
                {nextTrainingGroupName ? (
                  <p className="parent-pretty-text mt-1 text-[13px] leading-5 text-neutral-600">
                    {nextTrainingGroupName}
                    {nextTrainingTrainerName ? ` · ${nextTrainingTrainerName}` : ""}
                  </p>
                ) : null}
                {nextTrainingIsRescheduled || nextTrainingIsSubstitute ? (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {nextTrainingIsRescheduled ? (
                      <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-800 ring-1 ring-amber-200">
                        Перенос
                      </span>
                    ) : null}
                    {nextTrainingIsSubstitute ? (
                      <span className="rounded-full bg-sky-50 px-2 py-0.5 text-[11px] font-medium text-sky-800 ring-1 ring-sky-200">
                        Замена тренера
                      </span>
                    ) : null}
                  </div>
                ) : null}
              </div>
            ) : null}

            {!nextTrainingLabel && !lastVisitDate ? (
              <div className="rounded-xl border border-black/5 bg-neutral-50 px-3 py-2.5">
                <p className="text-[13px] text-neutral-500">
                  Группы появятся после первых посещений
                </p>
              </div>
            ) : null}
          </div>

          {lastVisitDate ? (
            <p className="parent-number text-[12px] text-neutral-400">
              Последнее посещение: {formatDateRu(lastVisitDate)}
            </p>
          ) : null}
        </CardContent>
      </Card>
    </Link>
  );
}

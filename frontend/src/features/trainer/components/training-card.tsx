import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router";
import { ChevronRight, EllipsisVertical, UserPlus } from "lucide-react";
import { useBrandingStore } from "@/features/branding/use-branding";
import { toDateTimeParamInTimeZone } from "@/lib/club-date";
import { formatDateShort } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { ScheduleOccurrenceOut } from "../types";

function formatTime(time: string): string {
  return time.slice(0, 5);
}

function getDurationHours(start: string, end: string): string {
  const [sh, sm] = start.split(":").map(Number);
  const [eh, em] = end.split(":").map(Number);
  const mins = eh * 60 + em - (sh * 60 + sm);
  const hours = mins / 60;
  return hours === 1 ? "1ч" : `${hours.toFixed(1)}ч`;
}

function getOccurrenceDateTimeKey(date: string, time: string): string {
  const [hour = "00", minute = "00", second = "00"] = time.split(":");
  return `${date}T${hour.padStart(2, "0")}:${minute.padStart(2, "0")}:${second.padStart(2, "0")}`;
}

function hasOccurrenceStarted(date: string, time: string, timeZone: string): boolean {
  return (
    getOccurrenceDateTimeKey(date, time) <=
    toDateTimeParamInTimeZone(new Date(Date.now()), timeZone)
  );
}

interface TrainingCardProps {
  readonly schedule: ScheduleOccurrenceOut;
  readonly currentTrainerId?: number;
  readonly onEdit?: (schedule: ScheduleOccurrenceOut) => void;
  readonly onCancel?: (schedule: ScheduleOccurrenceOut) => void;
  readonly onReschedule?: (schedule: ScheduleOccurrenceOut) => void;
  readonly onAddGuest?: (schedule: ScheduleOccurrenceOut) => void;
  readonly actionsDisabled?: boolean;
}

export function TrainingCard({
  schedule,
  currentTrainerId,
  onEdit,
  onCancel,
  onReschedule,
  onAddGuest,
  actionsDisabled = false,
}: TrainingCardProps) {
  const navigate = useNavigate();
  const timeZone = useBrandingStore((state) => state.timeZone);
  const occurrenceStarted = hasOccurrenceStarted(
    schedule.effective_date,
    schedule.effective_start_time,
    timeZone,
  );
  const upcoming = !occurrenceStarted;
  const isOwn = currentTrainerId != null && schedule.trainer_id === currentTrainerId;
  const canManage = isOwn && upcoming && !schedule.is_substitute && !actionsDisabled;
  const canAddGuest =
    isOwn && !actionsDisabled && schedule.one_time_date == null && schedule.training_type_kind === "group";
  const sessionDetailPath = `/trainer/schedule/${schedule.schedule_id}/checkin?date=${schedule.effective_date}`;

  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    if (!menuOpen) return;
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [menuOpen]);

  function openSessionDetail() {
    if (isOwn && !actionsDisabled) {
      navigate(sessionDetailPath);
    }
  }

  return (
    <div
      role="group"
      aria-label={`Тренировка ${schedule.group_name}`}
      className={cn(
        "relative grid grid-cols-[56px_minmax(0,1fr)] gap-3 rounded-xl border-l-4 border-[var(--branding-accent)] bg-white p-2.5 shadow-sm transition-shadow sm:grid-cols-[64px_minmax(0,1fr)_auto] sm:p-3",
        isOwn ? "hover:shadow-md" : "",
      )}
    >
      <div
        className={`flex min-h-[74px] w-[56px] flex-col items-center justify-center rounded-lg px-2 py-2 sm:w-[64px] ${
          upcoming
            ? "bg-[var(--branding-accent)] text-white"
            : "bg-neutral-100 text-neutral-500"
        }`}
      >
        <span className="text-[16px] font-semibold leading-tight">
          {formatTime(schedule.effective_start_time)}
        </span>
        <span className="text-[12px] leading-tight opacity-80">
          {getDurationHours(
            schedule.effective_start_time,
            schedule.effective_end_time,
          )}
        </span>
      </div>

      <div className="min-w-0 pr-1 sm:pr-0">
        <span className="line-clamp-2 break-words text-[15px] font-semibold leading-snug text-foreground sm:text-[16px]">
          {schedule.group_name}
        </span>
        <div className="mt-1 flex min-w-0 flex-col gap-0.5 text-[12px] leading-snug text-muted-foreground sm:text-[13px]">
          <span className="truncate">{formatDateShort(schedule.effective_date)}</span>
          <span className="truncate">{schedule.location_name}</span>
          <span className="truncate">{schedule.trainer_name}</span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
          {schedule.is_rescheduled && (
            <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium uppercase text-amber-800">
              перенос
            </span>
          )}
          {schedule.is_substitute && (
            <span className="rounded-full bg-sky-100 px-2 py-0.5 text-[10px] font-medium uppercase text-sky-800">
              замена
            </span>
          )}
        </div>
      </div>

      <div className="col-start-2 flex min-w-0 items-center gap-1.5 sm:col-start-3 sm:row-span-2 sm:row-start-1 sm:gap-2">
        {canAddGuest && onAddGuest ? (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onAddGuest(schedule);
            }}
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-[var(--branding-accent)]/10 text-[var(--branding-accent)] transition-transform hover:bg-[var(--branding-accent)]/15 active:scale-95"
            aria-label="Добавить гостя"
            title="Добавить гостя"
          >
            <UserPlus size={18} />
          </button>
        ) : null}

        {isOwn ? (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              openSessionDetail();
            }}
            disabled={actionsDisabled}
            className="min-h-11 shrink-0 rounded-lg bg-[var(--branding-accent)] px-3 text-[12px] font-semibold text-white transition-transform active:scale-95 sm:px-4 sm:text-[13px]"
          >
            {occurrenceStarted ? "Статус" : "Открыть"}
          </button>
        ) : null}

        {canManage && (
          <div ref={menuRef} className="relative">
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                setMenuOpen((prev) => !prev);
              }}
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-transform hover:bg-neutral-100 active:scale-95"
              aria-label="Действия"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
            >
              <EllipsisVertical size={18} />
            </button>

            {menuOpen && (
              <div
                role="menu"
                className="absolute right-0 top-full mt-1 z-50 w-48 rounded-lg bg-white py-1 shadow-lg ring-1 ring-foreground/10"
              >
                <button
                  type="button"
                  role="menuitem"
                  className="flex min-h-11 w-full items-center px-4 text-left text-[14px] text-foreground hover:bg-neutral-50 active:bg-neutral-100"
                  onClick={(event) => {
                    event.stopPropagation();
                    setMenuOpen(false);
                    onEdit?.(schedule);
                  }}
                >
                  Редактировать
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="flex min-h-11 w-full items-center px-4 text-left text-[14px] text-foreground hover:bg-neutral-50 active:bg-neutral-100"
                  onClick={(event) => {
                    event.stopPropagation();
                    setMenuOpen(false);
                    onReschedule?.(schedule);
                  }}
                >
                  Перенести
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="flex min-h-11 w-full items-center px-4 text-left text-[14px] text-red-500 hover:bg-neutral-50 active:bg-neutral-100"
                  onClick={(event) => {
                    event.stopPropagation();
                    setMenuOpen(false);
                    onCancel?.(schedule);
                  }}
                >
                  Отменить
                </button>
              </div>
            )}
          </div>
        )}
        {isOwn ? (
          <ChevronRight
            size={16}
            className="hidden shrink-0 text-muted-foreground/50 sm:block"
          />
        ) : null}
      </div>
    </div>
  );
}

import { useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import type { AttendanceItem } from "./attendance-list";
import { MONTH_NAMES } from "@/lib/locale";
import { StudentSurfaceCard } from "./student-surface-card";
import { parseDateOnly } from "../lib/student-schedule-utils";

const DAY_HEADERS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

interface AttendanceCalendarProps {
  attendanceDates: Record<string, AttendanceItem[]>;
  currentMonth: Date;
  onMonthChange: (date: Date) => void;
}

function toDateKey(year: number, month: number, day: number): string {
  const m = String(month + 1).padStart(2, "0");
  const d = String(day).padStart(2, "0");
  return `${year}-${m}-${d}`;
}

function getCalendarDays(
  year: number,
  month: number,
): { day: number; isCurrentMonth: boolean }[] {
  const firstDay = new Date(year, month, 1);
  // Monday-based: 0=Mon..6=Sun
  const startOffset = (firstDay.getDay() + 6) % 7;
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const daysInPrevMonth = new Date(year, month, 0).getDate();

  const days: { day: number; isCurrentMonth: boolean }[] = [];

  // Previous month trailing days
  for (let i = startOffset - 1; i >= 0; i--) {
    days.push({ day: daysInPrevMonth - i, isCurrentMonth: false });
  }
  // Current month
  for (let d = 1; d <= daysInMonth; d++) {
    days.push({ day: d, isCurrentMonth: true });
  }
  // Next month leading days (fill to complete last row)
  const remaining = 7 - (days.length % 7);
  if (remaining < 7) {
    for (let d = 1; d <= remaining; d++) {
      days.push({ day: d, isCurrentMonth: false });
    }
  }

  return days;
}

function formatVisitCount(count: number): string {
  const remainder10 = count % 10;
  const remainder100 = count % 100;

  if (remainder10 === 1 && remainder100 !== 11) return `${count} посещение`;
  if (remainder10 >= 2 && remainder10 <= 4 && (remainder100 < 10 || remainder100 >= 20)) {
    return `${count} посещения`;
  }
  return `${count} посещений`;
}

export function AttendanceCalendar({
  attendanceDates,
  currentMonth,
  onMonthChange,
}: AttendanceCalendarProps) {
  const [selectedDay, setSelectedDay] = useState<string | null>(null);

  const year = currentMonth.getFullYear();
  const month = currentMonth.getMonth();
  const today = new Date();
  const isCurrentMonth =
    today.getFullYear() === year && today.getMonth() === month;
  const todayDate = today.getDate();

  const days = getCalendarDays(year, month);
  const monthTitle = `${MONTH_NAMES[month]} ${year}`;
  const hasMonthAttendance = Object.keys(attendanceDates).some((key) =>
    key.startsWith(`${year}-${String(month + 1).padStart(2, "0")}-`),
  );

  const handlePrev = () => {
    onMonthChange(new Date(year, month - 1, 1));
  };

  const handleNext = () => {
    onMonthChange(new Date(year, month + 1, 1));
  };

  const handleDayClick = (day: number, isCurrentMonth: boolean) => {
    if (!isCurrentMonth) return;
    const key = toDateKey(year, month, day);
    if (attendanceDates[key]?.length) {
      setSelectedDay(key);
    }
  };

  const selectedItems = selectedDay ? (attendanceDates[selectedDay] ?? []) : [];
  const monthHasToday = isCurrentMonth;

  return (
    <StudentSurfaceCard className="space-y-3 p-3.5 sm:p-4">
      <div className="ui-row-between-center">
        <button
          type="button"
          onClick={handlePrev}
          className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full bg-neutral-100 text-neutral-700 ring-1 ring-black/6 transition hover:bg-neutral-200/80"
          aria-label="Предыдущий месяц"
        >
          <ChevronLeft className="size-5" />
        </button>

        <div className="min-w-0 flex-1 text-center">
          <p className="ui-overline">
            Месяц посещений
          </p>
          <p className="mt-1 text-[17px] font-semibold">
            {monthTitle}
          </p>
        </div>

        <button
          type="button"
          onClick={handleNext}
          className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full bg-neutral-100 text-neutral-700 ring-1 ring-black/6 transition hover:bg-neutral-200/80"
          aria-label="Следующий месяц"
        >
          <ChevronRight className="size-5" />
        </button>
      </div>

      <div className="flex items-center justify-center">
        <p className="text-center text-[12px] leading-5 text-muted-foreground">
          {hasMonthAttendance
            ? "Нажми на день с точкой, чтобы открыть список занятий."
            : "За этот месяц отметок пока нет."}
          {monthHasToday ? " Сегодня выделено контуром." : ""}
        </p>
      </div>

      <div className="grid grid-cols-7 gap-1">
        {DAY_HEADERS.map((d) => (
          <div
            key={d}
            className="rounded-lg bg-neutral-50 py-1.5 text-center text-[9px] font-semibold uppercase tracking-[0.08em] text-muted-foreground"
          >
            {d}
          </div>
        ))}
      </div>

      <div className="grid grid-cols-7 gap-1">
        {days.map((cell, idx) => {
          const dateKey = toDateKey(year, month, cell.day);
          const hasAttendance =
            cell.isCurrentMonth && !!attendanceDates[dateKey]?.length;
          const attendanceCount = attendanceDates[dateKey]?.length ?? 0;
          const isToday =
            cell.isCurrentMonth && isCurrentMonth && cell.day === todayDate;
          const isSelected = selectedDay === dateKey;
          const ariaLabel = !cell.isCurrentMonth
            ? `${cell.day}: вне текущего месяца`
            : hasAttendance
              ? `${cell.day} ${monthTitle}: ${formatVisitCount(attendanceCount)}, открыть`
              : `${cell.day} ${monthTitle}: нет посещений`;

          return (
            <button
              key={idx}
              type="button"
              onClick={() => handleDayClick(cell.day, cell.isCurrentMonth)}
              disabled={!hasAttendance}
              aria-label={ariaLabel}
              className={`
                relative flex aspect-square min-h-[44px] w-full flex-col items-center justify-center rounded-[16px] text-[13px] font-medium transition
                ${cell.isCurrentMonth ? "bg-white text-neutral-900 ring-1 ring-black/6" : "bg-neutral-50 text-neutral-300"}
                ${hasAttendance ? "cursor-pointer hover:-translate-y-0.5 hover:shadow-sm" : "cursor-default opacity-75"}
                ${isToday ? "ring-2 ring-[var(--branding-accent)]" : ""}
                ${isSelected ? "bg-[var(--branding-accent)]/8 ring-2 ring-[var(--branding-accent)]" : ""}
              `}
            >
              <span className="leading-none">{cell.day}</span>
              {hasAttendance && (
                <span
                  className="absolute bottom-1.5 h-1.5 w-1.5 rounded-full"
                  style={{ backgroundColor: "var(--branding-accent)" }}
                />
              )}
              {isToday && (
                <span
                  className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full"
                  style={{ backgroundColor: "var(--branding-accent)" }}
                  aria-hidden="true"
                >
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Day detail sheet */}
      <Sheet
        open={selectedDay !== null}
        onOpenChange={(open) => {
          if (!open) setSelectedDay(null);
        }}
      >
        <SheetContent side="bottom">
          <SheetHeader>
            <SheetTitle>
              {selectedDay &&
                new Intl.DateTimeFormat("ru-RU", {
                  day: "numeric",
                  month: "long",
                }).format(parseDateOnly(selectedDay))}
            </SheetTitle>
          </SheetHeader>
          <div className="space-y-2.5 px-4 pb-4">
            {selectedItems.map((item) => (
              <div
                key={item.id}
                className="rounded-2xl border border-black/6 bg-white p-3 shadow-sm"
              >
                <div className="ui-row-between">
                  <div className="space-y-1">
                    <p className="text-[15px] font-semibold">
                      {item.training_type_name}
                    </p>
                    <p className="ui-muted-14">
                      {item.group_name}
                    </p>
                  </div>
                  <span className="rounded-full bg-[var(--branding-accent)]/10 px-2.5 py-1 text-[10px] font-semibold text-[var(--branding-accent)]">
                    {item.start_time}
                  </span>
                </div>
                <p className="mt-2 text-[12px] text-muted-foreground">
                  {item.trainer_name} &middot; {item.location_name}
                </p>
              </div>
            ))}
          </div>
        </SheetContent>
      </Sheet>
    </StudentSurfaceCard>
  );
}

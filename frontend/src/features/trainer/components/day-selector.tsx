import { useMemo } from "react";
import { cn, isSameDay } from "@/lib/utils";

interface DaySelectorProps {
  selectedDate: Date;
  onDateChange: (date: Date) => void;
  today?: Date;
}

function getWeekDays(referenceDate: Date): Date[] {
  const monday = new Date(referenceDate);
  const day = monday.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  monday.setDate(monday.getDate() + diff);
  monday.setHours(0, 0, 0, 0);
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(monday);
    d.setDate(monday.getDate() + i);
    return d;
  });
}

const SHORT_DAYS = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"] as const;

function formatDayShort(date: Date): string {
  return SHORT_DAYS[date.getDay()];
}

export function DaySelector({ selectedDate, onDateChange, today: todayProp }: DaySelectorProps) {
  const fallbackToday = useMemo(() => new Date(), []);
  const today = todayProp ?? fallbackToday;
  const days = getWeekDays(selectedDate);

  return (
    <div className="flex gap-1 overflow-x-auto px-4 py-2">
      {days.map((day) => {
        const isSelected = isSameDay(day, selectedDate);
        const isCurrentDay = isSameDay(day, today);
        return (
          <button
            key={day.toISOString()}
            type="button"
            aria-current={isCurrentDay ? "date" : undefined}
            aria-pressed={isSelected}
            aria-label={`${formatDayShort(day)} ${day.getDate()}${isCurrentDay ? ", сегодня" : ""}`}
            onClick={() => onDateChange(day)}
            className={cn(
              "flex flex-col items-center min-w-[48px] rounded-lg py-2 px-1 transition-colors",
              isSelected
                ? "bg-[var(--branding-accent)] text-white"
                : "text-muted-foreground",
            )}
          >
            <span className="text-[12px]">{formatDayShort(day)}</span>
            <span className="text-[16px] font-semibold">{day.getDate()}</span>
            {isCurrentDay && !isSelected && (
              <span aria-hidden="true" className="h-1 w-1 rounded-full bg-[var(--branding-accent)] mt-0.5" />
            )}
          </button>
        );
      })}
    </div>
  );
}

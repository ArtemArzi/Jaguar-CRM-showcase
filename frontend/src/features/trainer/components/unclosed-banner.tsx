import { AlertTriangle, ChevronRight } from "lucide-react";
import { useNavigate } from "react-router";

interface UnclosedSchedule {
  id: number;
  date: string;
  group_name: string;
  start_time: string;
}

interface UnclosedBannerProps {
  readonly schedules: readonly UnclosedSchedule[];
  readonly actionsDisabled?: boolean;
}

function formatShortDate(value: string): string {
  const [year, month, day] = value.split("-");
  if (!year || !month || !day) return value;
  return `${day}.${month}`;
}

function ScheduleRow({
  schedule,
  onClick,
  disabled = false,
}: {
  readonly schedule: UnclosedSchedule;
  readonly onClick: () => void;
  readonly disabled?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      className="flex items-center justify-between w-full rounded-lg bg-amber-100/60 px-3 py-2.5 text-left active:bg-amber-200/60 transition-colors"
      onClick={onClick}
    >
      <div className="ui-row-2">
        <span className="rounded bg-amber-200/70 px-1.5 py-0.5 text-[12px] font-medium text-amber-900">
          {formatShortDate(schedule.date)}
        </span>
        <span className="text-[14px] font-medium text-amber-900">
          {schedule.start_time.slice(0, 5)}
        </span>
        <span className="text-[14px] text-amber-800">
          {schedule.group_name}
        </span>
      </div>
      <div className="flex items-center gap-1 shrink-0">
        <span className="text-[12px] text-amber-600">
          {disabled ? "обновите данные" : "проверить"}
        </span>
        <ChevronRight size={16} className="text-amber-500" />
      </div>
    </button>
  );
}

export function UnclosedBanner({ schedules, actionsDisabled = false }: UnclosedBannerProps) {
  const navigate = useNavigate();

  if (schedules.length === 0) return null;

  return (
    <div className="rounded-xl border border-amber-200 bg-amber-50 p-3">
      <div className="flex items-center gap-2 mb-2">
        <AlertTriangle size={18} className="shrink-0 text-amber-600" />
        <p className="text-[14px] font-medium text-amber-800">
          Есть незакрытые тренировки ({schedules.length})
        </p>
      </div>
      <div className="flex flex-col gap-1.5">
        {schedules.map((s) => (
          <ScheduleRow
            key={`${s.id}-${s.date}`}
            schedule={s}
            disabled={actionsDisabled}
            onClick={() => navigate(`/trainer/schedule/${s.id}/checkin?date=${s.date}`)}
          />
        ))}
      </div>
    </div>
  );
}

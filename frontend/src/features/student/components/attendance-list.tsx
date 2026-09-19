import { formatDateRu } from "@/lib/locale";
import { StudentSurfaceCard } from "./student-surface-card";

interface AttendanceItem {
  id: number;
  date: string;
  group_name: string;
  trainer_name: string;
  location_name: string;
  training_type_name: string;
  start_time: string;
}

interface AttendanceListProps {
  items: AttendanceItem[];
  totalCount: number;
}

export function AttendanceList({ items, totalCount }: AttendanceListProps) {
  return (
    <div className="space-y-3">
      <div className="flex items-end justify-between gap-3 px-1">
        <div className="space-y-1">
          <p className="ui-overline">
            Лента посещений
          </p>
          <p className="text-[16px] font-semibold">Тренировки и отметки</p>
        </div>
        <p className="ui-muted-13" aria-live="polite">
          {items.length} из {totalCount}
        </p>
      </div>

      {items.length === 0 ? (
        <StudentSurfaceCard className="p-5 text-center" aria-live="polite">
          <p className="text-[15px] font-semibold">Посещений пока нет</p>
          <p className="mt-2 text-[14px] leading-6 text-muted-foreground">
            После первой отметки тренера здесь появятся дата, группа, тренер и зал.
          </p>
        </StudentSurfaceCard>
      ) : (
        <div className="space-y-2.5">
          {items.map((item) => (
            <StudentSurfaceCard
              key={item.id}
              className="relative overflow-hidden p-3.5"
            >
              <span
                className="absolute inset-y-0 left-0 w-1.5 rounded-r-full"
                style={{ backgroundColor: "var(--branding-accent)" }}
              />
              <div className="flex items-start justify-between gap-3 pl-2">
                <div className="min-w-0 space-y-1.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="truncate text-[15px] font-semibold leading-6">
                      {item.training_type_name}
                    </p>
                    <span className="rounded-full bg-[var(--branding-accent)]/10 px-2.5 py-1 text-[10px] font-semibold text-[var(--branding-accent)]">
                      {item.start_time}
                    </span>
                  </div>
                  <p className="ui-caption-muted">
                    {item.group_name}
                  </p>
                </div>

                <p className="shrink-0 rounded-full bg-neutral-100 px-2.5 py-1 text-[11px] font-semibold text-neutral-700">
                  {formatDateRu(item.date)}
                </p>
              </div>

              <div className="mt-2.5 flex flex-wrap gap-2 pl-2">
                <span className="rounded-full bg-neutral-100 px-2.5 py-1 text-[11px] text-neutral-600">
                  {item.trainer_name}
                </span>
                <span className="rounded-full bg-neutral-100 px-2.5 py-1 text-[11px] text-neutral-600">
                  {item.location_name}
                </span>
              </div>
            </StudentSurfaceCard>
          ))}
        </div>
      )}
    </div>
  );
}

export type { AttendanceItem };

import { useMemo, useState } from "react";
import { CalendarDays, ListFilter } from "lucide-react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuthStore } from "@/features/auth/auth-store";
import apiClient from "@/api/custom-fetch";
import { AttendanceList } from "../components/attendance-list";
import type { AttendanceItem } from "../components/attendance-list";
import { AttendanceCalendar } from "../components/attendance-calendar";
import { DisciplineFilter } from "../components/discipline-filter";
import { StudentPageIntro } from "../components/student-page-intro";
import { StudentSurfaceCard } from "../components/student-surface-card";
import { readAttendanceSummary } from "../lib/student-normalizers";

const ATTENDANCE_PAGE_LIMIT = 50;
const EMPTY_ATTENDANCE_ITEMS: AttendanceItem[] = [];

function toMonthParam(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  return `${y}-${m}`;
}

function AttendanceSkeleton() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-28 w-full rounded-[28px]" />
      <Skeleton className="h-14 w-full rounded-2xl" />
      <div className="space-y-3">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-24 w-full rounded-[24px]" />
        ))}
      </div>
    </div>
  );
}

export default function StudentAttendance() {
  const studentId = useAuthStore((s) => s.studentId);
  const bootstrapStatus = useAuthStore((s) => s.studentBootstrapStatus);
  const [viewMode, setViewMode] = useState<"list" | "calendar">("list");
  const [selectedDiscipline, setSelectedDiscipline] = useState<string | null>(
    null,
  );
  const [currentMonth, setCurrentMonth] = useState(() => new Date());

  const listQuery = useInfiniteQuery({
    queryKey: ["student", "attendance", "list", studentId],
    queryFn: async ({ pageParam }) => {
      const offset = Number(pageParam ?? 0);
      const response = await apiClient.get("/students/me/attendance/", {
        params: { limit: String(ATTENDANCE_PAGE_LIMIT), offset: String(offset) },
      });
      const raw = response.data;
      if (Array.isArray(raw)) {
        return { items: raw as AttendanceItem[], totalCount: raw.length };
      }
      return readAttendanceSummary(raw);
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => {
      if (lastPage.items.length === 0) return undefined;

      const loadedCount = pages.reduce((count, page) => count + page.items.length, 0);
      return loadedCount < lastPage.totalCount ? loadedCount : undefined;
    },
    enabled: bootstrapStatus === "resolved" && !!studentId && viewMode === "list",
    staleTime: 60_000,
  });

  const calendarQuery = useQuery({
    queryKey: ["student", "attendance", "calendar", studentId, toMonthParam(currentMonth)],
    queryFn: async () => {
      const response = await apiClient.get("/students/me/attendance/", {
        params: { month: toMonthParam(currentMonth) },
      });
      const raw = response.data;
      if (Array.isArray(raw)) {
        return { items: raw as AttendanceItem[], totalCount: raw.length };
      }
      const summary = readAttendanceSummary(raw);
      return { items: summary.items, totalCount: summary.items.length };
    },
    enabled: bootstrapStatus === "resolved" && !!studentId && viewMode === "calendar",
    staleTime: 60_000,
  });

  const listItems = useMemo(
    () => listQuery.data?.pages.flatMap((page) => page.items) ?? EMPTY_ATTENDANCE_ITEMS,
    [listQuery.data],
  );
  const calendarItems = calendarQuery.data?.items ?? EMPTY_ATTENDANCE_ITEMS;
  const items = viewMode === "list" ? listItems : calendarItems;
  const totalCount =
    viewMode === "list"
      ? (listQuery.data?.pages.at(-1)?.totalCount ?? 0)
      : (calendarQuery.data?.totalCount ?? 0);
  const isLoading = viewMode === "list" ? listQuery.isLoading : calendarQuery.isLoading;
  const isError = viewMode === "list" ? listQuery.isError : calendarQuery.isError;
  const canLoadMore = viewMode === "list" && listQuery.hasNextPage;
  const isInitialLoading = isLoading && items.length === 0;
  const refetchActiveAttendance =
    viewMode === "list" ? listQuery.refetch : calendarQuery.refetch;
  const monthLabel = useMemo(
    () =>
      new Intl.DateTimeFormat("ru-RU", {
        month: "long",
        year: "numeric",
      })
        .format(currentMonth)
        .replace(" г.", ""),
    [currentMonth],
  );

  // Derive unique disciplines
  const disciplines = useMemo(
    () => [...new Set(items.map((i) => i.training_type_name))],
    [items],
  );

  // Filtered items
  const filteredItems = useMemo(
    () =>
      selectedDiscipline
        ? items.filter((i) => i.training_type_name === selectedDiscipline)
        : items,
    [items, selectedDiscipline],
  );

  // Calendar data: group by date
  const attendanceDates = useMemo(() => {
    const map: Record<string, AttendanceItem[]> = {};
    for (const item of filteredItems) {
      const key = item.date;
      if (!map[key]) map[key] = [];
      map[key].push(item);
    }
    return map;
  }, [filteredItems]);

  if (bootstrapStatus === "idle" || bootstrapStatus === "loading") {
    return <AttendanceSkeleton />;
  }

  if (!studentId) return <AttendanceSkeleton />;

  if (isError && items.length === 0) {
    return (
      <div className="min-h-full bg-[radial-gradient(circle_at_top,_rgba(0,0,0,0.024),_transparent_38%)] px-5 pb-24 pt-5">
        <div className="space-y-4">
          <StudentPageIntro
            eyebrow="Журнал посещений"
            title="Посещения"
            description="Смотри историю занятий в списке или по месяцам, фильтруй по дисциплинам и быстро открывай детали нужного дня."
          />

          <StudentSurfaceCard className="p-5 text-center">
            <p className="text-[17px] font-semibold">Не удалось загрузить посещения</p>
            <p className="mt-2 text-[14px] leading-6 text-muted-foreground">
              Проверьте соединение и повторите попытку. История посещений пока не обновлена.
            </p>
            <button
              type="button"
              onClick={() => {
                void refetchActiveAttendance();
              }}
              className="mt-4 inline-flex min-h-[44px] w-full items-center justify-center rounded-2xl border border-black/6 bg-black/[0.03] px-4 text-[14px] font-semibold text-foreground transition active:scale-[0.99]"
            >
              Повторить
            </button>
          </StudentSurfaceCard>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-full bg-[radial-gradient(circle_at_top,_rgba(0,0,0,0.024),_transparent_38%)] px-5 pb-24 pt-5">
      <div className="space-y-4">
        <StudentPageIntro
          eyebrow="Журнал посещений"
          title="Посещения"
          description="Смотри историю занятий в списке или по месяцам, фильтруй по дисциплинам и быстро открывай детали нужного дня."
        />

        <StudentSurfaceCard className="p-3.5 sm:p-4">
          <div className="space-y-3.5">
            <div className="ui-row-between">
              <div className="space-y-1">
                <p className="ui-overline">
                  Режим просмотра
                </p>
                <p className="text-[18px] font-semibold leading-tight">
                  {viewMode === "list"
                    ? "Список посещений"
                    : `Календарь на ${monthLabel}`}
                </p>
                <p className="max-w-[34rem] text-[13px] leading-5 text-muted-foreground">
                  {viewMode === "list"
                    ? "Лента занятий с быстрым доступом к дате, залу и тренеру."
                    : "Месячная сетка с точками посещений и открытием деталей дня."}
                </p>
              </div>

              <span className="shrink-0 rounded-full bg-black/[0.04] px-2.5 py-1 text-[11px] font-semibold text-muted-foreground">
                {totalCount} всего
              </span>
            </div>

            <div className="grid grid-cols-2 gap-2 rounded-[24px] bg-neutral-100 p-1 ring-1 ring-black/6">
              <button
                type="button"
                onClick={() => setViewMode("list")}
                aria-pressed={viewMode === "list"}
                className={`inline-flex min-h-[46px] items-center justify-center gap-2 rounded-[20px] px-3.5 text-[14px] font-semibold transition-all ${
                  viewMode === "list"
                    ? "bg-white text-neutral-950 shadow-sm"
                    : "text-neutral-500 hover:text-neutral-900"
                }`}
              >
                <ListFilter className="size-4" />
                Список
              </button>
              <button
                type="button"
                onClick={() => setViewMode("calendar")}
                aria-pressed={viewMode === "calendar"}
                className={`inline-flex min-h-[46px] items-center justify-center gap-2 rounded-[20px] px-3.5 text-[14px] font-semibold transition-all ${
                  viewMode === "calendar"
                    ? "bg-white text-neutral-950 shadow-sm"
                    : "text-neutral-500 hover:text-neutral-900"
                }`}
              >
                <CalendarDays className="size-4" />
                Календарь
              </button>
            </div>

            <div className="grid grid-cols-3 gap-2">
              <div className="rounded-2xl bg-neutral-50 px-3 py-2 ring-1 ring-black/5">
                <p className="text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                  Показано
                </p>
                <p className="mt-1 text-[17px] font-semibold">{filteredItems.length}</p>
              </div>
              <div className="rounded-2xl bg-neutral-50 px-3 py-2 ring-1 ring-black/5">
                <p className="text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                  Дисциплины
                </p>
                <p className="mt-1 text-[17px] font-semibold">{disciplines.length}</p>
              </div>
              <div className="rounded-2xl bg-neutral-50 px-3 py-2 ring-1 ring-black/5">
                <p className="text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                  Режим
                </p>
                <p className="mt-1 truncate text-[15px] font-semibold">
                  {viewMode === "list" ? "Список" : "Месяц"}
                </p>
              </div>
            </div>
          </div>
        </StudentSurfaceCard>

        <DisciplineFilter
          disciplines={disciplines}
          selected={selectedDiscipline}
          onSelect={setSelectedDiscipline}
        />

        {/* Content */}
        {isInitialLoading ? (
          <AttendanceSkeleton />
        ) : viewMode === "list" ? (
          <AttendanceList items={filteredItems} totalCount={totalCount} />
        ) : (
          <AttendanceCalendar
            attendanceDates={attendanceDates}
            currentMonth={currentMonth}
            onMonthChange={setCurrentMonth}
          />
        )}

        {canLoadMore ? (
          <button
            type="button"
            onClick={() => {
              void listQuery.fetchNextPage();
            }}
            disabled={listQuery.isFetchingNextPage}
            className="inline-flex min-h-[46px] w-full items-center justify-center rounded-2xl border border-black/6 bg-white/90 px-4 text-[14px] font-semibold text-foreground shadow-sm transition active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-60"
          >
            {listQuery.isFetchingNextPage ? "Загружаем..." : "Показать ещё"}
          </button>
        ) : viewMode === "list" && totalCount > 0 ? (
          <p className="text-center text-[12px] text-neutral-400">
            Показана вся доступная история
          </p>
        ) : null}
      </div>
    </div>
  );
}

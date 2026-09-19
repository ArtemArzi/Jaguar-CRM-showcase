import { useEffect, useState, useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarDays, AlertTriangle, CalendarPlus, Plus } from "lucide-react";
import { useNavigate } from "react-router";
import { Skeleton } from "@/components/ui/skeleton";
import apiClient from "@/api/custom-fetch";
import { useBrandingStore } from "@/features/branding/use-branding";
import { todayInTimeZone } from "@/lib/club-date";
import { getInitials, isSameDay, formatRub, toISODate } from "@/lib/utils";
import type { EarningSummary } from "../types";
import { TrainingCard } from "../components/training-card";
import { UnclosedBanner } from "../components/unclosed-banner";
import { DaySelector } from "../components/day-selector";
import { ScheduleFormSheet } from "../components/schedule-form-sheet";
import { CancelSessionDialog } from "../components/cancel-session-dialog";
import { RescheduleSheet } from "../components/reschedule-sheet";
import { GuestVisitSheet } from "../components/guest-visit-sheet";
import { QueryStateNotice } from "../components/query-state-notice";
import { useTrainerIdentity } from "../hooks/use-trainer-identity";
import type { ScheduleOccurrenceOut, ScheduleOut, StudentWithAlerts } from "../types";

function formatRussianDateForDate(date: Date): string {
  const days = [
    "Воскресенье",
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
  ];
  const months = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
  ];
  return `${days[date.getDay()]}, ${date.getDate()} ${months[date.getMonth()]}`;
}

function ScheduleSkeleton() {
  return (
    <div className="ui-col-3">
      {[1, 2, 3].map((i) => (
        <Skeleton key={i} className="h-[80px] rounded-xl" />
      ))}
    </div>
  );
}

function EmptyState({ isToday }: { isToday: boolean }) {
  return (
    <div className="ui-empty-state">
      <CalendarDays size={48} className="ui-muted" />
      <p className="ui-title-20">
        {isToday ? "Сегодня нет тренировок" : "Нет тренировок"}
      </p>
      <p className="text-[16px] text-muted-foreground">
        {isToday ? "Отдыхайте!" : "В этот день тренировок нет"}
      </p>
    </div>
  );
}

interface RetentionTaskPreview {
  id: number;
  level: string;
  student_name: string;
  due_date: string;
}

export default function ScheduleHome() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const timeZone = useBrandingStore((s) => s.timeZone);
  const today = useMemo(() => todayInTimeZone(timeZone), [timeZone]);
  const [selectedDate, setSelectedDate] = useState<Date>(today);

  useEffect(() => {
    setSelectedDate(today);
  }, [today]);

  // Schedule form state
  const [formOpen, setFormOpen] = useState(false);
  const [editSchedule, setEditSchedule] = useState<ScheduleOut | null>(null);

  // Cancel dialog state
  const [cancelTarget, setCancelTarget] = useState<{
    id: number;
    name: string;
    date: string;
  } | null>(null);

  // Reschedule state
  const [rescheduleTarget, setRescheduleTarget] =
    useState<ScheduleOccurrenceOut | null>(null);
  const [guestTarget, setGuestTarget] = useState<ScheduleOccurrenceOut | null>(null);

  const isToday = isSameDay(selectedDate, today);
  const dateStr = toISODate(selectedDate);

  const schedulesQuery = useQuery<ScheduleOccurrenceOut[]>({
    queryKey: ["schedules", isToday ? "today" : "by-date", dateStr],
    queryFn: () =>
      isToday
        ? apiClient.get("/schedules/today/").then((r) => r.data)
        : apiClient
            .get("/schedules/by-date/", { params: { date: dateStr } })
            .then((r) => r.data),
    staleTime: 60_000,
  });

  const unclosedRange = useMemo(() => {
    const from = new Date(today);
    const to = new Date(today);
    from.setDate(from.getDate() - 14);
    to.setDate(to.getDate() - 1);
    return {
      from: toISODate(from),
      to: toISODate(to),
    };
  }, [today]);

  const unclosedQuery = useQuery<ScheduleOccurrenceOut[]>({
    queryKey: ["schedules", "unclosed", unclosedRange.from, unclosedRange.to],
    queryFn: () =>
      apiClient
        .get("/schedules/unclosed/", {
          params: { date_from: unclosedRange.from, date_to: unclosedRange.to },
        })
        .then((r) => r.data),
    staleTime: 60_000,
  });

  const trainerIdentity = useTrainerIdentity();
  const trainerInfo = trainerIdentity.data;

  const trainerDbId = trainerInfo?.id;

  const earningsQuery = useQuery<EarningSummary>({
    queryKey: ["trainer", "earnings", trainerDbId],
    queryFn: () =>
      apiClient
        .get(`/trainers/${trainerDbId}/earnings/summary/`)
        .then((r) => r.data),
    enabled: !!trainerDbId,
    staleTime: 5 * 60_000,
  });

  const tasksQuery = useQuery<RetentionTaskPreview[]>({
    queryKey: ["retention-tasks", trainerDbId, { preview: true, limit: 5 }],
    queryFn: () =>
      apiClient
        .get("/retention/tasks/", {
          params: { trainer_id: trainerDbId, resolved: false, limit: 5, offset: 0 },
        })
        .then((r) => r.data.items ?? r.data),
    enabled: !!trainerDbId,
    staleTime: 60_000,
  });

  const guestScheduleId = guestTarget ? String(guestTarget.schedule_id) : "";
  const guestDate = guestTarget?.effective_date ?? "";
  const guestRosterQuery = useQuery<StudentWithAlerts[]>({
    queryKey: ["schedule", guestScheduleId, "students", guestDate],
    queryFn: () =>
      apiClient
        .get(`/schedules/${guestScheduleId}/students/`, {
          params: { date: guestDate },
        })
        .then((r) => r.data),
    enabled: !!guestTarget,
    staleTime: 60_000,
  });

  const schedules = schedulesQuery.data;
  const unclosedScheduleRows = unclosedQuery.data;
  const earnings = earningsQuery.data;
  const tasks = tasksQuery.data;
  const guestRoster = guestRosterQuery.data;
  const firstName = trainerInfo?.first_name ?? "Тренер";
  const taskPreviewPending =
    trainerIdentity.isLoading || (Boolean(trainerDbId) && tasksQuery.isLoading);
  const scheduleUnavailable = schedulesQuery.isError || schedulesQuery.isRefetchError;
  const scheduleActionsBlocked = scheduleUnavailable;

  async function handleEditOccurrence(occurrence: ScheduleOccurrenceOut) {
    const rawSchedule = await queryClient.fetchQuery<ScheduleOut>({
      queryKey: ["schedule", occurrence.schedule_id, "edit"],
      queryFn: () =>
        apiClient.get(`/schedules/${occurrence.schedule_id}/`).then((r) => r.data),
      staleTime: 60_000,
    });
    setEditSchedule(rawSchedule);
    setFormOpen(true);
  }

  const unclosedSchedules = useMemo(
    () =>
      unclosedScheduleRows?.map((s) => ({
        id: s.schedule_id,
        date: s.effective_date,
        group_name: s.group_name,
        start_time: s.effective_start_time,
      })) ?? [],
    [unclosedScheduleRows],
  );

  return (
    <div className="flex flex-col gap-6 px-4 pt-6 pb-24">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="min-w-0">
          <h1 className="truncate text-lg font-semibold text-foreground">
            Привет, {firstName}
          </h1>
          <p className="ui-muted-14">
            {formatRussianDateForDate(selectedDate)}
          </p>
        </div>
        <div className="ui-row-2">
          <button
            type="button"
            onClick={() => navigate("/trainer/availability")}
            className="flex h-11 items-center gap-1.5 rounded-xl bg-white px-3 text-[13px] font-semibold text-[var(--branding-accent)] shadow-sm ring-1 ring-foreground/5 active:scale-95 transition-transform"
          >
            <CalendarPlus size={17} />
            Доступность
          </button>
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[var(--branding-accent)] text-white text-[14px] font-semibold">
            {getInitials(firstName)}
          </div>
        </div>
      </div>

      {/* D-22: unclosed training banner */}
      {unclosedQuery.isError || unclosedQuery.isRefetchError ? (
        <QueryStateNotice
          title={
            unclosedScheduleRows
              ? "Список незакрытых тренировок мог устареть"
              : "Не удалось проверить незакрытые тренировки"
          }
          message="Проверьте список повторно, чтобы не оставить посещения незакрытыми."
          retryLabel="Повторить проверку незакрытых"
          retrying={unclosedQuery.isFetching}
          onRetry={() => void unclosedQuery.refetch()}
        />
      ) : null}
      {unclosedSchedules.length > 0 ? (
        <UnclosedBanner
          schedules={unclosedSchedules}
          actionsDisabled={unclosedQuery.isError || unclosedQuery.isRefetchError}
        />
      ) : null}

      {/* D-21: Day selector for week view */}
      <DaySelector selectedDate={selectedDate} onDateChange={setSelectedDate} today={today} />

      {/* My trainings */}
      <section>
        <h2 className="mb-3 text-sm font-medium text-foreground/70">
          {isToday ? "Мои тренировки" : "Тренировки"}
        </h2>
        {schedulesQuery.isLoading && !schedules ? (
          <ScheduleSkeleton />
        ) : scheduleUnavailable ? (
          <>
            <QueryStateNotice
              title={schedules ? "Расписание могло устареть" : "Не удалось загрузить тренировки"}
              message="Сохранённые карточки можно просмотреть, но действия недоступны до успешного обновления."
              retryLabel="Повторить загрузку тренировок"
              retrying={schedulesQuery.isFetching}
              onRetry={() => void schedulesQuery.refetch()}
            />
            {schedules?.length ? (
              <div className="mt-3 flex flex-col gap-3">
                {schedules.map((s) => (
                  <TrainingCard
                    key={`${s.schedule_id}:${s.effective_date}:${s.effective_start_time}`}
                    schedule={s}
                    currentTrainerId={trainerDbId}
                    actionsDisabled
                  />
                ))}
              </div>
            ) : null}
          </>
        ) : !schedules?.length ? (
          <EmptyState isToday={isToday} />
        ) : (
          <div className="ui-col-3">
            {schedules.map((s) => (
              <TrainingCard
                key={`${s.schedule_id}:${s.effective_date}:${s.effective_start_time}`}
                schedule={s}
                currentTrainerId={trainerDbId}
                actionsDisabled={scheduleActionsBlocked}
                onEdit={handleEditOccurrence}
                onCancel={(sched) =>
                  setCancelTarget({
                    id: sched.schedule_id,
                    name: sched.group_name,
                    date: sched.effective_date,
                  })
                }
                onReschedule={(sched) => setRescheduleTarget(sched)}
                onAddGuest={(sched) => setGuestTarget(sched)}
              />
            ))}
          </div>
        )}
      </section>

      {/* Tasks preview */}
      <section>
        <h2 className="mb-3 text-sm font-medium text-foreground/70">
          Задачи на сегодня
        </h2>
        {taskPreviewPending ? (
          <Skeleton className="h-[78px] rounded-xl" />
        ) : trainerIdentity.isError || trainerIdentity.isRefetchError || tasksQuery.isError || tasksQuery.isRefetchError ? (
          <QueryStateNotice
            title={tasks ? "Задачи могли устареть" : "Не удалось загрузить задачи"}
            message="Повторите загрузку, чтобы проверить актуальный список звонков."
            retryLabel="Повторить загрузку задач"
            retrying={trainerIdentity.isFetching || tasksQuery.isFetching}
            onRetry={() => {
              if (trainerIdentity.isError || trainerIdentity.isRefetchError) {
                void trainerIdentity.refetch();
              } else {
                void tasksQuery.refetch();
              }
            }}
          />
        ) : !trainerDbId ? (
          <div className="ui-card">
            <p className="ui-title-14">
              Задачи загрузятся после профиля тренера
            </p>
          </div>
        ) : !tasks?.length ? (
          <div className="ui-card">
            <p className="ui-title-14">
              Сегодня задач нет
            </p>
            <p className="ui-muted-detail">
              Если появятся ученики для звонка, они будут здесь отдельными карточками.
            </p>
          </div>
        ) : (
          <div className="ui-col-2">
            {tasks.slice(0, 3).map((t) => (
              <div
                key={t.id}
                className={`flex items-center gap-3 rounded-xl p-3 cursor-pointer active:scale-[0.98] transition-transform shadow-sm border-l-4 ${t.level === "red" ? "border-red-500 bg-red-50" : "border-amber-500 bg-amber-50/50"}`}
                onClick={() => navigate("/trainer/tasks")}
              >
                <AlertTriangle
                  size={18}
                  className={
                    t.level === "red"
                      ? "text-red-500"
                      : "text-amber-500"
                  }
                />
                <div className="flex-1 min-w-0">
                  <p className="text-[14px] font-medium text-foreground truncate">
                    {t.student_name}
                  </p>
                  <p className="ui-muted-12">
                    {t.level === "red" ? "Высокий риск" : "Внимание"}
                  </p>
                </div>
              </div>
            ))}
            {tasks.length > 3 && (
              <p
                className="text-[13px] text-center text-muted-foreground cursor-pointer"
                onClick={() => navigate("/trainer/tasks")}
              >
                ещё {tasks.length - 3}...
              </p>
            )}
          </div>
        )}
      </section>

      {/* Bottom cards row */}
      <div className="grid grid-cols-2 gap-3 pb-4">
        <div className="rounded-xl bg-white p-4 shadow-sm">
          <p className="text-sm font-medium text-foreground/70">
            Мой заработок
          </p>
          {earningsQuery.isError || earningsQuery.isRefetchError ? (
            <p className="mt-1 text-[14px] font-semibold text-amber-700">
              Заработок недоступен
            </p>
          ) : earnings ? (
            <p className="mt-1 text-[20px] font-semibold text-foreground">
              {formatRub(earnings.total_amount)}
            </p>
          ) : (
            <Skeleton className="mt-2 h-7 w-20" />
          )}
        </div>
        <div className="rounded-xl bg-white p-4 shadow-sm">
          <p className="text-sm font-medium text-foreground/70">
            Мои ученики
          </p>
          <p className="mt-1 text-[20px] font-semibold text-foreground">
            {trainerInfo?.student_count ?? "--"}
          </p>
        </div>
      </div>

      {earningsQuery.isError || earningsQuery.isRefetchError ? (
        <QueryStateNotice
          title={earnings ? "Заработок мог устареть" : "Заработок недоступен"}
          message="Не используйте прежнюю сумму для сверки, пока обновление не завершится успешно."
          retryLabel="Повторить загрузку заработка"
          retrying={earningsQuery.isFetching}
          onRetry={() => void earningsQuery.refetch()}
        />
      ) : null}

      {/* FAB: create new training */}
      {trainerDbId && !scheduleActionsBlocked && (
        <button
          type="button"
          onClick={() => {
            setEditSchedule(null);
            setFormOpen(true);
          }}
          className="fixed bottom-20 right-4 z-40 flex h-14 w-14 items-center justify-center rounded-full bg-[var(--branding-accent)] text-white shadow-lg active:scale-95 transition-transform"
          aria-label="Создать тренировку"
        >
          <Plus size={28} />
        </button>
      )}

      {/* Schedule form sheet (create / edit) */}
      {trainerDbId && formOpen && (
        <ScheduleFormSheet
          key={
            editSchedule
              ? `edit-${editSchedule.id}`
              : `create-${dateStr}`
          }
          open={formOpen}
          onOpenChange={(o) => {
            setFormOpen(o);
            if (!o) setEditSchedule(null);
          }}
          trainerId={trainerDbId}
          editSchedule={editSchedule}
          defaultDate={dateStr}
        />
      )}

      {/* Cancel session dialog */}
      {cancelTarget && (
        <CancelSessionDialog
          open={!!cancelTarget}
          onOpenChange={() => setCancelTarget(null)}
          scheduleId={cancelTarget.id}
          scheduleName={cancelTarget.name}
          date={cancelTarget.date}
        />
      )}

      {/* Reschedule sheet */}
      {rescheduleTarget && (
        <RescheduleSheet
          key={`${rescheduleTarget.schedule_id}:${rescheduleTarget.effective_date}:${rescheduleTarget.effective_start_time}`}
          open={!!rescheduleTarget}
          onOpenChange={() => setRescheduleTarget(null)}
          scheduleId={rescheduleTarget.schedule_id}
          scheduleName={rescheduleTarget.group_name}
          originalDate={rescheduleTarget.effective_date}
          originalStartTime={rescheduleTarget.effective_start_time}
          originalEndTime={rescheduleTarget.effective_end_time}
        />
      )}

      {guestTarget ? (
        <GuestVisitSheet
          open={!!guestTarget}
          onOpenChange={(open) => {
            if (!open) setGuestTarget(null);
          }}
          scheduleId={String(guestTarget.schedule_id)}
          checkinDate={guestTarget.effective_date}
          rosterStudents={guestRoster ?? []}
          rosterLoading={guestRosterQuery.isLoading}
          rosterError={guestRosterQuery.isError || guestRosterQuery.isRefetchError}
          onRetryRoster={() => void guestRosterQuery.refetch()}
          trainerId={guestTarget.trainer_id}
        />
      ) : null}
    </div>
  );
}

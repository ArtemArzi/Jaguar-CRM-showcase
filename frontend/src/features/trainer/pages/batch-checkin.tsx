import { useCallback, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CheckCircle2, RefreshCw, UserPlus } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import apiClient from "@/api/custom-fetch";
import { cn, toISODate } from "@/lib/utils";
import { BatchCheckinList } from "../components/batch-checkin-list";
import { GuestVisitSheet } from "../components/guest-visit-sheet";
import type {
  GroupSessionOut,
  SessionDetailOut,
  SessionRosterStudent,
} from "../types";
import {
  getStudentCheckinBlockLabel,
  invalidateBatchCheckinQueries,
} from "./batch-checkin-helpers";

function getRosterStatusLabel(student: SessionRosterStudent): string {
  const blockLabel = getStudentCheckinBlockLabel(student);
  if (student.checkin_status === "checked_in") return "Отмечен";
  if (student.checkin_status === "blocked" || blockLabel) {
    return blockLabel ? `Недоступен: ${blockLabel.toLowerCase()}` : "Недоступен";
  }
  return "Ждет отметки";
}

function getRosterSourceLabel(student: SessionRosterStudent): string {
  if (student.created_from === "guest_visit" || student.is_guest_visit) return "Гость";
  if (student.created_from === "student_self_booking") return "Сам записался";
  if (student.created_from === "personal_booking") return "Персоналка";
  return "Плановая запись";
}

function formatCheckedInAt(value?: string | null): string {
  if (!value) return "Не отмечен";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export default function BatchCheckin() {
  const { scheduleId } = useParams<{ scheduleId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const checkinDate = searchParams.get("date") || toISODate();
  const [guestSheetOpen, setGuestSheetOpen] = useState(false);
  const [selectedStudentId, setSelectedStudentId] = useState<number | null>(null);
  const sessionQueryKey = ["schedule", scheduleId, "session-detail", checkinDate] as const;

  const {
    data: session,
    isLoading,
    isError,
    refetch: refetchSession,
  } = useQuery<SessionDetailOut>({
    queryKey: sessionQueryKey,
    queryFn: () =>
      apiClient
        .get(`/schedules/${scheduleId}/session-detail/`, {
          params: { date: checkinDate },
        })
        .then((r) => r.data),
    staleTime: 30_000,
    enabled: !!scheduleId,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data || data.is_closed || data.can_close) return false;
      const closeAllowedAt = Date.parse(data.close_allowed_at);
      if (!Number.isFinite(closeAllowedAt)) return false;
      const delayMs = closeAllowedAt - Date.now();
      if (delayMs <= 0) return 1_000;
      return Math.min(delayMs + 250, 60_000);
    },
  });

  const occurrence = session?.occurrence;
  const roster = useMemo<SessionRosterStudent[]>(
    () => session?.roster ?? [],
    [session],
  );
  const waitingRoster = useMemo(
    () => roster.filter((student) => student.checkin_status === "waiting"),
    [roster],
  );
  const selectedRosterStudent = useMemo(
    () => roster.find((student) => student.id === selectedStudentId) ?? null,
    [roster, selectedStudentId],
  );
  const summary = session?.summary ?? {
    expected_count: 0,
    checked_in_count: 0,
    waiting_count: 0,
    blocked_count: 0,
  };
  const isClosed = session?.is_closed ?? false;
  const closeAllowed = session?.can_close ?? false;
  const canBookGuest = Boolean(
    scheduleId &&
      occurrence?.training_type_id != null &&
      occurrence.training_type_kind === "group" &&
      occurrence.one_time_date == null &&
      !isClosed,
  );

  const invalidateSession = useCallback(() => {
    if (!scheduleId) return;
    invalidateBatchCheckinQueries(queryClient, {
      scheduleId,
      checkinDate,
      studentIds: [],
      trainerId: occurrence?.trainer_id ?? null,
    });
  }, [checkinDate, occurrence?.trainer_id, queryClient, scheduleId]);

  const closeMutation = useMutation<GroupSessionOut>({
    mutationFn: () =>
      apiClient
        .post(`/schedules/${scheduleId}/sessions/close/`, {
          date: checkinDate,
          topic_tags: [],
          notes: "",
        })
        .then((r) => r.data),
    onSuccess() {
      invalidateSession();
    },
    onError() {
      invalidateSession();
    },
  });

  const handleRefresh = useCallback(() => {
    refetchSession();
  }, [refetchSession]);

  const handleGuestBooked = useCallback(() => {
    invalidateSession();
    refetchSession();
  }, [invalidateSession, refetchSession]);

  return (
    <div className="flex min-h-full flex-col bg-[#F5F2ED] pb-24">
      <div className="bg-white px-4 pb-4 pt-4">
        <button
          type="button"
          onClick={() => navigate("/trainer")}
          className="mb-3 flex min-h-11 items-center gap-2 text-[14px] text-muted-foreground"
        >
          <ArrowLeft size={18} />
          Назад
        </button>

        <div className="ui-row-between">
          <div className="min-w-0">
            <h1 className="truncate text-[20px] font-semibold text-foreground">
              {occurrence?.group_name ?? "Загрузка..."}
            </h1>
            <p className="ui-muted-14">
              {occurrence
                ? `${occurrence.effective_start_time.slice(0, 5)}-${occurrence.effective_end_time.slice(0, 5)} · ${occurrence.location_name} · ${summary.expected_count} учеников`
                : ""}
            </p>
          </div>
          {isClosed ? (
            <span className="shrink-0 rounded-full bg-emerald-50 px-3 py-1 text-[12px] font-medium text-emerald-800 ring-1 ring-emerald-200">
              Закрыта
            </span>
          ) : null}
        </div>

        <div className="mt-3 rounded-lg bg-sky-50 px-3 py-2 text-[13px] leading-snug text-sky-900 ring-1 ring-sky-100">
          Отметку делает ученик: через киоск в клубе или приложение. Тренер
          здесь только проверяет список.
        </div>

        <div className="mt-3 grid grid-cols-3 gap-2">
          <CounterCard label="Отмечены" value={summary.checked_in_count} />
          <CounterCard label="Ждут" value={summary.waiting_count} />
          <CounterCard label="Недоступны" value={summary.blocked_count} />
        </div>
      </div>

      <div className="mt-2 bg-white px-4 py-3">
        {occurrence?.training_type_kind === "group" && occurrence.one_time_date != null ? (
          <div className="flex min-h-11 w-full items-center justify-center gap-2 rounded-xl bg-muted px-3 text-center text-[14px] font-medium text-muted-foreground">
            Разовое занятие: список формируется вручную
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setGuestSheetOpen(true)}
            disabled={!canBookGuest}
            className={cn(
              "flex min-h-11 w-full items-center justify-center gap-2 rounded-xl text-[15px] font-medium transition-colors",
              canBookGuest
                ? "bg-[var(--branding-accent)]/10 text-[var(--branding-accent)] active:bg-[var(--branding-accent)]/15"
                : "bg-muted text-muted-foreground",
            )}
          >
            <UserPlus size={18} />
            Добавить в список занятия
          </button>
        )}
      </div>

      <div className="mt-2 bg-white px-4 py-3">
        <div className="ui-row-between-center">
          <div>
            <h2 className="ui-title-16">
              {waitingRoster.length > 0 ? "Кого отправить на отметку" : "Список занятия"}
            </h2>
            <p className="ui-muted-13">
              Если ученик в зале, но не отмечен, отправьте его на киоск.
            </p>
          </div>
          <button
            type="button"
            onClick={handleRefresh}
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-neutral-100 text-muted-foreground active:bg-neutral-200"
            aria-label="Обновить"
            title="Обновить"
          >
            <RefreshCw size={18} />
          </button>
        </div>
      </div>

      <div className="mt-2 flex-1 bg-white">
        {isLoading ? (
          <div className="flex flex-col gap-2 p-4">
            {[1, 2, 3, 4, 5].map((i) => (
              <Skeleton key={i} className="h-[56px] rounded-lg" />
            ))}
          </div>
        ) : isError ? (
          <div className="p-4 text-[14px] text-destructive">
            Не удалось загрузить занятие. Обновите экран.
          </div>
        ) : (
          <BatchCheckinList
            students={roster}
            onStudentSelect={(student) => setSelectedStudentId(student.id)}
          />
        )}
      </div>

      <div className="sticky bottom-0 border-t border-neutral-200 bg-white px-4 py-3">
        {isClosed ? (
          <div className="flex min-h-11 items-center justify-center gap-2 rounded-xl bg-emerald-50 text-[15px] font-semibold text-emerald-800">
            <CheckCircle2 size={18} />
            Итоги сохранены
          </div>
        ) : closeAllowed ? (
          <button
            type="button"
            onClick={() => closeMutation.mutate()}
            disabled={closeMutation.isPending}
            className="min-h-11 w-full rounded-xl bg-[var(--branding-accent)] px-4 text-[15px] font-semibold text-white active:opacity-90 disabled:opacity-50"
          >
            {closeMutation.isPending ? "Сохранение..." : "Закрыть тренировку"}
          </button>
        ) : (
          <div className="rounded-xl bg-muted px-4 py-3 text-center text-[14px] font-medium text-muted-foreground">
            Закрыть можно после окончания тренировки
          </div>
        )}
        {closeMutation.isError ? (
          <p className="mt-2 text-center text-[13px] text-destructive">
            Не удалось сохранить итоги. Попробуйте снова.
          </p>
        ) : null}
      </div>

      {scheduleId ? (
        <GuestVisitSheet
          open={guestSheetOpen}
          onOpenChange={setGuestSheetOpen}
          scheduleId={scheduleId}
          checkinDate={checkinDate}
          rosterStudents={roster}
          trainerId={occurrence?.trainer_id ?? null}
          onBooked={handleGuestBooked}
        />
      ) : null}

      <Sheet
        open={selectedRosterStudent !== null}
        onOpenChange={(open) => {
          if (!open) setSelectedStudentId(null);
        }}
      >
        <SheetContent side="bottom" className="rounded-t-2xl">
          <SheetHeader>
            <SheetTitle>
              {selectedRosterStudent
                ? `${selectedRosterStudent.last_name} ${selectedRosterStudent.first_name}`
                : "Ученик"}
            </SheetTitle>
            <SheetDescription>Статус в текущем занятии</SheetDescription>
          </SheetHeader>
          {selectedRosterStudent ? (
            <div className="space-y-4 px-4 pb-5">
              <div className="grid grid-cols-1 gap-2 text-[14px] sm:grid-cols-2">
                <RosterContextRow
                  label="Статус"
                  value={getRosterStatusLabel(selectedRosterStudent)}
                />
                <RosterContextRow
                  label="Источник"
                  value={getRosterSourceLabel(selectedRosterStudent)}
                />
                <RosterContextRow
                  label="Отметка"
                  value={formatCheckedInAt(selectedRosterStudent.checked_in_at)}
                />
                <RosterContextRow
                  label="Причина"
                  value={getStudentCheckinBlockLabel(selectedRosterStudent) ?? "Нет"}
                />
              </div>

              {selectedRosterStudent.alerts.length > 0 ? (
                <div className="rounded-xl bg-amber-50 px-3 py-2 text-[13px] leading-5 text-amber-900 ring-1 ring-amber-100">
                  {selectedRosterStudent.alerts.map((alert) => alert.message).join(" · ")}
                </div>
              ) : null}

              <button
                type="button"
                className="min-h-11 w-full rounded-xl bg-[var(--branding-accent)] px-4 text-[15px] font-semibold text-white active:opacity-90"
                onClick={() => navigate(`/trainer/students/${selectedRosterStudent.id}`)}
              >
                Открыть карточку
              </button>
            </div>
          ) : null}
        </SheetContent>
      </Sheet>
    </div>
  );
}

function CounterCard({ label, value }: { readonly label: string; readonly value: number }) {
  return (
    <div className="rounded-lg bg-neutral-50 px-3 py-2 text-center ring-1 ring-neutral-100">
      <div className="text-[20px] font-semibold leading-tight text-foreground">{value}</div>
      <div className="ui-muted-12">{label}</div>
    </div>
  );
}

function RosterContextRow({
  label,
  value,
}: {
  readonly label: string;
  readonly value: string;
}) {
  return (
    <div className="rounded-xl bg-neutral-50 px-3 py-2 ring-1 ring-neutral-100">
      <p className="text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </p>
      <p className="mt-1 text-[14px] font-medium text-foreground">{value}</p>
    </div>
  );
}

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Search, UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import apiClient from "@/api/custom-fetch";
import { getApiError } from "@/lib/utils";
import type { GuestVisitCandidate, GuestVisitOut, StudentWithAlerts } from "../types";
import { filterGuestVisitCandidates } from "../lib/guest-visit-candidates";
import { invalidateBatchCheckinQueries } from "../pages/batch-checkin-helpers";
import { QueryStateNotice } from "./query-state-notice";

type GuestVisitCandidateResponse =
  | GuestVisitCandidate[]
  | { items?: GuestVisitCandidate[] };

interface GuestVisitSheetProps {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly scheduleId: string;
  readonly checkinDate: string;
  readonly rosterStudents: readonly StudentWithAlerts[];
  readonly rosterLoading?: boolean;
  readonly rosterError?: boolean;
  readonly onRetryRoster?: () => void;
  readonly trainerId?: number | null;
  readonly onBooked?: (student: StudentWithAlerts) => void;
}

function candidateItems(
  data: GuestVisitCandidateResponse | undefined,
): GuestVisitCandidate[] {
  if (!data) return [];
  return Array.isArray(data) ? data : (data.items ?? []);
}

function studentName(student: Pick<GuestVisitCandidate, "first_name" | "last_name">): string {
  return [student.last_name, student.first_name].filter(Boolean).join(" ");
}

function canRunGuestSearch(value: string): boolean {
  const normalized = value.trim();
  if (normalized.length < 2) return false;
  const digits = normalized.replace(/\D/g, "");
  const hasLetters = /[A-Za-zА-Яа-яЁё]/.test(normalized);
  if (!hasLetters && digits.length > 0) {
    return digits.length >= 4;
  }
  return true;
}

function guestVisitPayload({
  scheduleId,
  checkinDate,
  student,
}: {
  scheduleId: string;
  checkinDate: string;
  student: GuestVisitCandidate;
}) {
  return {
    date: checkinDate,
    ...(student.kind === "lead"
      ? { lead_id: student.id }
      : { student_id: student.id }),
    origin: "walk_in_checkin",
    idempotency_key: `trainer-walk-in-${scheduleId}-${checkinDate}-${student.kind}-${student.id}`,
  };
}

function rosterRowFromBooking(
  booking: GuestVisitOut,
  student: GuestVisitCandidate,
): StudentWithAlerts {
  return {
    id: booking.student_id,
    first_name: student.first_name,
    last_name: student.last_name,
    alerts: [],
    enrollment_id: booking.enrollment_id,
    created_from: booking.created_from,
    starts_on: booking.starts_on ?? null,
    ends_on: booking.ends_on ?? null,
    is_guest_visit: booking.is_guest_visit,
    enrollment_status: "active",
    checkin_blocked_reason: null,
  };
}

function mergeRosterStudent(
  current: readonly StudentWithAlerts[] | undefined,
  student: StudentWithAlerts,
): StudentWithAlerts[] {
  const rows = current ?? [];
  if (rows.some((row) => row.id === student.id)) {
    return rows.map((row) => (row.id === student.id ? { ...row, ...student } : row));
  }
  return [...rows, student];
}

export function GuestVisitSheet({
  open,
  onOpenChange,
  scheduleId,
  checkinDate,
  rosterStudents,
  rosterLoading = false,
  rosterError = false,
  onRetryRoster = () => undefined,
  trainerId,
  onBooked,
}: GuestVisitSheetProps) {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [selectedCandidate, setSelectedCandidate] = useState<GuestVisitCandidate | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const normalizedQuery = query.trim();
  const normalizedDebouncedQuery = debouncedQuery.trim();
  const canSearch = canRunGuestSearch(normalizedDebouncedQuery);
  const isDebouncing = canRunGuestSearch(normalizedQuery) && normalizedDebouncedQuery !== normalizedQuery;

  useEffect(() => {
    const timeoutId = window.setTimeout(() => {
      setDebouncedQuery(normalizedQuery);
    }, 300);
    return () => window.clearTimeout(timeoutId);
  }, [normalizedQuery]);

  const candidatesQuery = useQuery<GuestVisitCandidateResponse>({
    queryKey: [
      "schedule",
      scheduleId,
      "guest-visit-candidates",
      checkinDate,
      normalizedDebouncedQuery,
    ],
    queryFn: () =>
      apiClient
        .get(`/schedules/${scheduleId}/guest-visit-candidates/`, {
          params: { date: checkinDate, q: normalizedDebouncedQuery },
        })
        .then((r) => r.data),
    staleTime: 2 * 60_000,
    enabled: open && canSearch && !rosterLoading && !rosterError,
  });

  const data = candidatesQuery.data;
  const candidateUnavailable =
    candidatesQuery.isError || candidatesQuery.isRefetchError;

  const candidates = useMemo(
    () =>
      filterGuestVisitCandidates({
        candidates: candidateItems(data),
        rosterStudents,
      }),
    [data, rosterStudents],
  );

  const mutation = useMutation({
    mutationFn: (student: GuestVisitCandidate) => {
      if (rosterLoading || rosterError || candidateUnavailable) {
        throw new Error("Guest prerequisites are unavailable");
      }
      return apiClient
        .post<GuestVisitOut>(
          `/schedules/${scheduleId}/guest-visits/`,
          guestVisitPayload({ scheduleId, checkinDate, student }),
        )
        .then((r) => r.data);
    },
    onSuccess: (booking, student) => {
      const rosterStudent = rosterRowFromBooking(booking, student);
      queryClient.setQueryData<StudentWithAlerts[]>(
        ["schedule", scheduleId, "students", checkinDate],
        (current) => mergeRosterStudent(current, rosterStudent),
      );
      invalidateBatchCheckinQueries(queryClient, {
        scheduleId,
        checkinDate,
        studentIds: [booking.student_id],
        trainerId,
      });
      onBooked?.(rosterStudent);
      setQuery("");
      setDebouncedQuery("");
      setSelectedCandidate(null);
      setErrorMsg(null);
      onOpenChange(false);
    },
    onError: (error) => {
      setErrorMsg(getApiError(error, "Не удалось добавить гостя"));
    },
  });

  function handleQueryChange(value: string) {
    setQuery(value);
    setSelectedCandidate(null);
    setErrorMsg(null);
  }

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) {
      setQuery("");
      setDebouncedQuery("");
      setSelectedCandidate(null);
      setErrorMsg(null);
    }
    onOpenChange(nextOpen);
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent
        side="bottom"
        showCloseButton={false}
        className="max-h-[88vh] overflow-y-auto rounded-t-2xl"
      >
        <SheetHeader>
          <SheetTitle>Добавить гостя</SheetTitle>
          <SheetDescription>{checkinDate}</SheetDescription>
        </SheetHeader>

        <div className="flex flex-col gap-3 p-4 pt-0">
          <div className="relative">
            <Search
              size={18}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
            />
            <Input
              value={query}
              onChange={(event) => handleQueryChange(event.target.value)}
              placeholder="Имя или телефон"
              className="min-h-11 pl-10"
              disabled={rosterLoading || rosterError}
            />
          </div>

          {rosterLoading ? (
            <p role="status" className="rounded-xl bg-muted p-4 text-[14px] text-muted-foreground">
              Загружаем состав тренировки...
            </p>
          ) : rosterError ? (
            <QueryStateNotice
              title="Не удалось загрузить состав тренировки"
              message="Без актуального состава нельзя безопасно исключить уже записанного ученика."
              retryLabel="Повторить загрузку состава"
              onRetry={onRetryRoster}
            />
          ) : null}

          {selectedCandidate ? (
            <div className="rounded-xl bg-sky-50 p-3 ring-1 ring-sky-100">
              <p className="text-[15px] font-medium text-foreground">
                {studentName(selectedCandidate)}
              </p>
              <p className="mt-1 text-[13px] text-muted-foreground">
                Финансы будут рассчитаны при check-in
              </p>
              <div className="mt-3 flex gap-2">
                <Button
                  type="button"
                  className="min-h-11 flex-1"
                  onClick={() => mutation.mutate(selectedCandidate)}
                  disabled={mutation.isPending || rosterLoading || rosterError || candidateUnavailable}
                >
                  Добавить гостя
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11"
                  onClick={() => setSelectedCandidate(null)}
                  disabled={mutation.isPending}
                >
                  Отмена
                </Button>
              </div>
            </div>
          ) : null}

          {rosterLoading || rosterError ? null : !canRunGuestSearch(normalizedQuery) ? (
            <p className="rounded-xl bg-muted p-4 text-center text-[14px] text-muted-foreground">
              Введите минимум 2 буквы или 4 цифры
            </p>
          ) : isDebouncing || candidatesQuery.isLoading ? (
            <div className="ui-col-2">
              {[1, 2, 3].map((item) => (
                <Skeleton key={item} className="h-14 rounded-xl" />
              ))}
            </div>
          ) : candidateUnavailable ? (
            <QueryStateNotice
              title={data ? "Результаты поиска могли устареть" : "Не удалось выполнить поиск"}
              message="Повторите поиск перед выбором ученика или лида."
              retryLabel="Повторить поиск"
              retrying={candidatesQuery.isFetching}
              onRetry={() => void candidatesQuery.refetch()}
            />
          ) : candidates.length === 0 ? (
            <p className="rounded-xl bg-muted p-4 text-center text-[14px] text-muted-foreground">
              Подходящих учеников нет
            </p>
          ) : (
            <div className="ui-col-2">
              {candidates.map((student) => (
                <button
                  key={`${student.kind}-${student.id}`}
                  type="button"
                  onClick={() => {
                    setSelectedCandidate(student);
                    setErrorMsg(null);
                  }}
                  disabled={mutation.isPending || candidateUnavailable}
                  className="flex min-h-14 items-center gap-3 rounded-xl bg-white p-3 text-left ring-1 ring-foreground/5 transition active:bg-muted disabled:opacity-50"
                >
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[var(--branding-accent)]/10 text-[var(--branding-accent)]">
                    <UserPlus size={18} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[15px] font-medium text-foreground">
                      {studentName(student)}
                    </span>
                    <span className="block truncate text-[12px] text-muted-foreground">
                      {student.masked_phone || student.status}
                    </span>
                  </span>
                  <span className="shrink-0 text-[13px] font-medium text-[var(--branding-accent)]">
                    Выбрать
                  </span>
                </button>
              ))}
            </div>
          )}

          {errorMsg ? (
            <p className="rounded-lg bg-destructive/10 px-3 py-2 text-center text-[14px] text-destructive">
              {errorMsg}
            </p>
          ) : null}

          <Button
            type="button"
            variant="outline"
            className="w-full"
            onClick={() => handleOpenChange(false)}
          >
            Закрыть
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

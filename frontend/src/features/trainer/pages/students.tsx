import { useState, useEffect, useMemo } from "react";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { Users, Search, Plus } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import apiClient from "@/api/custom-fetch";
import { useUnifiedClientJourneyCapability } from "@/api/unified-client-journey";
import { StudentCard } from "../components/student-card";
import { ClientIntakeSheet } from "../components/client-intake-sheet";
import type { PersonSearchResult, StudentListItem } from "../types";

interface StudentFilter {
  readonly value: string;
  readonly label: string;
  readonly color: string;
  readonly parameter: "status" | "commercial_segment" | null;
}

const LEGACY_STATUS_FILTERS: readonly StudentFilter[] = [
  { value: "", label: "Все", color: "bg-foreground text-white", parameter: null },
  { value: "active", label: "Активные", color: "bg-emerald-100 text-emerald-700", parameter: "status" },
  { value: "trial", label: "Пробные", color: "bg-blue-100 text-blue-700", parameter: "status" },
  { value: "lead", label: "Заявки", color: "bg-amber-100 text-amber-700", parameter: "status" },
  { value: "at_risk", label: "В риске", color: "bg-orange-100 text-orange-700", parameter: "status" },
  { value: "churned", label: "Ушли", color: "bg-red-100 text-red-700", parameter: "status" },
];
const UNIFIED_STATUS_FILTERS: readonly StudentFilter[] = [
  { value: "", label: "Все", color: "bg-foreground text-white", parameter: null },
  { value: "active_entitlement", label: "Активные", color: "bg-emerald-100 text-emerald-700", parameter: "commercial_segment" },
  { value: "no_crm_entitlement", label: "Без абонемента", color: "bg-blue-100 text-blue-700", parameter: "commercial_segment" },
  { value: "at_risk", label: "В риске", color: "bg-orange-100 text-orange-700", parameter: "commercial_segment" },
  { value: "former", label: "Ушли", color: "bg-red-100 text-red-700", parameter: "commercial_segment" },
];
const STUDENTS_PAGE_LIMIT = 50;
const EMPTY_STUDENTS: StudentListItem[] = [];

type StudentListResponse = {
  readonly items: StudentListItem[];
  readonly count?: number;
};

function normalizeStudentResponse(data: StudentListItem[] | StudentListResponse): StudentListResponse {
  return Array.isArray(data)
    ? { items: data }
    : { items: data.items ?? [], count: data.count };
}

function nextStudentPageParam(
  lastPage: StudentListResponse,
  allPages: StudentListResponse[],
): number | undefined {
  const loaded = allPages.reduce((total, page) => total + page.items.length, 0);
  if (lastPage.count === undefined) return undefined;
  return loaded < lastPage.count ? loaded : undefined;
}

function useDebounce(value: string, delay: number): string {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const handler = setTimeout(() => setDebouncedValue(value), delay);
    return () => clearTimeout(handler);
  }, [value, delay]);

  return debouncedValue;
}

function StudentsSkeleton() {
  return (
    <div className="ui-col-2">
      {Array.from({ length: 3 }).map((_, i) => (
        <Skeleton key={i} className="h-[56px] rounded-xl" />
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="ui-empty-state">
      <Users size={48} className="text-[var(--branding-accent)]" />
      <p className="text-lg font-semibold text-foreground">Пока нет учеников</p>
      <p className="text-sm text-muted-foreground">
        Создайте первую заявку через +
      </p>
    </div>
  );
}

export default function Students() {
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [requestSheetOpen, setRequestSheetOpen] = useState(false);
  const debouncedSearch = useDebounce(search, 300);
  const unifiedIntakeEnabled = useUnifiedClientJourneyCapability();
  const filters = unifiedIntakeEnabled ? UNIFIED_STATUS_FILTERS : LEGACY_STATUS_FILTERS;
  const selectedFilter = filters.find((filter) => filter.value === statusFilter) ?? filters[0];
  const effectiveStatusFilter = selectedFilter.value;
  const personSearchActive = unifiedIntakeEnabled && debouncedSearch.trim().length >= 2;

  const {
    data: studentResponse,
    isLoading,
    isFetchingNextPage,
    hasNextPage,
    fetchNextPage,
  } = useInfiniteQuery<StudentListResponse>({
    queryKey: [
      "students",
      {
        unified: unifiedIntakeEnabled,
        filter: effectiveStatusFilter,
        q: personSearchActive ? "" : debouncedSearch,
        limit: STUDENTS_PAGE_LIMIT,
      },
    ],
    queryFn: ({ pageParam }) =>
      apiClient
        .get("/students/", {
          params: {
            limit: STUDENTS_PAGE_LIMIT,
            offset: pageParam,
            workspace: unifiedIntakeEnabled ? "students" : undefined,
            status:
              selectedFilter.parameter === "status"
                ? effectiveStatusFilter || undefined
                : undefined,
            commercial_segment:
              selectedFilter.parameter === "commercial_segment"
                ? effectiveStatusFilter || undefined
                : undefined,
            q: personSearchActive ? undefined : debouncedSearch || undefined,
          },
        })
        .then((r) => normalizeStudentResponse(r.data)),
    initialPageParam: 0,
    getNextPageParam: nextStudentPageParam,
    staleTime: 2 * 60_000,
    enabled: !personSearchActive,
  });
  const personSearch = useQuery<PersonSearchResult[]>({
    queryKey: ["students", "person-search", debouncedSearch],
    queryFn: () =>
      apiClient
        .get("/students/search/", { params: { q: debouncedSearch, limit: 20 } })
        .then((response) => response.data),
    enabled: personSearchActive,
    staleTime: 60_000,
  });
  const reopenAndClaim = useMutation<{ id: number }, unknown, number>({
    mutationFn: (studentId) =>
      apiClient
        .post(`/leads/${studentId}/reopen-and-claim`)
        .then((response) => response.data),
    onSuccess: (lead) => navigate(`/trainer/leads?lead=${lead.id}`),
  });
  const students = studentResponse?.pages.flatMap((page) => page.items) ?? EMPTY_STUDENTS;
  const totalCount = studentResponse?.pages[0]?.count ?? students.length;
  const filterCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const student of students) {
      const statusKey = `status:${student.status}`;
      counts[statusKey] = (counts[statusKey] ?? 0) + 1;
      if (student.commercial_segment) {
        const segmentKey = `commercial_segment:${student.commercial_segment}`;
        counts[segmentKey] = (counts[segmentKey] ?? 0) + 1;
      }
    }
    return counts;
  }, [students]);

  const loadedAllStudents = students.length >= totalCount;

  function countForFilter(value: string): number | undefined {
    if (value === effectiveStatusFilter) return totalCount;
    if (!effectiveStatusFilter && !debouncedSearch && loadedAllStudents && value) {
      const filter = filters.find((item) => item.value === value);
      return filter?.parameter ? filterCounts[`${filter.parameter}:${value}`] ?? 0 : undefined;
    }
    return undefined;
  }

  return (
    <div className="flex flex-col gap-4 px-4 pt-6">
      {/* Header */}
      <h1 className="text-lg font-semibold text-foreground">Ученики</h1>

      {/* Search */}
      <div className="relative">
        <Search
          size={18}
          className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
        />
        <input
          type="text"
          placeholder="Поиск по имени или телефону"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full rounded-xl border border-input bg-white py-2.5 pl-10 pr-4 text-[14px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-[var(--branding-accent)]"
        />
      </div>

      {/* Status filter chips */}
      <div className="flex gap-2 overflow-x-auto pb-1 -mx-4 px-4 scrollbar-none">
        {filters.map((f) => {
          const isActive = effectiveStatusFilter === f.value;
          const count = countForFilter(f.value);
          return (
            <button
              key={f.value}
              type="button"
              onClick={() => setStatusFilter(f.value)}
              className={cn(
                "flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-[13px] font-medium transition-colors",
                isActive ? f.color : "bg-muted text-muted-foreground",
              )}
            >
              {f.label}
              {count !== undefined ? (
                <span className={cn(
                  "text-[11px] font-semibold",
                  isActive ? "opacity-80" : "opacity-50",
                )}>
                  {count}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>

      {/* List */}
      {personSearchActive && personSearch.isLoading ? (
        <StudentsSkeleton />
      ) : personSearchActive ? (
        personSearch.data?.length ? (
          <div className="flex flex-col gap-2 pb-4">
            {personSearch.data.map((person, index) => (
              <div
                key={person.id ?? `hidden-${index}`}
                className="min-h-12 rounded-xl bg-white p-3 text-left shadow-sm"
              >
                <span className="block text-[14px] font-medium text-foreground">
                  {person.display_name ?? "Совпадение уже есть в CRM"}
                </span>
                <span className="ui-muted-12">
                  {person.target_workspace === "students"
                    ? "Ученик"
                    : person.target_workspace === "leads_archived"
                      ? "Завершённая заявка"
                      : person.target_workspace === "leads_active"
                        ? "Заявка"
                        : "Карточка недоступна"}
                  {person.masked_phone ? ` · ${person.masked_phone}` : ""}
                </span>
                {person.allowed_action === "can_reopen_and_claim" && person.id ? (
                  <button
                    type="button"
                    disabled={reopenAndClaim.isPending}
                    onClick={() => reopenAndClaim.mutate(person.id!)}
                    className="mt-2 min-h-11 rounded-lg bg-[var(--branding-accent)] px-3 text-[13px] font-semibold text-white disabled:opacity-60"
                  >
                    {reopenAndClaim.isPending ? "Возвращаю..." : "Вернуть и забрать"}
                  </button>
                ) : person.route ? (
                  <button
                    type="button"
                    onClick={() => navigate(person.route!)}
                    className="mt-2 min-h-11 rounded-lg border border-input px-3 text-[13px] font-medium text-foreground"
                  >
                    Открыть
                  </button>
                ) : null}
              </div>
            ))}
          </div>
        ) : (
          <EmptyState />
        )
      ) : isLoading ? (
        <StudentsSkeleton />
      ) : !students.length ? (
        <EmptyState />
      ) : (
        <div className="flex flex-col gap-2 pb-4">
          {students.map((s) => (
            <StudentCard key={s.id} student={s} />
          ))}
          {hasNextPage ? (
            <button
              type="button"
              onClick={() => void fetchNextPage()}
              disabled={isFetchingNextPage}
              className="mt-2 min-h-11 rounded-xl border border-input bg-white px-4 text-[14px] font-medium text-foreground disabled:opacity-60"
            >
              {isFetchingNextPage ? "Загружаю..." : `Загрузить ещё (${students.length} из ${totalCount})`}
            </button>
          ) : null}
        </div>
      )}

      {/* FAB: unified client intake */}
      <button
        type="button"
        aria-label={unifiedIntakeEnabled ? "Добавить ученика" : "Новая заявка"}
        onClick={() => setRequestSheetOpen(true)}
        className="fixed bottom-20 right-4 z-40 flex h-14 w-14 items-center justify-center rounded-full bg-[var(--branding-accent)] text-white shadow-lg active:scale-95 transition-transform"
      >
        <Plus size={24} />
      </button>

      <ClientIntakeSheet
        open={requestSheetOpen}
        onOpenChange={setRequestSheetOpen}
        onNavigate={navigate}
        unifiedEnabled={unifiedIntakeEnabled}
      />
    </div>
  );
}

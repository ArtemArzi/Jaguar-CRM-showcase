import { useState } from "react";
import { useNavigate } from "react-router";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Banknote,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import apiClient from "@/api/custom-fetch";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import { SalaryCard, type EarningData } from "../components/salary-card";
import { QueryStateNotice } from "../components/query-state-notice";
import { formatRub } from "@/lib/utils";
import { MONTH_NAMES } from "@/lib/locale";
import { getMonthRange } from "@/lib/format";
import { todayInTimeZone } from "@/lib/club-date";
import type { EarningSummary } from "../types";

function SalarySkeleton() {
  return (
    <div className="ui-col-3">
      <Skeleton className="h-[100px] rounded-xl" />
      {[1, 2, 3].map((i) => (
        <Skeleton key={i} className="h-[64px] rounded-xl" />
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="ui-empty-state">
      <Banknote size={48} className="ui-muted" />
      <p className="ui-title-20">
        Нет начислений
      </p>
      <p className="text-[16px] text-muted-foreground">
        В этом месяце начислений пока нет
      </p>
    </div>
  );
}

export default function SalaryDetail() {
  const navigate = useNavigate();
  const timeZone = useBrandingStore((s) => s.timeZone);
  const now = todayInTimeZone(timeZone);
  const [year, setYear] = useState(now.getFullYear());
  const [month, setMonth] = useState(now.getMonth());
  const trainerId = useAuthStore((s) => s.trainerId);

  const { dateFrom, dateTo } = getMonthRange(year, month);

  const earningsQuery = useQuery<EarningData[]>({
    queryKey: ["trainer-earnings", trainerId, dateFrom, dateTo],
    queryFn: () =>
      apiClient
        .get(`/trainers/${trainerId}/earnings/`, {
          params: { date_from: dateFrom, date_to: dateTo },
        })
        .then((r) => r.data.items ?? r.data),
    enabled: trainerId !== null,
    staleTime: 2 * 60_000,
  });

  const summaryQuery = useQuery<EarningSummary>({
      queryKey: ["trainer-earnings-summary", trainerId, dateFrom, dateTo],
      queryFn: () =>
        apiClient
          .get(`/trainers/${trainerId}/earnings/summary/`, {
            params: { date_from: dateFrom, date_to: dateTo },
          })
          .then((r) => r.data),
      enabled: trainerId !== null,
      staleTime: 2 * 60_000,
  });

  const earnings = earningsQuery.data;
  const summary = summaryQuery.data;
  const salaryRows = earnings ?? [];
  const hasOnlyAdjustments =
    salaryRows.length > 0 &&
    salaryRows.every((earning) => earning.row_type === "adjustment");

  function prevMonth() {
    if (month === 0) {
      setMonth(11);
      setYear((y) => y - 1);
    } else {
      setMonth((m) => m - 1);
    }
  }

  function nextMonth() {
    if (month === 11) {
      setMonth(0);
      setYear((y) => y + 1);
    } else {
      setMonth((m) => m + 1);
    }
  }

  const sessionsLabel = (() => {
    const count = summary?.total_sessions;
    if (count === undefined) return null;
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod100 >= 11 && mod100 <= 19) return `${count} тренировок`;
    if (mod10 === 1) return `${count} тренировка`;
    if (mod10 >= 2 && mod10 <= 4) return `${count} тренировки`;
    return `${count} тренировок`;
  })();

  return (
    <div className="flex flex-col gap-4 px-4 pt-4 pb-24">
      {/* Back button + title */}
      <button
        type="button"
        onClick={() => navigate("/trainer/profile")}
        className="flex items-center gap-2 text-muted-foreground active:opacity-70 self-start -ml-1"
      >
        <ArrowLeft size={20} />
        <span className="text-[14px]">Профиль</span>
      </button>

      <h1 className="text-[22px] font-semibold text-foreground">Заработок</h1>

      {/* Month picker */}
      <div className="flex items-center justify-between rounded-xl bg-white p-3 ring-1 ring-foreground/5">
        <Button variant="ghost" size="icon" onClick={prevMonth}>
          <ChevronLeft size={20} />
        </Button>
        <span className="ui-title-16">
          {MONTH_NAMES[month]} {year}
        </span>
        <Button variant="ghost" size="icon" onClick={nextMonth}>
          <ChevronRight size={20} />
        </Button>
      </div>

      {summaryQuery.isLoading && !summary ? (
        <Skeleton className="h-[148px] rounded-xl" />
      ) : summaryQuery.isError || summaryQuery.isRefetchError ? (
        <>
          <QueryStateNotice
            title={summary ? "Итог зарплаты мог устареть" : "Не удалось загрузить итог зарплаты"}
            message="Сумму и число тренировок нельзя считать нулевыми, пока сервер не ответил."
            retryLabel="Повторить загрузку итога"
            retrying={summaryQuery.isFetching}
            onRetry={() => void summaryQuery.refetch()}
          />
          {summary ? (
            <div className="ui-card-center">
              <p className="text-[48px] font-semibold text-foreground leading-tight">
                {formatRub(summary.total_amount)}
              </p>
              <p className="text-[14px] text-muted-foreground mt-1">{sessionsLabel}</p>
            </div>
          ) : null}
        </>
      ) : summary ? (
        <div className="ui-card-center">
          <p className="text-[48px] font-semibold text-foreground leading-tight">
            {formatRub(summary.total_amount)}
          </p>
          <p className="text-[14px] text-muted-foreground mt-1">{sessionsLabel}</p>
        </div>
      ) : null}

      {earningsQuery.isLoading && !earnings ? (
        <SalarySkeleton />
      ) : earningsQuery.isError || earningsQuery.isRefetchError ? (
        <>
          <QueryStateNotice
            title={earnings ? "Начисления могли устареть" : "Не удалось загрузить начисления"}
            message="Список начислений временно недоступен. Повторите загрузку перед сверкой зарплаты."
            retryLabel="Повторить загрузку начислений"
            retrying={earningsQuery.isFetching}
            onRetry={() => void earningsQuery.refetch()}
          />
          {salaryRows.length > 0 ? (
            <div className="ui-col-3">
              {hasOnlyAdjustments && (
                <p className="text-[14px] font-medium text-muted-foreground">
                  Корректировки за период
                </p>
              )}
              {salaryRows.map((earning) => (
                <SalaryCard
                  key={`${earning.row_type ?? "earning"}-${earning.id}`}
                  earning={earning}
                />
              ))}
            </div>
          ) : null}
        </>
      ) : !salaryRows.length ? (
        <EmptyState />
      ) : (
        <div className="ui-col-3">
          {hasOnlyAdjustments && (
            <p className="text-[14px] font-medium text-muted-foreground">
              Корректировки за период
            </p>
          )}
          {salaryRows.map((earning) => (
            <SalaryCard
              key={`${earning.row_type ?? "earning"}-${earning.id}`}
              earning={earning}
            />
          ))}
        </div>
      )}
    </div>
  );
}

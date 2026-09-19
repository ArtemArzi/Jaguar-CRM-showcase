import type { ReactNode } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { PortalSectionTitle } from "@/components/portal/portal-section-title";
import {
  OnlinePaymentLinkPanel,
  type BankPaymentOrderLink,
} from "@/components/portal/online-payment-link-panel";
import { isSubscriptionBankPaymentOrder } from "@/components/portal/payment-link-state";
import {
  formatRenewalOfferLabel,
  findRenewalOrderForSubscription,
  hasUnresolvedRenewalOrderForSource,
  hasUnresolvedRenewalOrderForTariff,
  isLiveRenewalOrder,
  selectEntitlementSubscriptions,
  selectPendingRenewalOrders,
  type RenewalTargetOffer,
} from "@/components/portal/subscription-renewal-state";
import { SubscriptionCard } from "@/features/student/components/subscription-card";
import { GradeCard } from "@/features/student/components/grade-card";
import { formatDateRu } from "@/lib/locale";
import { cn } from "@/lib/utils";
import {
  AlertTriangle,
  CalendarDays,
  CheckCircle2,
  ClipboardList,
  Dumbbell,
  MapPin,
  XCircle,
} from "lucide-react";
import type { StudentChecklistItem } from "@/features/student/lib/student-normalizers";
import { ParentSurfaceCard } from "./parent-surface-card";
import {
  getParentSubscriptionAlert,
  rankSubscriptionAlert,
} from "../lib/subscription-alerts";

interface GradeInfo {
  grade_system_name: string | null;
  current_grade: {
    id: number;
    name: string;
    order: number;
    min_trainings: number;
  } | null;
  trainings_since_last_grade: number;
  next_grade: {
    id: number;
    name: string;
    order: number;
    min_trainings: number;
  } | null;
  trainings_to_next: number | null;
}

interface AttendanceItem {
  date: string;
  group_name: string;
  trainer_name: string;
  training_type_name: string;
  start_time: string;
}

interface ScheduleExceptionItem {
  exception_type: string;
  date?: string | null;
  reason?: string | null;
  new_date?: string | null;
  new_start_time?: string | null;
  new_end_time?: string | null;
  substitute_trainer_name?: string | null;
}

interface ScheduleOccurrencePreview {
  schedule_id?: number;
  enrollment_id?: number | null;
  created_from?: string;
  can_cancel?: boolean;
  one_time_date?: string | null;
  effective_date: string;
  effective_start_time: string;
  effective_end_time: string;
  trainer_name?: string | null;
  training_type_kind?: string | null;
  is_rescheduled?: boolean;
  is_substitute?: boolean;
}

interface ScheduleItem {
  id: number;
  day_of_week: number;
  start_time: string;
  end_time: string;
  group_name: string;
  training_type_id?: number | null;
  training_type_name?: string;
  training_type_kind?: string;
  one_time_date?: string | null;
  trainer_name: string;
  location_name: string;
  upcoming_exceptions?: ScheduleExceptionItem[];
  upcoming_occurrences?: ScheduleOccurrencePreview[];
}

interface SubscriptionSummary {
  id: number;
  tariff_id?: number | null;
  tariff_name: string;
  trainings_used: number;
  trainings_total: number | null;
  trainings_left: number | null;
  expires_at: string | null;
  status: string;
  freeze_status?: string | null;
  renewal_target_tariff_id?: number | null;
  renewal_target_tariff_name?: string | null;
  renewal_target_price?: string | number | null;
}

interface DebtSummary {
  id: number;
  checkin_id: number;
  tariff_price: string | null;
  reason: string;
  training_type_name: string;
  checkin_date: string;
  created_at: string;
}

interface OperationalAdmissionState {
  payment_id: number;
  payment_status: string;
  payment_method: string;
  subscription_status: string | null;
  enrollment_status: string;
  group_label: string;
  training_group_id?: number | null;
  group_membership_id?: number | null;
  start_date: string;
  checkin_ready: boolean;
  account_access_eligible: boolean;
  covered_visit_count: number;
}

interface OperationalAdmissionV2State {
  kind: "group" | "personal";
  payment_id: number;
  payment_status: string;
  payment_method: string;
  subscription_status: string | null;
  start_date: string;
  checkin_ready: boolean;
  account_access_eligible: boolean;
  is_qualifying: boolean;
  group_label: string | null;
  training_group_id: number | null;
  group_membership_id: number | null;
  enrollment_status: string | null;
  booking_id: number | null;
  session_id: number | null;
  booking_state: string | null;
}

interface CoveredVisitState {
  payment_id?: number;
  debt_id: number;
  checkin_id: number;
  training_type_name: string;
  checkin_date: string;
  coverage_state: string;
  is_payable: boolean;
}

interface CabinetFinancialState {
  operational_admission: OperationalAdmissionState | null;
  operational_admissions?: OperationalAdmissionState[];
  operational_admission_v2?: OperationalAdmissionV2State | null;
  operational_admissions_v2?: OperationalAdmissionV2State[];
  covered_visits: CoveredVisitState[];
}

interface ChildDetailProps {
  activeSubscription: SubscriptionSummary | null;
  activeSubscriptions?: SubscriptionSummary[];
  openDebts?: DebtSummary[];
  financialState?: CabinetFinancialState;
  onlinePaymentOrders?: BankPaymentOrderLink[];
  onlinePaymentErrorMessage?: string | null;
  onCreateBankPaymentOrder?: (input: {
    tariffId: number;
    debtIds: number[];
    renewedFromSubscriptionId?: number;
    renewalOffer?: RenewalTargetOffer;
  }) => void;
  onCancelBankPaymentOrder?: (order: BankPaymentOrderLink) => void;
  onRefreshBankPaymentOrders?: (order: BankPaymentOrderLink) => void;
  onRefetchBankPaymentOrders?: () => void;
  isCreatingBankPaymentOrder?: boolean;
  isCancelingBankPaymentOrder?: boolean;
  isRefreshingBankPaymentOrder?: boolean;
  onlinePaymentsEnabled?: boolean;
  renewalMode?: "unified" | "legacy" | "unavailable";
  onlinePaymentUnavailableMessage?: string;
  documentChecklist?: StudentChecklistItem[];
  gradeProgress: GradeInfo[];
  schedule: ScheduleItem[];
  attendance: AttendanceItem[];
  attendanceCount: number;
  attendanceError?: boolean;
  onAttendanceRetry?: () => void;
  isAttendanceRetrying?: boolean;
  canLoadMoreAttendance?: boolean;
  onAttendanceLoadMore?: () => void;
  isAttendanceLoadingMore?: boolean;
  attendanceLoadMoreError?: boolean;
  onCancelBooking?: (occurrence: ScheduleOccurrencePreview) => void;
  cancelingEnrollmentId?: number | null;
  cancelBookingErrorMessage?: string | null;
  bookingSection?: ReactNode;
}

const DAY_NAMES = [
  "Понедельник",
  "Вторник",
  "Среда",
  "Четверг",
  "Пятница",
  "Суббота",
  "Воскресенье",
];

function SubscriptionAttentionCard({
  title,
  description,
  tone,
}: {
  title: string;
  description: string;
  tone: "danger" | "warning";
}) {
  return (
    <Card
      className={cn(
        "overflow-hidden border-0 shadow-[0_14px_34px_rgba(17,24,39,0.08)] ring-1",
        tone === "danger"
          ? "bg-red-50/90 ring-red-500/18"
          : "bg-amber-50/90 ring-amber-500/18",
      )}
    >
      <CardContent className="flex items-start gap-3 py-4">
        <span
          className={cn(
            "flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl",
            tone === "danger"
              ? "bg-red-600 text-white"
              : "bg-amber-300 text-amber-950",
          )}
        >
          <AlertTriangle size={18} />
        </span>
        <div className="space-y-1">
          <p
            className={cn(
              "parent-balanced-title text-[15px] font-semibold",
              tone === "danger" ? "text-red-950" : "text-amber-950",
            )}
          >
            {title}
          </p>
          <p
            className={cn(
              "parent-pretty-text text-[13px] leading-5",
              tone === "danger" ? "text-red-900/75" : "text-amber-900/75",
            )}
          >
            {description}
          </p>
        </div>
      </CardContent>
    </Card>
  );
}

function formatScheduleTime(value: string) {
  return value.slice(0, 5);
}

function formatScheduleDateLabel(value?: string | null) {
  if (!value) return "";

  return new Date(`${value}T00:00:00`).toLocaleDateString("ru-RU", {
    day: "numeric",
    month: "long",
  });
}

function formatAttendanceCount(count: number) {
  const mod10 = Math.abs(count) % 10;
  const mod100 = Math.abs(count) % 100;

  if (mod10 === 1 && mod100 !== 11) {
    return `${count} посещение`;
  }

  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) {
    return `${count} посещения`;
  }

  return `${count} посещений`;
}

function formatDebtAmount(value: string | null) {
  return value ? `${value} ₽` : "Сумма уточняется";
}

const DEBT_REASON_LABELS: Record<string, string> = {
  no_subscription: "нет подходящего абонемента",
  subscription_exhausted: "закончились тренировки по абонементу",
  subscription_expired: "абонемент истёк",
  tariff_mismatch: "тип тренировки не входит в абонемент",
};

function formatDebtReason(reason: string) {
  return DEBT_REASON_LABELS[reason] ?? reason.replaceAll("_", " ");
}

function formatDebtDate(value: string) {
  return formatDateRu(value.slice(0, 10));
}

function isPersonalScheduleItem(item: ScheduleItem) {
  return item.training_type_kind === "personal" || item.training_type_kind === "mini_group";
}

function scheduleKindLabel(item: ScheduleItem) {
  return isPersonalScheduleItem(item) ? "Персоналка" : "Группа";
}

function AttendanceSection({
  attendance,
  attendanceCount,
  attendanceError,
  onAttendanceRetry,
  isAttendanceRetrying,
  canLoadMore,
  onLoadMore,
  isLoadingMore,
  loadMoreError,
}: {
  attendance: AttendanceItem[];
  attendanceCount: number;
  attendanceError?: boolean;
  onAttendanceRetry?: () => void;
  isAttendanceRetrying?: boolean;
  canLoadMore?: boolean;
  onLoadMore?: () => void;
  isLoadingMore?: boolean;
  loadMoreError?: boolean;
}) {
  return (
    <section
      id="activity"
      aria-label="Активность"
      className="parent-section-enter parent-delay-3 space-y-3 scroll-mt-24"
    >
      <div className="ui-row-between-center">
        <PortalSectionTitle eyebrow="Активность" title="Посещения" />
        <span className="parent-number rounded-full bg-black/[0.04] px-3 py-1 text-[13px] font-semibold text-neutral-700">
          {formatAttendanceCount(attendanceCount)}
        </span>
      </div>
      {attendanceError ? (
        <ParentSurfaceCard className="ring-destructive/15">
          <CardContent className="space-y-3 py-4">
            <p className="ui-error-14">
              Не удалось загрузить активность.
            </p>
            {onAttendanceRetry ? (
              <Button
                type="button"
                variant="outline"
                className="min-h-[44px] w-full"
                onClick={onAttendanceRetry}
                disabled={isAttendanceRetrying}
              >
                Повторить
              </Button>
            ) : null}
          </CardContent>
        </ParentSurfaceCard>
      ) : attendance.length === 0 ? (
        <ParentSurfaceCard>
          <CardContent className="py-4">
            <p className="text-[14px] text-neutral-500">
              Нет записей о посещениях
            </p>
          </CardContent>
        </ParentSurfaceCard>
      ) : (
        <div className="space-y-2">
          {attendance.map((a) => (
            <Card
              key={`${a.date}-${a.start_time}-${a.training_type_name}`}
              className="parent-card-lift overflow-hidden border-0 bg-white/94 shadow-sm ring-1 ring-black/6"
            >
              <CardContent className="flex items-center justify-between gap-3 py-3">
                <div className="flex min-w-0 items-start gap-3">
                  <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-emerald-50 text-emerald-600 ring-1 ring-emerald-100">
                    <CheckCircle2 size={16} />
                  </span>
                  <div className="min-w-0">
                    <p className="text-[14px] font-semibold">
                      {a.training_type_name}
                    </p>
                    <p className="parent-pretty-text text-[12px] leading-4 text-neutral-500">
                      {a.group_name} &middot; {a.trainer_name}
                    </p>
                  </div>
                </div>
                <div className="parent-number shrink-0 text-right">
                  <p className="text-[14px] font-medium">{formatDateRu(a.date)}</p>
                  <p className="text-[12px] text-neutral-500">{a.start_time}</p>
                </div>
              </CardContent>
            </Card>
          ))}
          {canLoadMore && onLoadMore ? (
            <div className="space-y-2">
              {loadMoreError ? (
                <p className="rounded-2xl bg-red-50 px-3 py-2 text-[13px] text-red-700 ring-1 ring-red-100">
                  Не удалось загрузить ещё посещения.
                </p>
              ) : null}
              <Button
                type="button"
                variant="outline"
                className="min-h-[44px] w-full"
                onClick={onLoadMore}
                disabled={isLoadingMore}
              >
                {isLoadingMore ? "Загружаем..." : "Показать ещё"}
              </Button>
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}

function TrainingGroupsSection({
  schedule,
  onCancelBooking,
  cancelingEnrollmentId,
  cancelBookingErrorMessage,
}: {
  schedule: ScheduleItem[];
  onCancelBooking?: (occurrence: ScheduleOccurrencePreview) => void;
  cancelingEnrollmentId?: number | null;
  cancelBookingErrorMessage?: string | null;
}) {
  return (
    <section
      id="groups"
      aria-label="Занятия ребёнка"
      className="parent-section-enter parent-delay-2 space-y-3 scroll-mt-24"
    >
      <PortalSectionTitle eyebrow="Занятия" title="Занятия и тренеры" />
      {schedule.length === 0 ? (
        <ParentSurfaceCard>
          <CardContent className="space-y-1 py-4">
            <p className="text-[14px] font-semibold">Группа ещё не определена</p>
            <p className="ui-caption-muted">
              Когда тренер отметит посещения или закрепит группу, здесь появятся
              группа, время и зал.
            </p>
          </CardContent>
        </ParentSurfaceCard>
      ) : (
        <div className="space-y-2">
          {schedule.map((item) => {
            const exceptions = item.upcoming_exceptions ?? [];
            const occurrences = item.upcoming_occurrences ?? [];
            const cancellableOccurrences = occurrences.filter(
              (occurrence) => occurrence.can_cancel && occurrence.enrollment_id,
            );
            const substituteOccurrence =
              occurrences.find((occurrence) => occurrence.is_substitute) ?? null;

            return (
              <Card
                key={item.id}
                className="parent-card-lift overflow-hidden border-0 bg-white/94 shadow-sm ring-1 ring-black/6"
              >
                <div
                  className="h-1 w-full"
                  style={{
                    background:
                      "linear-gradient(90deg, #0ea5e9, var(--branding-accent), #10b981)",
                  }}
                />
                <CardContent className="space-y-3 py-4">
                  <div className="ui-row-between">
                    <div className="flex min-w-0 items-start gap-3">
                      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-black/[0.04] text-neutral-800">
                        <Dumbbell size={17} />
                      </span>
                      <div className="min-w-0">
                        <p className="text-[15px] font-semibold">{item.group_name}</p>
                        {item.training_type_name ? (
                          <p className="text-[12px] font-medium text-neutral-600">
                            {item.training_type_name}
                          </p>
                        ) : null}
                        <p className="mt-1 inline-flex rounded-full bg-black/5 px-2.5 py-1 text-[12px] font-medium text-neutral-700">
                          {scheduleKindLabel(item)}
                        </p>
                        <p className="ui-muted-13">
                          {item.trainer_name}
                        </p>
                      </div>
                    </div>
                    <div className="parent-number rounded-xl bg-[var(--branding-accent)]/12 px-3 py-1.5 text-right">
                      <p className="text-[12px] font-semibold">
                        {item.one_time_date
                          ? formatScheduleDateLabel(item.one_time_date)
                          : DAY_NAMES[item.day_of_week] ?? "День не задан"}
                      </p>
                      <p className="ui-muted-12">
                        {formatScheduleTime(item.start_time)}-{formatScheduleTime(item.end_time)}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 text-[13px] text-muted-foreground">
                    <MapPin size={14} />
                    <span>{item.location_name}</span>
                  </div>
                  {cancellableOccurrences.length > 0 && onCancelBooking ? (
                    <div className="space-y-2 rounded-2xl bg-red-50/75 px-3 py-2.5 ring-1 ring-red-500/10">
                      {cancellableOccurrences.map((occurrence) => {
                        const isPending =
                          cancelingEnrollmentId === occurrence.enrollment_id;
                        return (
                          <div
                            key={`${occurrence.enrollment_id}-${occurrence.effective_date}`}
                            className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between"
                          >
                            <p className="text-[12px] leading-5 text-red-900/75">
                              {formatScheduleDateLabel(occurrence.effective_date)},{" "}
                              {formatScheduleTime(occurrence.effective_start_time)}-
                              {formatScheduleTime(occurrence.effective_end_time)}
                            </p>
                            <Button
                              type="button"
                              variant="destructive"
                              aria-label={`Отменить запись ${item.group_name}`}
                              className="min-h-[40px] justify-center rounded-2xl"
                              onClick={() => onCancelBooking(occurrence)}
                              disabled={isPending}
                            >
                              <XCircle className="size-4" />
                              {isPending ? "Отменяем..." : "Отменить запись"}
                            </Button>
                          </div>
                        );
                      })}
                      {cancelBookingErrorMessage ? (
                        <p className="text-[12px] leading-5 text-destructive">
                          {cancelBookingErrorMessage}
                        </p>
                      ) : null}
                    </div>
                  ) : null}
                  {exceptions.length > 0 || occurrences.some((occurrence) => occurrence.is_rescheduled || occurrence.is_substitute) ? (
                    <div className="space-y-2 rounded-2xl bg-black/[0.025] px-3 py-2.5">
                      {exceptions.map((exception, index) => {
                        const key = `${exception.exception_type}-${exception.date ?? index}`;
                        if (exception.exception_type === "cancelled") {
                          return (
                            <div key={key} className="space-y-1">
                              <span className="inline-flex rounded-full bg-red-50 px-2.5 py-1 text-[12px] font-medium text-red-700 ring-1 ring-red-200">
                                Отменено
                              </span>
                              <p className="ui-caption-muted">
                                {formatScheduleDateLabel(exception.date)}
                                {exception.reason ? ` · ${exception.reason}` : ""}
                              </p>
                            </div>
                          );
                        }

                        if (exception.exception_type === "rescheduled") {
                          const label = `${formatScheduleDateLabel(exception.new_date)}, ${formatScheduleTime(
                            exception.new_start_time ?? "",
                          )}-${formatScheduleTime(exception.new_end_time ?? "")}`;
                          const originalDate = formatScheduleDateLabel(exception.date);
                          return (
                            <div key={key} className="space-y-1">
                              <span className="inline-flex rounded-full bg-amber-50 px-2.5 py-1 text-[12px] font-medium text-amber-800 ring-1 ring-amber-200">
                                Перенос
                              </span>
                              <p className="ui-caption-muted">
                                {label}
                              </p>
                              {originalDate || exception.reason ? (
                                <p className="text-[12px] leading-4 text-muted-foreground">
                                  {originalDate ? `Изначально: ${originalDate}` : ""}
                                  {originalDate && exception.reason ? " · " : ""}
                                  {exception.reason ?? ""}
                                </p>
                              ) : null}
                            </div>
                          );
                        }

                        if (exception.exception_type === "substitute") {
                          const trainerName =
                            exception.substitute_trainer_name ||
                            substituteOccurrence?.trainer_name ||
                            "";
                          return (
                            <div key={key} className="space-y-1">
                              <span className="inline-flex rounded-full bg-sky-50 px-2.5 py-1 text-[12px] font-medium text-sky-800 ring-1 ring-sky-200">
                                Замена тренера
                              </span>
                              {trainerName ? (
                                <p className="ui-caption-muted">
                                  {trainerName}
                                </p>
                              ) : null}
                              {exception.date || exception.reason ? (
                                <p className="text-[12px] leading-4 text-muted-foreground">
                                  {formatScheduleDateLabel(exception.date)}
                                  {exception.date && exception.reason ? " · " : ""}
                                  {exception.reason ?? ""}
                                </p>
                              ) : null}
                            </div>
                          );
                        }

                        return null;
                      })}
                    </div>
                  ) : null}
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </section>
  );
}

function DocumentChecklistSection({
  items,
}: {
  items: StudentChecklistItem[];
}) {
  return (
    <section
      id="documents"
      aria-label="Документы ребёнка"
      className="parent-section-enter parent-delay-1 space-y-3 scroll-mt-24"
    >
      <PortalSectionTitle eyebrow="Профиль" title="Документы" />
      {items.length === 0 ? (
        <ParentSurfaceCard>
          <CardContent className="flex gap-3 py-4">
            <span
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl text-white"
              style={{ backgroundColor: "var(--branding-primary)" }}
            >
              <ClipboardList size={18} />
            </span>
            <div className="space-y-1">
              <p className="text-[14px] font-semibold">Документы не требуются</p>
              <p className="ui-caption-muted">
                Для этого ученика сейчас нет обязательных документов.
              </p>
            </div>
          </CardContent>
        </ParentSurfaceCard>
      ) : (
        <div className="space-y-2">
          {items.map((item) => {
            const statusLabel = item.hasFile
              ? "Файл загружен"
              : item.isProvided
                ? "Отмечен"
                : "Не предоставлен";
            const statusClass = item.hasFile || item.isProvided
              ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
              : item.isRequired
                ? "bg-red-50 text-red-700 ring-red-200"
                : "bg-neutral-50 text-neutral-700 ring-neutral-200";

            return (
              <Card
                key={item.documentTypeId}
                className="parent-card-lift overflow-hidden border-0 bg-white/94 shadow-sm ring-1 ring-black/6"
              >
                <CardContent className="space-y-3 py-4">
                  <div className="ui-row-between">
                    <div className="min-w-0">
                      <p className="text-[15px] font-semibold">
                        {item.documentTypeName}
                      </p>
                      {item.description ? (
                        <p className="parent-pretty-text mt-1 text-[13px] leading-5 text-muted-foreground">
                          {item.description}
                        </p>
                      ) : null}
                    </div>
                    <span
                      className={cn(
                        "shrink-0 rounded-full px-3 py-1 text-[12px] font-medium ring-1",
                        statusClass,
                      )}
                    >
                      {statusLabel}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <span className="rounded-full bg-black/5 px-3 py-1 text-[12px] text-neutral-700">
                      {item.isRequired ? "Обязательный" : "Необязательный"}
                    </span>
                    {!item.isActive ? (
                      <span className="rounded-full bg-black/5 px-3 py-1 text-[12px] text-neutral-700">
                        Архивный тип
                      </span>
                    ) : null}
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </section>
  );
}

function GradeSection({ gradeProgress }: { gradeProgress: GradeInfo[] }) {
  return (
    <section
      id="progress"
      aria-label="Прогресс по грейду"
      className="parent-section-enter parent-delay-1 space-y-3 scroll-mt-24"
    >
      <PortalSectionTitle eyebrow="Прогресс" title="Грейды" />
      {gradeProgress.length === 0 ? (
        <ParentSurfaceCard>
          <CardContent className="flex gap-3 py-4">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl text-white"
              style={{ backgroundColor: "var(--branding-primary)" }}
            >
              <CalendarDays size={18} />
            </div>
            <div className="space-y-1">
              <p className="text-[14px] font-semibold">Прогресс пока не назначен</p>
              <p className="ui-caption-muted">
                После первых аттестаций здесь появится текущий уровень и сколько
                тренировок осталось до следующего грейда.
              </p>
            </div>
          </CardContent>
        </ParentSurfaceCard>
      ) : (
        <div className="space-y-3">
          {gradeProgress.map((g, idx) => {
            const progressPercent =
              g.next_grade && g.trainings_to_next !== null
                ? Math.min(
                    (g.trainings_since_last_grade / g.next_grade.min_trainings) *
                      100,
                    100,
                  )
                : 0;
            return (
              <GradeCard
                key={g.current_grade?.id ?? g.next_grade?.id ?? `no-grade-${idx}`}
                variant={idx === 0 ? "hero" : "compact"}
                systemName={g.grade_system_name ?? ""}
                currentGradeName={g.current_grade?.name ?? "Без грейда"}
                nextGradeName={g.next_grade?.name ?? null}
                currentCheckins={g.trainings_since_last_grade}
                requiredCheckins={g.next_grade?.min_trainings ?? null}
                progressPercent={Math.round(progressPercent)}
              />
            );
          })}
        </div>
      )}
    </section>
  );
}

export function ChildDetail({
  activeSubscription,
  activeSubscriptions,
  openDebts = [],
  financialState,
  onlinePaymentOrders = [],
  onlinePaymentErrorMessage = null,
  onCreateBankPaymentOrder,
  onCancelBankPaymentOrder,
  onRefreshBankPaymentOrders,
  onRefetchBankPaymentOrders,
  isCreatingBankPaymentOrder = false,
  isCancelingBankPaymentOrder = false,
  isRefreshingBankPaymentOrder = false,
  onlinePaymentsEnabled = false,
  renewalMode = "unavailable",
  onlinePaymentUnavailableMessage = "",
  documentChecklist = [],
  gradeProgress,
  schedule,
  attendance,
  attendanceCount,
  attendanceError,
  onAttendanceRetry,
  isAttendanceRetrying,
  canLoadMoreAttendance,
  onAttendanceLoadMore,
  isAttendanceLoadingMore,
  attendanceLoadMoreError,
  onCancelBooking,
  cancelingEnrollmentId,
  cancelBookingErrorMessage,
  bookingSection,
}: ChildDetailProps) {
  const allSubscriptions =
    activeSubscriptions && activeSubscriptions.length > 0
      ? activeSubscriptions
      : activeSubscription
        ? [activeSubscription]
        : [];
  const visibleSubscriptions = selectEntitlementSubscriptions(allSubscriptions);
  const subscriptionPaymentOrders = onlinePaymentOrders.filter(
    isSubscriptionBankPaymentOrder,
  );
  const subscriptionRows = visibleSubscriptions.map((subscription) => ({
    subscription,
    bankPaymentOrder: findRenewalOrderForSubscription(
      subscriptionPaymentOrders,
      subscription,
      visibleSubscriptions,
    ),
  }));
  const attachedBankPaymentOrderIds = new Set(
    subscriptionRows
      .map((row) => row.bankPaymentOrder?.id)
      .filter((orderId): orderId is number => typeof orderId === "number"),
  );
  const pendingRenewalOrders = selectPendingRenewalOrders(
    subscriptionPaymentOrders,
    allSubscriptions,
  ).filter((order) => !attachedBankPaymentOrderIds.has(order.id));
  const recentRenewalOrders = subscriptionPaymentOrders.filter(
    (order) => !isLiveRenewalOrder(order),
  );
  const subscriptionAlert =
    visibleSubscriptions.length === 0
      ? getParentSubscriptionAlert({
          hasSubscription: false,
          remaining: null,
          total: null,
          status: null,
          freezeStatus: null,
        })
      : visibleSubscriptions
          .map((subscription) =>
            getParentSubscriptionAlert({
              hasSubscription: true,
              remaining: subscription.trainings_left,
              total: subscription.trainings_total,
              status: subscription.status,
              freezeStatus: subscription.freeze_status,
            }),
          )
          .sort((a, b) => rankSubscriptionAlert(b) - rankSubscriptionAlert(a))[0];
  const operationalAdmissions = financialState?.operational_admissions_v2?.length
    ? financialState.operational_admissions_v2
    : financialState?.operational_admission_v2
      ? [financialState.operational_admission_v2]
      : financialState?.operational_admissions?.length
        ? financialState.operational_admissions
        : financialState?.operational_admission
          ? [financialState.operational_admission]
          : [];
  const coveredVisits = financialState?.covered_visits ?? [];

  return (
    <div className="space-y-4">
      <section
        id="subscription"
        aria-label="Абонемент ребёнка"
        className="parent-section-enter space-y-3 scroll-mt-24"
      >
        <PortalSectionTitle eyebrow="Абонемент" title="Абонемент ребёнка" />
        {operationalAdmissions.map((admission, index) => {
          const admissionCoveredVisits = coveredVisits.filter(
            (visit) =>
              visit.payment_id === admission.payment_id ||
              (visit.payment_id === undefined && index === 0),
          );

          return (
            <Card
              key={admission.payment_id}
              className="overflow-hidden border-0 bg-amber-50/90 shadow-[0_14px_34px_rgba(17,24,39,0.08)] ring-1 ring-amber-500/18"
            >
              <CardContent className="space-y-2 py-4">
                <p className="text-[11px] uppercase tracking-[0.16em] text-amber-900/70">
                  {"kind" in admission && admission.kind === "personal"
                    ? "Персональная тренировка"
                    : "Запись в группу"}
                </p>
                <p className="parent-balanced-title text-[15px] font-semibold text-amber-950">
                  {admission.payment_status === "pending"
                    ? "Оплата ожидает подтверждения"
                    : admission.payment_status === "confirmed"
                      ? "Оплата подтверждена"
                      : admission.payment_status === "rejected"
                        ? "Оплата отклонена"
                        : `Статус оплаты: ${admission.payment_status}`}
                </p>
                {admission.payment_status === "rejected" ? (
                  <p className="text-[13px] leading-5 text-amber-900/75">
                    Запись отменена; будущая готовность к чекину снята.
                  </p>
                ) : (
                  <p className="text-[13px] leading-5 text-amber-900/75">
                    {"kind" in admission && admission.kind === "personal"
                      ? `Персональная запись · ${formatDebtDate(admission.start_date)}`
                      : `${admission.group_label} · старт ${formatDebtDate(admission.start_date)}`}
                  </p>
                )}
                {admissionCoveredVisits.map((visit) => (
                  <p key={visit.debt_id} className="rounded-2xl bg-white/70 px-3 py-2 text-[13px] text-amber-950 ring-1 ring-amber-500/10">
                    {visit.training_type_name} · {formatDebtDate(visit.checkin_date)} · покрыто оплатой
                  </p>
                ))}
                {admissionCoveredVisits.length > 0 ? (
                  <p className="text-[12px] leading-5 text-amber-900/70">
                    Отдельная оплата и ссылка не нужны до подтверждения.
                  </p>
                ) : null}
              </CardContent>
            </Card>
          );
        })}
        {openDebts.length > 0 ? (
          <Card className="overflow-hidden border-0 bg-red-50/90 shadow-[0_14px_34px_rgba(17,24,39,0.08)] ring-1 ring-red-500/18">
            <CardContent className="space-y-2 py-4">
              <p className="text-[11px] uppercase tracking-[0.16em] text-red-900/70">
                Задолженность
              </p>
              <p className="parent-balanced-title text-[15px] font-semibold text-red-950">
                Есть задолженность
              </p>
              <div className="space-y-2">
                {openDebts.map((debt) => (
                  <div
                    key={debt.id}
                    className="flex items-start justify-between gap-3 rounded-2xl bg-white/70 px-3 py-2 ring-1 ring-red-500/10"
                  >
                    <div className="min-w-0 space-y-1">
                      <p className="parent-pretty-text text-[13px] font-semibold leading-5 text-red-950">
                        {debt.training_type_name}
                      </p>
                      <div className="space-y-0.5 text-[12px] leading-4 text-red-900/70">
                        <p>Причина: {formatDebtReason(debt.reason)}</p>
                        <p>Тренировка: {formatDebtDate(debt.checkin_date)}</p>
                        <p>Создано: {formatDebtDate(debt.created_at)}</p>
                      </div>
                    </div>
                    <p className="shrink-0 text-[14px] font-semibold text-red-950">
                      {formatDebtAmount(debt.tariff_price)}
                    </p>
                  </div>
                ))}
              </div>
              {openDebts.length > 1 ? (
                <p className="text-[13px] leading-5 text-red-900/65">
                  Открытых долгов: {openDebts.length}
                </p>
              ) : null}
              <p className="text-[13px] leading-5 text-red-900/65">
                Для закрытия долга тренер сформирует отдельную ссылку.
              </p>
            </CardContent>
          </Card>
        ) : null}
        {subscriptionAlert.requiresAttention ? (
          <SubscriptionAttentionCard
            title={subscriptionAlert.title}
            description={subscriptionAlert.description}
            tone={subscriptionAlert.tone === "danger" ? "danger" : "warning"}
          />
        ) : null}
        {subscriptionRows.map(({ subscription, bankPaymentOrder }) => {
          const hasExistingBankPaymentOrder = hasUnresolvedRenewalOrderForTariff(
            onlinePaymentOrders,
            subscription.tariff_id,
          );
          const hasExactSourceOrder = hasUnresolvedRenewalOrderForSource(
            onlinePaymentOrders,
            subscription.id,
          );
          const renewalOfferLabel = formatRenewalOfferLabel(subscription);
          return (
            <div key={subscription.id} className="space-y-2">
              <SubscriptionCard
                tariffName={subscription.tariff_name}
                trainingsUsed={subscription.trainings_used}
                trainingsTotal={subscription.trainings_total}
                trainingsLeft={subscription.trainings_left}
                expiresAt={subscription.expires_at}
                status={subscription.status}
                freezeStatus={subscription.freeze_status}
              />
              {renewalMode !== "unavailable" &&
              !(renewalMode === "unified" ? hasExactSourceOrder : hasExistingBankPaymentOrder) &&
              subscription.tariff_id &&
              ["active", "expired"].includes(subscription.status) &&
              onCreateBankPaymentOrder ? (
                <div className="space-y-2">
                  {renewalOfferLabel ? (
                    <p className="ui-muted-13">Продление: {renewalOfferLabel}</p>
                  ) : null}
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-[44px] w-full bg-white/80"
                    disabled={
                      !onlinePaymentsEnabled ||
                      isCreatingBankPaymentOrder ||
                      isCancelingBankPaymentOrder
                    }
                    onClick={() =>
                      onCreateBankPaymentOrder({
                        tariffId: subscription.tariff_id as number,
                        debtIds: [],
                        renewalOffer: subscription,
                        ...(renewalMode === "unified"
                          ? { renewedFromSubscriptionId: subscription.id }
                          : {}),
                      })
                    }
                    wrap
                  >
                    {isCancelingBankPaymentOrder
                      ? "Отмена продления..."
                      : isCreatingBankPaymentOrder
                        ? "Создание..."
                        : "Продлить через СБП"}
                  </Button>
                </div>
              ) : null}
              {renewalMode === "unavailable" ? (
                <p className="ui-muted-13">
                  Продление через СБП станет доступно после проверки настроек клуба.
                </p>
              ) : null}
              {bankPaymentOrder ? (
                <OnlinePaymentLinkPanel
                  order={bankPaymentOrder}
                  className="bg-white/92"
                  title="Продление ожидает оплаты"
                  cancelLabel="Отменить продление"
                  isCanceling={isCancelingBankPaymentOrder}
                  isRefreshing={isRefreshingBankPaymentOrder}
                  onCancel={onCancelBankPaymentOrder}
                  onRefresh={onRefetchBankPaymentOrders}
                  onRequestRefresh={() => onRefreshBankPaymentOrders?.(bankPaymentOrder)}
                />
              ) : null}
            </div>
          );
        })}
        {pendingRenewalOrders.map((order) => (
          <OnlinePaymentLinkPanel
            key={order.id}
            order={order}
            className="bg-white/92"
            title="Продление ожидает оплаты"
            cancelLabel="Отменить продление"
            isCanceling={isCancelingBankPaymentOrder}
            isRefreshing={isRefreshingBankPaymentOrder}
            onCancel={onCancelBankPaymentOrder}
            onRefresh={onRefetchBankPaymentOrders}
            onRequestRefresh={() => onRefreshBankPaymentOrders?.(order)}
          />
        ))}
        {!onlinePaymentsEnabled &&
        visibleSubscriptions.some(
          (subscription) =>
            Boolean(subscription.tariff_id) && ["active", "expired"].includes(subscription.status),
        ) ? (
          <p role="status" className="ui-warning">
            {onlinePaymentUnavailableMessage || "Онлайн-оплата сейчас недоступна. Обратитесь в клуб."}
          </p>
        ) : null}
        {recentRenewalOrders.map((order) => (
          <OnlinePaymentLinkPanel
            key={`recent-${order.id}`}
            order={order}
            className="bg-white/92"
            title="Статус продления"
            onRefresh={onRefetchBankPaymentOrders}
            onRequestRefresh={() => onRefreshBankPaymentOrders?.(order)}
            isRefreshing={isRefreshingBankPaymentOrder}
          />
        ))}
        {onlinePaymentErrorMessage ? (
          <Card className="border-0 bg-red-50/90 shadow-sm ring-1 ring-red-500/18">
            <CardContent className="py-3">
              <p className="text-[14px] text-red-950">
                {onlinePaymentErrorMessage}
              </p>
            </CardContent>
          </Card>
        ) : null}
        {visibleSubscriptions.length === 0 && !subscriptionAlert.requiresAttention ? (
          <Card className="border-0 bg-white/92 shadow-sm ring-1 ring-black/6">
            <CardContent className="py-4">
              <p className="text-[14px] text-neutral-500">
                Нет активного абонемента
              </p>
            </CardContent>
          </Card>
        ) : null}
      </section>

      <DocumentChecklistSection items={documentChecklist} />
      <GradeSection gradeProgress={gradeProgress} />
      <TrainingGroupsSection
        schedule={schedule}
        onCancelBooking={onCancelBooking}
        cancelingEnrollmentId={cancelingEnrollmentId}
        cancelBookingErrorMessage={cancelBookingErrorMessage}
      />
      {bookingSection}

      <AttendanceSection
        attendance={attendance}
        attendanceCount={attendanceCount}
        attendanceError={attendanceError}
        onAttendanceRetry={onAttendanceRetry}
        isAttendanceRetrying={isAttendanceRetrying}
        canLoadMore={canLoadMoreAttendance}
        onLoadMore={onAttendanceLoadMore}
        isLoadingMore={isAttendanceLoadingMore}
        loadMoreError={attendanceLoadMoreError}
      />
    </div>
  );
}

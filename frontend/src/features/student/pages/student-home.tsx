import { lazy, Suspense, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PortalActionLink } from "@/components/portal/portal-action-link";
import { getAuthTokenSubject, useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import apiClient from "@/api/custom-fetch";
import {
  getOnlinePaymentUnavailableMessage,
  hasOnlinePaymentsCapability,
  usePaymentCapabilities,
} from "@/api/payment-capabilities";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import {
  OnlinePaymentLinkPanel,
  type BankPaymentOrderLink,
} from "@/components/portal/online-payment-link-panel";
import {
  isPayableBankPaymentOrder,
  isSubscriptionBankPaymentOrder,
} from "@/components/portal/payment-link-state";
import { PersonalPaymentReservationLinkPanel } from "@/components/portal/guest-self-booking-section";
import {
  isVisiblePersonalPaymentReservation,
  type PersonalBookingPaymentReservation,
} from "@/components/portal/personal-payment-reservation-state";
import {
  formatRenewalOfferLabel,
  findRenewalOrderForSubscription,
  getExpectedRenewalOfferFields,
  getRenewalOfferErrorMessage,
  hasUnresolvedRenewalOrderForSource,
  hasUnresolvedRenewalOrderForTariff,
  isRenewalOfferError,
  selectEntitlementSubscriptions,
  selectPendingRenewalOrders,
  type RenewalTargetOffer,
} from "@/components/portal/subscription-renewal-state";
import {
  getUnifiedClientJourneyCapabilityMode,
  getPersonalAvailabilityCapabilityMode,
  usePersonalAvailabilityCapabilityQuery,
  useUnifiedClientJourneyCapabilityQuery,
} from "@/api/unified-client-journey";
import {
  clearContextualCommercialCommandKey,
  getOrCreateContextualCommercialCommandKey,
} from "@/api/contextual-commercial-command-key";
import {
  SelfServicePersonalCommandCards,
  SelfServicePersonalUnavailableNotice,
} from "@/components/portal/self-service-personal";
import { formatDateRu } from "@/lib/locale";
import { todayInTimeZone } from "@/lib/club-date";
import { SubscriptionCard } from "../components/subscription-card";
import { GradeCard } from "../components/grade-card";
import { NextTrainingCard } from "../components/next-training-card";
import { StudentPageIntro } from "../components/student-page-intro";
import { StudentQuickActions } from "../components/student-quick-actions";
import { StudentSectionTitle } from "../components/student-section-title";
import { StudentSurfaceCard } from "../components/student-surface-card";
import { CardContent } from "@/components/ui/card";
import {
  findNextTrainingStreams,
  formatUpcomingTrainingDayLabel,
} from "../lib/student-home-utils";
import {
  getMonday,
  toDateParam,
  type StudentScheduleOccurrence,
} from "../lib/student-schedule-utils";

const PushPermissionBanner = lazy(
  () => import("@/features/notifications/components/push-permission-banner"),
);

interface SubscriptionData {
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

interface StudentDebtData {
  id: number;
  checkin_id: number;
  tariff_price: string | null;
  reason: string;
  training_type_name: string;
  checkin_date: string;
  created_at: string;
}

interface OperationalAdmissionData {
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

interface OperationalAdmissionV2Data {
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

interface CoveredVisitData {
  payment_id?: number;
  debt_id: number;
  checkin_id: number;
  training_type_name: string;
  checkin_date: string;
  coverage_state: string;
  is_payable: boolean;
}

interface CabinetFinancialState {
  operational_admission: OperationalAdmissionData | null;
  operational_admissions?: OperationalAdmissionData[];
  operational_admission_v2?: OperationalAdmissionV2Data | null;
  operational_admissions_v2?: OperationalAdmissionV2Data[];
  covered_visits: CoveredVisitData[];
}

interface GradeData {
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

const ONLINE_PAYMENT_SUBSCRIPTION_STATUSES = new Set(["active", "expired"]);

function HomeSkeleton() {
  return (
    <div className="p-6 space-y-4">
      <Skeleton className="h-32 w-full rounded-xl" />
      <Skeleton className="h-24 w-full rounded-xl" />
      <Skeleton className="h-20 w-full rounded-xl" />
    </div>
  );
}

function formatDebtAmount(value: string | null) {
  return value ? `${value} ₽` : "Сумма уточняется";
}

const DEBT_REASON_LABELS: Record<string, string> = {
  no_subscription: "Нет подходящего абонемента",
  subscription_exhausted: "Закончились тренировки по абонементу",
  subscription_expired: "Абонемент истек",
  tariff_mismatch: "Тип тренировки не входит в абонемент",
};

function formatDebtReason(reason: string): string {
  return DEBT_REASON_LABELS[reason] ?? reason.replaceAll("_", " ");
}

function formatDebtDate(value: string): string {
  return formatDateRu(value.slice(0, 10));
}

export default function StudentHome() {
  const queryClient = useQueryClient();
  const paymentCapabilitiesQuery = usePaymentCapabilities();
  const unifiedJourneyCapability = useUnifiedClientJourneyCapabilityQuery();
  const personalAvailabilityCapability = usePersonalAvailabilityCapabilityQuery();
  const personalAvailabilityCapabilityMode = getPersonalAvailabilityCapabilityMode(
    personalAvailabilityCapability,
  );
  const unifiedClientJourneyEnabled = personalAvailabilityCapabilityMode === "unified";
  const unifiedJourneyMode = getUnifiedClientJourneyCapabilityMode(unifiedJourneyCapability);
  const contextualRenewalEnabled = unifiedJourneyMode === "unified";
  const legacyRenewalEnabled = unifiedJourneyMode === "legacy";
  const legacyPersonalAvailabilityEnabled = personalAvailabilityCapabilityMode === "legacy";
  const studentId = useAuthStore((s) => s.studentId);
  const clubId = useAuthStore((s) => s.clubId);
  const accessToken = useAuthStore((s) => s.accessToken);
  const bootstrapStatus = useAuthStore((s) => s.studentBootstrapStatus);
  const timeZone = useBrandingStore((s) => s.timeZone);
  const [studentBankPaymentOrder, setStudentBankPaymentOrder] =
    useState<BankPaymentOrderLink | null>(null);
  const [studentBankPaymentOrderAt, setStudentBankPaymentOrderAt] = useState<number | null>(null);
  const [hiddenBankPaymentOrderIds, setHiddenBankPaymentOrderIds] = useState<Set<number>>(
    () => new Set(),
  );
  const [studentBankPaymentCancelError, setStudentBankPaymentCancelError] = useState<
    string | null
  >(null);
  const [hiddenPersonalPaymentReservationIds, setHiddenPersonalPaymentReservationIds] =
    useState<number[]>([]);

  const queriesEnabled = bootstrapStatus === "resolved" && !!studentId;
  const currentMonday = useMemo(() => getMonday(todayInTimeZone(timeZone)), [timeZone]);
  const nextMonday = useMemo(() => {
    const next = new Date(currentMonday);
    next.setDate(currentMonday.getDate() + 7);
    return next;
  }, [currentMonday]);
  const currentWeekStart = toDateParam(currentMonday);
  const nextWeekStart = toDateParam(nextMonday);

  const subscriptionsQuery = useQuery({
    queryKey: ["student", "subscriptions", studentId],
    queryFn: async () => (await apiClient.get<SubscriptionData[]>("/students/me/subscriptions/")).data,
    enabled: queriesEnabled,
    staleTime: 5 * 60_000,
  });
  const debtsQuery = useQuery({
    queryKey: ["student", "debts", studentId],
    queryFn: async () => (await apiClient.get<StudentDebtData[]>("/students/me/debts/")).data,
    enabled: queriesEnabled,
    staleTime: 5 * 60_000,
  });
  const financialStateQuery = useQuery({
    queryKey: ["student", "financial-state", studentId],
    queryFn: async () =>
      (await apiClient.get<CabinetFinancialState>("/students/me/financial-state/")).data,
    enabled: queriesEnabled,
    staleTime: 5 * 60_000,
  });
  const gradesQuery = useQuery({
    queryKey: ["student", "grades", studentId],
    queryFn: async () => (await apiClient.get<GradeData[]>("/grades/my-progress/")).data,
    enabled: queriesEnabled,
    staleTime: 5 * 60_000,
  });
  const scheduleQuery = useQuery({
    queryKey: ["student", "schedule-home", studentId, currentWeekStart, nextWeekStart],
    queryFn: async () => {
      const [currentWeekRes, nextWeekRes] = await Promise.all([
        apiClient.get<StudentScheduleOccurrence[]>("/students/me/schedule-week/", {
          params: { week_start: currentWeekStart },
        }),
        apiClient.get<StudentScheduleOccurrence[]>("/students/me/schedule-week/", {
          params: { week_start: nextWeekStart },
        }),
      ]);
      return [...currentWeekRes.data, ...nextWeekRes.data];
    },
    enabled: queriesEnabled,
    staleTime: 5 * 60_000,
  });
  const bankPaymentOrdersQuery = useQuery({
    queryKey: ["student", "bank-payment-orders", studentId],
    queryFn: async () =>
      (
        await apiClient.get<BankPaymentOrderLink[]>("/students/me/bank-payment-orders/", {
          params: { status: "recent" },
        })
      ).data,
    enabled: queriesEnabled,
    staleTime: 60_000,
  });
  const personalPaymentReservationsQuery = useQuery({
    queryKey: ["student", "personal-payment-reservations", studentId],
    queryFn: async () =>
      (
        await apiClient.get<PersonalBookingPaymentReservation[]>(
          "/personal-availability/payment-reservations/",
          {
            params: { status: "open_actionable" },
          },
        )
      ).data,
    enabled: queriesEnabled && legacyPersonalAvailabilityEnabled,
    staleTime: 60_000,
  });

  const createStudentBankPaymentOrder = useMutation({
    mutationFn: async ({
      tariffId,
      debtIds,
      renewedFromSubscriptionId,
      renewalOffer,
    }: {
      tariffId: number;
      debtIds: number[];
      renewedFromSubscriptionId?: number;
      renewalOffer?: RenewalTargetOffer;
    }) => {
      if (!hasOnlinePaymentsCapability(paymentCapabilitiesQuery)) {
        throw new Error("Online payment capability is unavailable");
      }
      if (!contextualRenewalEnabled && !legacyRenewalEnabled) {
        throw new Error("Renewal capability is unavailable");
      }
      if (contextualRenewalEnabled && !renewedFromSubscriptionId) {
        throw new Error("Exact renewal source is unavailable");
      }
      if (contextualRenewalEnabled && !studentId) {
        throw new Error("Student command scope is not resolved");
      }
      const renewalScope =
        contextualRenewalEnabled && renewedFromSubscriptionId
          ? {
              clubId,
              actorSubject: getAuthTokenSubject(accessToken),
              audience: "student" as const,
              kind: "subscription_renewal" as const,
              studentId: studentId as number,
              paymentMethod: "sbp" as const,
              renewedFromSubscriptionId,
            }
          : null;
      const expectedOffer = renewalOffer
        ? getExpectedRenewalOfferFields(renewalOffer)
        : null;
      const response = await apiClient.post<BankPaymentOrderLink>(
        "/students/me/bank-payment-orders/",
        renewalScope
          ? {
              renewed_from_subscription_id: renewedFromSubscriptionId,
              idempotency_key: getOrCreateContextualCommercialCommandKey(renewalScope),
              ...(expectedOffer ?? {}),
            }
          : {
              tariff_id: tariffId,
              debt_ids: debtIds,
              ...(expectedOffer ?? {}),
            },
      );
      return response.data;
    },
    onSuccess: (order, variables) => {
      if (contextualRenewalEnabled && studentId && variables.renewedFromSubscriptionId) {
        clearContextualCommercialCommandKey({
          clubId,
          actorSubject: getAuthTokenSubject(accessToken),
          audience: "student",
          kind: "subscription_renewal",
          studentId,
          paymentMethod: "sbp",
          renewedFromSubscriptionId: variables.renewedFromSubscriptionId,
        });
      }
      setStudentBankPaymentCancelError(null);
      setStudentBankPaymentOrder(order);
      setStudentBankPaymentOrderAt(Date.now());
      void queryClient.invalidateQueries({
        queryKey: ["student", "bank-payment-orders", studentId],
      });
    },
    onError: (error, variables) => {
      if (isRenewalOfferError(error)) {
        if (contextualRenewalEnabled && studentId && variables.renewedFromSubscriptionId) {
          clearContextualCommercialCommandKey({
            clubId,
            actorSubject: getAuthTokenSubject(accessToken),
            audience: "student",
            kind: "subscription_renewal",
            studentId,
            paymentMethod: "sbp",
            renewedFromSubscriptionId: variables.renewedFromSubscriptionId,
          });
        }
        void subscriptionsQuery.refetch();
      }
      setStudentBankPaymentOrder(null);
      setStudentBankPaymentOrderAt(null);
    },
  });

  const cancelStudentBankPaymentOrder = useMutation({
    mutationFn: async (order: BankPaymentOrderLink) => {
      const response = await apiClient.post<BankPaymentOrderLink>(
        `/students/me/bank-payment-orders/${order.id}/cancel/`,
        {},
      );
      return response.data;
    },
    onMutate: (order) => {
      setStudentBankPaymentCancelError(null);
      setHiddenBankPaymentOrderIds((current) => {
        const next = new Set(current);
        next.add(order.id);
        return next;
      });
    },
    onSuccess: (order) => {
      setStudentBankPaymentCancelError(null);
      setHiddenBankPaymentOrderIds((current) => {
        const next = new Set(current);
        next.add(order.id);
        return next;
      });
      setStudentBankPaymentOrder((current) => (current?.id === order.id ? null : current));
      setStudentBankPaymentOrderAt((current) =>
        studentBankPaymentOrder?.id === order.id ? null : current,
      );
      void queryClient.invalidateQueries({
        queryKey: ["student", "bank-payment-orders", studentId],
      });
    },
    onError: (_error, order) => {
      setHiddenBankPaymentOrderIds((current) => {
        const next = new Set(current);
        next.delete(order.id);
        return next;
      });
      setStudentBankPaymentCancelError("Не удалось отменить продление. Попробуйте ещё раз.");
    },
  });

  const refreshStudentBankPaymentOrder = useMutation({
    mutationFn: async (order: BankPaymentOrderLink) => {
      const response = await apiClient.post<BankPaymentOrderLink>(
        `/students/me/bank-payment-orders/${order.id}/refresh/`,
        {},
      );
      return response.data;
    },
    onSettled: () =>
      bankPaymentOrdersQuery.refetch(),
  });

  const cancelPersonalPaymentReservation = useMutation({
    mutationFn: async (reservation: PersonalBookingPaymentReservation) => {
      const response = await apiClient.post<PersonalBookingPaymentReservation>(
        `/personal-availability/payment-reservations/${reservation.id}/cancel/`,
        {},
      );
      return response.data;
    },
    onSuccess: (reservation) => {
      setHiddenPersonalPaymentReservationIds((current) =>
        current.includes(reservation.id) ? current : [...current, reservation.id],
      );
      void queryClient.invalidateQueries({
        queryKey: ["student", "personal-payment-reservations", studentId],
      });
    },
  });

  if (bootstrapStatus === "idle" || bootstrapStatus === "loading") {
    return <HomeSkeleton />;
  }

  if (!studentId) return <HomeSkeleton />;

  const coreQueries = [subscriptionsQuery, debtsQuery, gradesQuery, scheduleQuery] as const;
  if (coreQueries.every((query) => query.isError)) {
    return (
      <div className="p-6 text-center text-neutral-500">
        <p>Не удалось загрузить данные. Попробуйте обновить страницу.</p>
      </div>
    );
  }

  const subscriptions = subscriptionsQuery.data ?? [];
  const debts = debtsQuery.data ?? [];
  const financialState = financialStateQuery.data;
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
  const grades = gradesQuery.data ?? [];
  const schedule = scheduleQuery.data ?? [];
  const bankPaymentOrders = bankPaymentOrdersQuery.data ?? [];
  const subscriptionBankPaymentOrders = bankPaymentOrders.filter(
    isSubscriptionBankPaymentOrder,
  );
  const personalPaymentReservations = personalPaymentReservationsQuery.data ?? [];
  const personalPaymentReservationsError = personalPaymentReservationsQuery.isError;
  const visibleSubscriptions = selectEntitlementSubscriptions(subscriptions);
  const liveBankPaymentOrders = subscriptionBankPaymentOrders.filter(isPayableBankPaymentOrder);
  const recentTerminalBankPaymentOrders = subscriptionBankPaymentOrders.filter(
    (order) => !isPayableBankPaymentOrder(order) && !hiddenBankPaymentOrderIds.has(order.id),
  );
  const serverLiveBankPaymentOrderIds = new Set(liveBankPaymentOrders.map((order) => order.id));
  const localBankPaymentOrderStillAwaitingServerSnapshot =
    studentBankPaymentOrderAt !== null &&
    bankPaymentOrdersQuery.dataUpdatedAt <= studentBankPaymentOrderAt;
  const bankPaymentOrderCandidates = [
    ...(studentBankPaymentOrder &&
    isPayableBankPaymentOrder(studentBankPaymentOrder) &&
    (localBankPaymentOrderStillAwaitingServerSnapshot ||
      serverLiveBankPaymentOrderIds.has(studentBankPaymentOrder.id))
      ? [studentBankPaymentOrder]
      : []),
    ...liveBankPaymentOrders,
  ].filter((order) => !hiddenBankPaymentOrderIds.has(order.id));
  const subscriptionRows = visibleSubscriptions.map((subscription) => ({
    subscription,
    bankPaymentOrder: findRenewalOrderForSubscription(
      bankPaymentOrderCandidates,
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
    bankPaymentOrderCandidates,
    subscriptions,
  ).filter((order) => !attachedBankPaymentOrderIds.has(order.id));
  const visiblePersonalPaymentReservations = personalPaymentReservations.filter(
    (reservation) =>
      isVisiblePersonalPaymentReservation(reservation) &&
      !hiddenPersonalPaymentReservationIds.includes(reservation.id),
  );
  const nextTrainingStreams = findNextTrainingStreams(schedule);
  const nextTrainingCards = [
    {
      occurrence: nextTrainingStreams.group,
      eyebrow: "Следующая группа",
    },
    {
      occurrence: nextTrainingStreams.personal,
      eyebrow: "Следующая персоналка",
    },
  ].filter(
    (item): item is { occurrence: StudentScheduleOccurrence; eyebrow: string } =>
      item.occurrence !== null,
  );
  const primaryGrade = grades[0] ?? null;
  const extraGrades = grades.slice(1);

  return (
    <div className="min-h-full bg-[radial-gradient(circle_at_top,_rgba(0,0,0,0.025),_transparent_36%)] px-5 pb-24 pt-4">
      <div className="space-y-4">
        <StudentPageIntro
          eyebrow="Личный кабинет"
          title="Главная"
          description="Следи за своим прогрессом, абонементом и ближайшей тренировкой в одном месте."
        />

        {gradesQuery.isLoading ? (
          <Skeleton className="h-32 w-full rounded-xl" />
        ) : gradesQuery.isError ? (
          <StudentSurfaceCard>
            <CardContent className="py-3.5">
              <p className="ui-muted-14">
                Не удалось загрузить прогресс по грейдам.
              </p>
            </CardContent>
          </StudentSurfaceCard>
        ) : (
          <GradeCard
            variant="hero"
            systemName={primaryGrade?.grade_system_name ?? ""}
            currentGradeName={primaryGrade?.current_grade?.name ?? "Без грейда"}
            nextGradeName={primaryGrade?.next_grade?.name ?? null}
            currentCheckins={primaryGrade?.trainings_since_last_grade ?? 0}
            requiredCheckins={primaryGrade?.next_grade?.min_trainings ?? null}
            progressPercent={
              primaryGrade?.next_grade && primaryGrade?.trainings_to_next !== null
                ? Math.round(
                    Math.min(
                      (primaryGrade.trainings_since_last_grade /
                        primaryGrade.next_grade.min_trainings) *
                        100,
                      100,
                    ),
                  )
                : 0
            }
          />
        )}

        <StudentQuickActions />

        <div className="grid grid-cols-1 gap-2.5">
          {operationalAdmissions.map((admission, index) => {
            const admissionCoveredVisits = coveredVisits.filter(
              (visit) =>
                visit.payment_id === admission.payment_id ||
                (visit.payment_id === undefined && index === 0),
            );

            return (
              <StudentSurfaceCard
                key={admission.payment_id}
                className="bg-amber-50/90 ring-amber-500/18"
              >
                <CardContent className="space-y-2 py-3.5">
                  <p className="text-[11px] uppercase tracking-[0.16em] text-amber-900/70">
                    {"kind" in admission && admission.kind === "personal"
                      ? "Персональная тренировка"
                      : "Запись в группу"}
                  </p>
                  <p className="text-[17px] font-semibold text-amber-950">
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
                  {admissionCoveredVisits.length > 0 ? (
                    <div className="space-y-1 rounded-2xl bg-white/70 px-3 py-2.5 ring-1 ring-amber-500/10">
                      {admissionCoveredVisits.map((visit) => (
                        <p key={visit.debt_id} className="text-[13px] text-amber-950">
                          {visit.training_type_name} · {formatDebtDate(visit.checkin_date)} · покрыто оплатой
                        </p>
                      ))}
                      <p className="text-[12px] text-amber-900/70">
                        Отдельная оплата и ссылка не нужны до подтверждения.
                      </p>
                    </div>
                  ) : null}
                </CardContent>
              </StudentSurfaceCard>
            );
          })}
          {debtsQuery.isLoading ? (
            <Skeleton className="h-24 w-full rounded-xl" />
          ) : debtsQuery.isError ? (
            <StudentSurfaceCard className="bg-amber-50/90 ring-amber-500/18">
              <CardContent className="py-3.5">
                <p className="text-[14px] text-amber-950">
                  Не удалось проверить задолженности.
                </p>
              </CardContent>
            </StudentSurfaceCard>
          ) : debts.length > 0 ? (
            <StudentSurfaceCard className="ring-red-500/18 bg-red-50/90">
              <CardContent className="space-y-2 py-3.5">
                <p className="text-[11px] uppercase tracking-[0.16em] text-red-900/70">
                  Задолженность
                </p>
                <p className="text-[17px] font-semibold text-red-950">
                  Есть задолженность
                </p>
                <div className="space-y-2">
                  {debts.map((debt) => (
                    <div
                      key={debt.id}
                      className="flex items-start justify-between gap-3 rounded-2xl bg-white/70 px-3 py-2.5 ring-1 ring-red-500/10"
                    >
                      <div className="min-w-0 space-y-1">
                        <p className="text-[14px] font-semibold leading-5 text-red-950">
                          {debt.training_type_name}
                        </p>
                        <p className="text-[13px] leading-5 text-red-900/75">
                          {formatDebtReason(debt.reason)}
                        </p>
                        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[12px] leading-5 text-red-900/65">
                          <span>Тренировка: {formatDebtDate(debt.checkin_date)}</span>
                          <span>Создано: {formatDebtDate(debt.created_at)}</span>
                        </div>
                      </div>
                      <p className="shrink-0 text-[15px] font-semibold text-red-950">
                        {formatDebtAmount(debt.tariff_price)}
                      </p>
                    </div>
                  ))}
                </div>
                {debts.length > 1 ? (
                  <p className="text-[13px] leading-5 text-red-900/65">
                    Открытых долгов: {debts.length}
                  </p>
                ) : null}
                <p className="text-[13px] leading-5 text-red-900/65">
                  Для закрытия долга тренер сформирует отдельную ссылку.
                </p>
                <PortalActionLink to="/student/profile">
                  Открыть профиль
                </PortalActionLink>
              </CardContent>
            </StudentSurfaceCard>
          ) : null}

          {subscriptionsQuery.isLoading ? (
            <Skeleton className="h-24 w-full rounded-xl" />
          ) : subscriptionsQuery.isError ? (
            <StudentSurfaceCard className="bg-amber-50/90 ring-amber-500/18">
              <CardContent className="py-3.5">
                <p className="text-[14px] text-amber-950">
                  Не удалось загрузить абонементы.
                </p>
              </CardContent>
            </StudentSurfaceCard>
          ) : subscriptionRows.length > 0 ? (
            subscriptionRows.map(({ subscription, bankPaymentOrder }) => {
              const hasExistingBankPaymentOrder = legacyRenewalEnabled && hasUnresolvedRenewalOrderForTariff(
                bankPaymentOrders,
                subscription.tariff_id,
              );
              const hasExactSourceOrder = hasUnresolvedRenewalOrderForSource(
                bankPaymentOrders,
                subscription.id,
              );
              return (
                <div key={subscription.id} className="space-y-2">
                  <SubscriptionCard
                    tariffName={subscription.tariff_name}
                    trainingsUsed={subscription.trainings_used}
                    trainingsTotal={subscription.trainings_total}
                    trainingsLeft={subscription.trainings_left}
                    expiresAt={subscription.expires_at ?? ""}
                    status={subscription.status}
                    freezeStatus={subscription.freeze_status ?? null}
                    actionLabel="Профиль и документы"
                    actionTo="/student/profile"
                  />
                  {(contextualRenewalEnabled || legacyRenewalEnabled) &&
                  !(contextualRenewalEnabled ? hasExactSourceOrder : hasExistingBankPaymentOrder) &&
                  subscription.tariff_id &&
                  ONLINE_PAYMENT_SUBSCRIPTION_STATUSES.has(subscription.status) ? (
                    <div className="space-y-2">
                      {formatRenewalOfferLabel(subscription) ? (
                        <p className="ui-muted-13">
                          Продление: {formatRenewalOfferLabel(subscription)}
                        </p>
                      ) : null}
                      <Button
                        type="button"
                        variant="outline"
                        className="min-h-[44px] w-full bg-white/80"
                        disabled={
                          !hasOnlinePaymentsCapability(paymentCapabilitiesQuery) ||
                          createStudentBankPaymentOrder.isPending ||
                          cancelStudentBankPaymentOrder.isPending
                        }
                        onClick={() =>
                          createStudentBankPaymentOrder.mutate({
                            tariffId: subscription.tariff_id as number,
                            debtIds: [],
                            renewalOffer: subscription,
                            ...(contextualRenewalEnabled
                              ? { renewedFromSubscriptionId: subscription.id }
                              : {}),
                          })
                        }
                        wrap
                      >
                        {cancelStudentBankPaymentOrder.isPending
                          ? "Отмена продления..."
                          : createStudentBankPaymentOrder.isPending
                          ? "Создание..."
                          : "Продлить через СБП"}
                      </Button>
                    </div>
                  ) : null}
                  {!contextualRenewalEnabled && !legacyRenewalEnabled ? (
                    <p className="ui-muted-13">
                      Продление через СБП станет доступно после проверки настроек клуба.
                    </p>
                  ) : null}
                  {bankPaymentOrder ? (
                    <OnlinePaymentLinkPanel
                      order={bankPaymentOrder}
                      className="bg-white/90"
                      title="Продление ожидает оплаты"
                      cancelLabel="Отменить продление"
                      isCanceling={cancelStudentBankPaymentOrder.isPending}
                      isRefreshing={refreshStudentBankPaymentOrder.isPending}
                      onCancel={(order) => cancelStudentBankPaymentOrder.mutate(order)}
                      onRefresh={() => void bankPaymentOrdersQuery.refetch()}
                      onRequestRefresh={() => refreshStudentBankPaymentOrder.mutate(bankPaymentOrder)}
                    />
                  ) : null}
                </div>
              );
            })
          ) : (
            <StudentSurfaceCard>
              <CardContent className="space-y-2 py-3.5">
                <p className="ui-overline">
                  Абонемент
                </p>
                <p className="text-[17px] font-semibold">Нет активного абонемента</p>
                <p className="ui-body-muted">
                  Когда у тебя появится действующий абонемент, здесь отобразятся срок действия и остаток занятий.
                </p>
                <PortalActionLink to="/student/profile">
                  Открыть профиль
                </PortalActionLink>
              </CardContent>
            </StudentSurfaceCard>
          )}

          {subscriptionRows.some(
            ({ subscription }) =>
              Boolean(subscription.tariff_id) &&
              ONLINE_PAYMENT_SUBSCRIPTION_STATUSES.has(subscription.status),
          ) && !hasOnlinePaymentsCapability(paymentCapabilitiesQuery) ? (
            <p role="status" className="ui-warning">
              {getOnlinePaymentUnavailableMessage(paymentCapabilitiesQuery, "self_service")}
            </p>
          ) : null}

          {pendingRenewalOrders.map((order) => (
            <OnlinePaymentLinkPanel
              key={order.id}
              order={order}
              className="bg-white/90"
              title="Продление ожидает оплаты"
              cancelLabel="Отменить продление"
              isCanceling={cancelStudentBankPaymentOrder.isPending}
              isRefreshing={refreshStudentBankPaymentOrder.isPending}
              onCancel={(candidate) => cancelStudentBankPaymentOrder.mutate(candidate)}
              onRefresh={() => void bankPaymentOrdersQuery.refetch()}
              onRequestRefresh={() => refreshStudentBankPaymentOrder.mutate(order)}
            />
          ))}

          {recentTerminalBankPaymentOrders.map((order) => (
            <OnlinePaymentLinkPanel
              key={order.id}
              order={order}
              className="bg-white/90"
              title="Последняя онлайн-оплата"
              onRefresh={() => void bankPaymentOrdersQuery.refetch()}
              onRequestRefresh={() => refreshStudentBankPaymentOrder.mutate(order)}
              isRefreshing={refreshStudentBankPaymentOrder.isPending}
            />
          ))}

          {createStudentBankPaymentOrder.isError ? (
            <StudentSurfaceCard className="bg-red-50/90 ring-red-500/18">
              <CardContent className="py-3.5">
                <p className="text-[14px] text-red-950">
                  {getRenewalOfferErrorMessage(
                    createStudentBankPaymentOrder.error,
                    "Не удалось создать ссылку на оплату",
                  )}
                </p>
              </CardContent>
            </StudentSurfaceCard>
          ) : null}

          {studentBankPaymentCancelError ? (
            <StudentSurfaceCard className="bg-red-50/90 ring-red-500/18">
              <CardContent className="py-3.5">
                <p className="text-[14px] text-red-950">
                  {studentBankPaymentCancelError}
                </p>
              </CardContent>
            </StudentSurfaceCard>
          ) : null}

          {unifiedClientJourneyEnabled ? (
            <SelfServicePersonalCommandCards
              scope={{ audience: "student" }}
              enabled
              onlinePaymentsEnabled={hasOnlinePaymentsCapability(paymentCapabilitiesQuery)}
            />
          ) : !legacyPersonalAvailabilityEnabled ? (
            <SelfServicePersonalUnavailableNotice
              isError={
                personalAvailabilityCapability.isError || personalAvailabilityCapability.isRefetchError
              }
            />
          ) : null}

          {legacyPersonalAvailabilityEnabled && personalPaymentReservationsError ? (
            <StudentSurfaceCard className="bg-amber-50/90 ring-amber-500/18">
              <CardContent className="space-y-1 py-3.5">
                <p className="text-[14px] font-semibold text-amber-950">
                  Не удалось проверить ожидающие оплаты персоналки
                </p>
                <p className="text-[13px] leading-5 text-amber-900">
                  Обновите экран перед новой оплатой, чтобы не создать дубль ссылки.
                </p>
              </CardContent>
            </StudentSurfaceCard>
          ) : null}

          {legacyPersonalAvailabilityEnabled && visiblePersonalPaymentReservations.map((reservation) => (
            <PersonalPaymentReservationLinkPanel
              key={reservation.id}
              reservation={reservation}
              className="bg-white/90"
              cancelingPaymentReservationId={
                cancelPersonalPaymentReservation.isPending
                  ? cancelPersonalPaymentReservation.variables?.id
                  : null
              }
              onPaymentReservationCancel={
                reservation.can_cancel
                  ? (item) => cancelPersonalPaymentReservation.mutate(item)
                  : undefined
              }
            />
          ))}

          {scheduleQuery.isLoading ? (
            <Skeleton className="h-24 w-full rounded-xl" />
          ) : scheduleQuery.isError ? (
            <StudentSurfaceCard className="bg-amber-50/90 ring-amber-500/18">
              <CardContent className="py-3.5">
                <p className="text-[14px] text-amber-950">
                  Не удалось загрузить ближайшие тренировки.
                </p>
              </CardContent>
            </StudentSurfaceCard>
          ) : nextTrainingCards.length > 0 ? (
            nextTrainingCards.map(({ occurrence, eyebrow }) => (
              <NextTrainingCard
                key={`${occurrence.schedule_id}-${occurrence.effective_date}-${occurrence.effective_start_time}`}
                eyebrow={eyebrow}
                groupName={occurrence.group_name}
                dayLabel={formatUpcomingTrainingDayLabel(occurrence.effective_date)}
                startTime={occurrence.effective_start_time}
                endTime={occurrence.effective_end_time}
                trainerName={occurrence.trainer_name}
                locationName={occurrence.location_name}
                actionLabel="Открыть расписание"
                actionTo="/student/schedule"
              />
            ))
          ) : visiblePersonalPaymentReservations.length === 0 ? (
            <StudentSurfaceCard>
              <CardContent className="space-y-2 py-3.5">
                <p className="ui-overline">
                  Следующая тренировка
                </p>
                <p className="text-[17px] font-semibold">Пока нет ближайшего занятия</p>
                <p className="ui-body-muted">
                  Как только появится ближайшее занятие, карточка обновится автоматически.
                </p>
                <PortalActionLink to="/student/schedule">
                  Проверить расписание
                </PortalActionLink>
              </CardContent>
            </StudentSurfaceCard>
          ) : null}
        </div>

        {extraGrades.length > 0 ? (
          <section className="space-y-3">
            <StudentSectionTitle
              eyebrow="Дополнительные дисциплины"
              title="Прогресс по другим направлениям"
            />
            <div className="space-y-3">
              {extraGrades.map((g, index) => {
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
                    key={`${g.grade_system_name ?? "extra"}-${index}`}
                    variant="compact"
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
          </section>
        ) : null}

        <Suspense fallback={null}>
          <PushPermissionBanner role="student" />
        </Suspense>
      </div>
    </div>
  );
}

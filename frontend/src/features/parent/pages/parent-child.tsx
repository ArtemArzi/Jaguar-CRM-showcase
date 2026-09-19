import { useEffect, useMemo, useState } from "react";
import { Link, useParams, useNavigate, useLocation } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CalendarPlus, MessageSquareText, RotateCcw, UserRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CardContent } from "@/components/ui/card";
import { getAuthTokenSubject, useAuthStore } from "@/features/auth/auth-store";
import { usePrivateQueryScope } from "@/features/auth/private-query-scope";
import {
  GuestSelfBookingSheet,
  PersonalPaymentReservationLinkPanel,
  type GuestBookingDateOption,
  type GuestBookingOption,
  type GuestVisitOut,
  type PersonalAvailabilityOption,
  type PersonalBookingPaymentReservation,
  type PersonalBookingOut,
} from "@/components/portal/guest-self-booking-section";
import { isVisiblePersonalPaymentReservation } from "@/components/portal/personal-payment-reservation-state";
import {
  isPayableBankPaymentOrder,
  type BankPaymentOrderLink,
} from "@/components/portal/payment-link-state";
import {
  getExpectedRenewalOfferFields,
  getRenewalOfferErrorMessage,
  isRenewalOfferError,
  type RenewalTargetOffer,
} from "@/components/portal/subscription-renewal-state";
import {
  getOnlinePaymentUnavailableMessage,
  hasOnlinePaymentsCapability,
  usePaymentCapabilities,
} from "@/api/payment-capabilities";
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
  SelfServicePersonalBookingSection,
  SelfServicePersonalCommandCards,
  SelfServicePersonalUnavailableNotice,
} from "@/components/portal/self-service-personal";
import { Skeleton } from "@/components/ui/skeleton";
import { useBrandingStore } from "@/features/branding/use-branding";
import { toDateParamInTimeZone, todayInTimeZone } from "@/lib/club-date";
import { getApiError, getInitials } from "@/lib/utils";
import { ParentPageIntro } from "../components/parent-page-intro";
import { ParentSurfaceCard } from "../components/parent-surface-card";
import { ChildDetail } from "../components/child-detail";
import apiClient from "@/api/custom-fetch";
import { normalizeChecklistItems } from "@/features/student/lib/student-normalizers";
import {
  formatWeekLabel,
  getMonday,
  toDateParam,
} from "@/features/student/lib/student-schedule-utils";

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

function getPersonalBookingErrorMessage(error: unknown, fallback: string) {
  const code =
    error && typeof error === "object"
      ? (error as { response?: { data?: { code?: unknown } } }).response?.data?.code
      : undefined;
  if (code === "personal_offer_changed") {
    return "Цена или условия персоналки изменились. Мы обновили доступные слоты — проверьте сумму и повторите действие.";
  }
  if (
    code === "personal_booking_tariff_not_configured" ||
    code === "personal_booking_tariff_ambiguous"
  ) {
    return "Цена персональной тренировки пока не настроена. Обратитесь в клуб.";
  }
  return getApiError(error, fallback);
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
  covered_visits: CoveredVisitState[];
}

interface ChecklistItem {
  document_type: {
    id: number;
    name: string;
    description: string;
    is_required: boolean;
    is_active: boolean;
  };
  is_provided: boolean;
  has_file: boolean;
}

interface ChildProfile {
  id: number;
  first_name: string;
  last_name: string;
  status: string;
  grade_progress: Array<{
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
  }>;
  attendance_count: number;
  active_subscription: SubscriptionSummary | null;
  active_subscriptions?: SubscriptionSummary[];
  open_debts?: DebtSummary[];
  financial_state?: CabinetFinancialState;
  document_checklist?: ChecklistItem[];
  schedule: Array<{
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
    upcoming_exceptions?: Array<{
      exception_type: string;
      date?: string | null;
      reason?: string | null;
      new_date?: string | null;
      new_start_time?: string | null;
      new_end_time?: string | null;
      substitute_trainer_name?: string | null;
    }>;
    upcoming_occurrences?: Array<{
      schedule_id?: number;
      enrollment_id?: number | null;
      created_from?: string;
      can_cancel?: boolean;
      effective_date: string;
      effective_start_time: string;
      effective_end_time: string;
      trainer_name?: string | null;
      training_type_kind?: string | null;
      one_time_date?: string | null;
      is_rescheduled?: boolean;
      is_substitute?: boolean;
    }>;
  }>;
}

interface AttendanceItem {
  date: string;
  group_name: string;
  trainer_name: string;
  training_type_name: string;
  start_time: string;
}

const STATUS_LABELS: Record<string, string> = {
  lead: "Лид",
  trial: "Пробное",
  active: "Активен",
  at_risk: "В риске",
  churned: "Не посещает 30+ дней",
  lost: "Потерян",
};

const ATTENDANCE_PAGE_SIZE = 10;

function getSubscriptionBadgeLabel(subscriptions: SubscriptionSummary[]) {
  if (subscriptions.some((subscription) => subscription.freeze_status === "pending")) {
    return "Заморозка ожидает подтверждения";
  }

  if (subscriptions.some((subscription) => subscription.status === "active")) {
    return "Абонемент активен";
  }

  if (subscriptions.some((subscription) => subscription.status === "frozen")) {
    return "Абонемент заморожен";
  }

  if (subscriptions.some((subscription) => subscription.status === "pending")) {
    return "Ожидает подтверждения";
  }

  if (subscriptions.some((subscription) => subscription.status === "expired")) {
    return "Абонемент истек";
  }

  if (subscriptions.some((subscription) => subscription.status === "cancelled")) {
    return "Абонемент отменён после возврата";
  }

  return "Нет активного абонемента";
}

function buildWeekBookingDates(monday: Date): GuestBookingDateOption[] {
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(monday);
    date.setDate(monday.getDate() + index);
    return {
      value: toDateParam(date),
      shortLabel: date.toLocaleDateString("ru-RU", { weekday: "short" }),
      label: date.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }),
    };
  });
}

function defaultBookingDate(dates: readonly GuestBookingDateOption[], timeZone: string) {
  const today = toDateParamInTimeZone(new Date(), timeZone);
  return dates.some((date) => date.value === today) ? today : (dates[0]?.value ?? today);
}

interface CancelableScheduleOccurrence {
  enrollment_id?: number | null;
  created_from?: string;
  can_cancel?: boolean;
}

function getCancelBookingPath(occurrence: CancelableScheduleOccurrence): string | null {
  if (!occurrence.can_cancel || !occurrence.enrollment_id) return null;
  if (occurrence.created_from === "personal_booking") {
    return `/personal-bookings/${occurrence.enrollment_id}/cancel/`;
  }
  if (occurrence.created_from === "student_self_booking") {
    return `/guest-bookings/${occurrence.enrollment_id}/cancel/`;
  }
  return null;
}

function getCancelBookingErrorMessage(error: unknown): string {
  const e = error as { response?: { data?: { code?: string } } };
  if (e?.response?.data?.code === "booking_past_date") {
    return "Эту запись уже нельзя отменить, занятие прошло";
  }
  return getApiError(error, "Не удалось отменить запись");
}

export default function ParentChild() {
  const queryClient = useQueryClient();
  const { scope: parentPrivateScope, isReady: parentPrivateReady } = usePrivateQueryScope("parent");
  const parentPrivateQueryKey = (...parts: readonly unknown[]) => [
    ...parts,
    ...parentPrivateScope,
  ];
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
  const clubId = useAuthStore((state) => state.clubId);
  const accessToken = useAuthStore((state) => state.accessToken);
  const { childId } = useParams<{ childId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const timeZone = useBrandingStore((s) => s.timeZone);
  const [extraAttendance, setExtraAttendance] = useState<AttendanceItem[]>([]);
  const [isLoadingMoreAttendance, setIsLoadingMoreAttendance] = useState(false);
  const [attendanceLoadMoreError, setAttendanceLoadMoreError] = useState(false);
  const [bookingWeekOffset, setBookingWeekOffset] = useState(0);
  const currentBookingMonday = useMemo(() => {
    const monday = getMonday(todayInTimeZone(timeZone));
    monday.setDate(monday.getDate() + bookingWeekOffset * 7);
    return monday;
  }, [bookingWeekOffset, timeZone]);
  const bookingDates = useMemo(
    () => buildWeekBookingDates(currentBookingMonday),
    [currentBookingMonday],
  );
  const [requestedBookingDate, setRequestedBookingDate] = useState(() =>
    defaultBookingDate(buildWeekBookingDates(getMonday(todayInTimeZone(timeZone))), timeZone),
  );
  const bookingDate = useMemo(() => {
    return bookingDates.some((date) => date.value === requestedBookingDate)
      ? requestedBookingDate
      : defaultBookingDate(bookingDates, timeZone);
  }, [bookingDates, requestedBookingDate, timeZone]);
  const [bookingSheetOpen, setBookingSheetOpen] = useState(false);
  const [bookedScheduleId, setBookedScheduleId] = useState<number | null>(null);
  const [bookedPersonalSlotId, setBookedPersonalSlotId] = useState<number | null>(null);
  const [createdPersonalPaymentReservation, setCreatedPersonalPaymentReservation] =
    useState<PersonalBookingPaymentReservation | null>(null);
  const [createdPersonalPaymentReservationAt, setCreatedPersonalPaymentReservationAt] =
    useState<number | null>(null);
  const [hiddenPersonalPaymentReservationIds, setHiddenPersonalPaymentReservationIds] = useState<
    number[]
  >([]);
  const [bookingErrorMessage, setBookingErrorMessage] = useState<string | null>(null);
  const [personalBookingErrorMessage, setPersonalBookingErrorMessage] = useState<string | null>(
    null,
  );
  const [cancelBookingErrorMessage, setCancelBookingErrorMessage] = useState<string | null>(
    null,
  );
  const [parentBankPaymentOrder, setParentBankPaymentOrder] =
    useState<BankPaymentOrderLink | null>(null);
  const [parentBankPaymentOrderAt, setParentBankPaymentOrderAt] = useState<number | null>(null);
  const [hiddenParentBankPaymentOrderIds, setHiddenParentBankPaymentOrderIds] =
    useState<number[]>([]);
  const [bankPaymentCancelErrorMessage, setBankPaymentCancelErrorMessage] = useState<string | null>(
    null,
  );

  const {
    data: profile,
    isLoading: loading,
    error,
    refetch,
    isFetching,
  } = useQuery<ChildProfile>({
    queryKey: parentPrivateQueryKey("parent", "child", childId),
    queryFn: () =>
      apiClient
        .get<ChildProfile>(`/parents/children/${childId}/`)
        .then((r) => r.data),
    enabled: parentPrivateReady && !!childId,
    staleTime: 5 * 60_000,
  });

  const {
    data: attendancePage = [],
    error: attendanceError,
    refetch: refetchAttendance,
    isFetching: attendanceFetching,
  } = useQuery<AttendanceItem[]>({
    queryKey: parentPrivateQueryKey("parent", "child", childId, "attendance"),
    queryFn: () =>
      apiClient
        .get<AttendanceItem[]>(`/parents/children/${childId}/attendance/`)
        .then((r) => r.data),
    enabled: parentPrivateReady && !!childId,
    staleTime: 5 * 60_000,
  });

  const childStudentId = profile?.id ?? null;
  const {
    data: pendingBankPaymentOrders = [],
    dataUpdatedAt: pendingBankPaymentOrdersUpdatedAt,
    refetch: refetchBankPaymentOrders,
  } = useQuery<BankPaymentOrderLink[]>({
    queryKey: parentPrivateQueryKey(
      "parent",
      "child",
      childId,
      "bank-payment-orders",
      "recent",
    ),
    queryFn: () =>
      apiClient
        .get<BankPaymentOrderLink[]>(
          `/parents/children/${childId}/bank-payment-orders/`,
          {
            params: { status: "recent" },
          },
        )
        .then((r) => r.data),
    enabled: parentPrivateReady && !!childId,
    staleTime: 30_000,
  });

  const {
    data: bookingOptions = [],
    isLoading: bookingOptionsLoading,
    isError: bookingOptionsError,
    refetch: refetchBookingOptions,
  } = useQuery<GuestBookingOption[]>({
    queryKey: parentPrivateQueryKey(
      "parent",
      "child",
      childId,
      "guest-booking-options",
      bookingDate,
    ),
    queryFn: () =>
      apiClient
        .get<GuestBookingOption[]>("/schedules/guest-booking-options/", {
          params: { date: bookingDate, child_student_id: childStudentId },
        })
        .then((r) => r.data),
    enabled:
      parentPrivateReady &&
      legacyPersonalAvailabilityEnabled &&
      bookingSheetOpen &&
      !!childStudentId,
    staleTime: 60_000,
  });

  const {
    data: personalOptions = [],
    isLoading: personalOptionsLoading,
    isError: personalOptionsError,
    refetch: refetchPersonalOptions,
  } = useQuery<PersonalAvailabilityOption[]>({
    queryKey: parentPrivateQueryKey(
      "parent",
      "child",
      childId,
      "personal-availability-options",
      bookingDate,
    ),
    queryFn: () =>
      apiClient
        .get<PersonalAvailabilityOption[]>("/personal-availability/options/", {
          params: { date: bookingDate, child_student_id: childStudentId },
        })
        .then((r) => r.data),
    enabled:
      parentPrivateReady &&
      legacyPersonalAvailabilityEnabled &&
      bookingSheetOpen &&
      !!childStudentId,
    staleTime: 60_000,
  });

  const {
    data: pendingPersonalPaymentReservations = [],
    isError: pendingPersonalPaymentReservationsError,
    isFetching: pendingPersonalPaymentReservationsFetching,
    dataUpdatedAt: pendingPersonalPaymentReservationsUpdatedAt,
  } = useQuery<
    PersonalBookingPaymentReservation[]
  >({
    queryKey: parentPrivateQueryKey(
      "parent",
      "child",
      childId,
      "personal-payment-reservations",
      "open_actionable",
      childStudentId,
    ),
    queryFn: () =>
      apiClient
        .get<PersonalBookingPaymentReservation[]>(
          "/personal-availability/payment-reservations/",
          {
            params: { status: "open_actionable", child_student_id: childStudentId },
          },
        )
        .then((r) => r.data),
    enabled: parentPrivateReady && legacyPersonalAvailabilityEnabled && !!childStudentId,
    staleTime: 30_000,
  });

  const bookingMutation = useMutation({
    mutationFn: (option: GuestBookingOption) => {
      if (!childStudentId) {
        throw new Error("Child student scope is not resolved");
      }
      return apiClient
        .post<GuestVisitOut>(`/schedules/${option.schedule_id}/guest-bookings/`, {
          date: option.date,
          child_student_id: childStudentId,
          idempotency_key: `parent-self-booking-${childStudentId}-${option.schedule_id}-${option.date}`,
        })
        .then((r) => r.data);
    },
    onSuccess: (booking, option) => {
      setBookedScheduleId(booking.schedule_id);
      setBookingErrorMessage(null);
      void queryClient.invalidateQueries({
        queryKey: ["parent", "child", childId, "guest-booking-options", option.date],
      });
      void queryClient.invalidateQueries({ queryKey: ["parent", "child", childId] });
    },
    onError: (error) => {
      setBookingErrorMessage(getApiError(error, "Не удалось записать ребёнка"));
    },
  });

  const personalBookingMutation = useMutation({
    mutationFn: (option: PersonalAvailabilityOption) => {
      if (!childStudentId) {
        throw new Error("Child student scope is not resolved");
      }
      return apiClient
        .post<PersonalBookingOut>(`/personal-availability/${option.slot_id}/book/`, {
          child_student_id: childStudentId,
          subscription_id: option.subscription_id,
          idempotency_key: `parent-personal-self-booking-${childStudentId}-${option.slot_id}`,
        })
        .then((r) => r.data);
    },
    onSuccess: (booking, option) => {
      setBookedPersonalSlotId(booking.availability_slot_id ?? option.slot_id);
      setPersonalBookingErrorMessage(null);
      void queryClient.invalidateQueries({
        queryKey: ["parent", "child", childId, "personal-availability-options", option.date],
      });
      void queryClient.invalidateQueries({ queryKey: ["parent", "child", childId] });
    },
    onError: (error) => {
      if (
        (error as { response?: { data?: { code?: unknown } } })?.response?.data?.code ===
        "personal_offer_changed"
      ) {
        void queryClient.invalidateQueries({
          queryKey: ["parent", "child", childId, "personal-availability-options"],
        });
      }
      setPersonalBookingErrorMessage(getPersonalBookingErrorMessage(error, "Не удалось записать ребёнка"));
    },
  });

  const personalPaymentReservationMutation = useMutation({
    mutationFn: (option: PersonalAvailabilityOption) => {
      if (!hasOnlinePaymentsCapability(paymentCapabilitiesQuery)) {
        throw new Error("Online payment capability is unavailable");
      }
      if (!childStudentId) {
        throw new Error("Child student scope is not resolved");
      }
      const tariffId = unifiedClientJourneyEnabled
        ? option.offer_tariff_id || null
        : option.payment_tariff_id;
      if (!tariffId) {
        throw new Error("Personal payment tariff is not available");
      }
      return apiClient
        .post<PersonalBookingPaymentReservation>(
          `/personal-availability/${option.slot_id}/payment-reservations/`,
          {
            child_student_id: childStudentId,
            tariff_id: tariffId,
            ...(unifiedClientJourneyEnabled ? { offer_digest: option.offer_digest } : {}),
            idempotency_key: `parent-personal-payment-reservation-${childStudentId}-${option.slot_id}-${tariffId}`,
          },
        )
        .then((r) => r.data);
    },
    onSuccess: (reservation, option) => {
      setCreatedPersonalPaymentReservation(reservation);
      setCreatedPersonalPaymentReservationAt(Date.now());
      setHiddenPersonalPaymentReservationIds((current) =>
        current.filter((id) => id !== reservation.id),
      );
      setPersonalBookingErrorMessage(null);
      void queryClient.invalidateQueries({
        queryKey: ["parent", "child", childId, "personal-availability-options", option.date],
      });
      void queryClient.invalidateQueries({
        queryKey: [
          "parent",
          "child",
          childId,
          "personal-payment-reservations",
          "open_actionable",
          childStudentId,
        ],
      });
      void queryClient.invalidateQueries({ queryKey: ["parent", "child", childId] });
    },
    onError: (error) => {
      if (
        (error as { response?: { data?: { code?: unknown } } })?.response?.data?.code ===
        "personal_offer_changed"
      ) {
        void queryClient.invalidateQueries({
          queryKey: ["parent", "child", childId, "personal-availability-options"],
        });
      }
      setPersonalBookingErrorMessage(
        getPersonalBookingErrorMessage(error, "Не удалось создать ссылку на оплату"),
      );
    },
  });

  const cancelPersonalPaymentReservationMutation = useMutation({
    mutationFn: (reservation: PersonalBookingPaymentReservation) => {
      if (!childStudentId) {
        throw new Error("Child student scope is not resolved");
      }
      return apiClient
        .post<PersonalBookingPaymentReservation>(
          `/personal-availability/payment-reservations/${reservation.id}/cancel/`,
          { child_student_id: childStudentId },
        )
        .then((r) => r.data);
    },
    onSuccess: (reservation) => {
      setHiddenPersonalPaymentReservationIds((current) =>
        current.includes(reservation.id) ? current : [...current, reservation.id],
      );
      setCreatedPersonalPaymentReservation((current) =>
        current?.id === reservation.id ? null : current,
      );
      setCreatedPersonalPaymentReservationAt((current) =>
        createdPersonalPaymentReservation?.id === reservation.id ? null : current,
      );
      setPersonalBookingErrorMessage(null);
      void queryClient.invalidateQueries({
        queryKey: ["parent", "child", childId, "personal-payment-reservations", "open_actionable"],
      });
      void queryClient.invalidateQueries({
        queryKey: ["parent", "child", childId, "personal-availability-options"],
      });
      void queryClient.invalidateQueries({ queryKey: ["parent", "child", childId] });
    },
    onError: (error) => {
      setPersonalBookingErrorMessage(getApiError(error, "Не удалось отменить ссылку"));
    },
  });

  const cancelBookingMutation = useMutation({
    mutationFn: (occurrence: CancelableScheduleOccurrence) => {
      const path = getCancelBookingPath(occurrence);
      if (!path) {
        throw new Error("Booking cannot be cancelled from this screen");
      }
      return apiClient.post(path, { reason: "" }).then((r) => r.data);
    },
    onSuccess: () => {
      setCancelBookingErrorMessage(null);
      void queryClient.invalidateQueries({ queryKey: ["parent", "child", childId] });
      void queryClient.invalidateQueries({
        queryKey: ["parent", "child", childId, "guest-booking-options"],
      });
      void queryClient.invalidateQueries({
        queryKey: ["parent", "child", childId, "personal-availability-options"],
      });
    },
    onError: (error) => {
      setCancelBookingErrorMessage(getCancelBookingErrorMessage(error));
      void queryClient.invalidateQueries({ queryKey: ["parent", "child", childId] });
    },
  });

  const bankPaymentMutation = useMutation({
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
      if (!childId) {
        throw new Error("Child route is not resolved");
      }
      if (contextualRenewalEnabled && !renewedFromSubscriptionId) {
        throw new Error("Exact renewal source is unavailable");
      }
      if (contextualRenewalEnabled && !childStudentId) {
        throw new Error("Child student scope is not resolved");
      }
      const renewalScope =
        contextualRenewalEnabled && renewedFromSubscriptionId && childStudentId
          ? {
              clubId,
              actorSubject: getAuthTokenSubject(accessToken),
              audience: "parent" as const,
              kind: "subscription_renewal" as const,
              studentId: childStudentId,
              paymentMethod: "sbp" as const,
              renewedFromSubscriptionId,
            }
          : null;
      const expectedOffer = renewalOffer
        ? getExpectedRenewalOfferFields(renewalOffer)
        : null;

      const response = await apiClient.post<BankPaymentOrderLink>(
        `/parents/children/${childId}/bank-payment-orders/`,
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
      if (contextualRenewalEnabled && childStudentId && variables.renewedFromSubscriptionId) {
        clearContextualCommercialCommandKey({
          clubId,
          actorSubject: getAuthTokenSubject(accessToken),
          audience: "parent",
          kind: "subscription_renewal",
          studentId: childStudentId,
          paymentMethod: "sbp",
          renewedFromSubscriptionId: variables.renewedFromSubscriptionId,
        });
      }
      setBankPaymentCancelErrorMessage(null);
      setParentBankPaymentOrder(order);
      setParentBankPaymentOrderAt(Date.now());
      void queryClient.invalidateQueries({ queryKey: ["parent", "child", childId] });
      void queryClient.invalidateQueries({
        queryKey: ["parent", "child", childId, "bank-payment-orders", "recent"],
      });
    },
    onError: (error, variables) => {
      if (isRenewalOfferError(error)) {
        if (contextualRenewalEnabled && childStudentId && variables.renewedFromSubscriptionId) {
          clearContextualCommercialCommandKey({
            clubId,
            actorSubject: getAuthTokenSubject(accessToken),
            audience: "parent",
            kind: "subscription_renewal",
            studentId: childStudentId,
            paymentMethod: "sbp",
            renewedFromSubscriptionId: variables.renewedFromSubscriptionId,
          });
        }
        void refetch();
      }
      setParentBankPaymentOrder(null);
    },
  });

  const cancelBankPaymentMutation = useMutation({
    mutationFn: async (order: BankPaymentOrderLink) => {
      if (!childId) {
        throw new Error("Child route is not resolved");
      }

      await apiClient.post(
        `/parents/children/${childId}/bank-payment-orders/${order.id}/cancel/`,
        {},
      );
      return order.id;
    },
    onMutate: (order) => {
      setBankPaymentCancelErrorMessage(null);
      setHiddenParentBankPaymentOrderIds((current) =>
        current.includes(order.id) ? current : [...current, order.id],
      );
    },
    onSuccess: (orderId) => {
      setBankPaymentCancelErrorMessage(null);
      setHiddenParentBankPaymentOrderIds((current) =>
        current.includes(orderId) ? current : [...current, orderId],
      );
      setParentBankPaymentOrder((current) =>
        current?.id === orderId ? null : current,
      );
      setParentBankPaymentOrderAt((current) =>
        parentBankPaymentOrder?.id === orderId ? null : current,
      );
      void queryClient.invalidateQueries({ queryKey: ["parent", "child", childId] });
      void queryClient.invalidateQueries({
        queryKey: ["parent", "child", childId, "bank-payment-orders", "recent"],
      });
    },
    onError: (error, order) => {
      setHiddenParentBankPaymentOrderIds((current) =>
        current.filter((id) => id !== order.id),
      );
      setBankPaymentCancelErrorMessage(
        getApiError(error, "Не удалось отменить продление"),
      );
    },
  });

  const refreshBankPaymentMutation = useMutation({
    mutationFn: async (order: BankPaymentOrderLink) => {
      if (!childId) throw new Error("Child route is not resolved");
      const response = await apiClient.post<BankPaymentOrderLink>(
        `/parents/children/${childId}/bank-payment-orders/${order.id}/refresh/`,
        {},
      );
      return response.data;
    },
    onSettled: () => refetchBankPaymentOrders(),
  });

  const visiblePersonalPaymentReservations = useMemo(() => {
    const hidden = new Set(hiddenPersonalPaymentReservationIds);
    const reservations = new Map<number, PersonalBookingPaymentReservation>();
    const serverVisibleReservationIds = new Set<number>();
    const append = (reservation: PersonalBookingPaymentReservation | null) => {
      if (!reservation || hidden.has(reservation.id)) return;
      if (!isVisiblePersonalPaymentReservation(reservation)) return;
      reservations.set(reservation.id, reservation);
    };

    pendingPersonalPaymentReservations.forEach((reservation) => {
      if (isVisiblePersonalPaymentReservation(reservation)) {
        serverVisibleReservationIds.add(reservation.id);
      }
      append(reservation);
    });
    const createdStillAwaitingServerSnapshot =
      createdPersonalPaymentReservationAt !== null &&
      (pendingPersonalPaymentReservationsFetching ||
        pendingPersonalPaymentReservationsUpdatedAt < createdPersonalPaymentReservationAt);
    if (
      pendingPersonalPaymentReservationsError ||
      createdStillAwaitingServerSnapshot ||
      (createdPersonalPaymentReservation &&
        serverVisibleReservationIds.has(createdPersonalPaymentReservation.id))
    ) {
      append(createdPersonalPaymentReservation);
    }
    return Array.from(reservations.values());
  }, [
    createdPersonalPaymentReservation,
    createdPersonalPaymentReservationAt,
    hiddenPersonalPaymentReservationIds,
    pendingPersonalPaymentReservations,
    pendingPersonalPaymentReservationsError,
    pendingPersonalPaymentReservationsFetching,
    pendingPersonalPaymentReservationsUpdatedAt,
  ]);

  useEffect(() => {
    setExtraAttendance([]);
    setParentBankPaymentOrder(null);
    setParentBankPaymentOrderAt(null);
    setHiddenParentBankPaymentOrderIds([]);
    setBankPaymentCancelErrorMessage(null);
    setCreatedPersonalPaymentReservation(null);
    setCreatedPersonalPaymentReservationAt(null);
    setHiddenPersonalPaymentReservationIds([]);
    setBookingWeekOffset(0);
    setRequestedBookingDate(
      defaultBookingDate(buildWeekBookingDates(getMonday(todayInTimeZone(timeZone))), timeZone),
    );
  }, [childId, parentPrivateScope, timeZone]);

  useEffect(() => {
    if (!profile || !location.hash) return;

    window.requestAnimationFrame(() => {
      document
        .getElementById(location.hash.slice(1))
        ?.scrollIntoView({ block: "start" });
    });
  }, [location.hash, profile]);

  if (!parentPrivateReady || loading) {
    return (
      <div className="space-y-4 px-5 pb-24 pt-4">
        <div className="flex items-center gap-3">
          <Skeleton className="h-6 w-6 rounded" />
          <Skeleton className="h-6 w-40 rounded" />
        </div>
        <Skeleton className="h-[100px] rounded-xl" />
        <Skeleton className="h-[80px] rounded-xl" />
        <Skeleton className="h-[80px] rounded-xl" />
      </div>
    );
  }

  if (error || !profile) {
    return (
      <div className="space-y-4 px-5 pb-24 pt-4">
        <button
          onClick={() => navigate("/parent")}
          className="flex items-center gap-2 text-[14px] min-h-[44px]"
        >
          <ArrowLeft className="w-5 h-5" />
          Назад
        </button>
        <p className="ui-error-14">
          {error ? "Не удалось загрузить профиль" : "Профиль не найден"}
        </p>
        {error ? (
          <ParentSurfaceCard>
            <div className="space-y-3 p-5">
              <p className="ui-body-muted">
                Попробуйте повторить загрузку профиля ребёнка.
              </p>
              <Button
                type="button"
                className="min-h-[44px] w-full"
                onClick={() => void refetch()}
                disabled={isFetching}
              >
                <RotateCcw />
                Повторить
              </Button>
            </div>
          </ParentSurfaceCard>
        ) : null}
      </div>
    );
  }

  const displayName = `${profile.first_name} ${profile.last_name}`.trim();
  const statusLabel = STATUS_LABELS[profile.status] ?? profile.status;
  const activeSubscriptions =
    profile.active_subscriptions && profile.active_subscriptions.length > 0
      ? profile.active_subscriptions
      : profile.active_subscription
        ? [profile.active_subscription]
        : [];
  const activeSubscription = activeSubscriptions[0] ?? profile.active_subscription;
  const livePendingBankPaymentOrders = pendingBankPaymentOrders.filter(isPayableBankPaymentOrder);
  const recentTerminalBankPaymentOrders = pendingBankPaymentOrders.filter(
    (order) => !isPayableBankPaymentOrder(order),
  );
  const serverLiveBankPaymentOrderIds = new Set(
    livePendingBankPaymentOrders.map((order) => order.id),
  );
  const localBankPaymentOrderStillAwaitingServerSnapshot =
    parentBankPaymentOrderAt !== null &&
    pendingBankPaymentOrdersUpdatedAt <= parentBankPaymentOrderAt;
  const parentBankPaymentOrders = [
    ...(parentBankPaymentOrder &&
    isPayableBankPaymentOrder(parentBankPaymentOrder) &&
    (localBankPaymentOrderStillAwaitingServerSnapshot ||
      serverLiveBankPaymentOrderIds.has(parentBankPaymentOrder.id))
      ? [parentBankPaymentOrder]
      : []),
    ...livePendingBankPaymentOrders,
    ...recentTerminalBankPaymentOrders,
  ].filter((order) => !hiddenParentBankPaymentOrderIds.includes(order.id));
  const attendance = [...attendancePage, ...extraAttendance];
  const subscriptionBadgeLabel = getSubscriptionBadgeLabel(activeSubscriptions);

  async function handleAttendanceRetry() {
    setExtraAttendance([]);
    setAttendanceLoadMoreError(false);
    await refetchAttendance();
  }

  async function handleAttendanceLoadMore() {
    if (!childId || isLoadingMoreAttendance) return;

    setIsLoadingMoreAttendance(true);
    setAttendanceLoadMoreError(false);
    try {
      const response = await apiClient.get<AttendanceItem[]>(
        `/parents/children/${childId}/attendance/?limit=${ATTENDANCE_PAGE_SIZE}&offset=${attendance.length}`,
      );
      setExtraAttendance((current) => [...current, ...response.data]);
    } catch {
      setAttendanceLoadMoreError(true);
    } finally {
      setIsLoadingMoreAttendance(false);
    }
  }

  return (
    <div className="space-y-4 px-5 pb-24 pt-4">
      <div className="parent-section-enter flex items-center gap-3">
        <button
          onClick={() => navigate("/parent")}
          className="min-h-[44px] min-w-[44px] flex items-center justify-center -ml-2"
          aria-label="Назад"
        >
          <ArrowLeft className="w-5 h-5" />
        </button>
        <ParentPageIntro
          eyebrow="Профиль ребёнка"
          title="Обзор ребёнка"
          description="Абонемент, прогресс, группы и посещения ребёнка."
          className="flex-1"
        />
      </div>
      <ParentSurfaceCard className="parent-section-enter parent-delay-1 relative overflow-hidden p-4.5">
        <div
          className="absolute inset-x-0 top-0 h-1.5"
          style={{ backgroundColor: "var(--branding-accent)" }}
        />
        <CardContent className="p-0">
          <div className="flex items-start gap-4">
            <div
              className="flex h-16 w-16 shrink-0 items-center justify-center rounded-2xl text-[22px] font-semibold text-white shadow-sm"
              style={{ backgroundColor: "var(--branding-accent)" }}
            >
              {displayName ? getInitials(displayName) : <UserRound className="size-7" />}
            </div>
            <div className="min-w-0 flex-1">
              <p className="ui-overline">
                Обзор
              </p>
              <h2 className="portal-balanced-title mt-1 text-[26px] font-semibold leading-tight text-foreground">
                {displayName || "Ребёнок"}
              </h2>
              <div className="mt-3 flex flex-wrap gap-2">
                <span className="inline-flex items-center rounded-full bg-[var(--branding-accent)]/10 px-3 py-1 text-[12px] font-medium text-foreground">
                  {statusLabel}
                </span>
                <span className="inline-flex items-center rounded-full bg-black/5 px-3 py-1 text-[12px] font-medium text-foreground">
                  {subscriptionBadgeLabel}
                </span>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                {legacyPersonalAvailabilityEnabled ? (
                  <button
                  type="button"
                  className="inline-flex min-h-[44px] items-center gap-2 rounded-2xl bg-[var(--branding-accent)] px-3 text-[13px] font-semibold text-white shadow-sm transition active:scale-[0.99]"
                  onClick={() => setBookingSheetOpen(true)}
                >
                  <CalendarPlus className="h-4 w-4" />
                  Записаться
                </button>
                ) : null}
                <Link
                  to={`/parent/child/${profile.id}/feedback`}
                  className="inline-flex min-h-[44px] items-center gap-2 rounded-2xl bg-black/[0.04] px-3 text-[13px] font-semibold text-foreground ring-1 ring-black/6 transition active:scale-[0.99]"
                >
                  <MessageSquareText className="h-4 w-4" />
                  Открыть опрос
                </Link>
              </div>
            </div>
          </div>
        </CardContent>
      </ParentSurfaceCard>
      {unifiedClientJourneyEnabled || legacyPersonalAvailabilityEnabled ? (
        <SelfServicePersonalCommandCards
          scope={{ audience: "parent", childStudentId }}
          enabled={Boolean(childStudentId)}
          onlinePaymentsEnabled={hasOnlinePaymentsCapability(paymentCapabilitiesQuery)}
          childName={displayName || "Ребёнок"}
          className="parent-section-enter parent-delay-1"
          hideWhenEmpty={!unifiedClientJourneyEnabled}
        />
      ) : null}
      {legacyPersonalAvailabilityEnabled && visiblePersonalPaymentReservations.length > 0 ? (
        <section
          className="parent-section-enter parent-delay-1 space-y-2"
          aria-label="Ожидающие оплаты персональные тренировки"
        >
          {visiblePersonalPaymentReservations.map((reservation) => (
            <PersonalPaymentReservationLinkPanel
              key={reservation.id}
              reservation={reservation}
              className="bg-white/95 shadow-sm"
              cancelingPaymentReservationId={
                cancelPersonalPaymentReservationMutation.isPending
                  ? cancelPersonalPaymentReservationMutation.variables?.id
                  : null
              }
              onPaymentReservationCancel={(item) =>
                cancelPersonalPaymentReservationMutation.mutate(item)
              }
            />
          ))}
        </section>
      ) : null}
      <ChildDetail
        activeSubscription={activeSubscription}
        activeSubscriptions={activeSubscriptions}
        openDebts={profile.open_debts ?? []}
        financialState={profile.financial_state}
        onlinePaymentOrders={parentBankPaymentOrders}
        onlinePaymentErrorMessage={
          bankPaymentMutation.isError
            ? getRenewalOfferErrorMessage(bankPaymentMutation.error, "Не удалось создать ссылку")
            : bankPaymentCancelErrorMessage
        }
        onCreateBankPaymentOrder={(input) => bankPaymentMutation.mutate(input)}
        isCreatingBankPaymentOrder={bankPaymentMutation.isPending}
        onCancelBankPaymentOrder={(order) => cancelBankPaymentMutation.mutate(order)}
        onRefreshBankPaymentOrders={(order) => refreshBankPaymentMutation.mutate(order)}
        onRefetchBankPaymentOrders={() => void refetchBankPaymentOrders()}
        isRefreshingBankPaymentOrder={refreshBankPaymentMutation.isPending}
        isCancelingBankPaymentOrder={cancelBankPaymentMutation.isPending}
        onlinePaymentsEnabled={hasOnlinePaymentsCapability(paymentCapabilitiesQuery)}
        renewalMode={unifiedJourneyMode}
        onlinePaymentUnavailableMessage={getOnlinePaymentUnavailableMessage(
          paymentCapabilitiesQuery,
          "self_service",
        )}
        documentChecklist={normalizeChecklistItems(profile.document_checklist ?? [])}
        gradeProgress={profile.grade_progress}
        schedule={profile.schedule}
        attendance={attendance}
        attendanceCount={profile.attendance_count}
        attendanceError={Boolean(attendanceError)}
        onAttendanceRetry={() => void handleAttendanceRetry()}
        isAttendanceRetrying={attendanceFetching}
        canLoadMoreAttendance={attendance.length < profile.attendance_count}
        onAttendanceLoadMore={() => void handleAttendanceLoadMore()}
        isAttendanceLoadingMore={isLoadingMoreAttendance}
        attendanceLoadMoreError={attendanceLoadMoreError}
        onCancelBooking={(occurrence) => cancelBookingMutation.mutate(occurrence)}
        cancelingEnrollmentId={
          cancelBookingMutation.isPending
            ? (cancelBookingMutation.variables?.enrollment_id ?? null)
            : null
        }
        cancelBookingErrorMessage={cancelBookingErrorMessage}
        bookingSection={
          unifiedClientJourneyEnabled ? (
            <SelfServicePersonalBookingSection
              scope={{ audience: "parent", childStudentId }}
              enabled={Boolean(childStudentId)}
              onlinePaymentsEnabled={hasOnlinePaymentsCapability(paymentCapabilitiesQuery)}
            />
          ) : legacyPersonalAvailabilityEnabled ? (
          <div className="space-y-3">
            <Button
              type="button"
              className="min-h-[44px] w-full justify-center rounded-2xl"
              onClick={() => setBookingSheetOpen(true)}
            >
              <CalendarPlus className="size-4" />
              Записать ребёнка
            </Button>
            <GuestSelfBookingSheet
              open={bookingSheetOpen}
              onOpenChange={setBookingSheetOpen}
              subjectName={displayName || "Ребёнок"}
              ariaLabel="Запись ребёнка на групповую тренировку"
              eyebrow="Запись"
              title="Записать ребёнка на тренировку"
              description="Выберите группу или персональный слот. Персоналка без абонемента сначала удерживается ссылкой на оплату."
              dateOptions={bookingDates}
              selectedDate={bookingDate}
              onDateChange={(date) => {
                setBookedScheduleId(null);
                setBookedPersonalSlotId(null);
                setBookingErrorMessage(null);
                setPersonalBookingErrorMessage(null);
                setRequestedBookingDate(date);
              }}
              dateRangeLabel={formatWeekLabel(currentBookingMonday)}
              onPreviousDateRange={() => setBookingWeekOffset((o) => o - 1)}
              onNextDateRange={() => setBookingWeekOffset((o) => o + 1)}
              onCurrentDateRange={() => setBookingWeekOffset(0)}
              options={bookingOptions}
              isLoading={bookingOptionsLoading}
              isError={bookingOptionsError}
              onRetry={() => void refetchBookingOptions()}
              onBook={(option) => bookingMutation.mutate(option)}
              pendingScheduleId={
                bookingMutation.isPending ? bookingMutation.variables?.schedule_id : null
              }
              bookedScheduleId={bookedScheduleId}
              errorMessage={bookingErrorMessage}
              personalOptions={personalOptions}
              personalIsLoading={personalOptionsLoading}
              personalIsError={personalOptionsError}
              onPersonalRetry={() => void refetchPersonalOptions()}
              personalPaymentReservationsError={pendingPersonalPaymentReservationsError}
              onlinePaymentsEnabled={hasOnlinePaymentsCapability(paymentCapabilitiesQuery)}
              unifiedClientJourneyEnabled={unifiedClientJourneyEnabled}
              onPersonalBook={(option) => {
                if (option.booking_status === "can_pay") {
                  personalPaymentReservationMutation.mutate(option);
                  return;
                }
                personalBookingMutation.mutate(option);
              }}
              personalPaymentReservations={visiblePersonalPaymentReservations}
              onPersonalPaymentReservationCancel={(reservation) =>
                cancelPersonalPaymentReservationMutation.mutate(reservation)
              }
              pendingPersonalSlotId={
                personalBookingMutation.isPending
                  ? personalBookingMutation.variables?.slot_id
                  : null
              }
              pendingPersonalPaymentSlotId={
                personalPaymentReservationMutation.isPending
                  ? personalPaymentReservationMutation.variables?.slot_id
                  : null
              }
              bookedPersonalSlotId={bookedPersonalSlotId}
              cancelingPersonalPaymentReservationId={
                cancelPersonalPaymentReservationMutation.isPending
                  ? cancelPersonalPaymentReservationMutation.variables?.id
                  : null
              }
              personalErrorMessage={personalBookingErrorMessage}
            />
          </div>
          ) : (
            <SelfServicePersonalUnavailableNotice
              isError={
                personalAvailabilityCapability.isError || personalAvailabilityCapability.isRefetchError
              }
            />
          )
        }
      />
    </div>
  );
}

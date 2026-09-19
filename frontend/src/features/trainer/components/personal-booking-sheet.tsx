import { useMemo, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  OnlinePaymentLinkPanel,
  type BankPaymentOrderLink,
} from "@/components/portal/online-payment-link-panel";
import apiClient from "@/api/custom-fetch";
import {
  getOnlinePaymentUnavailableMessage,
  hasOnlinePaymentsCapability,
  usePaymentCapabilities,
} from "@/api/payment-capabilities";
import {
  getPersonalAvailabilityCapabilityMode,
  getPersonalStaffCommandProtocol,
  usePersonalAvailabilityCapabilityQuery,
} from "@/api/unified-client-journey";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import { toDateParamInTimeZone, toDateTimeParamInTimeZone } from "@/lib/club-date";
import { formatRub, getApiError } from "@/lib/utils";
import type {
  StudentSubscription,
  SubscriptionBookingEntitlement,
} from "../types";
import { normalizeStudentSubscriptionsPayload } from "../lib/subscriptions";
import {
  getDirectPersonalOffer,
  listTrainerAvailability,
  normalizePersonalOffer,
  directPersonalOfferQueryKey,
  fixedPersonalOfferQueryKey,
  previewTrainerPersonalOffer,
  type PersonalOffer,
} from "../lib/personal-availability";
import {
  PersonalCommercialReceiptCard,
} from "./personal-commercial-context";
import {
  createDirectPersonalStaffIntent,
  createPersonalStaffIntent,
  leadPersonalCommercialContextQueryKey,
  personalCommercialContextQueryKey,
  type PersonalCommercialReceipt,
  type PersonalStaffIntentPaymentMethod,
  useCommercialCacheScope,
} from "./personal-commercial-context-api";
import { useTrainerIdentity } from "../hooks/use-trainer-identity";

interface LocationItem {
  id: number;
  name: string;
}

interface TrainingTypeItem {
  id: number;
  name: string;
  slug: string;
  kind: string;
  is_active: boolean;
}

interface TariffItem {
  id: number;
  name: string;
  price: number;
  training_type: TrainingTypeItem;
  trainings_limit: number | null;
  duration_days: number;
  scope?: string;
  location_id?: number | null;
  is_active: boolean;
}

interface PersonalBookingDiscount {
  id: number;
  name: string;
  discount_type: "percent" | "fixed";
  value: string | number;
  is_active: boolean;
}

interface PersonalBookingPaymentReservation {
  id: number;
  tariff_id: number;
  tariff_name: string;
  availability_slot_id?: number | null;
  starts_at: string;
  ends_at: string;
  status: string;
  expires_at: string;
  bank_payment_order_id: number | null;
  subscription_id: number | null;
  provider_payment_url: string;
  amount_snapshot: string | number;
  order_status: string;
  can_cancel: boolean;
}

export type PersonalBookingMode =
  | "entitlement"
  | "pay_at_club"
  | "online"
  | "cash"
  | "transfer"
  | "sbp"
  | "pay_at_visit";

export interface FixedPersonalSlotContext {
  slotId: number;
  startsAt: string;
  endsAt: string;
  /** Exact trainer that owns the slot; absent only for older callers. */
  trainerId?: number;
  trainerName: string;
  locationId: number;
  locationName: string;
  trainingTypeId: number;
  trainingTypeName: string;
  offerTariffId?: number | null;
  offerTariffName?: string;
  offerPrice?: string | number | null;
  offerDigest?: string;
  offerErrorCode?: string;
}

export interface PersonalBookingSuccess {
  mode: Exclude<PersonalBookingMode, "online" | "sbp">;
  startsAt: string;
  endsAt: string;
  price: number | string | null;
}

interface PersonalBookingFormProps {
  studentId: number;
  studentName: string;
  subscriptions?: StudentSubscription[];
  fixedSlot?: FixedPersonalSlotContext;
  retryBankPaymentReceipt?: PersonalCommercialReceipt | null;
  onStaffIntentCreated?: (receipt: PersonalCommercialReceipt) => void;
  onBooked?: (success: PersonalBookingSuccess) => void;
  onBack?: () => void;
  onClose?: () => void;
}

interface PersonalBookingSheetProps extends PersonalBookingFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const SELECT_CLASS =
  "flex min-h-[44px] min-w-0 w-full max-w-full rounded-lg border border-input bg-background px-3 py-2 text-base ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 md:text-sm";

function isPersonalKind(kind?: string) {
  return kind === "personal";
}

function todayInputValue(timeZone: string) {
  return toDateParamInTimeZone(new Date(), timeZone);
}

function hasUsableCredits(creditsLeft: number | null | undefined) {
  return creditsLeft == null || creditsLeft > 0;
}

function formatDiscountValue(discount: PersonalBookingDiscount) {
  if (discount.discount_type === "percent") return `${discount.value}%`;
  return formatRub(discount.value);
}

function personalOfferBaseAmount(offer: PersonalOffer | null) {
  return offer?.baseAmount ?? offer?.price ?? null;
}

function personalOfferPayableAmount(offer: PersonalOffer | null) {
  return offer?.payableAmount ?? offer?.price ?? null;
}

function trainerDisplayName(
  trainer: { first_name?: string; last_name?: string } | undefined,
) {
  return [trainer?.first_name, trainer?.last_name].filter(Boolean).join(" ");
}

interface BookingEligibilityContext {
  bookingDate: string;
  locationId: number | "";
  timeZone: string;
}

function isActiveOnBookingDate(
  expiresAt: string | null,
  bookingDate: string,
  timeZone: string,
) {
  return (
    !expiresAt ||
    toDateParamInTimeZone(new Date(expiresAt), timeZone) >= bookingDate
  );
}

function isEligibleAtLocation(
  scope: string | undefined,
  entitlementLocationId: number | null | undefined,
  bookingLocationId: number | "",
) {
  return (
    scope !== "location" ||
    (bookingLocationId !== "" && entitlementLocationId === bookingLocationId)
  );
}

function hasWeeklyCapacity(
  entitlement: SubscriptionBookingEntitlement,
  subscription: StudentSubscription,
  bookingDate: string,
) {
  if (entitlement.weekly_limit == null) return true;
  return (
    subscription.booking_date === bookingDate &&
    entitlement.weekly_used != null &&
    entitlement.weekly_used < entitlement.weekly_limit
  );
}

function usablePersonalEntitlements(
  subscription: StudentSubscription,
  context: BookingEligibilityContext,
  requiredTrainingTypeId?: number,
) {
  if (
    subscription.status !== "active" ||
    !isActiveOnBookingDate(
      subscription.expires_at,
      context.bookingDate,
      context.timeZone,
    )
  ) {
    return [];
  }

  const bookingEntitlements = subscription.booking_entitlements ?? [];
  const hasComponentEntitlements = subscription.has_components || bookingEntitlements.length > 0;
  if (hasComponentEntitlements) {
    return bookingEntitlements.filter(
      (entitlement) =>
        isPersonalKind(entitlement.training_type_kind) &&
        hasUsableCredits(entitlement.credits_left) &&
        isEligibleAtLocation(
          entitlement.scope,
          entitlement.location_id,
          context.locationId,
        ) &&
        hasWeeklyCapacity(entitlement, subscription, context.bookingDate) &&
        (requiredTrainingTypeId === undefined ||
          entitlement.training_type_id === requiredTrainingTypeId),
    );
  }

  if (
    !hasUsableCredits(subscription.trainings_left) ||
    !isPersonalKind(subscription.training_type_kind) ||
    !subscription.training_type_id ||
    !isEligibleAtLocation(
      subscription.scope,
      subscription.location_id,
      context.locationId,
    ) ||
    (requiredTrainingTypeId !== undefined &&
      subscription.training_type_id !== requiredTrainingTypeId)
  ) {
    return [];
  }

  return [
    {
      training_type_id: subscription.training_type_id,
      training_type_name: subscription.training_type_name ?? "",
      training_type_kind: subscription.training_type_kind ?? "",
      credits_left: subscription.trainings_left,
      weekly_limit: null,
      weekly_used: null,
      scope: "club",
      location_id: null,
    },
  ];
}

function isUsablePersonalSubscription(
  subscription: StudentSubscription,
  context: BookingEligibilityContext,
  requiredTrainingTypeId?: number,
) {
  return usablePersonalEntitlements(subscription, context, requiredTrainingTypeId).length > 0;
}

function suppliedSubscriptionsNeedBookingRefresh(
  subscriptions: StudentSubscription[] | undefined,
  bookingDate: string,
) {
  return Boolean(
    subscriptions?.some(
      (subscription) =>
        subscription.booking_date !== bookingDate &&
        subscription.booking_entitlements?.some(
          (entitlement) => entitlement.weekly_limit != null,
        ),
    ),
  );
}

let idempotencySequence = 0;

function createIdempotencyKey(prefix: string) {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return `${prefix}:${uuid}`;
  idempotencySequence += 1;
  return `${prefix}:${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${idempotencySequence}`;
}

function isVisiblePersonalPaymentReservation(reservation: PersonalBookingPaymentReservation) {
  return (
    reservation.status === "manual_review" ||
    (reservation.status === "pending_payment" && reservation.bank_payment_order_id !== null)
  );
}

const terminalBankRetryStatuses = new Set(["cancelled", "rejected", "failed", "expired"]);

function isTerminalBankRetryReceipt(
  receipt: PersonalCommercialReceipt | null,
): receipt is PersonalCommercialReceipt {
  return Boolean(
    receipt &&
      receipt.payment_method === "sbp" &&
      terminalBankRetryStatuses.has(receipt.status) &&
      receipt.allowed_actions.includes("retry_bank_payment"),
  );
}

function isTerminalRetryOriginalReservation(
  reservation: PersonalBookingPaymentReservation,
  receipt: PersonalCommercialReceipt | null,
) {
  if (!isTerminalBankRetryReceipt(receipt)) return false;

  const matchesReservation =
    Number.isSafeInteger(receipt.reservation_id) &&
    receipt.reservation_id! > 0 &&
    reservation.id === receipt.reservation_id;
  const matchesBankOrder =
    Number.isSafeInteger(receipt.bank_payment_order_id) &&
    receipt.bank_payment_order_id! > 0 &&
    reservation.bank_payment_order_id === receipt.bank_payment_order_id;
  return matchesReservation || matchesBankOrder;
}

function StaffPersonalBookingReservationPanel({
  reservation,
  commercialCacheScope,
  isCanceling,
  onCancel,
}: {
  reservation: PersonalBookingPaymentReservation;
  commercialCacheScope: ReturnType<typeof useCommercialCacheScope>;
  isCanceling: boolean;
  onCancel: () => void;
}) {
  const orderQuery = useQuery<BankPaymentOrderLink>({
    queryKey: [
      "billing",
      "bank-payment-orders",
      "personal-booking",
      reservation.bank_payment_order_id,
      ...commercialCacheScope,
    ],
    queryFn: () =>
      apiClient
        .get<BankPaymentOrderLink>(`/billing/bank-payment-orders/${reservation.bank_payment_order_id}/`)
        .then((response) => response.data),
    enabled: reservation.bank_payment_order_id !== null,
    staleTime: 0,
  });

  if (reservation.status === "manual_review" && reservation.bank_payment_order_id === null) {
    return (
      <div className="rounded-xl bg-amber-50 p-3 text-[13px] leading-5 text-amber-950">
        <p className="font-semibold">Оплата под защищённой проверкой</p>
        <p className="mt-1">Не создавайте новую ссылку: клуб завершит сверку по защищённым данным банка.</p>
      </div>
    );
  }

  if (!orderQuery.data) {
    if (orderQuery.isError) {
      return (
        <div role="status" className="ui-warning-dark">
          <p>Не удалось проверить статус оплаты.</p>
          <Button type="button" variant="outline" className="mt-2 min-h-[44px]" onClick={() => void orderQuery.refetch()}>
            Повторить проверку
          </Button>
        </div>
      );
    }
    return <p role="status" className="ui-muted-status">Проверяем статус оплаты…</p>;
  }

  return (
    <OnlinePaymentLinkPanel
      order={orderQuery.data}
      title={
        reservation.status === "manual_review"
          ? "Оплата под защищённой проверкой"
          : "Оплата ожидает подтверждения"
      }
      subtitle={
        reservation.status === "manual_review"
          ? "Не создавайте новую ссылку: клуб завершит сверку по защищённым данным банка."
          : "Запись появится в расписании после подтверждения оплаты."
      }
      cancelLabel="Отменить ссылку"
      isCanceling={isCanceling}
      isRefreshing={orderQuery.isFetching}
      onRefresh={() => void orderQuery.refetch()}
      onRequestRefresh={() => {
        void apiClient
          .post(`/billing/bank-payment-orders/${orderQuery.data.id}/refresh/`, {})
          .finally(() => orderQuery.refetch());
      }}
      onCancel={onCancel}
    />
  );
}

function apiErrorMessage(error: unknown, fallback: string) {
  const code =
    error && typeof error === "object"
      ? (error as { response?: { data?: { code?: unknown } } }).response?.data?.code
      : undefined;
  if (
    code === "personal_drop_in_tariff_invalid" ||
    code === "single_session_tariff_invalid" ||
    code === "personal_booking_tariff_not_configured" ||
    code === "personal_booking_tariff_ambiguous"
  ) {
    return "Разовая персоналка настроена не полностью. Попросите владельца проверить тариф, цену и ставку тренера.";
  }
  if (code === "personal_offer_changed") {
    return "Цена или условия персоналки изменились. Проверьте обновлённую сумму и повторите действие.";
  }
  if (
    code === "pending_payment_exists" ||
    code === "personal_payment_reservation_pending_exists" ||
    code === "bank_payment_order_pending_exists" ||
    code === "debt_payment_pending"
  ) {
    return "Оплата ожидает подтверждения. Завершите или отмените текущую попытку.";
  }
  return getApiError(error, fallback);
}

function toLocalDateTimeParts(value: string) {
  const match = value.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
  return match ? { date: match[1], time: match[2] } : { date: "", time: "" };
}

interface DateTimeParts {
  readonly date: string;
  readonly time: string;
}

function hasExplicitTimeZone(value: string) {
  return /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
}

function isSupportedTimeZone(timeZone: string) {
  if (!timeZone) return false;
  try {
    new Intl.DateTimeFormat("en-CA", { timeZone }).format();
    return true;
  } catch {
    return false;
  }
}

/**
 * Receipt instants are persisted in UTC/offset form. A retry must rebuild the
 * direct-offer request from the club wall time, never from the browser zone.
 */
function toClubDateTimeParts(value: string, timeZone: string): DateTimeParts | null {
  if (!hasExplicitTimeZone(value) || !isSupportedTimeZone(timeZone)) return null;

  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return null;

  const match = toDateTimeParamInTimeZone(instant, timeZone).match(
    /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/,
  );
  return match ? { date: match[1], time: match[2] } : null;
}

function hasExactDirectRetryContext(
  receipt: PersonalCommercialReceipt | null,
  startsAt: DateTimeParts | null,
  endsAt: DateTimeParts | null,
): receipt is PersonalCommercialReceipt & {
  readonly trainer_id: number;
  readonly starts_at: string;
  readonly ends_at: string;
  readonly location_id: number;
  readonly training_type_id: number;
} {
  return Boolean(
    receipt &&
      Number.isSafeInteger(receipt.trainer_id) &&
      receipt.trainer_id > 0 &&
      typeof receipt.starts_at === "string" &&
      hasExplicitTimeZone(receipt.starts_at) &&
      typeof receipt.ends_at === "string" &&
      hasExplicitTimeZone(receipt.ends_at) &&
      startsAt !== null &&
      endsAt !== null &&
      Number.isSafeInteger(receipt.location_id) &&
      receipt.location_id > 0 &&
      Number.isSafeInteger(receipt.training_type_id) &&
      receipt.training_type_id > 0,
  );
}

function formatSlotContext(slot: FixedPersonalSlotContext, timeZone: string) {
  const start = toClubDateTimeParts(slot.startsAt, timeZone) ?? toLocalDateTimeParts(slot.startsAt);
  const end = toClubDateTimeParts(slot.endsAt, timeZone) ?? toLocalDateTimeParts(slot.endsAt);
  return `${start.date} · ${start.time}–${end.time} · ${slot.locationName}`;
}

function fixedSlotOffer(slot: FixedPersonalSlotContext): PersonalOffer | null {
  if (!slot.offerTariffId || !slot.offerTariffName || slot.offerPrice == null) {
    return slot.offerErrorCode ? { errorCode: slot.offerErrorCode } : null;
  }
  return {
    tariffId: slot.offerTariffId,
    tariffName: slot.offerTariffName,
    price: slot.offerPrice,
    baseAmount: slot.offerPrice,
    discountAmount: 0,
    payableAmount: slot.offerPrice,
    digest: slot.offerDigest,
    errorCode: slot.offerErrorCode,
  };
}

function fixedSlotContextFromAvailability(slot: {
  id: number;
  starts_at: string;
  ends_at: string;
  trainer_id: number;
  trainer_name: string;
  location_id: number;
  location_name: string;
  training_type_id: number;
  training_type_name: string;
  offer_tariff_id?: number | null;
  offer_tariff_name?: string;
  offer_price?: string | number | null;
  offer_digest?: string;
  offer_error_code?: string;
}): FixedPersonalSlotContext {
  return {
    slotId: slot.id,
    startsAt: slot.starts_at,
    endsAt: slot.ends_at,
    trainerId: slot.trainer_id,
    trainerName: slot.trainer_name,
    locationId: slot.location_id,
    locationName: slot.location_name,
    trainingTypeId: slot.training_type_id,
    trainingTypeName: slot.training_type_name,
    offerTariffId: slot.offer_tariff_id,
    offerTariffName: slot.offer_tariff_name,
    offerPrice: slot.offer_price,
    offerDigest: slot.offer_digest,
    offerErrorCode: slot.offer_error_code,
  };
}

export function PersonalBookingForm(props: PersonalBookingFormProps) {
  const clubId = useAuthStore((state) => state.clubId);
  const timeZone = useBrandingStore((state) => state.timeZone);
  const timeZoneStatus = useBrandingStore((state) => state.timeZoneStatus);
  const timeZoneClubId = useBrandingStore((state) => state.timeZoneClubId);
  const isTimeZoneAuthoritative = useBrandingStore(
    (state) => state.isTimeZoneAuthoritative,
  );
  const isBankPaymentRetry = props.retryBankPaymentReceipt !== null && props.retryBankPaymentReceipt !== undefined;
  const hasAuthoritativeRetryTimeZone =
    isTimeZoneAuthoritative && clubId !== null && timeZoneClubId === clubId;
  const retrySlotId = props.retryBankPaymentReceipt?.slot_id ?? null;
  const retryDate = props.retryBankPaymentReceipt?.starts_at
    ? toClubDateTimeParts(props.retryBankPaymentReceipt.starts_at, timeZone)?.date ?? null
    : null;
  const retrySlotQuery = useQuery({
    queryKey: ["trainer", "availability", "retry-bank-payment", retrySlotId, retryDate, timeZone],
    queryFn: async () => {
      const slots = await listTrainerAvailability({ dateFrom: retryDate!, dateTo: retryDate! });
      return slots.find((slot) => slot.id === retrySlotId) ?? null;
    },
    enabled:
      retrySlotId !== null &&
      retryDate !== null &&
      (!isBankPaymentRetry || hasAuthoritativeRetryTimeZone),
    staleTime: 0,
  });
  if (isBankPaymentRetry && !hasAuthoritativeRetryTimeZone) {
    return (
      <p role="status" className={timeZoneStatus === "failed" ? "ui-warning" : "ui-muted-14"}>
        {timeZoneStatus === "failed"
          ? "Не удалось получить часовой пояс клуба для повторной оплаты. Обновите страницу."
          : "Проверяем часовой пояс клуба для повторной оплаты..."}
      </p>
    );
  }
  if (retrySlotId !== null && retrySlotQuery.isLoading) {
    return <p role="status" className="ui-muted-14">Проверяем актуальную цену СБП...</p>;
  }
  if (retrySlotId !== null && (!retrySlotQuery.data || retrySlotQuery.isError)) {
    return (
      <p role="status" className="ui-warning">
        Не удалось подтвердить актуальный слот для повторной оплаты. Обновите карточку заявки.
      </p>
    );
  }
  const fixedSlot =
    props.fixedSlot ??
    (retrySlotQuery.data ? fixedSlotContextFromAvailability(retrySlotQuery.data) : undefined);
  const resetKey = [
    props.studentId,
    fixedSlot?.slotId ?? "direct",
    fixedSlot?.startsAt ?? props.retryBankPaymentReceipt?.starts_at ?? "",
    isBankPaymentRetry ? timeZone : "",
  ].join(":");
  return <PersonalBookingFormContent key={resetKey} {...props} fixedSlot={fixedSlot} />;
}

function PersonalBookingFormContent({
  studentId,
  studentName,
  subscriptions: suppliedSubscriptions,
  fixedSlot,
  retryBankPaymentReceipt = null,
  onStaffIntentCreated,
  onBooked,
  onBack,
  onClose,
}: PersonalBookingFormProps) {
  const queryClient = useQueryClient();
  const commercialCacheScope = useCommercialCacheScope();
  const paymentCapabilitiesQuery = usePaymentCapabilities();
  const personalAvailabilityCapability = usePersonalAvailabilityCapabilityQuery();
  const trainerIdentityQuery = useTrainerIdentity();
  const personalAvailabilityCapabilityMode = getPersonalAvailabilityCapabilityMode(
    personalAvailabilityCapability,
  );
  const personalStaffCommandProtocol = getPersonalStaffCommandProtocol(
    personalAvailabilityCapability,
  );
  const unifiedClientJourneyEnabled = personalAvailabilityCapabilityMode === "unified";
  const isV2PersonalStaffCommand =
    unifiedClientJourneyEnabled && personalStaffCommandProtocol === "v2";
  const timeZone = useBrandingStore((s) => s.timeZone);
  const isBankPaymentRetry = retryBankPaymentReceipt !== null;
  const defaultDate = todayInputValue(timeZone);
  const fixedStart = fixedSlot
    ? toClubDateTimeParts(fixedSlot.startsAt, timeZone) ??
      (isBankPaymentRetry ? null : toLocalDateTimeParts(fixedSlot.startsAt))
    : null;
  const fixedEnd = fixedSlot
    ? toClubDateTimeParts(fixedSlot.endsAt, timeZone) ??
      (isBankPaymentRetry ? null : toLocalDateTimeParts(fixedSlot.endsAt))
    : null;
  const retryDirectContext = retryBankPaymentReceipt && !retryBankPaymentReceipt.slot_id
    ? retryBankPaymentReceipt
    : null;
  const retryStart = retryDirectContext?.starts_at
    ? toClubDateTimeParts(retryDirectContext.starts_at, timeZone)
    : null;
  const retryEnd = retryDirectContext?.ends_at
    ? toClubDateTimeParts(retryDirectContext.ends_at, timeZone)
    : null;
  const hasDirectRetryContext = hasExactDirectRetryContext(
    retryDirectContext,
    retryStart,
    retryEnd,
  );
  const [date, setDate] = useState(fixedStart?.date ?? retryStart?.date ?? defaultDate);
  const [startTime, setStartTime] = useState(fixedStart?.time ?? retryStart?.time ?? "");
  const [endTime, setEndTime] = useState(fixedEnd?.time ?? retryEnd?.time ?? "");
  const [locationId, setLocationId] = useState<number | "">(
    fixedSlot?.locationId ?? retryDirectContext?.location_id ?? "",
  );
  const [trainingTypeId, setTrainingTypeId] = useState<number | "">(
    fixedSlot?.trainingTypeId ?? retryDirectContext?.training_type_id ?? "",
  );
  const [subscriptionId, setSubscriptionId] = useState<number | "">("");
  const [tariffId, setTariffId] = useState<number | "">("");
  const [refreshedFixedOffer, setRefreshedFixedOffer] =
    useState<PersonalOffer | null>(null);
  const [mode, setMode] = useState<PersonalBookingMode>(
    retryBankPaymentReceipt ? "sbp" : "pay_at_club",
  );
  const [createdReservation, setCreatedReservation] =
    useState<PersonalBookingPaymentReservation | null>(null);
  const [staffIntentReceipt, setStaffIntentReceipt] =
    useState<PersonalCommercialReceipt | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [selectedDiscountId, setSelectedDiscountId] = useState<number | null>(null);
  const idempotencyKeysRef = useRef(new Map<string, string>());
  const needsBookingRefresh = suppliedSubscriptionsNeedBookingRefresh(
    suppliedSubscriptions,
    date,
  );

  const fetchedSubscriptionsQuery = useQuery<StudentSubscription[]>({
    queryKey: ["student", String(studentId), "subscriptions", "booking-date", date],
    queryFn: () =>
      apiClient
        .get("/billing/subscriptions/", {
          params: { student_id: studentId, booking_date: date },
        })
        .then((response) => normalizeStudentSubscriptionsPayload(response.data)),
    enabled:
      Boolean(studentId) &&
      (suppliedSubscriptions === undefined || needsBookingRefresh),
    staleTime: 30_000,
  });
  const subscriptions = needsBookingRefresh
    ? fetchedSubscriptionsQuery.data
    : suppliedSubscriptions ?? fetchedSubscriptionsQuery.data;
  const subscriptionsLoaded = subscriptions !== undefined;
  const bookingEligibility = useMemo<BookingEligibilityContext>(
    () => ({ bookingDate: date, locationId, timeZone }),
    [date, locationId, timeZone],
  );
  const requiredTrainingTypeId = fixedSlot?.trainingTypeId ?? (trainingTypeId || undefined);
  const eligibleSubscriptions = useMemo(
    () =>
      subscriptions?.filter((subscription) =>
        isUsablePersonalSubscription(
          subscription,
          bookingEligibility,
          requiredTrainingTypeId,
        ),
      ) ?? [],
    [bookingEligibility, requiredTrainingTypeId, subscriptions],
  );
  const hasUsableSubscription = !isV2PersonalStaffCommand && eligibleSubscriptions.length > 0;
  const isUnifiedStaffIntent = unifiedClientJourneyEnabled || isBankPaymentRetry;
  const currentUnifiedMode: PersonalBookingMode = isBankPaymentRetry
    ? "sbp"
    : isV2PersonalStaffCommand
      ? ["cash", "transfer", "sbp"].includes(mode)
        ? mode
        : "cash"
    : mode === "entitlement" && hasUsableSubscription
      ? "entitlement"
      : ["cash", "transfer", "sbp", "pay_at_visit"].includes(mode)
        ? mode
        : "pay_at_club";
  const isFreshPaidStaffBooking =
    subscriptionsLoaded &&
    personalAvailabilityCapabilityMode === "unified" &&
    !isBankPaymentRetry &&
    ["cash", "transfer", "sbp", "pay_at_visit"].includes(currentUnifiedMode);
  const previewDiscountId = currentUnifiedMode === "entitlement" ? null : selectedDiscountId;
  const shouldPreviewFreshPaidOffer =
    subscriptionsLoaded &&
    personalAvailabilityCapabilityMode === "unified" &&
    !isBankPaymentRetry;

  const { data: locations = [] } = useQuery<LocationItem[]>({
    queryKey: ["locations"],
    queryFn: () => apiClient.get("/clubs/locations/").then((r) => r.data),
    staleTime: 5 * 60_000,
    enabled: !fixedSlot,
  });
  const { data: trainingTypes = [] } = useQuery<TrainingTypeItem[]>({
    queryKey: ["training-types"],
    queryFn: () => apiClient.get("/billing/training-types/").then((r) => r.data),
    staleTime: 5 * 60_000,
    enabled: !fixedSlot,
  });
  const { data: tariffs, isLoading: tariffsLoading } = useQuery<TariffItem[]>({
    queryKey: ["tariffs"],
    queryFn: () =>
      apiClient.get("/billing/tariffs/").then((r) => r.data.items ?? r.data),
    staleTime: 5 * 60_000,
    enabled: !unifiedClientJourneyEnabled,
  });
  const currentStartsAt = `${date}T${startTime}:00`;
  const currentEndsAt = `${date}T${endTime}:00`;
  const directOfferTrainerId = retryDirectContext
    ? hasDirectRetryContext
      ? retryDirectContext.trainer_id
      : undefined
    : trainerIdentityQuery.data?.id;
  const hasDirectOfferInput =
    !fixedSlot &&
    typeof directOfferTrainerId === "number" &&
    Boolean(date && startTime && endTime && endTime > startTime) &&
    typeof trainingTypeId === "number" &&
    typeof locationId === "number";
  const directOfferQueryKey =
    hasDirectOfferInput &&
    typeof directOfferTrainerId === "number" &&
    typeof trainingTypeId === "number" &&
    typeof locationId === "number"
      ? directPersonalOfferQueryKey({
          trainerId: directOfferTrainerId,
          startsAt: currentStartsAt,
          endsAt: currentEndsAt,
          trainingTypeId,
          locationId,
          discountId: previewDiscountId,
        })
      : ["trainer", "personal-offer", "incomplete"];
  const directOfferQuery = useQuery({
    queryKey: isBankPaymentRetry
      ? [...directOfferQueryKey, "retry-bank-payment"]
      : directOfferQueryKey,
    queryFn: () =>
      getDirectPersonalOffer({
        trainerId: Number(directOfferTrainerId),
        startsAt: currentStartsAt,
        endsAt: currentEndsAt,
        trainingTypeId: Number(trainingTypeId),
        locationId: Number(locationId),
        discountId: previewDiscountId,
      }),
    enabled:
      (shouldPreviewFreshPaidOffer || isBankPaymentRetry) &&
      hasDirectOfferInput,
    staleTime: isBankPaymentRetry ? 0 : 30_000,
    // A terminal bank retry is a new authoritative command. It must obtain a
    // current digest even when an ordinary direct preview warmed this key.
    refetchOnMount: isBankPaymentRetry ? "always" : true,
  });
  // A slot carries its own trainer. Falling back preserves older callers while
  // preventing the current trainer identity from changing a slot-bound offer.
  const fixedOfferTrainerId = fixedSlot?.trainerId ?? trainerIdentityQuery.data?.id;
  const fixedOfferQueryKey = fixedSlot
    ? fixedPersonalOfferQueryKey({
        slotId: fixedSlot.slotId,
        trainerId: fixedOfferTrainerId,
        locationId: fixedSlot.locationId,
        trainingTypeId: fixedSlot.trainingTypeId,
        discountId: previewDiscountId,
      })
    : ["trainer", "personal-fixed-offer", "none"];
  const fixedOfferQuery = useQuery({
    queryKey: isBankPaymentRetry
      ? [...fixedOfferQueryKey, "retry-bank-payment"]
      : fixedOfferQueryKey,
    queryFn: () =>
      previewTrainerPersonalOffer({
        slotId: fixedSlot!.slotId,
        trainerId: fixedOfferTrainerId,
        locationId: fixedSlot!.locationId,
        trainingTypeId: fixedSlot!.trainingTypeId,
        discountId: previewDiscountId,
      }),
    enabled: Boolean(fixedSlot) && (shouldPreviewFreshPaidOffer || isBankPaymentRetry),
    staleTime: isBankPaymentRetry ? 0 : 30_000,
    // A terminal slot retry is a new financial command, just like a direct
    // retry. Never let a warm pre-cancellation digest enable the submit button.
    refetchOnMount: isBankPaymentRetry ? "always" : true,
  });
  const {
    data: discounts = [],
    isLoading: discountsLoading,
    isError: discountsError,
    isSuccess: discountsReady,
    isFetching: discountsFetching,
    isRefetchError: discountsRefetchError,
    refetch: refetchDiscounts,
  } = useQuery<PersonalBookingDiscount[]>({
    queryKey: ["billing", "discounts", ...commercialCacheScope],
    queryFn: () => apiClient.get("/billing/discounts/").then((response) => response.data.items ?? response.data),
    staleTime: 5 * 60_000,
    enabled: shouldPreviewFreshPaidOffer,
  });
  const { data: pendingReservations = [] } = useQuery<PersonalBookingPaymentReservation[]>({
    queryKey: [
      "student",
      String(studentId),
      "personal-booking-payment-reservations",
      "open_actionable",
    ],
    queryFn: () =>
      apiClient
        .get<PersonalBookingPaymentReservation[]>(
          `/students/${studentId}/personal-booking-payment-reservations/`,
          { params: { status: "open_actionable" } },
        )
        .then((r) => r.data),
    staleTime: 30_000,
    enabled: !fixedSlot && Boolean(studentId),
  });

  const personalTrainingTypes = trainingTypes.filter(
    (type) => type.is_active && isPersonalKind(type.kind),
  );
  const possibleDropInTariffs = (tariffs ?? []).filter(
    (tariff) =>
      tariff.is_active &&
      isPersonalKind(tariff.training_type.kind) &&
      tariff.trainings_limit === 1 &&
      (!fixedSlot || tariff.training_type.id === fixedSlot.trainingTypeId),
  );
  const selectedTariff = possibleDropInTariffs.find((tariff) => tariff.id === tariffId);
  const serverOffer = fixedSlot
    ? isUnifiedStaffIntent
      ? refreshedFixedOffer ?? fixedOfferQuery.data ?? null
      : refreshedFixedOffer ?? fixedSlotOffer(fixedSlot)
    : directOfferQuery.data ?? null;
  const offerIsConfigured = Boolean(serverOffer && !serverOffer.errorCode);
  const displayedTariffId = isUnifiedStaffIntent
    ? serverOffer?.tariffId ?? ""
    : tariffId;
  const displayedOfferPrice = isUnifiedStaffIntent
    ? personalOfferPayableAmount(serverOffer)
    : selectedTariff?.price ?? null;
  const activeDiscounts = discounts.filter((discount) => discount.is_active);
  const selectedDiscount = activeDiscounts.find((discount) => discount.id === selectedDiscountId);
  const selectedDiscountIsAvailable =
    selectedDiscountId === null || selectedDiscount !== undefined;
  const discountsUnavailable =
    shouldPreviewFreshPaidOffer &&
    (discountsError || discountsRefetchError || (discountsReady && !selectedDiscountIsAvailable));
  const discountsLoaded =
    !shouldPreviewFreshPaidOffer || (discountsReady && !discountsRefetchError);
  const paidOfferPreviewFailed =
    shouldPreviewFreshPaidOffer &&
    (fixedSlot
      ? fixedOfferQuery.isError || fixedOfferQuery.isRefetchError
      : directOfferQuery.isError || directOfferQuery.isRefetchError);
  const visibleReservationIds = new Set<number>();
  const visibleReservations = [createdReservation, ...pendingReservations].filter(
    (reservation): reservation is PersonalBookingPaymentReservation => {
      if (
        reservation === null ||
        !isVisiblePersonalPaymentReservation(reservation) ||
        visibleReservationIds.has(reservation.id)
      ) {
        return false;
      }
      visibleReservationIds.add(reservation.id);
      return true;
    },
  );
  const blockingReservations = visibleReservations.filter(
    (reservation) =>
      !isTerminalRetryOriginalReservation(reservation, retryBankPaymentReceipt),
  );
  const directRetryOfferReady =
    !isBankPaymentRetry ||
    (fixedSlot
      ? fixedOfferQuery.isSuccess &&
        !fixedOfferQuery.isFetching &&
        !fixedOfferQuery.isRefetchError
      : directOfferQuery.isSuccess &&
        !directOfferQuery.isFetching &&
        !directOfferQuery.isRefetchError);
  const freshPaidOfferReady =
    !shouldPreviewFreshPaidOffer ||
    (fixedSlot
      ? fixedOfferQuery.isSuccess && !fixedOfferQuery.isRefetchError
      : directOfferQuery.isSuccess && !directOfferQuery.isRefetchError);
  const canUseServerPricedOffer =
    offerIsConfigured &&
    Boolean(serverOffer?.digest) &&
    directRetryOfferReady &&
    freshPaidOfferReady;
  const canChoosePaidStaffMethod = canUseServerPricedOffer && !discountsUnavailable;
  const canUsePayAtClub =
    !hasUsableSubscription &&
    !isV2PersonalStaffCommand &&
    (unifiedClientJourneyEnabled
      ? canUseServerPricedOffer
      : possibleDropInTariffs.length > 0);
  const onlineModeAvailable =
    !hasUsableSubscription &&
    (unifiedClientJourneyEnabled
      ? canUseServerPricedOffer
      : possibleDropInTariffs.length > 0);
  const onlinePaymentsEnabled = hasOnlinePaymentsCapability(paymentCapabilitiesQuery);
  const canUseOnline = onlineModeAvailable && onlinePaymentsEnabled;
  const currentMode: PersonalBookingMode = isUnifiedStaffIntent
    ? currentUnifiedMode
    : mode === "entitlement" && hasUsableSubscription
      ? "entitlement"
      : mode === "pay_at_club" && canUsePayAtClub
        ? "pay_at_club"
        : mode === "online" && canUseOnline
          ? "online"
          : hasUsableSubscription
            ? "entitlement"
            : canUsePayAtClub
              ? "pay_at_club"
              : "online";
  const staffIntentPaymentOptions: ReadonlyArray<readonly [PersonalBookingMode, string]> =
    isBankPaymentRetry
      ? [["sbp", "Повторить оплату через СБП"]]
      : [
          ["cash", "Наличные"],
          ["transfer", "Перевод"],
          ["sbp", "Оплата через СБП"],
          ...(isV2PersonalStaffCommand ? [] : [["pay_at_visit", "Оплата при посещении"]] as const),
        ];

  const invalidateBookingQueries = () => {
    queryClient.invalidateQueries({
      queryKey: ["student", String(studentId), "personal-bookings"],
    });
    queryClient.invalidateQueries({ queryKey: ["student", String(studentId)] });
    queryClient.invalidateQueries({
      queryKey: ["student", String(studentId), "subscriptions"],
    });
    queryClient.invalidateQueries({ queryKey: ["trainer", "availability"] });
    queryClient.invalidateQueries({ queryKey: ["schedules"] });
    queryClient.invalidateQueries({ queryKey: ["leads"] });
    queryClient.invalidateQueries({ queryKey: ["lead", studentId, "action-context"] });
    queryClient.invalidateQueries({ queryKey: ["billing", "bank-payment-orders", studentId] });
    queryClient.invalidateQueries({ queryKey: ["billing", "debts", studentId] });
    queryClient.invalidateQueries({ queryKey: ["retention-tasks"] });
    queryClient.invalidateQueries({ queryKey: ["task-badge-count"] });
  };

  const invalidateStaffIntentQueries = () => {
    queryClient.invalidateQueries({
      queryKey: ["student", String(studentId), "personal-bookings"],
    });
    queryClient.invalidateQueries({ queryKey: ["student", String(studentId)] });
    queryClient.invalidateQueries({
      queryKey: ["student", String(studentId), "subscriptions"],
    });
    queryClient.invalidateQueries({
      queryKey: personalCommercialContextQueryKey(studentId, commercialCacheScope),
    });
    queryClient.invalidateQueries({
      queryKey: leadPersonalCommercialContextQueryKey(studentId, commercialCacheScope),
    });
    queryClient.invalidateQueries({ queryKey: ["trainer", "availability"] });
    queryClient.invalidateQueries({
      queryKey: ["schedules", "by-date", fixedSlot?.startsAt.slice(0, 10)],
    });
    queryClient.invalidateQueries({ queryKey: ["lead", studentId] });
    queryClient.invalidateQueries({
      queryKey: ["lead", studentId, "action-context"],
    });
    queryClient.invalidateQueries({ queryKey: ["leads", "mine"] });
    queryClient.invalidateQueries({ queryKey: ["leads"] });
    queryClient.invalidateQueries({ queryKey: ["students"] });
  };

  function idempotencyKeyFor(intent: "booking" | "online" | "staff-intent") {
    const fingerprint = [
      intent,
      currentMode,
      studentId,
      fixedSlot?.slotId ?? "direct",
      date,
      startTime,
      endTime,
      locationId,
      trainingTypeId,
      subscriptionId || displayedTariffId,
      isUnifiedStaffIntent ? serverOffer?.digest ?? "missing-digest" : "legacy",
      isUnifiedStaffIntent ? selectedDiscountId ?? "no-discount" : "legacy",
      retryBankPaymentReceipt?.reservation_id ?? retryBankPaymentReceipt?.bank_payment_order_id ?? "new",
    ].join("|");
    const existing = idempotencyKeysRef.current.get(fingerprint);
    if (existing) return existing;
    const key = createIdempotencyKey(`trainer-personal-${intent}`);
    idempotencyKeysRef.current.set(fingerprint, key);
    return key;
  }

  const createBookingMutation = useMutation({
    mutationFn: () => {
      const idempotencyKey = idempotencyKeyFor("booking");
      if (fixedSlot) {
        if (currentMode === "entitlement") {
          return apiClient.post(
            `/personal-availability/slots/${fixedSlot.slotId}/book-client/`,
            {
              student_id: studentId,
              subscription_id: subscriptionId,
              idempotency_key: idempotencyKey,
            },
          );
        }
        return apiClient.post(
          `/personal-availability/slots/${fixedSlot.slotId}/drop-in-bookings/`,
          {
            student_id: studentId,
            tariff_id: displayedTariffId,
            ...(unifiedClientJourneyEnabled ? { offer_digest: serverOffer?.digest } : {}),
            idempotency_key: idempotencyKey,
          },
        );
      }
      if (currentMode === "entitlement") {
        return apiClient.post(`/students/${studentId}/personal-bookings/`, {
          starts_at: currentStartsAt,
          ends_at: currentEndsAt,
          location_id: locationId,
          training_type_id: trainingTypeId,
          subscription_id: subscriptionId,
          idempotency_key: idempotencyKey,
        });
      }
      return apiClient.post(`/students/${studentId}/personal-drop-in-bookings/`, {
        starts_at: currentStartsAt,
        ends_at: currentEndsAt,
        location_id: locationId,
        training_type_id: trainingTypeId,
        tariff_id: displayedTariffId,
        ...(unifiedClientJourneyEnabled && serverOffer?.digest
          ? { offer_digest: serverOffer.digest }
          : {}),
        idempotency_key: idempotencyKey,
      });
    },
    onSuccess: (response) => {
      invalidateBookingQueries();
      setErrorMsg(null);
      const responseData = response.data as { price_snapshot?: string | number } | undefined;
      onBooked?.({
        mode: currentMode === "entitlement" ? "entitlement" : "pay_at_club",
        startsAt: currentStartsAt,
        endsAt: currentEndsAt,
        price:
          currentMode === "pay_at_club"
            ? responseData?.price_snapshot ?? displayedOfferPrice
            : null,
      });
      onClose?.();
    },
    onError: (error: unknown) => {
      if (
        error &&
        typeof error === "object" &&
        (error as { response?: { data?: { code?: unknown } } }).response?.data?.code ===
          "personal_offer_changed"
      ) {
        const currentOffer = (
          error as {
            response?: { data?: { current_offer?: Parameters<typeof normalizePersonalOffer>[0] } };
          }
        ).response?.data?.current_offer;
        if (fixedSlot && currentOffer) {
          setRefreshedFixedOffer(normalizePersonalOffer(currentOffer));
        }
        void queryClient.invalidateQueries({ queryKey: ["trainer", "availability"] });
        if (!fixedSlot) void directOfferQuery.refetch();
      }
      setErrorMsg(apiErrorMessage(error, "Не удалось записать на персоналку"));
    },
  });

  const createStaffIntentMutation = useMutation({
    // Staff commands must attempt the HTTP request immediately. Keeping a
    // financial command paused behind TanStack's browser-online heuristic
    // leaves the button at "Отправка..." without ever reaching Django.
    networkMode: "always",
    mutationFn: () => {
      const paymentMethod = currentMode as PersonalStaffIntentPaymentMethod;
      const payload = {
        student_id: studentId,
        payment_method: paymentMethod,
        ...(paymentMethod === "entitlement" && subscriptionId
          ? { subscription_id: Number(subscriptionId) }
          : {}),
        ...(paymentMethod === "entitlement" ? {} : { offer_digest: serverOffer?.digest }),
        ...(isFreshPaidStaffBooking ? { discount_id: selectedDiscountId } : {}),
        idempotency_key: idempotencyKeyFor("staff-intent"),
      };
      if (fixedSlot) {
        if (!personalStaffCommandProtocol) {
          throw new Error("personal_staff_protocol_unavailable");
        }
        return createPersonalStaffIntent({
          slotId: fixedSlot.slotId,
          protocolVersion: personalStaffCommandProtocol,
          payload,
        });
      }
      if (
        typeof directOfferTrainerId !== "number" ||
        typeof locationId !== "number" ||
        typeof trainingTypeId !== "number"
      ) {
        throw new Error("direct_personal_context_required");
      }
      return createDirectPersonalStaffIntent({
        ...payload,
        protocolVersion: personalStaffCommandProtocol ?? undefined,
        trainer_id: directOfferTrainerId,
        starts_at: currentStartsAt,
        ends_at: currentEndsAt,
        location_id: locationId,
        training_type_id: trainingTypeId,
      });
    },
    onSuccess: (receipt) => {
      setStaffIntentReceipt(receipt);
      setErrorMsg(null);
      invalidateStaffIntentQueries();
      onStaffIntentCreated?.(receipt);
    },
    onError: (error: unknown) => {
      if (
        error &&
        typeof error === "object" &&
        (error as { response?: { data?: { code?: unknown } } }).response?.data?.code ===
          "personal_offer_changed"
      ) {
        const currentOffer = (
          error as {
            response?: { data?: { current_offer?: Parameters<typeof normalizePersonalOffer>[0] } };
          }
        ).response?.data?.current_offer;
        if (fixedSlot && currentOffer) {
          setRefreshedFixedOffer(normalizePersonalOffer(currentOffer));
        }
        void queryClient.invalidateQueries({ queryKey: ["trainer", "availability"] });
        if (!fixedSlot) void directOfferQuery.refetch();
      }
      setErrorMsg(apiErrorMessage(error, "Не удалось создать персональную запись"));
    },
  });

  const createOnlineMutation = useMutation({
    mutationFn: () => {
      if (!onlinePaymentsEnabled) {
        throw new Error("Online payment capability is unavailable");
      }
      const idempotencyKey = idempotencyKeyFor("online");
      if (fixedSlot) {
        return apiClient
          .post<PersonalBookingPaymentReservation>(
            `/personal-availability/slots/${fixedSlot.slotId}/staff-payment-reservations/`,
            {
              student_id: studentId,
              tariff_id: displayedTariffId,
              ...(unifiedClientJourneyEnabled ? { offer_digest: serverOffer?.digest } : {}),
              idempotency_key: idempotencyKey,
            },
          )
          .then((response) => response.data);
      }
      return apiClient
        .post<PersonalBookingPaymentReservation>(
          `/students/${studentId}/personal-booking-payment-reservations/`,
          {
            starts_at: currentStartsAt,
            ends_at: currentEndsAt,
            location_id: locationId,
            training_type_id: trainingTypeId,
            tariff_id: displayedTariffId,
            ...(unifiedClientJourneyEnabled ? { offer_digest: serverOffer?.digest } : {}),
            idempotency_key: idempotencyKey,
          },
        )
        .then((response) => response.data);
    },
    onSuccess: (reservation) => {
      setCreatedReservation(reservation);
      setErrorMsg(null);
      queryClient.invalidateQueries({
        queryKey: [
          "student",
          String(studentId),
          "personal-booking-payment-reservations",
          "open_actionable",
        ],
      });
      queryClient.invalidateQueries({ queryKey: ["trainer", "availability"] });
    },
    onError: (error: unknown) => {
      if (
        error &&
        typeof error === "object" &&
        (error as { response?: { data?: { code?: unknown } } }).response?.data?.code ===
          "personal_offer_changed"
      ) {
        const currentOffer = (
          error as {
            response?: { data?: { current_offer?: Parameters<typeof normalizePersonalOffer>[0] } };
          }
        ).response?.data?.current_offer;
        if (fixedSlot && currentOffer) {
          setRefreshedFixedOffer(normalizePersonalOffer(currentOffer));
        }
        void queryClient.invalidateQueries({ queryKey: ["trainer", "availability"] });
        if (!fixedSlot) void directOfferQuery.refetch();
      }
      setCreatedReservation(null);
      setErrorMsg(apiErrorMessage(error, "Не удалось создать ссылку на оплату"));
    },
  });

  const cancelPaymentReservationMutation = useMutation({
    mutationFn: (reservation: PersonalBookingPaymentReservation) =>
      apiClient
        .post<PersonalBookingPaymentReservation>(
          `/students/${studentId}/personal-booking-payment-reservations/${reservation.id}/cancel/`,
          {},
        )
        .then((response) => response.data),
    onSuccess: () => {
      setCreatedReservation(null);
      queryClient.invalidateQueries({
        queryKey: [
          "student",
          String(studentId),
          "personal-booking-payment-reservations",
        ],
      });
    },
    onError: (error: unknown) => {
      setErrorMsg(apiErrorMessage(error, "Не удалось отменить ссылку"));
    },
  });

  function selectMode(nextMode: PersonalBookingMode) {
    setMode(nextMode);
    if (nextMode === "entitlement" || isBankPaymentRetry) {
      setSelectedDiscountId(null);
      setRefreshedFixedOffer(null);
    }
    setCreatedReservation(null);
    setErrorMsg(null);
  }

  function handleDiscountChange(nextDiscountId: number | null) {
    setSelectedDiscountId(nextDiscountId);
    setRefreshedFixedOffer(null);
    setErrorMsg(null);
  }

  function handleSubscriptionChange(value: string) {
    const nextId = value ? Number(value) : "";
    setSubscriptionId(nextId);
    const subscription = eligibleSubscriptions.find((item) => item.id === nextId);
    const entitlement = subscription
      ? usablePersonalEntitlements(
          subscription,
          bookingEligibility,
          trainingTypeId || undefined,
        )[0]
      : undefined;
    if (!fixedSlot && entitlement) {
      setTrainingTypeId(entitlement.training_type_id);
    }
    setErrorMsg(null);
  }

  function handleTrainingTypeChange(value: string) {
    const nextTrainingTypeId = value ? Number(value) : "";
    setTrainingTypeId(nextTrainingTypeId);
    const selectedSubscription = subscriptions?.find((item) => item.id === subscriptionId);
    if (
      selectedSubscription &&
      !isUsablePersonalSubscription(
        selectedSubscription,
        bookingEligibility,
        nextTrainingTypeId || undefined,
      )
    ) {
      setSubscriptionId("");
    }
    setErrorMsg(null);
  }

  function handleTariffChange(value: string) {
    const nextId = value ? Number(value) : "";
    setTariffId(nextId);
    const tariff = possibleDropInTariffs.find((item) => item.id === nextId);
    if (!fixedSlot && tariff?.training_type.id) {
      setTrainingTypeId(tariff.training_type.id);
    }
    if (!fixedSlot && tariff?.scope === "location" && tariff.location_id) {
      setLocationId(tariff.location_id);
    }
    setCreatedReservation(null);
    setErrorMsg(null);
  }

  function isFormValid() {
    const hasContext = Boolean(
      date &&
        startTime &&
        endTime &&
        endTime > startTime &&
        locationId &&
        trainingTypeId,
    );
    if (!subscriptionsLoaded || !hasContext) return false;
    if (currentMode === "entitlement") {
      const selectedSubscription = eligibleSubscriptions.find((item) => item.id === subscriptionId);
      return Boolean(
        selectedSubscription &&
          isUsablePersonalSubscription(
            selectedSubscription,
            bookingEligibility,
            Number(trainingTypeId),
          ),
      );
    }
    if (isUnifiedStaffIntent) {
      if (!personalStaffCommandProtocol) return false;
      if (
        !(isV2PersonalStaffCommand
          ? ["cash", "transfer", "sbp"]
          : ["cash", "transfer", "sbp", "pay_at_visit"]
        ).includes(currentMode)
      ) {
        return false;
      }
      return Boolean(
        canUseServerPricedOffer &&
        discountsLoaded &&
        !discountsUnavailable &&
        selectedDiscountIsAvailable,
      );
    }
    return Boolean(tariffId && selectedTariff);
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!isFormValid()) {
      setErrorMsg("Заполните дату, время, зал, тип и способ оплаты.");
      return;
    }
    setErrorMsg(null);
    if (isUnifiedStaffIntent) {
      createStaffIntentMutation.mutate();
      return;
    }
    if (currentMode === "online") {
      createOnlineMutation.mutate();
      return;
    }
    createBookingMutation.mutate();
  }

  const isSubmitting =
    createBookingMutation.isPending ||
    createOnlineMutation.isPending ||
    createStaffIntentMutation.isPending;
  const hasLiveReservation = blockingReservations.length > 0;
  const directTrainerName = trainerDisplayName(trainerIdentityQuery.data);
  const selectedLocationName = locations.find((location) => location.id === locationId)?.name;
  const bookingTimeLabel = fixedSlot
    ? formatSlotContext(fixedSlot, timeZone)
    : date && startTime && endTime
      ? `${date} · ${startTime}–${endTime}${selectedLocationName ? ` · ${selectedLocationName}` : ""}`
      : "Укажите точное время в часовом поясе клуба";
  const bookingTrainerName = fixedSlot?.trainerName || directTrainerName || "Проверяем тренера";

  function confirmationLabel() {
    if (!isUnifiedStaffIntent) {
      if (currentMode === "online") return "Создать ссылку СБП";
      if (currentMode === "pay_at_club") return "Записать с оплатой в клубе";
      return "Записать";
    }
    const payableAmount = personalOfferPayableAmount(serverOffer);
    const amountLabel = payableAmount == null ? "по актуальной цене" : `на ${formatRub(payableAmount)}`;
    if (isBankPaymentRetry) return `Повторить оплату СБП для ${studentName} ${amountLabel}`;
    if (currentMode === "entitlement") return `Записать ${studentName} по абонементу`;
    if (currentMode === "sbp") return `Создать ссылку СБП для ${studentName} ${amountLabel}`;
    if (currentMode === "cash") return `Записать ${studentName}, наличные, ${amountLabel}`;
    if (currentMode === "transfer") return `Записать ${studentName}, перевод, ${amountLabel}`;
    if (currentMode === "pay_at_visit") {
      return `Записать ${studentName}, оплата при посещении, ${amountLabel}`;
    }
    return "Выберите способ оплаты";
  }

  if (personalAvailabilityCapability.isLoading) {
    return <p role="status" className="ui-muted-14">Проверяем доступный способ записи...</p>;
  }

  if (
    personalAvailabilityCapabilityMode === "unavailable" ||
    (unifiedClientJourneyEnabled && !personalStaffCommandProtocol)
  ) {
    return (
      <section role="alert" className="ui-col-2 ui-warning-dark">
        <p className="font-semibold">Не удалось подтвердить доступный способ записи.</p>
        <p>Повторите проверку, чтобы не создать запись с другим финансовым сценарием.</p>
        <Button
          type="button"
          variant="outline"
          className="min-h-[44px] self-start"
          disabled={personalAvailabilityCapability.isFetching}
          onClick={() => void personalAvailabilityCapability.refetch()}
        >
          {personalAvailabilityCapability.isFetching ? "Проверяем..." : "Повторить проверку"}
        </Button>
      </section>
    );
  }

  if (isBankPaymentRetry && !fixedSlot && !hasDirectRetryContext) {
    return (
      <p role="status" className="ui-warning">
        Не удалось подтвердить точный контекст персоналки для повторной оплаты. Обновите карточку клиента.
      </p>
    );
  }

  if (staffIntentReceipt) {
    return (
      <section className="ui-col-3" aria-label="Подтверждение персональной записи">
        <PersonalCommercialReceiptCard receipt={staffIntentReceipt} />
        <Button type="button" className="ui-brand-touch" onClick={onClose}>
          Закрыть
        </Button>
      </section>
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex min-h-0 min-w-0 w-full max-w-full flex-col gap-4 overflow-x-hidden"
    >
      <section
        aria-label="Детали персональной записи"
        className="min-w-0 rounded-xl bg-muted/50 p-3"
      >
        <p className="ui-muted-12">Клиент</p>
        <p className="break-words text-lg font-semibold text-foreground">{studentName}</p>
        <dl className="mt-3 grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-2 text-[13px]">
          <dt className="ui-muted">Тренер</dt>
          <dd className="min-w-0 break-words text-right font-medium text-foreground">
            {bookingTrainerName}
          </dd>
          <dt className="ui-muted">Время клуба</dt>
          <dd className="min-w-0 break-words text-right font-medium text-foreground">
            {bookingTimeLabel}
          </dd>
        </dl>
      </section>

      {fixedSlot ? (
        <section aria-label="Фиксированный слот" className="rounded-xl bg-muted/50 p-3 text-[13px]">
          <p className="font-medium text-foreground">Слот зафиксирован</p>
          <p className="mt-1 text-muted-foreground">{formatSlotContext(fixedSlot, timeZone)}</p>
          <p className="mt-1 text-muted-foreground">{fixedSlot.trainingTypeName} · {fixedSlot.trainerName}</p>
        </section>
      ) : null}

      {!subscriptionsLoaded ? (
        <p className="ui-muted-14">Загрузка абонементов...</p>
      ) : (
        <section aria-label="Способ записи" className="ui-col-2">
          <p className="ui-caption-label">Способ записи</p>
          <div className="grid grid-cols-1 gap-2">
            {hasUsableSubscription ? (
              <Button
                type="button"
                variant={currentMode === "entitlement" ? "default" : "outline"}
                aria-pressed={currentMode === "entitlement"}
                onClick={() => selectMode("entitlement")}
                className="min-h-[44px] justify-start"
              >
                По абонементу
              </Button>
            ) : null}
            {isUnifiedStaffIntent ? (
              <>
                {staffIntentPaymentOptions.map(([value, label]) => (
                  <Button
                    key={value}
                    type="button"
                    variant={currentMode === value ? "default" : "outline"}
                    aria-pressed={currentMode === value}
                    onClick={() => selectMode(value)}
                    disabled={
                      !canChoosePaidStaffMethod ||
                      (value === "sbp" && !onlinePaymentsEnabled && !isBankPaymentRetry)
                    }
                    className="min-h-[44px] justify-start"
                  >
                    {label}
                  </Button>
                ))}
              </>
            ) : canUsePayAtClub ? (
              <Button
                type="button"
                variant={currentMode === "pay_at_club" ? "default" : "outline"}
                aria-pressed={currentMode === "pay_at_club"}
                onClick={() => selectMode("pay_at_club")}
                className="min-h-[44px] justify-start"
              >
                Оплата в клубе
              </Button>
            ) : null}
            {onlineModeAvailable && !isUnifiedStaffIntent ? (
              <Button
                type="button"
                variant={currentMode === "online" ? "default" : "outline"}
                aria-pressed={currentMode === "online"}
                onClick={() => selectMode("online")}
                disabled={!onlinePaymentsEnabled}
                className="min-h-[44px] justify-start"
              >
                Оплата через СБП
              </Button>
            ) : null}
          </div>
          {onlineModeAvailable && !onlinePaymentsEnabled ? (
            <p role="status" className="ui-warning">
              {getOnlinePaymentUnavailableMessage(paymentCapabilitiesQuery, "staff")}
            </p>
          ) : null}
          {!hasUsableSubscription &&
          !canUsePayAtClub &&
          (unifiedClientJourneyEnabled ? !directOfferQuery.isLoading : !tariffsLoading) ? (
            <p role="status" className="ui-warning">
              {unifiedClientJourneyEnabled && !fixedSlot
                ? "Для записи без абонемента укажите точное время: сервер закрепит актуальную цену."
                : "Нет корректной цены для персоналки. Попросите владельца настроить тариф."}
            </p>
          ) : null}
        </section>
      )}

      {currentMode === "pay_at_club" || currentMode === "pay_at_visit" ? (
        <p className="rounded-xl bg-amber-50 p-3 text-[13px] leading-snug text-amber-900">
          {currentMode === "pay_at_visit"
            ? "К оплате при посещении. До check-in это только запись на точное время, не долг."
            : "Долг появится только после check-in. До посещения это только запись на точное время."}
        </p>
      ) : null}

      {currentMode === "cash" || currentMode === "transfer" ? (
        <p className="rounded-xl bg-amber-50 p-3 text-[13px] leading-snug text-amber-900">
          {isV2PersonalStaffCommand
            ? "После успешной записи клиент перейдёт в ученики, а оплата останется на финансовой проверке владельца."
            : "Оплата ожидает подтверждения владельцем. Это не подтверждает переход клиента в ученика."}
        </p>
      ) : null}

      {isBankPaymentRetry ? (
        <p className="rounded-xl bg-amber-50 p-3 text-[13px] leading-snug text-amber-900">
          Будет создана новая попытка СБП для этой же персоналки. Предыдущая попытка сохранится в истории.
        </p>
      ) : null}

      {currentMode === "entitlement" ? (
        <div>
          <label htmlFor="personal-booking-subscription" className="ui-field-label">
            Абонемент *
          </label>
          <select
            id="personal-booking-subscription"
            value={subscriptionId}
            onChange={(event) => handleSubscriptionChange(event.target.value)}
            className={SELECT_CLASS}
            required
          >
            <option value="">Выберите абонемент</option>
            {eligibleSubscriptions.map((subscription) => {
              const entitlement = usablePersonalEntitlements(
                subscription,
                bookingEligibility,
                requiredTrainingTypeId,
              )[0];
              return (
                <option key={subscription.id} value={subscription.id}>
                  {subscription.tariff_name}
                  {entitlement?.training_type_name
                    ? ` · ${entitlement.training_type_name}`
                    : ""}
                </option>
              );
            })}
          </select>
        </div>
      ) : (currentMode === "pay_at_club" || currentMode === "online") && !unifiedClientJourneyEnabled ? (
        <div>
          <label htmlFor="personal-booking-tariff" className="ui-field-label">
            Разовая персоналка *
          </label>
          <select
            id="personal-booking-tariff"
            value={tariffId}
            onChange={(event) => handleTariffChange(event.target.value)}
            className={SELECT_CLASS}
            required
          >
            <option value="">Выберите тариф</option>
            {possibleDropInTariffs.map((tariff) => (
              <option key={tariff.id} value={tariff.id}>
                {tariff.name} · {formatRub(tariff.price)}
              </option>
            ))}
          </select>
        </div>
      ) : null}

      {fixedSlot ? null : (
        <>
          <div>
            <label htmlFor="personal-booking-date" className="ui-field-label">Дата *</label>
            <Input id="personal-booking-date" type="date" value={date} onChange={(event) => setDate(event.target.value)} required disabled={isBankPaymentRetry} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="personal-booking-start" className="ui-field-label">Начало *</label>
              <Input id="personal-booking-start" type="time" value={startTime} onChange={(event) => setStartTime(event.target.value)} required disabled={isBankPaymentRetry} />
            </div>
            <div>
              <label htmlFor="personal-booking-end" className="ui-field-label">Конец *</label>
              <Input id="personal-booking-end" type="time" value={endTime} onChange={(event) => setEndTime(event.target.value)} required disabled={isBankPaymentRetry} />
            </div>
          </div>
          <div>
            <label htmlFor="personal-booking-training-type" className="ui-field-label">Тип тренировки *</label>
            <select id="personal-booking-training-type" value={trainingTypeId} onChange={(event) => handleTrainingTypeChange(event.target.value)} className={SELECT_CLASS} required disabled={isBankPaymentRetry}>
              <option value="">Выберите тип</option>
              {personalTrainingTypes.map((type) => <option key={type.id} value={type.id}>{type.name}</option>)}
            </select>
          </div>
          <div>
            <label htmlFor="personal-booking-location" className="ui-field-label">Зал *</label>
            <select id="personal-booking-location" value={locationId} onChange={(event) => setLocationId(event.target.value ? Number(event.target.value) : "")} className={SELECT_CLASS} required disabled={isBankPaymentRetry}>
              <option value="">Выберите зал</option>
              {locations.map((location) => <option key={location.id} value={location.id}>{location.name}</option>)}
            </select>
          </div>
        </>
      )}

      {paidOfferPreviewFailed ? (
        <section role="alert" className="ui-col-2 ui-warning-dark">
          <p>Не удалось получить актуальную цену персоналки. Платная запись заблокирована до повторной проверки.</p>
          <Button
            type="button"
            variant="outline"
            className="min-h-[44px] self-start"
            onClick={() => {
              if (fixedSlot) {
                void fixedOfferQuery.refetch();
              } else {
                void directOfferQuery.refetch();
              }
            }}
          >
            Повторить проверку цены
          </Button>
        </section>
      ) : null}

      {shouldPreviewFreshPaidOffer && currentMode !== "entitlement" ? (
        <section aria-labelledby="personal-booking-discount-heading" className="ui-col-2">
          <p id="personal-booking-discount-heading" className="ui-caption-label">Скидка</p>
          {discountsLoading ? (
            <p role="status" className="rounded-xl bg-muted/50 p-3 text-[13px] text-muted-foreground">
              Загружаем доступные скидки...
            </p>
          ) : discountsUnavailable ? (
            <div role="alert" className="ui-col-2 rounded-xl bg-destructive/10 p-3 text-[13px] text-destructive">
              <p>
                {selectedDiscountIsAvailable
                  ? "Не удалось загрузить скидки. Запись заблокирована до повторной проверки."
                  : "Выбранная скидка больше недоступна. Выберите актуальную скидку или повторите проверку."}
              </p>
              <Button
                type="button"
                variant="outline"
                className="min-h-[44px] self-start"
                disabled={discountsFetching}
                onClick={() => void refetchDiscounts()}
              >
                {discountsFetching ? "Повторная загрузка..." : "Повторить загрузку"}
              </Button>
            </div>
          ) : (
            <div role="radiogroup" aria-label="Скидка" className="ui-col-2">
              <label className="flex min-h-[44px] min-w-0 cursor-pointer items-center gap-3 rounded-xl bg-white p-3 text-left ring-1 ring-foreground/5 active:bg-muted">
                <input
                  type="radio"
                  name={`personal-booking-discount-${studentId}`}
                  checked={selectedDiscountId === null}
                  onChange={() => handleDiscountChange(null)}
                  className="size-4 shrink-0 accent-[var(--branding-accent)]"
                />
                <span className="min-w-0 flex-1 text-[14px] font-medium text-foreground">Без скидки</span>
              </label>
              {activeDiscounts.map((discount) => (
                <label
                  key={discount.id}
                  className="flex min-h-[44px] min-w-0 cursor-pointer items-center gap-3 rounded-xl bg-white p-3 text-left ring-1 ring-foreground/5 active:bg-muted"
                >
                  <input
                    type="radio"
                    name={`personal-booking-discount-${studentId}`}
                    checked={selectedDiscountId === discount.id}
                    onChange={() => handleDiscountChange(discount.id)}
                    className="size-4 shrink-0 accent-[var(--branding-accent)]"
                  />
                  <span className="flex min-w-0 flex-1 items-center justify-between gap-3">
                    <span className="min-w-0 break-words text-[14px] font-medium text-foreground">
                      {discount.name}
                    </span>
                    <span className="shrink-0 text-[13px] font-semibold text-muted-foreground">
                      {formatDiscountValue(discount)}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          )}
        </section>
      ) : null}

      {isUnifiedStaffIntent && currentMode !== "entitlement" && serverOffer ? (
        <section className="rounded-xl bg-muted/50 p-3" aria-label="Итог персональной записи">
          <p className="ui-caption-label">Стоимость персоналки</p>
          <dl className="mt-2 space-y-2 text-[13px]">
            <div className="flex min-w-0 items-center justify-between gap-3">
              <dt className="ui-muted">Обычная цена</dt>
              <dd className="shrink-0 font-medium text-foreground">
                {personalOfferBaseAmount(serverOffer) == null
                  ? "—"
                  : formatRub(personalOfferBaseAmount(serverOffer)!)}
              </dd>
            </div>
            {!isBankPaymentRetry ? (
              <div className="flex min-w-0 items-center justify-between gap-3">
                <dt className="min-w-0 break-words ui-muted">
                  {serverOffer.discountName ? `Скидка: ${serverOffer.discountName}` : "Скидка"}
                </dt>
                <dd className="shrink-0 font-medium text-foreground">
                  {serverOffer.discountName && serverOffer.discountAmount != null
                    ? `−${formatRub(serverOffer.discountAmount)}`
                    : "—"}
                </dd>
              </div>
            ) : null}
            <div className="flex min-w-0 items-center justify-between gap-3 border-t border-foreground/10 pt-2">
              <dt className="ui-caption-label">К оплате</dt>
              <dd className="ui-title-20 shrink-0">
                {personalOfferPayableAmount(serverOffer) == null
                  ? "—"
                  : formatRub(personalOfferPayableAmount(serverOffer)!)}
              </dd>
            </div>
          </dl>
          {serverOffer.tariffName ? (
            <p className="mt-2 break-words text-[13px] text-muted-foreground">{serverOffer.tariffName}</p>
          ) : null}
          {!serverOffer.digest ? (
            <p role="status" className="mt-2 ui-warning">
              Цена показана для ориентира. Для записи сервер должен подтвердить точный оффер.
            </p>
          ) : null}
        </section>
      ) : selectedTariff ? (
        <div className="rounded-xl bg-muted/50 p-3">
          <p className="ui-muted-12">Стоимость разовой персоналки</p>
          <p className="ui-title-20">{formatRub(selectedTariff.price)}</p>
        </div>
      ) : null}

      {visibleReservations.map((reservation) => {
        return (
          <StaffPersonalBookingReservationPanel
            key={reservation.id}
            reservation={reservation}
            commercialCacheScope={commercialCacheScope}
            isCanceling={cancelPaymentReservationMutation.isPending}
            onCancel={() => cancelPaymentReservationMutation.mutate(reservation)}
          />
        );
      })}

      {errorMsg ? <p role="alert" className="ui-error-center">{errorMsg}</p> : null}

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {onBack ? <Button type="button" variant="outline" className="min-h-[44px]" onClick={onBack}>Назад</Button> : null}
        <Button
          type="submit"
          className="ui-brand-touch"
          disabled={
            isSubmitting ||
            !isFormValid() ||
            ((currentMode === "online" || currentMode === "sbp") &&
              (!onlinePaymentsEnabled || hasLiveReservation))
          }
        >
          {isSubmitting
            ? "Отправка..."
            : currentMode === "online" && hasLiveReservation
              ? "Оплата ожидает подтверждения"
              : confirmationLabel()}
        </Button>
      </div>
    </form>
  );
}

export function PersonalBookingSheet({
  open,
  onOpenChange,
  studentId,
  studentName,
  subscriptions,
  fixedSlot,
  retryBankPaymentReceipt,
  onBooked,
  onBack,
  onStaffIntentCreated,
}: PersonalBookingSheetProps) {
  function handleClose() {
    onOpenChange(false);
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="bottom"
        className="max-h-[92dvh] w-full max-w-[100vw] overflow-x-hidden overflow-y-hidden overscroll-contain rounded-t-2xl"
      >
        <SheetHeader className="min-w-0">
          <SheetTitle className="break-words leading-snug">Записать персоналку</SheetTitle>
          <SheetDescription className="break-words leading-snug">{studentName}</SheetDescription>
        </SheetHeader>
        <div
          data-slot="personal-booking-scroll"
          className="min-h-0 min-w-0 w-full max-w-full touch-pan-y overflow-x-hidden overflow-y-auto overscroll-contain p-4 pb-[calc(env(safe-area-inset-bottom)+1rem)] pt-0"
        >
          {open ? (
            <PersonalBookingForm
              studentId={studentId}
              studentName={studentName}
              subscriptions={subscriptions}
              fixedSlot={fixedSlot}
              retryBankPaymentReceipt={retryBankPaymentReceipt}
              onBooked={onBooked}
              onBack={onBack}
              onStaffIntentCreated={onStaffIntentCreated}
              onClose={handleClose}
            />
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}

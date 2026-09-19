import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  OnlinePaymentLinkPanel,
  type BankPaymentOrderLink,
} from "@/components/portal/online-payment-link-panel";
import {
  isLiveBankPaymentOrder,
} from "@/components/portal/payment-link-state";
import apiClient from "@/api/custom-fetch";
import {
  clearContextualCommercialCommandKey,
  getOrCreateContextualCommercialCommandKey,
  type ContextualCommercialCommandScope,
} from "@/api/contextual-commercial-command-key";
import {
  getOnlinePaymentUnavailableMessage,
  getTrainingGroupPaymentSelectionMode,
  hasCanonicalTrainingGroupSelectionCapability,
  hasOnlinePaymentsCapability,
  usePaymentCapabilities,
} from "@/api/payment-capabilities";
import { formatRub, getApiError } from "@/lib/utils";
import { getAuthTokenSubject, useAuthStore } from "@/features/auth/auth-store";
import { QueryStateNotice } from "./query-state-notice";
import { useTrainerIdentity } from "../hooks/use-trainer-identity";

interface TariffItem {
  id: number;
  name: string;
  price: number;
  training_type: {
    id: number;
    name: string;
    slug: string;
    kind: string;
    is_active: boolean;
  };
  trainings_limit: number | null;
  duration_days: number;
  is_active: boolean;
  payout_timing_hint?: string;
  requires_package_owner?: boolean;
}

interface GroupEnrollmentOption {
  schedule_id: number;
  group_name: string;
  trainer_id: number;
  trainer_name: string;
  location_id: number;
  location_name: string;
  training_type_id: number;
  training_type_name: string;
  day_of_week: number;
  start_time: string;
  end_time: string;
  next_occurrence_date: string;
  occurrence_dates: string[];
  is_latest_trial_group: boolean;
  training_group_id?: number | null;
  responsible_trainer_id?: number | null;
  responsible_trainer_name?: string;
  target_group_membership_id?: number | null;
  slot_schedule_ids?: number[];
  upcoming_occurrences?: GroupOccurrence[];
  is_canonical_group_card?: boolean;
}

interface GroupOccurrence {
  schedule_id: number;
  date: string;
  start_time: string;
  end_time: string;
  trainer_id: number;
  trainer_name: string;
  location_id?: number;
  location_name?: string;
}

interface DebtItem {
  id: number;
  checkin_id: number;
  tariff_price: string | number | null;
  reason: string;
  booking_id?: number | null;
  required_tariff_id?: number | null;
}

interface DiscountItem {
  id: number;
  name: string;
  discount_type: "percent" | "fixed";
  value: string | number;
  is_active: boolean;
}

interface PaymentSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  studentId: number;
  studentName: string;
  groupEnrollmentOnly?: boolean;
  dropInBookingId?: number | null;
  requiredTariffId?: number | null;
  requiredDebtId?: number | null;
  exactPersonalDebtSettlement?: ExactPersonalDebtSettlement | null;
  unifiedPersonalSettlement?: boolean;
  onPaymentRecorded?: (result: PaymentRecordedSuccess) => void;
}

/** Immutable payable context returned by the authoritative personal receipt. */
export interface ExactPersonalDebtSettlement {
  readonly bookingId: number;
  readonly debtId: number;
  readonly tariffName: string;
  readonly amount: string | number;
  readonly termsVersion?: string | null;
}

export interface PaymentRecordedSuccess {
  studentId: number;
  paymentId: number;
  message: string;
}

interface PersonalDropInBankPaymentLink {
  bank_payment_order_id: number | null;
}

type BankPaymentResult =
  | { kind: "standard"; order: BankPaymentOrderLink }
  | { kind: "drop-in"; orderId: number };

interface DebtSelectionState {
  studentId: number;
  debtIds: number[];
}

interface DiscountSelectionState {
  studentId: number;
  discountId: number | null;
}

interface GroupTargetSelectionState {
  studentId: number;
  tariffId: number;
  trainingGroupId: number | null;
  scheduleId: number | null;
  startDate: string | null;
}

type PaymentMethod = "cash" | "transfer" | "online";

const debtReasonLabels: Record<string, string> = {
  no_subscription: "Без абонемента",
  no_trainings_left: "Тренировки закончились",
  trial_paid: "Платная пробная",
};

const payoutTimingLabels: Record<string, string> = {
  after_payment_confirmation: "Выплата после подтверждения",
  per_checkin: "Выплата за посещения",
  no_trainer_payout: "Без выплаты тренеру",
  mixed: "Смешанная выплата по пакету",
};

const dayOfWeekLabels = [
  "Пн",
  "Вт",
  "Ср",
  "Чт",
  "Пт",
  "Сб",
  "Вс",
];

function formatGroupTime(value: string) {
  return value.slice(0, 5);
}

function formatGroupDate(value: string) {
  const [year, month, day] = value.split("-");
  return year && month && day ? `${day}.${month}.${year}` : value;
}

function formatDebtReason(reason: string) {
  return debtReasonLabels[reason] ?? reason;
}

function formatDebtAmount(value: string | number | null) {
  if (value == null) {
    return "Без суммы";
  }
  return formatRub(value);
}

function formatPayoutTimingHint(value?: string) {
  if (!value) {
    return null;
  }
  return payoutTimingLabels[value] ?? null;
}

function formatDiscountValue(discount: DiscountItem) {
  const value = Number(discount.value);
  if (!Number.isFinite(value)) {
    return "Некорректная сумма";
  }
  if (discount.discount_type === "percent") {
    return `${value.toLocaleString("ru-RU", { maximumFractionDigits: 2 })}%`;
  }
  return formatRub(value);
}

function toMinorUnits(value: string | number) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return null;
  }
  return Math.round((numeric + Number.EPSILON) * 100);
}

function hasExactPersonalDebtBinding(
  settlement: ExactPersonalDebtSettlement | null,
): settlement is ExactPersonalDebtSettlement {
  if (
    !settlement ||
    !Number.isSafeInteger(settlement.bookingId) ||
    settlement.bookingId <= 0 ||
    !Number.isSafeInteger(settlement.debtId) ||
    settlement.debtId <= 0 ||
    typeof settlement.tariffName !== "string" ||
    !settlement.tariffName.trim() ||
    (typeof settlement.amount !== "string" && typeof settlement.amount !== "number")
  ) {
    return false;
  }

  const amountMinor = toMinorUnits(settlement.amount);
  return amountMinor !== null && amountMinor > 0;
}

function roundRatioHalfEven(numerator: number, denominator: number) {
  const quotient = Math.floor(numerator / denominator);
  const remainder = numerator % denominator;
  const doubledRemainder = remainder * 2;
  if (doubledRemainder > denominator || (doubledRemainder === denominator && quotient % 2 !== 0)) {
    return quotient + 1;
  }
  return quotient;
}

function getDiscountPreview(basePrice: number, discount: DiscountItem | undefined) {
  const basePriceMinor = toMinorUnits(basePrice);
  if (basePriceMinor === null) {
    return { discountAmount: 0, finalAmount: 0 };
  }
  if (!discount) {
    return { discountAmount: 0, finalAmount: basePriceMinor / 100 };
  }

  const discountValueMinor = toMinorUnits(discount.value);
  if (discountValueMinor === null || discountValueMinor <= 0) {
    return { discountAmount: 0, finalAmount: basePriceMinor / 100 };
  }

  const finalAmountMinor =
    discount.discount_type === "percent"
      ? roundRatioHalfEven(
          basePriceMinor * Math.max(0, 10_000 - discountValueMinor),
          10_000,
        )
      : Math.max(0, basePriceMinor - discountValueMinor);

  return {
    discountAmount: (basePriceMinor - finalAmountMinor) / 100,
    finalAmount: finalAmountMinor / 100,
  };
}

function sameIds(left: number[], right: number[]) {
  if (left.length !== right.length) {
    return false;
  }
  const rightIds = new Set(right);
  return left.every((id) => rightIds.has(id));
}

function createPersonalDebtIdempotencyKey() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `personal-debt-${crypto.randomUUID()}`;
  }
  return `personal-debt-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function findBankPaymentOrderForSelection(
  orders: BankPaymentOrderLink[],
  tariffId: number | null,
  debtIds: number[],
  visibleDebtIds: number[],
  targetTrainingGroupId: number | null,
  targetGroupMembershipId: number | null,
  targetScheduleId: number | null,
  targetStartDate: string | null,
): BankPaymentOrderLink | null {
  if (tariffId === null) {
    return null;
  }
  const visibleDebtIdSet = new Set(visibleDebtIds);
  return (
    orders.find((order) => {
      const orderDebtIds = order.debt_ids ?? [];
      const reservedDebtsHidden =
        debtIds.length === 0 &&
        orderDebtIds.length > 0 &&
        orderDebtIds.every((id) => !visibleDebtIdSet.has(id));
      return (
        order.tariff_id === tariffId &&
        (order.target_training_group_id ?? null) === targetTrainingGroupId &&
        (order.target_group_membership_id ?? null) === targetGroupMembershipId &&
        (order.target_schedule_id ?? null) === targetScheduleId &&
        (order.target_start_date ?? null) === targetStartDate &&
        (sameIds(orderDebtIds, debtIds) || reservedDebtsHidden)
      );
    }) ?? null
  );
}

export function PaymentSheet({
  open,
  onOpenChange,
  studentId,
  studentName,
  groupEnrollmentOnly = false,
  dropInBookingId = null,
  requiredTariffId = null,
  requiredDebtId = null,
  exactPersonalDebtSettlement = null,
  unifiedPersonalSettlement = false,
  onPaymentRecorded,
}: PaymentSheetProps) {
  const clubId = useAuthStore((state) => state.clubId);
  const accessToken = useAuthStore((state) => state.accessToken);
  const queryClient = useQueryClient();
  const paymentCapabilitiesQuery = usePaymentCapabilities();
  const [selectedTariffId, setSelectedTariffId] = useState<number | null>(null);
  const [selectedDebtSelection, setSelectedDebtSelection] = useState<DebtSelectionState>({
    studentId,
    debtIds: [],
  });
  const [selectedDiscountSelection, setSelectedDiscountSelection] =
    useState<DiscountSelectionState>({ studentId, discountId: null });
  const [paymentMethod, setPaymentMethod] = useState<PaymentMethod>("cash");
  const [bankPaymentOrder, setBankPaymentOrder] =
    useState<BankPaymentOrderLink | null>(null);
  const [dropInBankPaymentOrderId, setDropInBankPaymentOrderId] = useState<number | null>(null);
  const [hiddenBankPaymentOrderIds, setHiddenBankPaymentOrderIds] = useState<number[]>([]);
  const [groupTargetSelection, setGroupTargetSelection] =
    useState<GroupTargetSelectionState | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const exactSettlementKeyRef = useRef<{ fingerprint: string; value: string } | null>(null);
  const contextualGroupCommandScopeRef = useRef<ContextualCommercialCommandScope | null>(null);
  const commandSubmitInFlightRef = useRef(false);
  const hasExactPersonalDebtSettlement = exactPersonalDebtSettlement !== null;
  const exactPersonalDebtBindingAvailable = hasExactPersonalDebtBinding(
    exactPersonalDebtSettlement,
  );
  const effectiveDropInBookingId = exactPersonalDebtSettlement?.bookingId ?? dropInBookingId;
  const effectiveRequiredDebtId = exactPersonalDebtSettlement?.debtId ?? requiredDebtId;
  const hasDropInContext =
    hasExactPersonalDebtSettlement || dropInBookingId !== null || requiredTariffId !== null;
  const isDropInMode =
    hasExactPersonalDebtSettlement ||
    (dropInBookingId !== null && requiredTariffId !== null);
  const hasIncompleteDropInContext =
    (hasDropInContext && !isDropInMode) ||
    (hasExactPersonalDebtSettlement && !exactPersonalDebtBindingAvailable);
  const incompleteDropInContextMessage =
    hasExactPersonalDebtSettlement && !exactPersonalDebtBindingAvailable
      ? "Не удалось подтвердить точный долг персоналки. Обновите данные перед приёмом оплаты."
      : dropInBookingId === null
      ? "Не удалось открыть оплату персоналки: отсутствует идентификатор записи. Обновите карточку ученика и попробуйте снова."
      : "Не удалось открыть оплату персоналки: отсутствует обязательный тариф. Обновите карточку ученика и попробуйте снова.";

  const tariffsQuery = useQuery<TariffItem[]>({
    queryKey: ["tariffs"],
    queryFn: () =>
      apiClient.get("/billing/tariffs/").then((r) => r.data.items ?? r.data),
    staleTime: 5 * 60_000,
    enabled: open && !hasExactPersonalDebtSettlement,
  });

  const debtsQuery = useQuery<DebtItem[]>({
    queryKey: ["billing", "debts", studentId],
    queryFn: () =>
      apiClient
        .get(`/billing/debts/?student_id=${studentId}`)
        .then((r) => r.data.items ?? r.data),
    staleTime: 30_000,
    enabled: open && Boolean(studentId),
  });

  const {
    data: discounts,
    isLoading: discountsLoading,
    isError: discountsError,
    isSuccess: discountsReady,
    isFetching: discountsFetching,
    isRefetchError: discountsRefetchError,
    refetch: refetchDiscounts,
  } = useQuery<DiscountItem[]>({
    queryKey: ["billing", "discounts"],
    queryFn: () => apiClient.get("/billing/discounts/").then((r) => r.data.items ?? r.data),
    staleTime: 5 * 60_000,
    enabled: open && !hasExactPersonalDebtSettlement,
  });

  const bankOrdersQuery = useQuery<BankPaymentOrderLink[]>({
    queryKey: ["billing", "bank-payment-orders", studentId, "live"],
    queryFn: () =>
      apiClient
        .get("/billing/bank-payment-orders/", {
          params: { student_id: studentId, status: "live" },
        })
        .then((r) => r.data.items ?? r.data),
    staleTime: 30_000,
    enabled: open && Boolean(studentId) && !hasDropInContext,
  });

  const dropInBankPaymentOrderQuery = useQuery<BankPaymentOrderLink>({
    queryKey: ["billing", "bank-payment-orders", "personal-drop-in", dropInBankPaymentOrderId],
    queryFn: () =>
      apiClient
        .get<BankPaymentOrderLink>(`/billing/bank-payment-orders/${dropInBankPaymentOrderId}/`)
        .then((response) => response.data),
    enabled: open && dropInBankPaymentOrderId !== null,
    staleTime: 0,
  });

  const trainerQuery = useTrainerIdentity();
  const tariffs = tariffsQuery.data;
  const debts = debtsQuery.data;
  const pendingBankPaymentOrders = hasDropInContext ? [] : bankOrdersQuery.data ?? [];
  const trainerMe = trainerQuery.data;
  const tariffsUnavailable = tariffsQuery.isError || tariffsQuery.isRefetchError;
  const debtsUnavailable =
    !hasExactPersonalDebtSettlement && (debtsQuery.isError || debtsQuery.isRefetchError);
  const bankOrdersUnavailable =
    !hasDropInContext && (bankOrdersQuery.isError || bankOrdersQuery.isRefetchError);
  const trainerUnavailable = trainerQuery.isError || trainerQuery.isRefetchError;
  const discountsUnavailable = discountsError || discountsRefetchError;
  const tariffsReady = tariffsQuery.isSuccess && !tariffsQuery.isRefetchError;
  // The commercial-context receipt is the binding source for exact personal debt.
  // The generic debt list can legitimately omit it after check-in and must not
  // disable the server-validated exact-debt command.
  const debtsReady =
    hasExactPersonalDebtSettlement ||
    (debtsQuery.isSuccess && !debtsQuery.isRefetchError);
  const bankOrdersReady =
    hasDropInContext || (bankOrdersQuery.isSuccess && !bankOrdersQuery.isRefetchError);
  const trainerReady = trainerQuery.isSuccess && !trainerQuery.isRefetchError;

  const activeTariffs = tariffs?.filter((t) => t.is_active) ?? [];
  const visibleTariffs = hasExactPersonalDebtSettlement
    ? []
    : isDropInMode
    ? activeTariffs.filter((tariff) => tariff.id === requiredTariffId)
    : groupEnrollmentOnly
    ? activeTariffs.filter((tariff) => tariff.training_type.kind === "group")
    : activeTariffs;
  const activeDiscounts = discounts?.filter((discount) => discount.is_active) ?? [];
  const dropInDebts = (debts ?? []).filter((debt) => debt.booking_id != null);
  const openDebts = isDropInMode
    ? debts ?? []
    : (debts ?? []).filter((debt) => debt.booking_id == null);
  const selectedDebtIds = isDropInMode && effectiveRequiredDebtId !== null
    ? [effectiveRequiredDebtId]
    :
    selectedDebtSelection.studentId === studentId
      ? selectedDebtSelection.debtIds
      : [];
  const effectiveTariffId = isDropInMode
    ? hasExactPersonalDebtSettlement
      ? null
      : requiredTariffId
    : hasIncompleteDropInContext
      ? null
      : selectedTariffId;
  const selectedTariff = activeTariffs.find((t) => t.id === effectiveTariffId);
  const lockedDropInDebt = effectiveRequiredDebtId === null
    ? null
    : openDebts.find((debt) => debt.id === effectiveRequiredDebtId) ?? null;
  const requiredDropInDebtAvailable = hasExactPersonalDebtSettlement
    ? exactPersonalDebtBindingAvailable
    : effectiveRequiredDebtId === null ||
      (lockedDropInDebt !== null &&
        lockedDropInDebt.booking_id === effectiveDropInBookingId &&
        lockedDropInDebt.required_tariff_id === requiredTariffId);
  const requiresGroupTarget = selectedTariff?.training_type.kind === "group";
  const groupPaymentSelectionMode = getTrainingGroupPaymentSelectionMode(
    paymentCapabilitiesQuery,
  );
  const groupPaymentSelectionEnabled = groupPaymentSelectionMode !== "disabled";
  const groupPaymentSelectionCapabilityUnavailable =
    !paymentCapabilitiesQuery.isSuccess || paymentCapabilitiesQuery.isRefetchError;
  const groupPaymentSelectionUnavailableMessage = groupPaymentSelectionCapabilityUnavailable
    ? "Не удалось подтвердить режим выбора группы. Новые оплаты в группу заблокированы; уже созданные ссылки можно открыть или отменить ниже."
    : "Новые оплаты в группу временно отключены на сервере. Уже созданные ссылки можно открыть или отменить ниже.";
  const groupOptionsQuery = useQuery<GroupEnrollmentOption[]>({
    queryKey: ["billing", "group-enrollment-options", studentId, effectiveTariffId],
    queryFn: () =>
      apiClient
        .get("/billing/group-enrollment-options/", {
          params: { student_id: studentId, tariff_id: effectiveTariffId },
        })
        .then((r) => r.data),
    enabled:
      open &&
      requiresGroupTarget &&
      groupPaymentSelectionEnabled &&
      effectiveTariffId !== null,
    staleTime: 30_000,
  });
  const groupOptions = groupPaymentSelectionEnabled ? groupOptionsQuery.data ?? [] : [];
  const canonicalGroupSelectionEnabled = hasCanonicalTrainingGroupSelectionCapability(
    paymentCapabilitiesQuery,
  );
  const selectionMatchesContext =
    groupTargetSelection?.studentId === studentId &&
    groupTargetSelection.tariffId === effectiveTariffId;
  const selectedGroupOption = selectionMatchesContext
    ? groupOptions.find((option) => {
        if (
          canonicalGroupSelectionEnabled &&
          option.is_canonical_group_card === true &&
          option.training_group_id != null
        ) {
          return option.training_group_id === groupTargetSelection.trainingGroupId;
        }
        return option.schedule_id === groupTargetSelection.scheduleId;
      })
    : undefined;
  const selectedGroupOccurrence =
    selectedGroupOption && groupTargetSelection?.scheduleId != null && groupTargetSelection.startDate
      ? (selectedGroupOption.upcoming_occurrences ?? []).find(
          (occurrence) =>
            occurrence.schedule_id === groupTargetSelection.scheduleId &&
            occurrence.date === groupTargetSelection.startDate,
        ) ??
        (selectedGroupOption.schedule_id === groupTargetSelection.scheduleId &&
        selectedGroupOption.occurrence_dates.includes(groupTargetSelection.startDate)
          ? {
              schedule_id: selectedGroupOption.schedule_id,
              date: groupTargetSelection.startDate,
              start_time: selectedGroupOption.start_time,
              end_time: selectedGroupOption.end_time,
              trainer_id: selectedGroupOption.trainer_id,
              trainer_name: selectedGroupOption.trainer_name,
              location_id: selectedGroupOption.location_id,
              location_name: selectedGroupOption.location_name,
            }
          : undefined)
      : undefined;
  const selectedTargetStartDate = selectedGroupOccurrence?.date ?? "";
  const selectedCanonicalGroup =
    canonicalGroupSelectionEnabled && selectedGroupOption?.is_canonical_group_card === true
      ? selectedGroupOption
      : undefined;
  const onlinePaymentsEnabled = hasOnlinePaymentsCapability(paymentCapabilitiesQuery);
  const selectedDiscountCandidateId =
    hasExactPersonalDebtSettlement ||
    unifiedPersonalSettlement ||
    paymentMethod === "online" ||
    selectedDiscountSelection.studentId !== studentId
      ? null
      : selectedDiscountSelection.discountId;
  const selectedDiscount = activeDiscounts.find(
    (discount) => discount.id === selectedDiscountCandidateId,
  );
  const selectedDiscountId = selectedDiscount?.id ?? null;
  const discountPreview = selectedTariff
    ? getDiscountPreview(selectedTariff.price, selectedDiscount)
    : null;
  const discountAmount = discountPreview?.discountAmount ?? 0;
  const finalPaymentAmount = discountPreview?.finalAmount ?? null;
  const exactSettlementAmountMinor = exactPersonalDebtSettlement
    ? toMinorUnits(exactPersonalDebtSettlement.amount)
    : null;
  const exactSettlementAmountIsValid =
    exactSettlementAmountMinor !== null && exactSettlementAmountMinor > 0;
  const packageOwnerTrainerName =
    [trainerMe?.first_name, trainerMe?.last_name].filter(Boolean).join(" ") ||
    "текущим тренером";
  const packageOwnerTracked =
    selectedTariff?.requires_package_owner === true ||
    selectedTariff?.training_type.kind === "personal" ||
    selectedTariff?.training_type.kind === "mini_group";
  const canSubmitPayment =
    !hasIncompleteDropInContext &&
    (hasExactPersonalDebtSettlement || Boolean(effectiveTariffId)) &&
    (hasExactPersonalDebtSettlement || tariffsReady) &&
    debtsReady &&
    bankOrdersReady &&
    (hasExactPersonalDebtSettlement || trainerReady) &&
    (hasExactPersonalDebtSettlement
      ? exactPersonalDebtBindingAvailable && exactSettlementAmountIsValid
      : finalPaymentAmount !== null && finalPaymentAmount > 0) &&
    (hasExactPersonalDebtSettlement ||
      unifiedPersonalSettlement ||
      paymentMethod === "online" ||
      (discountsReady && !discountsRefetchError)) &&
    (hasExactPersonalDebtSettlement || !packageOwnerTracked || Boolean(trainerMe?.id)) &&
    (paymentMethod !== "online" || onlinePaymentsEnabled) &&
    (!isDropInMode || requiredDropInDebtAvailable) &&
    (!requiresGroupTarget ||
      (groupPaymentSelectionEnabled &&
        groupOptionsQuery.isSuccess && !groupOptionsQuery.isRefetchError &&
        Boolean(selectedGroupOption) &&
        Boolean(selectedTargetStartDate)));

  function exactSettlementIdempotencyKey(method: PaymentMethod) {
    if (
      !hasExactPersonalDebtSettlement ||
      !exactPersonalDebtBindingAvailable ||
      effectiveDropInBookingId === null ||
      effectiveRequiredDebtId === null
    ) {
      throw new Error("Не удалось подтвердить точный долг персоналки");
    }
    const fingerprint = [
      effectiveDropInBookingId,
      effectiveRequiredDebtId,
      method,
      exactPersonalDebtSettlement.amount,
      exactPersonalDebtSettlement.termsVersion ?? "",
    ].join(":");
    if (exactSettlementKeyRef.current?.fingerprint !== fingerprint) {
      exactSettlementKeyRef.current = {
        fingerprint,
        value: createPersonalDebtIdempotencyKey(),
      };
    }
    return exactSettlementKeyRef.current.value;
  }

  function contextualGroupCommandScope(
    method: "cash" | "transfer" | "sbp",
    tariffId: number,
  ): ContextualCommercialCommandScope {
    if (!selectedGroupOccurrence || !selectedTargetStartDate) {
      throw new Error("Не удалось подтвердить группу и дату старта");
    }
    return {
      clubId,
      actorSubject: getAuthTokenSubject(accessToken),
      audience: "staff",
      kind: "group_sale",
      protocolVersion: "v1",
      studentId,
      paymentMethod: method,
      tariffId,
      discountIds: selectedDiscountId === null ? [] : [selectedDiscountId],
      debtIds: selectedDebtIds,
      trainingGroupId: selectedCanonicalGroup?.training_group_id ?? undefined,
      scheduleId: selectedGroupOccurrence.schedule_id,
      startDate: selectedTargetStartDate,
    };
  }

  const paymentMutation = useMutation({
    mutationFn: () => {
      contextualGroupCommandScopeRef.current = null;
      if (hasIncompleteDropInContext) {
        throw new Error(incompleteDropInContextMessage);
      }
      if (
        !debtsReady ||
        !bankOrdersReady ||
        (!hasExactPersonalDebtSettlement && (!tariffsReady || !trainerReady))
      ) {
        throw new Error("Payment prerequisites are unavailable");
      }
      if (hasExactPersonalDebtSettlement) {
        if (
          effectiveDropInBookingId === null ||
          effectiveRequiredDebtId === null ||
          !exactPersonalDebtBindingAvailable ||
          !exactSettlementAmountIsValid
        ) {
          throw new Error("Не удалось подтвердить точный долг персоналки");
        }
        if (paymentMethod === "online") {
          throw new Error("Для точного долга доступна только ручная фиксация оплаты");
        }
        return apiClient.post(
          `/personal-drop-in-bookings/${effectiveDropInBookingId}/payments/`,
          {
            payment_method: paymentMethod,
            debt_id: effectiveRequiredDebtId,
            discount_ids: [],
            idempotency_key: exactSettlementIdempotencyKey(paymentMethod),
          },
        );
      }
      if (effectiveTariffId === null) {
        throw new Error("Tariff is required before accepting payment");
      }
      if (requiresGroupTarget && !groupPaymentSelectionEnabled) {
        throw new Error(groupPaymentSelectionUnavailableMessage);
      }
      if (paymentMethod === "online") {
        throw new Error("Online payments use bank payment orders");
      }

      if (isDropInMode) {
        return apiClient.post(`/personal-drop-in-bookings/${effectiveDropInBookingId}/payments/`, {
          payment_method: paymentMethod,
          discount_ids:
            unifiedPersonalSettlement || selectedDiscountId === null
              ? []
              : [selectedDiscountId],
        });
      }

      const payload: {
        student_id: number;
        tariff_id: number;
        payment_method: "cash" | "transfer";
        discount_ids: number[];
        debt_ids: number[];
        seller_trainer_id: number | null;
        package_owner_trainer_id?: number | null;
        target_training_group_id?: number;
        target_schedule_id?: number;
        target_start_date?: string;
        idempotency_key?: string;
      } = {
        student_id: studentId,
        tariff_id: effectiveTariffId,
        payment_method: paymentMethod,
        discount_ids: selectedDiscountId === null ? [] : [selectedDiscountId],
        debt_ids: selectedDebtIds,
        seller_trainer_id: trainerMe?.id ?? null,
      };

      if (packageOwnerTracked) {
        payload.package_owner_trainer_id = trainerMe?.id ?? null;
      }
      if (requiresGroupTarget && selectedGroupOccurrence) {
        if (selectedCanonicalGroup?.training_group_id != null) {
          payload.target_training_group_id = selectedCanonicalGroup.training_group_id;
        }
        payload.target_schedule_id = selectedGroupOccurrence.schedule_id;
        payload.target_start_date = selectedTargetStartDate;
        const commandScope = contextualGroupCommandScope(paymentMethod, effectiveTariffId);
        payload.idempotency_key = getOrCreateContextualCommercialCommandKey(commandScope);
        contextualGroupCommandScopeRef.current = commandScope;
      }

      return apiClient.post("/billing/payments/", payload);
    },
    onSuccess: async (response) => {
      if (contextualGroupCommandScopeRef.current) {
        clearContextualCommercialCommandKey(contextualGroupCommandScopeRef.current);
      }
      const targetGroupName = selectedGroupOption?.group_name;
      const invalidations = [
        queryClient.invalidateQueries({ queryKey: ["billing", "payment-capabilities"] }),
        queryClient.invalidateQueries({ queryKey: ["student", String(studentId)] }),
        queryClient.invalidateQueries({
          queryKey: ["student", String(studentId), "subscriptions"],
        }),
        queryClient.invalidateQueries({ queryKey: ["billing", "debts", studentId] }),
        queryClient.invalidateQueries({
          queryKey: ["student", String(studentId), "commercial-context"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["billing", "bank-payment-orders", studentId, "live"],
        }),
        queryClient.invalidateQueries({ queryKey: ["leads"] }),
        queryClient.invalidateQueries({ queryKey: ["retention-tasks"] }),
        queryClient.invalidateQueries({ queryKey: ["schedules"] }),
        queryClient.invalidateQueries({
          queryKey: ["billing", "group-enrollment-options", studentId, effectiveTariffId],
        }),
      ];
      if (selectedGroupOccurrence) {
        invalidations.push(
          queryClient.invalidateQueries({
            queryKey: ["schedule", String(selectedGroupOccurrence.schedule_id), "students"],
          }),
        );
      }
      await Promise.all(invalidations);
      setSelectedTariffId(null);
      setSelectedDebtSelection({ studentId, debtIds: [] });
      setSelectedDiscountSelection({ studentId, discountId: null });
      setGroupTargetSelection(null);
      setErrorMsg(null);
      exactSettlementKeyRef.current = null;
      if (targetGroupName) {
        onPaymentRecorded?.({
          studentId: response.data.student_id ?? studentId,
          paymentId: response.data.id,
          message: `Оплата ожидает подтверждения. Ученик записан в ${targetGroupName}.`,
        });
      }
      onOpenChange(false);
    },
    onError: async (error: unknown) => {
      await Promise.all([
        paymentCapabilitiesQuery.refetch(),
        groupOptionsQuery.refetch(),
        bankOrdersQuery.refetch(),
      ]);
      setErrorMsg(getApiError(error, "Ошибка при создании оплаты"));
    },
    onSettled: () => {
      commandSubmitInFlightRef.current = false;
    },
  });

  const bankPaymentMutation = useMutation({
    mutationFn: async () => {
      contextualGroupCommandScopeRef.current = null;
      if (hasIncompleteDropInContext) {
        throw new Error(incompleteDropInContextMessage);
      }
      if (
        !debtsReady ||
        !bankOrdersReady ||
        (!hasExactPersonalDebtSettlement && (!tariffsReady || !trainerReady))
      ) {
        throw new Error("Payment link prerequisites are unavailable");
      }
      if (hasExactPersonalDebtSettlement) {
        if (
          effectiveDropInBookingId === null ||
          effectiveRequiredDebtId === null ||
          !exactPersonalDebtBindingAvailable ||
          !exactSettlementAmountIsValid
        ) {
          throw new Error("Не удалось подтвердить точный долг персоналки");
        }
        if (!onlinePaymentsEnabled) {
          throw new Error("Online payment capability is unavailable");
        }
        const response = await apiClient.post<PersonalDropInBankPaymentLink>(
          `/personal-drop-in-bookings/${effectiveDropInBookingId}/bank-payment-orders/`,
          {
            debt_id: effectiveRequiredDebtId,
            idempotency_key: exactSettlementIdempotencyKey("online"),
          },
        );
        if (!response.data.bank_payment_order_id) {
          throw new Error("Банк не вернул идентификатор заказа персоналки");
        }
        return { kind: "drop-in", orderId: response.data.bank_payment_order_id } satisfies BankPaymentResult;
      }
      if (effectiveTariffId === null) {
        throw new Error("Tariff is required before creating payment link");
      }
      if (requiresGroupTarget && !groupPaymentSelectionEnabled) {
        throw new Error(groupPaymentSelectionUnavailableMessage);
      }
      if (!onlinePaymentsEnabled) {
        throw new Error("Online payment capability is unavailable");
      }

      if (isDropInMode) {
        const response = await apiClient.post<PersonalDropInBankPaymentLink>(
          `/personal-drop-in-bookings/${dropInBookingId}/bank-payment-orders/`,
          {},
        );
        if (!response.data.bank_payment_order_id) {
          throw new Error("Банк не вернул идентификатор заказа персоналки");
        }
        return { kind: "drop-in", orderId: response.data.bank_payment_order_id } satisfies BankPaymentResult;
      }

      const payload: {
        student_id: number;
        tariff_id: number;
        discount_ids: number[];
        debt_ids: number[];
        seller_trainer_id: number | null;
        package_owner_trainer_id?: number | null;
        target_training_group_id?: number;
        target_schedule_id?: number;
        target_start_date?: string;
        idempotency_key?: string;
      } = {
        student_id: studentId,
        tariff_id: effectiveTariffId,
        discount_ids: [],
        debt_ids: selectedDebtIds,
        seller_trainer_id: trainerMe?.id ?? null,
      };

      if (packageOwnerTracked) {
        payload.package_owner_trainer_id = trainerMe?.id ?? null;
      }
      if (requiresGroupTarget && selectedGroupOccurrence) {
        if (selectedCanonicalGroup?.training_group_id != null) {
          payload.target_training_group_id = selectedCanonicalGroup.training_group_id;
        }
        payload.target_schedule_id = selectedGroupOccurrence.schedule_id;
        payload.target_start_date = selectedTargetStartDate;
        const commandScope = contextualGroupCommandScope("sbp", effectiveTariffId);
        payload.idempotency_key = getOrCreateContextualCommercialCommandKey(commandScope);
        contextualGroupCommandScopeRef.current = commandScope;
      }

      const response = await apiClient.post<BankPaymentOrderLink>(
        "/billing/bank-payment-orders/",
        payload,
      );
      return { kind: "standard", order: response.data } satisfies BankPaymentResult;
    },
    onSuccess: async (result) => {
      if (contextualGroupCommandScopeRef.current) {
        clearContextualCommercialCommandKey(contextualGroupCommandScopeRef.current);
      }
      if (result.kind === "drop-in") {
        setDropInBankPaymentOrderId(result.orderId);
      } else {
        setBankPaymentOrder(result.order);
        setHiddenBankPaymentOrderIds((current) =>
          current.filter((orderId) => orderId !== result.order.id),
        );
      }
      setErrorMsg(null);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["billing", "payment-capabilities"],
        }),
        !isDropInMode
          ? queryClient.invalidateQueries({
              queryKey: ["billing", "bank-payment-orders", studentId],
            })
          : Promise.resolve(),
        queryClient.invalidateQueries({
          queryKey: ["student", String(studentId), "subscriptions"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["student", String(studentId), "personal-bookings"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["student", String(studentId), "commercial-context"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["billing", "group-enrollment-options", studentId, effectiveTariffId],
        }),
      ]);
    },
    onError: async (error: unknown) => {
      setBankPaymentOrder(null);
      setDropInBankPaymentOrderId(null);
      await Promise.all([
        paymentCapabilitiesQuery.refetch(),
        groupOptionsQuery.refetch(),
        bankOrdersQuery.refetch(),
      ]);
      setErrorMsg(getApiError(error, "Ошибка при создании ссылки"));
    },
    onSettled: () => {
      commandSubmitInFlightRef.current = false;
    },
  });

  const cancelBankPaymentMutation = useMutation({
    mutationFn: async (order: BankPaymentOrderLink) => {
      if (!bankOrdersReady) {
        throw new Error("Bank payment order data is unavailable");
      }
      const response = await apiClient.post<BankPaymentOrderLink>(
        `/billing/bank-payment-orders/${order.id}/cancel/`,
        {},
      );
      return response.data;
    },
    onMutate: (order) => {
      setHiddenBankPaymentOrderIds((current) =>
        current.includes(order.id) ? current : [...current, order.id],
      );
    },
    onSuccess: async (order) => {
      setBankPaymentOrder((current) => (current?.id === order.id ? null : current));
      setDropInBankPaymentOrderId((current) => (current === order.id ? null : current));
      setErrorMsg(null);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["billing", "bank-payment-orders", studentId],
        }),
        queryClient.invalidateQueries({ queryKey: ["billing", "debts", studentId] }),
        queryClient.invalidateQueries({
          queryKey: ["student", String(studentId), "subscriptions"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["billing", "group-enrollment-options", studentId, effectiveTariffId],
        }),
      ]);
    },
    onError: (error: unknown, order) => {
      setHiddenBankPaymentOrderIds((current) =>
        current.filter((orderId) => orderId !== order.id),
      );
      setErrorMsg(getApiError(error, "Ошибка при отмене ссылки"));
    },
  });

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) {
      setSelectedTariffId(null);
      setSelectedDebtSelection({ studentId, debtIds: [] });
      setSelectedDiscountSelection({ studentId, discountId: null });
      setBankPaymentOrder(null);
      setDropInBankPaymentOrderId(null);
      setHiddenBankPaymentOrderIds([]);
      setGroupTargetSelection(null);
      setErrorMsg(null);
      exactSettlementKeyRef.current = null;
    }
    onOpenChange(nextOpen);
  }

  function handlePaymentMethodChange(method: PaymentMethod) {
    if (method === "online" && !onlinePaymentsEnabled) {
      return;
    }
    if (hasExactPersonalDebtSettlement && method === paymentMethod) return;
    setPaymentMethod(method);
    if (method === "online") {
      setSelectedDiscountSelection({ studentId, discountId: null });
    }
    setBankPaymentOrder(null);
    setDropInBankPaymentOrderId(null);
    setErrorMsg(null);
    if (hasExactPersonalDebtSettlement) {
      exactSettlementKeyRef.current = null;
    }
  }

  function toggleDebt(debtId: number) {
    setSelectedDebtSelection((current) => {
      const currentDebtIds = current.studentId === studentId ? current.debtIds : [];
      return {
        studentId,
        debtIds: currentDebtIds.includes(debtId)
          ? currentDebtIds.filter((id) => id !== debtId)
          : [...currentDebtIds, debtId],
      };
    });
    setBankPaymentOrder(null);
    setDropInBankPaymentOrderId(null);
    setErrorMsg(null);
  }

  function handleDiscountChange(discountId: number | null) {
    setSelectedDiscountSelection({ studentId, discountId });
    setErrorMsg(null);
  }

  function handleSubmit() {
    if (commandSubmitInFlightRef.current) {
      return;
    }
    if (hasIncompleteDropInContext) {
      setErrorMsg(incompleteDropInContextMessage);
      return;
    }
    if (requiresGroupTarget && !groupPaymentSelectionEnabled) {
      setErrorMsg(groupPaymentSelectionUnavailableMessage);
      return;
    }
    if (paymentMethod === "online") {
      commandSubmitInFlightRef.current = true;
      bankPaymentMutation.mutate();
      return;
    }
    commandSubmitInFlightRef.current = true;
    paymentMutation.mutate();
  }

  const isSubmitting = paymentMutation.isPending || bankPaymentMutation.isPending;
  const bankPaymentOrderCandidates = [
    ...(bankPaymentOrder ? [bankPaymentOrder] : []),
    ...pendingBankPaymentOrders,
  ].filter(
    (order) =>
      isLiveBankPaymentOrder(order) && !hiddenBankPaymentOrderIds.includes(order.id),
  );
  const matchingBankPaymentOrder = findBankPaymentOrderForSelection(
    bankPaymentOrderCandidates,
    effectiveTariffId,
    selectedDebtIds,
    openDebts.map((debt) => debt.id),
    selectedCanonicalGroup?.training_group_id ?? null,
    selectedCanonicalGroup?.target_group_membership_id ?? null,
    requiresGroupTarget ? selectedGroupOccurrence?.schedule_id ?? null : null,
    requiresGroupTarget ? selectedTargetStartDate || null : null,
  );
  const activeBankPaymentOrder =
    matchingBankPaymentOrder !== null &&
    isLiveBankPaymentOrder(matchingBankPaymentOrder)
      ? matchingBankPaymentOrder
      : null;
  const creatingBankPaymentOrder =
    matchingBankPaymentOrder !== null && activeBankPaymentOrder === null
      ? matchingBankPaymentOrder
      : null;
  const conflictingBankPaymentOrder =
    matchingBankPaymentOrder === null && effectiveTariffId !== null
      ? bankPaymentOrderCandidates.find(
          (order) => order.tariff_id === effectiveTariffId,
        ) ?? null
      : null;
  const displayedBankPaymentOrder =
    activeBankPaymentOrder ?? creatingBankPaymentOrder ?? conflictingBankPaymentOrder;
  const canCreateBankPaymentOrder =
    !hasIncompleteDropInContext &&
    (paymentMethod !== "online" ||
          (onlinePaymentsEnabled &&
        (isDropInMode
          ? dropInBankPaymentOrderId === null
          : bankOrdersReady &&
            matchingBankPaymentOrder === null &&
            conflictingBankPaymentOrder === null &&
            !cancelBankPaymentMutation.isPending)));

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent
        side="bottom"
        className="max-h-[92dvh] overflow-hidden"
      >
        <SheetHeader>
          <SheetTitle className="break-words leading-snug">
            {groupEnrollmentOnly ? "Оформить в группу" : "Принять оплату"}
          </SheetTitle>
          <SheetDescription className="break-words leading-snug">
            {studentName}
          </SheetDescription>
        </SheetHeader>

        <div className="flex min-h-0 flex-col gap-4 overflow-y-auto p-4 pb-[calc(env(safe-area-inset-bottom)+1rem)] pt-0">
          {hasIncompleteDropInContext ? (
            <p role="alert" className="rounded-xl bg-destructive/10 p-3 text-[13px] text-destructive">
              {incompleteDropInContextMessage}
            </p>
          ) : null}
          {/* Tariff list */}
          {!hasExactPersonalDebtSettlement && tariffsUnavailable ? (
            <QueryStateNotice
              title={tariffs ? "Тарифы могли устареть" : "Не удалось загрузить тарифы"}
              message="Выбор и отправка оплаты заблокированы до успешного обновления тарифов."
              retryLabel="Повторить загрузку тарифов"
              retrying={tariffsQuery.isFetching}
              onRetry={() => void tariffsQuery.refetch()}
            />
          ) : null}
          {!hasExactPersonalDebtSettlement && (tariffsQuery.isLoading && !tariffs ? (
            <p className="ui-muted-14">Загрузка тарифов...</p>
          ) : tariffsUnavailable && !tariffs ? null : visibleTariffs.length === 0 ? (
            <p className="ui-muted-14">
              Нет доступных тарифов
            </p>
          ) : (
            <div className="flex flex-col gap-2 max-h-[40vh] overflow-y-auto">
              {visibleTariffs.map((tariff) => (
                <button
                  key={tariff.id}
                  type="button"
                  disabled={hasDropInContext}
                  aria-pressed={effectiveTariffId === tariff.id}
                  onClick={() => {
                    if (hasDropInContext) return;
                    setSelectedTariffId(tariff.id);
                    setGroupTargetSelection(null);
                    setBankPaymentOrder(null);
                    setErrorMsg(null);
                  }}
                  className={`flex items-center justify-between rounded-xl p-3 text-left ring-1 transition-colors ${
                    effectiveTariffId === tariff.id
                      ? "bg-[var(--branding-accent)]/10 ring-[var(--branding-accent)]"
                      : "bg-white ring-foreground/5 active:bg-muted"
                  }`}
                >
                  <div className="min-w-0 flex-1 pr-3">
                    <p className="text-[14px] font-medium text-foreground break-words">
                      {tariff.name}
                    </p>
                    <p className="text-[12px] text-muted-foreground break-words">
                      {tariff.training_type.name}
                      {tariff.trainings_limit != null
                        ? ` / ${tariff.trainings_limit} трен.`
                        : ""}
                      {" / "}
                      {tariff.duration_days} дн.
                    </p>
                    {formatPayoutTimingHint(tariff.payout_timing_hint) && (
                      <p className="text-[12px] text-muted-foreground break-words">
                        {formatPayoutTimingHint(tariff.payout_timing_hint)}
                      </p>
                    )}
                  </div>
                  <Badge
                    variant="secondary"
                    className="shrink-0 text-[14px] font-semibold"
                  >
                    {formatRub(tariff.price)}
                  </Badge>
                </button>
              ))}
            </div>
          ))}

          {hasExactPersonalDebtSettlement && exactPersonalDebtBindingAvailable ? (
            <section aria-label="Зафиксированная сумма оплаты" className="rounded-lg bg-muted/50 p-3">
              <p className="text-[14px] font-medium text-foreground">
                {exactPersonalDebtSettlement.tariffName}
              </p>
              <div className="mt-2 flex items-center justify-between gap-3 border-t border-foreground/10 pt-2">
                <span className="ui-caption-label">К оплате</span>
                <span className="ui-title-20">{formatRub(exactPersonalDebtSettlement.amount)}</span>
              </div>
              <p className="mt-1 text-[12px] text-muted-foreground">
                Сумма и условия зафиксированы в записи персоналки.
              </p>
            </section>
          ) : null}

          {isDropInMode ? (
            <p className="ui-muted-status">
              {hasExactPersonalDebtSettlement
                ? "Сумма и долг этой персоналки зафиксированы. Скидки и другой долг выбрать нельзя."
                : "Тариф и долг этой персоналки зафиксированы. Другую оплату или долг выбрать нельзя."}
            </p>
          ) : null}

          {!hasExactPersonalDebtSettlement && trainerUnavailable ? (
            <QueryStateNotice
              title={trainerMe ? "Профиль тренера мог устареть" : "Не удалось подтвердить профиль тренера"}
              message="Без подтверждённого профиля нельзя указать продавца и владельца пакета."
              retryLabel="Повторить загрузку профиля"
              retrying={trainerQuery.isFetching}
              onRetry={() => void trainerQuery.refetch()}
            />
          ) : null}

          {bankOrdersUnavailable ? (
            <QueryStateNotice
              title={
                bankOrdersQuery.data
                  ? "Данные об активных ссылках могли устареть"
                  : "Не удалось проверить активные ссылки"
              }
              message="Новая ссылка и ручная оплата заблокированы, чтобы не создать дубликат и не обойти резерв долга."
              retryLabel="Повторить проверку ссылок"
              retrying={bankOrdersQuery.isFetching}
              onRetry={() => void bankOrdersQuery.refetch()}
            />
          ) : null}

          {requiresGroupTarget ? (
            <section aria-label="Постоянная группа" className="ui-col-2">
              <p className="ui-caption-label">
                Постоянная группа
              </p>
              {!groupPaymentSelectionEnabled ? (
                <div
                  role="alert"
                  className="flex flex-col gap-2 rounded-xl bg-destructive/10 p-3 text-[13px] text-destructive"
                >
                  <span>{groupPaymentSelectionUnavailableMessage}</span>
                  {groupPaymentSelectionCapabilityUnavailable ? (
                    <Button
                      type="button"
                      variant="outline"
                      className="min-h-[44px] self-start"
                      disabled={paymentCapabilitiesQuery.isFetching}
                      onClick={() => void paymentCapabilitiesQuery.refetch()}
                    >
                      {paymentCapabilitiesQuery.isFetching
                        ? "Повторная проверка..."
                        : "Повторить проверку"}
                    </Button>
                  ) : null}
                </div>
              ) : groupOptionsQuery.isLoading ? (
                <p role="status" className="ui-muted-14">
                  Загружаем подходящие группы...
                </p>
              ) : groupOptionsQuery.isError || groupOptionsQuery.isRefetchError ? (
                <div
                  role="alert"
                  className="flex flex-col gap-2 rounded-xl bg-destructive/10 p-3 text-[13px] text-destructive"
                >
                  <span>Не удалось загрузить группы. Повторите попытку.</span>
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-[44px] self-start"
                    onClick={() => void groupOptionsQuery.refetch()}
                  >
                    Повторить
                  </Button>
                </div>
              ) : groupOptions.length === 0 ? (
                <p className="ui-muted-status">
                  Для этого тарифа нет доступной постоянной группы. Обратитесь к владельцу или администратору.
                </p>
              ) : (
                <>
                  <div role="radiogroup" aria-label="Выбор постоянной группы" className="ui-col-2">
                    {groupOptions.map((option) => {
                      const canonicalCard =
                        canonicalGroupSelectionEnabled &&
                        option.is_canonical_group_card === true &&
                        option.training_group_id != null;
                      const selected = canonicalCard
                        ? selectedGroupOption?.training_group_id === option.training_group_id
                        : selectedGroupOption?.schedule_id === option.schedule_id;
                      return (
                        <button
                          key={canonicalCard ? `group-${option.training_group_id}` : `schedule-${option.schedule_id}`}
                          type="button"
                          role="radio"
                          aria-checked={selected}
                          onClick={() => {
                            setGroupTargetSelection({
                              studentId,
                              tariffId: effectiveTariffId!,
                              trainingGroupId: canonicalCard ? option.training_group_id! : null,
                              scheduleId: canonicalCard ? null : option.schedule_id,
                              // The old schedule-shaped surface remains compatible outside
                              // active rollout. Canonical cards deliberately require the
                              // separate exact-occurrence action below.
                              startDate: canonicalCard ? null : option.next_occurrence_date,
                            });
                            setBankPaymentOrder(null);
                            setErrorMsg(null);
                          }}
                          className={`min-h-[44px] rounded-xl p-3 text-left ring-1 transition-colors ${
                            selected
                              ? "bg-[var(--branding-accent)]/10 ring-[var(--branding-accent)]"
                              : "bg-white ring-foreground/5 active:bg-muted"
                          }`}
                        >
                          <span className="flex items-start justify-between gap-2">
                            <span className="min-w-0">
                              <span className="block break-words text-[14px] font-medium text-foreground">
                                {option.group_name}
                              </span>
                              <span className="block break-words text-[12px] text-muted-foreground">
                                {canonicalCard
                                  ? `${option.slot_schedule_ids?.length ?? 0} слота · выберите точное первое занятие`
                                  : `${dayOfWeekLabels[option.day_of_week] ?? ""} · ${formatGroupTime(option.start_time)}–${formatGroupTime(option.end_time)} · ${option.location_name}`}
                              </span>
                              <span className="block break-words text-[12px] text-muted-foreground">
                                {canonicalCard
                                  ? `Ответственный / продажа: ${option.responsible_trainer_name ?? option.trainer_name}`
                                  : `Тренер: ${option.trainer_name}`}
                              </span>
                            </span>
                            {option.is_latest_trial_group ? (
                              <Badge variant="secondary" className="shrink-0">
                                Пробная
                              </Badge>
                            ) : null}
                          </span>
                        </button>
                      );
                    })}
                  </div>

                  {selectedGroupOption ? (
                    <div className="flex flex-col gap-2 rounded-xl bg-muted/50 p-3">
                      {selectedCanonicalGroup ? (
                        <div role="radiogroup" aria-label="Выбор первого занятия" className="ui-col-2">
                          <p className="ui-caption-label">Выберите первое занятие</p>
                          {(selectedCanonicalGroup.upcoming_occurrences ?? []).map((occurrence) => {
                            const selected =
                              selectedGroupOccurrence?.schedule_id === occurrence.schedule_id &&
                              selectedGroupOccurrence.date === occurrence.date;
                            return (
                              <button
                                key={`${occurrence.schedule_id}-${occurrence.date}`}
                                type="button"
                                role="radio"
                                aria-checked={selected}
                                onClick={() => {
                                  setGroupTargetSelection({
                                    studentId,
                                    tariffId: effectiveTariffId!,
                                    trainingGroupId: selectedCanonicalGroup.training_group_id ?? null,
                                    scheduleId: occurrence.schedule_id,
                                    startDate: occurrence.date,
                                  });
                                  setBankPaymentOrder(null);
                                  setErrorMsg(null);
                                }}
                                className={`min-h-[44px] rounded-lg p-3 text-left ring-1 transition-colors ${
                                  selected
                                    ? "bg-[var(--branding-accent)]/10 ring-[var(--branding-accent)]"
                                    : "bg-white ring-foreground/5 active:bg-muted"
                                }`}
                              >
                                <span className="block text-[14px] font-medium text-foreground">
                                  {formatGroupDate(occurrence.date)} · {formatGroupTime(occurrence.start_time)}–{formatGroupTime(occurrence.end_time)}
                                </span>
                                <span className="block text-[12px] text-muted-foreground">
                                  {occurrence.location_name ?? selectedCanonicalGroup.location_name} · тренер занятия: {occurrence.trainer_name}
                                </span>
                              </button>
                            );
                          })}
                        </div>
                      ) : (
                        <>
                          <label
                            htmlFor={`group-start-date-${studentId}`}
                            className="ui-caption-label"
                          >
                            Первое занятие
                          </label>
                          <select
                            id={`group-start-date-${studentId}`}
                            value={selectedTargetStartDate}
                            onChange={(event) => {
                              setGroupTargetSelection({
                                studentId,
                                tariffId: effectiveTariffId!,
                                trainingGroupId: null,
                                scheduleId: selectedGroupOption.schedule_id,
                                startDate: event.target.value || null,
                              });
                              setBankPaymentOrder(null);
                              setErrorMsg(null);
                            }}
                            className="min-h-[44px] rounded-lg border border-input bg-background px-3 text-[14px] text-foreground"
                          >
                            <option value="">Выберите дату</option>
                            {selectedGroupOption.occurrence_dates.map((date) => (
                              <option key={date} value={date}>
                                {formatGroupDate(date)}
                              </option>
                            ))}
                          </select>
                        </>
                      )}
                      {selectedGroupOccurrence ? (
                        <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-2 gap-y-1 text-[12px]">
                          <dt className="ui-muted">Группа</dt>
                          <dd className="ui-value">{selectedGroupOption.group_name}</dd>
                          <dt className="ui-muted">Первое занятие</dt>
                          <dd className="ui-value">{formatGroupDate(selectedGroupOccurrence.date)} · {formatGroupTime(selectedGroupOccurrence.start_time)}–{formatGroupTime(selectedGroupOccurrence.end_time)}</dd>
                          <dt className="ui-muted">Зал</dt>
                          <dd className="ui-value">{selectedGroupOccurrence.location_name ?? selectedGroupOption.location_name}</dd>
                          <dt className="ui-muted">Тренер занятия</dt>
                          <dd className="ui-value">{selectedGroupOccurrence.trainer_name}</dd>
                          <dt className="ui-muted">Ответственный / продажа</dt>
                          <dd className="ui-value">{selectedCanonicalGroup?.responsible_trainer_name ?? selectedGroupOption.trainer_name}</dd>
                          <dt className="ui-muted">Оформляет оплату</dt>
                          <dd className="ui-value">{packageOwnerTrainerName}</dd>
                          <dt className="ui-muted">Статус оплаты</dt>
                          <dd className="ui-value">Ожидает подтверждения</dd>
                        </dl>
                      ) : null}
                      <p className="text-[12px] leading-snug text-muted-foreground">
                        Точный слот и дата будут повторно проверены сервером перед созданием оплаты или ссылки.
                      </p>
                    </div>
                  ) : null}
                </>
              )}
            </section>
          ) : null}

          {selectedTariff && paymentMethod !== "online" && !unifiedPersonalSettlement ? (
            <section aria-labelledby="payment-discount-heading" className="ui-col-2">
              <div className="ui-row-between-center">
                <p id="payment-discount-heading" className="ui-caption-label">
                  Скидка
                </p>
              </div>
              {discountsLoading ? (
                <div
                  role="status"
                  className="min-h-[44px] rounded-xl bg-muted/50 p-3 text-[13px] text-muted-foreground"
                >
                  Загружаем доступные скидки...
                </div>
              ) : discountsUnavailable ? (
                <div
                  role="alert"
                  className="flex flex-col gap-2 rounded-xl bg-destructive/10 p-3 text-[13px] text-destructive"
                >
                  <span>Не удалось загрузить скидки. Проверьте список перед приёмом оплаты.</span>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="min-h-[44px] self-start"
                    disabled={discountsFetching}
                    onClick={() => void refetchDiscounts()}
                  >
                    {discountsFetching ? "Повторная загрузка..." : "Повторить загрузку"}
                  </Button>
                </div>
              ) : (
                <div role="radiogroup" aria-label="Скидка" className="ui-col-2">
                  <label
                    className={`flex min-h-[44px] cursor-pointer items-center gap-3 rounded-xl p-3 text-left ring-1 transition-colors ${
                      selectedDiscountId === null
                        ? "bg-[var(--branding-accent)]/10 ring-[var(--branding-accent)]"
                        : "bg-white ring-foreground/5 active:bg-muted"
                    }`}
                  >
                    <input
                      type="radio"
                      name={`payment-discount-${studentId}`}
                      checked={selectedDiscountId === null}
                      onChange={() => handleDiscountChange(null)}
                      className="size-4 shrink-0 accent-[var(--branding-accent)]"
                    />
                    <span className="flex min-w-0 flex-1 items-center justify-between gap-3">
                      <span className="text-[14px] font-medium text-foreground">Без скидки</span>
                      <span className="shrink-0 text-[12px] text-muted-foreground">
                        Полная стоимость
                      </span>
                    </span>
                  </label>
                  {activeDiscounts.map((discount) => {
                    const selected = selectedDiscountId === discount.id;
                    return (
                      <label
                        key={discount.id}
                        className={`flex min-h-[44px] cursor-pointer items-center gap-3 rounded-xl p-3 text-left ring-1 transition-colors ${
                          selected
                            ? "bg-[var(--branding-accent)]/10 ring-[var(--branding-accent)]"
                            : "bg-white ring-foreground/5 active:bg-muted"
                        }`}
                      >
                        <input
                          type="radio"
                          name={`payment-discount-${studentId}`}
                          checked={selected}
                          onChange={() => handleDiscountChange(discount.id)}
                          className="size-4 shrink-0 accent-[var(--branding-accent)]"
                        />
                        <span className="flex min-w-0 flex-1 items-center justify-between gap-3">
                          <span className="min-w-0 text-[14px] font-medium text-foreground break-words">
                            {discount.name}
                          </span>
                          <Badge variant="secondary" className="shrink-0 text-[13px] font-semibold">
                            {formatDiscountValue(discount)}
                          </Badge>
                        </span>
                      </label>
                    );
                  })}
                </div>
              )}
            </section>
          ) : null}

          {/* Client-side preview only; the server validates the final amount. */}
          {!hasExactPersonalDebtSettlement && selectedTariff && (
            <section aria-label="Итог оплаты" className="rounded-lg bg-muted/50 p-3">
              <div className="flex items-center justify-between gap-3 text-[13px]">
                <span className="ui-muted">Цена</span>
                <span className="font-medium text-foreground">{formatRub(selectedTariff.price)}</span>
              </div>
              <div className="mt-1 flex items-center justify-between gap-3 text-[13px]">
                <span className="ui-muted">Скидка</span>
                <span className="font-medium text-foreground">
                  {selectedDiscount ? `−${formatRub(discountAmount)}` : "—"}
                </span>
              </div>
              <div className="mt-2 flex items-center justify-between gap-3 border-t border-foreground/10 pt-2">
                <span className="ui-caption-label">К оплате</span>
                <span className="ui-title-20">
                  {formatRub(finalPaymentAmount ?? selectedTariff.price)}
                </span>
              </div>
              {formatPayoutTimingHint(selectedTariff.payout_timing_hint) && (
                <p className="mt-1 text-[12px] text-muted-foreground">
                  {formatPayoutTimingHint(selectedTariff.payout_timing_hint)}
                </p>
              )}
            </section>
          )}

          {!hasExactPersonalDebtSettlement && selectedTariff && finalPaymentAmount !== null && finalPaymentAmount <= 0 ? (
            <p role="status" className="text-[13px] text-destructive">
              Сумма к оплате должна быть больше 0 — выберите другой тариф или скидку.
            </p>
          ) : null}

          {!hasExactPersonalDebtSettlement && selectedTariff && packageOwnerTracked && (
            <div className="rounded-lg bg-white p-3 ring-1 ring-foreground/5">
              <p className="ui-muted-12">Владелец пакета</p>
              <p className="text-[14px] font-medium text-foreground break-words">
                Пакет закрепится за {packageOwnerTrainerName}
              </p>
            </div>
          )}

          {debtsUnavailable ? (
            <QueryStateNotice
              title={debts ? "Данные о долгах могли устареть" : "Не удалось загрузить долги"}
              message="Оплата заблокирована до успешной проверки долгов выбранного ученика."
              retryLabel="Повторить загрузку долгов"
              retrying={debtsQuery.isFetching}
              onRetry={() => void debtsQuery.refetch()}
            />
          ) : null}
          {debtsQuery.isLoading && !debts ? (
            <p className="ui-muted-14">Загрузка долгов...</p>
          ) : debtsUnavailable && !debts ? null : (isDropInMode
            ? openDebts.filter((debt) => debt.id === effectiveRequiredDebtId)
            : openDebts).length > 0 ? (
            <section
              aria-label="Долги ученика"
              className="ui-col-2"
            >
              <div className="ui-row-between-center">
                <p className="ui-caption-label">
                  {isDropInMode ? "Долг за персоналку" : "Открытые долги"}
                </p>
                <Badge variant="secondary" className="shrink-0">
                  {selectedDebtIds.length}/{isDropInMode ? 1 : openDebts.length}
                </Badge>
              </div>
              <div className="ui-col-2">
                {(isDropInMode
                  ? openDebts.filter((debt) => debt.id === effectiveRequiredDebtId)
                  : openDebts).map((debt) => {
                  const selected = selectedDebtIds.includes(debt.id);
                  return (
                    <label
                      key={debt.id}
                      className={`flex items-start gap-3 rounded-lg p-3 ring-1 transition-colors ${
                        selected
                          ? "bg-[var(--branding-accent)]/10 ring-[var(--branding-accent)]"
                          : "bg-white ring-foreground/5"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={selected}
                        disabled={isDropInMode}
                        onChange={() => {
                          if (!isDropInMode) toggleDebt(debt.id);
                        }}
                        className="mt-1 size-4 shrink-0 accent-[var(--branding-accent)]"
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block text-[14px] font-medium text-foreground">
                          Долг #{debt.checkin_id}
                        </span>
                        <span className="block text-[12px] text-muted-foreground">
                          {formatDebtReason(debt.reason)}
                        </span>
                      </span>
                      <Badge variant="secondary" className="shrink-0">
                        {hasExactPersonalDebtSettlement && exactPersonalDebtSettlement
                          ? formatRub(exactPersonalDebtSettlement.amount)
                          : formatDebtAmount(debt.tariff_price)}
                      </Badge>
                    </label>
                  );
                })}
              </div>
            </section>
          ) : null}

          {isDropInMode && !hasIncompleteDropInContext && !requiredDropInDebtAvailable && debtsReady ? (
            <p role="alert" className="rounded-xl bg-destructive/10 p-3 text-[13px] text-destructive">
              Не удалось подтвердить точный долг персоналки. Обновите данные перед приёмом оплаты.
            </p>
          ) : null}

          {!isDropInMode && dropInDebts.length > 0 ? (
            <p role="status" className="ui-warning">
              Долг за персоналку оплачивается из блока «Персоналки» в карточке ученика.
            </p>
          ) : null}

          {/* Payment method toggle */}
          <div className="grid grid-cols-3 gap-2">
            <Button
              type="button"
              aria-pressed={paymentMethod === "cash"}
              variant={paymentMethod === "cash" ? "default" : "outline"}
              disabled={hasIncompleteDropInContext}
              className={`min-h-[44px] ${paymentMethod === "cash" ? "bg-[var(--branding-accent)] text-white hover:opacity-90" : ""}`}
              onClick={() => handlePaymentMethodChange("cash")}
            >
              Наличные
            </Button>
            <Button
              type="button"
              aria-pressed={paymentMethod === "transfer"}
              variant={paymentMethod === "transfer" ? "default" : "outline"}
              disabled={hasIncompleteDropInContext}
              className={`min-h-[44px] ${paymentMethod === "transfer" ? "bg-[var(--branding-accent)] text-white hover:opacity-90" : ""}`}
              onClick={() => handlePaymentMethodChange("transfer")}
            >
              Перевод
            </Button>
            <Button
              type="button"
              aria-pressed={paymentMethod === "online"}
              variant={paymentMethod === "online" ? "default" : "outline"}
              disabled={
                hasIncompleteDropInContext ||
                !onlinePaymentsEnabled
              }
              className={`min-h-[44px] ${paymentMethod === "online" ? "bg-[var(--branding-accent)] text-white hover:opacity-90" : ""}`}
              onClick={() => handlePaymentMethodChange("online")}
            >
              СБП
            </Button>
          </div>
          {!onlinePaymentsEnabled ? (
            <p role="status" className="ui-warning">
              {getOnlinePaymentUnavailableMessage(paymentCapabilitiesQuery, "staff")}
            </p>
          ) : null}

          {displayedBankPaymentOrder ? (
            <OnlinePaymentLinkPanel
              order={displayedBankPaymentOrder}
              title={
                creatingBankPaymentOrder
                  ? "Ссылка создаётся"
                  : conflictingBankPaymentOrder
                  ? "Другая активная ссылка"
                  : "Ссылка на оплату"
              }
              subtitle={
                creatingBankPaymentOrder
                  ? "Банк ещё не вернул адрес. Можно отменить эту попытку и создать новую."
                  : conflictingBankPaymentOrder
                  ? "Сначала отмените эту ссылку, затем выберите новую группу или дату."
                  : undefined
              }
              isCanceling={cancelBankPaymentMutation.isPending}
              onCancel={
                bankOrdersReady
                  ? (order) => cancelBankPaymentMutation.mutate(order)
                  : undefined
              }
              onRefresh={() => void bankOrdersQuery.refetch()}
              onRequestRefresh={() => {
                void apiClient
                  .post(`/billing/bank-payment-orders/${displayedBankPaymentOrder.id}/refresh/`, {})
                  .finally(() => bankOrdersQuery.refetch());
              }}
            />
          ) : null}

          {dropInBankPaymentOrderId !== null ? (
            dropInBankPaymentOrderQuery.data ? (
              <OnlinePaymentLinkPanel
                order={dropInBankPaymentOrderQuery.data}
                title="Ссылка на оплату персоналки"
                subtitle="Оплата останется привязана только к этой записи."
                isCanceling={cancelBankPaymentMutation.isPending}
                isRefreshing={dropInBankPaymentOrderQuery.isFetching}
                onRefresh={() => void dropInBankPaymentOrderQuery.refetch()}
                onRequestRefresh={() => {
                  void apiClient
                    .post(
                      `/billing/bank-payment-orders/${dropInBankPaymentOrderQuery.data.id}/refresh/`,
                      {},
                    )
                    .finally(() => dropInBankPaymentOrderQuery.refetch());
                }}
                onCancel={(order) => cancelBankPaymentMutation.mutate(order)}
              />
            ) : dropInBankPaymentOrderQuery.isError ? (
              <div role="status" className="ui-warning-dark">
                <p>Не удалось проверить статус созданной ссылки.</p>
                <Button type="button" variant="outline" className="mt-2 min-h-[44px]" onClick={() => void dropInBankPaymentOrderQuery.refetch()}>
                  Повторить проверку
                </Button>
              </div>
            ) : (
              <p role="status" className="ui-muted-status">
                Проверяем статус созданной ссылки…
              </p>
            )
          ) : null}

          {/* Error */}
          {errorMsg && (
            <p className="ui-error-center">{errorMsg}</p>
          )}

          {/* Submit */}
          <Button
            className="min-h-[44px] w-full bg-[var(--branding-accent)] text-white hover:opacity-90"
            disabled={
              !canSubmitPayment ||
              isSubmitting ||
              cancelBankPaymentMutation.isPending ||
              !canCreateBankPaymentOrder
            }
            onClick={handleSubmit}
            wrap
          >
            {isSubmitting
              ? "Отправка..."
              : paymentMethod === "online"
                ? cancelBankPaymentMutation.isPending
                  ? "Отмена ссылки..."
                  : activeBankPaymentOrder
                    ? "Ссылка создана"
                    : creatingBankPaymentOrder
                      ? "Ссылка создаётся"
                    : conflictingBankPaymentOrder
                      ? "Сначала отмените ссылку"
                    : "Создать ссылку СБП"
                : "Принять оплату"}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

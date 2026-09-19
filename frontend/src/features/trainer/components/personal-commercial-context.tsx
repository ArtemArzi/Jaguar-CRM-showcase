import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import {
  OnlinePaymentLinkPanel,
  type BankPaymentOrderLink,
} from "@/components/portal/online-payment-link-panel";
import { Button } from "@/components/ui/button";
import apiClient from "@/api/custom-fetch";
import { useBrandingStore } from "@/features/branding/use-branding";
import { formatRub } from "@/lib/utils";
import {
  personalCommercialBankOrderQueryKey,
  replacePersonalPaymentMethod,
  type PersonalCommercialReceipt,
  type PersonalStaffIntentPaymentMethod,
  useCommercialCacheScope,
} from "./personal-commercial-context-api";

export type { PersonalCommercialReceipt } from "./personal-commercial-context-api";

const paymentMethodLabels: Record<PersonalStaffIntentPaymentMethod, string> = {
  entitlement: "По абонементу",
  cash: "Наличные",
  transfer: "Перевод",
  sbp: "СБП",
  pay_at_visit: "Оплата при посещении",
};

const terminalReceiptStatuses = new Set([
  "confirmed",
  "approved",
  "paid",
  "rejected",
  "failed",
  "cancelled",
  "no_show",
  "expired",
  "refunded",
  "refunded_partially",
]);

function formatDateTime(value: string | null | undefined, timeZone: string): string {
  if (!value) return "Время уточняется";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Время уточняется";
  try {
    return new Intl.DateTimeFormat("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      timeZone,
    }).format(date);
  } catch {
    return "Время уточняется";
  }
}

function formatTime(value: string | null | undefined, timeZone: string): string {
  if (!value) return "Время уточняется";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Время уточняется";
  try {
    return new Intl.DateTimeFormat("ru-RU", {
      hour: "2-digit",
      minute: "2-digit",
      timeZone,
    }).format(date);
  } catch {
    return "Время уточняется";
  }
}

function receiptStatusCopy(receipt: PersonalCommercialReceipt): string {
  if (receipt.status === "no_show") {
    return "Клиент не пришёл";
  }
  if (receipt.status === "cancelled") {
    return receipt.booking_id ? "Запись отменена" : "Оплата отменена";
  }
  if (receipt.status === "rejected") {
    return "Оплата отклонена";
  }
  if (
    receipt.payment_method === "pay_at_visit" &&
    ["pay_at_visit", "scheduled"].includes(receipt.status)
  ) {
    return "К оплате при посещении";
  }
  if (
    (receipt.payment_method === "cash" || receipt.payment_method === "transfer") &&
    ["pending", "pending_payment"].includes(receipt.status)
  ) {
    return "Оплата ожидает подтверждения владельцем";
  }
  if (
    receipt.payment_method === "sbp" &&
    ["pending", "pending_payment", "created", "authorized"].includes(receipt.status)
  ) {
    return "Ожидает оплаты через СБП";
  }
  const labels: Record<string, string> = {
    debt_open: "Долг ожидает оплаты",
    pending_payment: "Оплата ожидает подтверждения",
    approved: "Оплата подтверждена",
    manual_review: "Оплата на ручной проверке",
    booked: "Записан",
    confirmed: "Оплата подтверждена",
    paid: "Оплачено",
    scheduled: "Запланировано",
    expired: "Срок оплаты истёк",
    failed: "Оплата не прошла",
    refunded: "Оплата возвращена",
    refunded_partially: "Оплата возвращена частично",
  };
  return labels[receipt.status] ?? "Статус уточняется";
}

function hasExactDebtSettlementAction(receipt: PersonalCommercialReceipt): boolean {
  return (receipt.allowed_actions ?? []).includes("settle_exact_debt") ||
    (receipt.allowed_actions ?? []).includes("open_personal_debt_settlement");
}

function hasBankPaymentRetryAction(receipt: PersonalCommercialReceipt): boolean {
  return (
    receipt.payment_method === "sbp" &&
    (receipt.allowed_actions ?? []).includes("retry_bank_payment")
  );
}

function hasContextualManualCreateAction(receipt: PersonalCommercialReceipt): boolean {
  const action =
    receipt.kind === "group_sale"
      ? "create_group_sale"
      : receipt.kind === "renewal"
        ? "create_renewal"
        : null;
  return action !== null && (receipt.allowed_actions ?? []).includes(action);
}

function hasOpenBankPaymentOrderAction(receipt: PersonalCommercialReceipt): boolean {
  return (receipt.allowed_actions ?? []).includes("open_bank_payment_order");
}

function receiptTimeRange(receipt: PersonalCommercialReceipt, timeZone: string): string {
  if (receipt.kind === "group_sale" && (receipt.target_start_date || receipt.start_date)) {
    return receipt.target_start_date || receipt.start_date || "Время уточняется";
  }
  if (receipt.attempted_at) return formatDateTime(receipt.attempted_at, timeZone);
  if (!receipt.starts_at || !receipt.ends_at) return "Время уточняется";
  return `${formatDateTime(receipt.starts_at, timeZone)}–${formatTime(receipt.ends_at, timeZone)}`;
}

function receiptTitle(receipt: PersonalCommercialReceipt): string {
  if (receipt.kind === "group_sale") return "Групповое обучение";
  if (receipt.kind === "renewal") return "Продление абонемента";
  return "Персональная тренировка";
}

function receiptAriaLabel(receipt: PersonalCommercialReceipt): string {
  if (receipt.kind === "personal_staff_intent") return "Коммерческий контекст персоналки";
  return "Коммерческий контекст";
}

function receiptServiceLabel(receipt: PersonalCommercialReceipt): string {
  if (receipt.kind === "group_sale") {
    return receipt.group_name || receipt.tariff_name || receipt.training_type_name || "Группа уточняется";
  }
  if (receipt.kind === "renewal") {
    return receipt.renewed_from_subscription_name ||
      (receipt.renewed_from_subscription_id
        ? `Абонемент #${receipt.renewed_from_subscription_id}`
        : receipt.tariff_name || receipt.training_type_name || "Абонемент уточняется");
  }
  return receipt.training_type_name || "Услуга уточняется";
}

export function PersonalCommercialReceiptCard({
  receipt,
  studentId,
  onReceiptChanged,
  onSettleExactDebt,
  onRetryPersonalBankPayment,
  onRetryContextualBankPayment,
  canRetryContextualBankPayment,
}: {
  readonly receipt: PersonalCommercialReceipt;
  readonly studentId?: number;
  readonly onReceiptChanged?: () => void;
  readonly onSettleExactDebt?: (receipt: PersonalCommercialReceipt) => void;
  readonly onRetryPersonalBankPayment?: (receipt: PersonalCommercialReceipt) => void;
  readonly onRetryContextualBankPayment?: (receipt: PersonalCommercialReceipt) => void;
  readonly canRetryContextualBankPayment?: (receipt: PersonalCommercialReceipt) => boolean;
}) {
  const timeZone = useBrandingStore((state) => state.timeZone);
  const commercialCacheScope = useCommercialCacheScope();
  const queryClient = useQueryClient();
  const [showReplacementMethods, setShowReplacementMethods] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const replacementKeysRef = useRef(new Map<string, string>());
  const replacementOrigin = `${receipt.reservation_id ? "reservation" : "payment"}:${receipt.reservation_id ?? receipt.payment_id ?? "missing"}`;
  useEffect(() => {
    replacementKeysRef.current.clear();
  }, [replacementOrigin]);
  const replacementIdempotencyKey = (
    replacementPaymentMethod: "cash" | "transfer" | "pay_at_visit",
  ) => {
    const fingerprint = `${replacementOrigin}:${replacementPaymentMethod}`;
    const existing = replacementKeysRef.current.get(fingerprint);
    if (existing) return existing;
    const created = `personal-method-correction:${fingerprint}:${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    replacementKeysRef.current.set(fingerprint, created);
    return created;
  };
  const hasAction = (action: string) => (receipt.allowed_actions ?? []).includes(action);
  const invalidateBankOrder = () => {
    if (receipt.bank_payment_order_id) {
      void queryClient.invalidateQueries({
        queryKey: personalCommercialBankOrderQueryKey(
          receipt.bank_payment_order_id,
          commercialCacheScope,
        ),
      });
    }
  };
  const cancelMutation = useMutation({
    mutationFn: () => apiClient.post(`/billing/bank-payment-orders/${receipt.bank_payment_order_id}/cancel/`, {}),
    onSuccess: () => {
      invalidateBankOrder();
      onReceiptChanged?.();
    },
    onError: () => setActionError("Не удалось безопасно отменить попытку. Обновите карточку."),
  });
  const refreshMutation = useMutation({
    mutationFn: () => apiClient.post(`/billing/bank-payment-orders/${receipt.bank_payment_order_id}/refresh/`, {}),
    onSuccess: () => {
      invalidateBankOrder();
      onReceiptChanged?.();
    },
    onError: () => setActionError("Не удалось запросить сверку. Обновите карточку."),
  });
  const replacementMutation = useMutation({
    mutationFn: (replacementPaymentMethod: "cash" | "transfer" | "pay_at_visit") => {
      if (!studentId) throw new Error("Personal commercial subject is required");
      return replacePersonalPaymentMethod({
        studentId,
        payload: {
          ...(receipt.reservation_id
            ? { reservation_id: receipt.reservation_id }
            : { payment_id: receipt.payment_id ?? undefined }),
          replacement_payment_method: replacementPaymentMethod,
          reason: "staff_payment_method_correction",
          idempotency_key: replacementIdempotencyKey(replacementPaymentMethod),
        },
      });
    },
    onSuccess: (_data, replacementPaymentMethod) => {
      replacementKeysRef.current.delete(`${replacementOrigin}:${replacementPaymentMethod}`);
      setShowReplacementMethods(false);
      onReceiptChanged?.();
    },
    onError: () => setActionError("Не удалось заменить способ оплаты. Исходная попытка сохранена для сверки."),
  });
  const bankOrderQuery = useQuery<BankPaymentOrderLink>({
    queryKey: personalCommercialBankOrderQueryKey(
      receipt.bank_payment_order_id ?? 0,
      commercialCacheScope,
    ),
    queryFn: () =>
      apiClient
        .get<BankPaymentOrderLink>(`/billing/bank-payment-orders/${receipt.bank_payment_order_id}/`)
        .then((response) => response.data),
    enabled:
      receipt.payment_method === "sbp" &&
      Boolean(receipt.bank_payment_order_id) &&
      hasOpenBankPaymentOrderAction(receipt),
    staleTime: 30_000,
    retry: false,
  });

  return (
    <section
      role="region"
      aria-label={receiptAriaLabel(receipt)}
      className="space-y-3 rounded-xl bg-muted/50 p-3 ring-1 ring-foreground/5"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[14px] font-semibold text-foreground">{receiptTitle(receipt)}</p>
          <p className="mt-1 text-[13px] text-muted-foreground">
            {receiptTimeRange(receipt, timeZone)}
          </p>
        </div>
        <p className="shrink-0 text-[14px] font-semibold text-foreground">
          {receipt.amount == null || receipt.amount === "" ? "По абонементу" : formatRub(receipt.amount)}
        </p>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[13px]">
        <dt className="text-muted-foreground">Услуга</dt>
        <dd className="min-w-0 text-foreground">{receiptServiceLabel(receipt)}</dd>
        {receipt.kind === "renewal" && receipt.renewed_from_subscription_id ? (
          <>
            <dt className="text-muted-foreground">Источник</dt>
            <dd className="min-w-0 text-foreground">Абонемент #{receipt.renewed_from_subscription_id}</dd>
          </>
        ) : null}
        <dt className="text-muted-foreground">Тренер</dt>
        <dd className="min-w-0 text-foreground">{receipt.trainer_name || "Уточняется"}</dd>
        <dt className="text-muted-foreground">Зал</dt>
        <dd className="min-w-0 text-foreground">{receipt.location_name || "Уточняется"}</dd>
        <dt className="text-muted-foreground">Способ</dt>
        <dd className="min-w-0 text-foreground">{paymentMethodLabels[receipt.payment_method]}</dd>
        <dt className="text-muted-foreground">Статус</dt>
        <dd className="min-w-0 font-medium text-foreground">{receiptStatusCopy(receipt)}</dd>
      </dl>

      {receipt.payment_method === "sbp" && hasOpenBankPaymentOrderAction(receipt) && bankOrderQuery.data ? (
        <OnlinePaymentLinkPanel
          order={bankOrderQuery.data}
          title="Ссылка на оплату СБП"
          subtitle="Статус и сумма берутся из банковского заказа"
          onRefresh={() => void bankOrderQuery.refetch()}
        />
      ) : null}
      {receipt.payment_method === "sbp" && hasOpenBankPaymentOrderAction(receipt) && bankOrderQuery.isLoading ? (
        <p className="ui-muted-14">Загрузка ссылки СБП...</p>
      ) : null}
      {receipt.payment_method === "sbp" && hasOpenBankPaymentOrderAction(receipt) && bankOrderQuery.isError ? (
        <p role="status" className="ui-warning">
          Ссылка СБП сейчас недоступна. Обновите карточку клиента.
        </p>
      ) : null}

      {hasAction("cancel_if_safe") && receipt.bank_payment_order_id ? (
        <Button
          type="button"
          variant="outline"
          className="min-h-[44px] w-full"
          disabled={cancelMutation.isPending}
          onClick={() => cancelMutation.mutate()}
        >
          Отменить попытку
        </Button>
      ) : null}
      {hasAction("refresh_or_reconcile") && receipt.bank_payment_order_id ? (
        <Button
          type="button"
          variant="outline"
          className="min-h-[44px] w-full"
          disabled={refreshMutation.isPending}
          onClick={() => refreshMutation.mutate()}
        >
          Обновить и сверить оплату
        </Button>
      ) : null}
      {hasAction("replace_payment_method") && studentId && (receipt.reservation_id || receipt.payment_id) ? (
        <div className="space-y-2">
          <Button
            type="button"
            variant="outline"
            className="min-h-[44px] w-full"
            disabled={replacementMutation.isPending}
            onClick={() => setShowReplacementMethods((value) => !value)}
          >
            Заменить способ оплаты
          </Button>
          {showReplacementMethods ? (
            <div className="grid grid-cols-1 gap-2">
              {([
                ["cash", "Наличные"],
                ["transfer", "Перевод"],
                ["pay_at_visit", "Оплата при посещении"],
              ] as const)
                .filter(([method]) => method !== receipt.payment_method)
                .map(([method, label]) => (
                  <Button
                    key={method}
                    type="button"
                    variant="secondary"
                    className="min-h-[44px] w-full"
                    disabled={replacementMutation.isPending}
                    onClick={() => replacementMutation.mutate(method)}
                  >
                    {label}
                  </Button>
                ))}
            </div>
          ) : null}
        </div>
      ) : null}
      {hasAction("owner_review") ? (
        <p role="status" className="ui-warning">
          Нужна проверка владельца: не создавайте новую оплату до сверки.
        </p>
      ) : null}
      {actionError ? <p role="status" className="ui-warning">{actionError}</p> : null}

      {hasBankPaymentRetryAction(receipt) &&
      receipt.kind === "personal_staff_intent" &&
      onRetryPersonalBankPayment ? (
        <Button
          type="button"
          variant="outline"
          className="min-h-[44px] w-full"
          onClick={() => onRetryPersonalBankPayment?.(receipt)}
        >
          Повторить оплату СБП
        </Button>
      ) : null}
      {(hasBankPaymentRetryAction(receipt) || hasContextualManualCreateAction(receipt)) &&
      receipt.kind !== "personal_staff_intent" &&
      onRetryContextualBankPayment &&
      canRetryContextualBankPayment?.(receipt) ? (
        <Button
          type="button"
          variant="outline"
          className="min-h-[44px] w-full"
          onClick={() => onRetryContextualBankPayment?.(receipt)}
        >
          {hasBankPaymentRetryAction(receipt) ? "Повторить оплату СБП" : "Создать оплату снова"}
        </Button>
      ) : null}

      {hasExactDebtSettlementAction(receipt) &&
      receipt.booking_id &&
      receipt.debt_id &&
      receipt.amount != null &&
      receipt.amount !== "" ? (
        <Button
          type="button"
          variant="outline"
          className="min-h-[44px] w-full"
          onClick={() => onSettleExactDebt?.(receipt)}
        >
          Принять уже полученную оплату
        </Button>
      ) : null}
    </section>
  );
}

function orderPersonalCommercialAttempts(
  attempts: readonly PersonalCommercialReceipt[],
): readonly PersonalCommercialReceipt[] {
  return attempts
    .map((receipt, index) => ({ receipt, index }))
    .sort((left, right) => {
      const leftLive = !terminalReceiptStatuses.has(left.receipt.status);
      const rightLive = !terminalReceiptStatuses.has(right.receipt.status);
      if (leftLive === rightLive) return left.index - right.index;
      return leftLive ? -1 : 1;
    })
    .map(({ receipt }) => receipt);
}

const historyOnlyStatuses = new Set(["cancelled", "failed", "expired", "rejected"]);

function isHistoryOnlyAttempt(receipt: PersonalCommercialReceipt): boolean {
  if (!historyOnlyStatuses.has(receipt.status)) return false;
  return !(receipt.allowed_actions ?? []).some((action) =>
    ["refresh_or_reconcile", "replace_payment_method", "retry_bank_payment", "owner_review"].includes(action),
  );
}

export function PersonalCommercialReceiptList({
  attempts,
  studentId,
  onReceiptChanged,
  onSettleExactDebt,
  onRetryPersonalBankPayment,
  onRetryContextualBankPayment,
  canRetryContextualBankPayment,
}: {
  readonly attempts: readonly PersonalCommercialReceipt[];
  readonly studentId?: number;
  readonly onReceiptChanged?: () => void;
  readonly onSettleExactDebt?: (receipt: PersonalCommercialReceipt) => void;
  readonly onRetryPersonalBankPayment?: (receipt: PersonalCommercialReceipt) => void;
  readonly onRetryContextualBankPayment?: (receipt: PersonalCommercialReceipt) => void;
  readonly canRetryContextualBankPayment?: (receipt: PersonalCommercialReceipt) => boolean;
}) {
  const orderedAttempts = orderPersonalCommercialAttempts(attempts);
  const activeAttempts = orderedAttempts.filter((receipt) => !isHistoryOnlyAttempt(receipt));
  const historyAttempts = orderedAttempts.filter(isHistoryOnlyAttempt);
  const renderReceipt = (receipt: PersonalCommercialReceipt, index: number) => (
    <PersonalCommercialReceiptCard
      key={`${receipt.booking_id ?? receipt.reservation_id ?? receipt.payment_id ?? receipt.slot_id ?? index}:${receipt.status}`}
      receipt={receipt}
      studentId={studentId}
      onReceiptChanged={onReceiptChanged}
      onSettleExactDebt={onSettleExactDebt}
      onRetryPersonalBankPayment={onRetryPersonalBankPayment}
      onRetryContextualBankPayment={onRetryContextualBankPayment}
      canRetryContextualBankPayment={canRetryContextualBankPayment}
    />
  );
  return (
    <>
      {activeAttempts.map(renderReceipt)}
      {historyAttempts.length ? (
        <details className="rounded-xl bg-muted/30 p-3">
          <summary className="cursor-pointer text-[13px] font-medium text-muted-foreground">
            История попыток ({historyAttempts.length})
          </summary>
          <div className="mt-3 space-y-3">{historyAttempts.map(renderReceipt)}</div>
        </details>
      ) : null}
    </>
  );
}

import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import apiClient from "@/api/custom-fetch";
import {
  getOnlinePaymentUnavailableMessage,
  hasOnlinePaymentsCapability,
  usePaymentCapabilities,
} from "@/api/payment-capabilities";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { getAuthTokenSubject, useAuthStore } from "@/features/auth/auth-store";
import { getApiError } from "@/lib/utils";
import {
  formatRenewalOfferLabel,
  getExpectedRenewalOfferFields,
  getRenewalOfferErrorMessage,
  isRenewalOfferError,
} from "@/components/portal/subscription-renewal-state";
import {
  createContextualGroupSale,
  createContextualGroupSaleBankOrder,
  createContextualGroupSaleBankOrderV2,
  createContextualGroupSaleManualV2,
  createManualSubscriptionRenewal,
  createStaffSubscriptionRenewalBankOrder,
  type ContextualCommercialContext,
  type ContextualCommercialCommandResult,
  type GroupSaleContext,
  useCommercialCacheScope,
} from "./personal-commercial-context-api";
import {
  clearContextualCommercialCommandKey,
  getOrCreateContextualCommercialCommandKey,
  type ContextualCommercialCommandScope,
} from "@/api/contextual-commercial-command-key";

type ContextualCommercialPaymentMethod = "cash" | "transfer" | "sbp";

const methodLabels: Record<ContextualCommercialPaymentMethod, string> = {
  cash: "Наличные",
  transfer: "Перевод",
  sbp: "СБП",
};

const offlinePaymentMethods: readonly ContextualCommercialPaymentMethod[] = ["cash", "transfer"];
const onlinePaymentMethods: readonly ContextualCommercialPaymentMethod[] = [
  ...offlinePaymentMethods,
  "sbp",
];

function formatRubles(amount: string | number) {
  const numeric = Number(amount);
  if (!Number.isFinite(numeric)) return `${amount} ₽`;
  return `${new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: Number.isInteger(numeric) ? 0 : 2,
    maximumFractionDigits: 2,
  }).format(numeric)} ₽`;
}

function groupSaleSubmitLabel(
  context: ContextualCommercialContext | null,
  paymentMethod: ContextualCommercialPaymentMethod,
) {
  if (context?.kind !== "group_sale" || context.protocolVersion !== "v2" || !context.amount) {
    return paymentMethod === "sbp" ? "Создать ссылку СБП" : "Зафиксировать оплату";
  }
  const amount = formatRubles(context.amount);
  if (paymentMethod === "sbp") return `Создать ссылку СБП на ${amount} для ${context.studentName}`;
  const method = paymentMethod === "cash" ? "наличные" : "перевод";
  return `Зафиксировать ${method} ${amount} и оформить ${context.studentName}`;
}

function tariffTermsLabel(tariff: {
  readonly trainings_limit?: number | null;
  readonly duration_days?: number | null;
}) {
  const terms: string[] = [];
  if (tariff.trainings_limit != null) terms.push(`${tariff.trainings_limit} занятий`);
  if (tariff.duration_days != null) terms.push(`${tariff.duration_days} дней`);
  return terms.join(" · ");
}

function apiErrorCode(error: unknown): string | null {
  const code = (error as { response?: { data?: { code?: unknown } } })?.response?.data?.code;
  return typeof code === "string" ? code : null;
}

function commandScope(
  context: ContextualCommercialContext,
  clubId: number | null,
  actorSubject: string | null,
  audience: "staff" | "student" | "parent",
  paymentMethod: ContextualCommercialPaymentMethod,
): ContextualCommercialCommandScope {
  if (context.kind === "group_sale") {
    return {
      clubId,
      actorSubject,
      audience,
      kind: context.kind,
      protocolVersion: context.protocolVersion ?? "v1",
      studentId: context.studentId,
      paymentMethod,
      tariffId: context.tariffId,
      trainingGroupId: context.trainingGroupId,
      scheduleId: context.scheduleId,
      startDate: context.startDate,
      renewedFromSubscriptionId: context.renewedFromSubscriptionId,
      offerDigest: context.offerDigest,
    };
  }
  return {
    clubId,
    actorSubject,
    audience,
    kind: context.kind,
    studentId: context.studentId,
    paymentMethod,
    renewedFromSubscriptionId: context.renewedFromSubscriptionId,
  };
}

async function submitContextualCommand({
  context,
  paymentMethod,
  idempotencyKey,
  buyerEmail,
}: {
  readonly context: ContextualCommercialContext;
  readonly paymentMethod: ContextualCommercialPaymentMethod;
  readonly idempotencyKey: string;
  readonly buyerEmail: string;
}): Promise<ContextualCommercialCommandResult> {
  if (context.kind === "group_sale") {
    if (context.protocolVersion === "v2") {
      if (!context.offerDigest) throw new Error("v2_group_offer_required");
      const strictTarget = {
        protocol_version: "v2" as const,
        student_id: context.studentId,
        tariff_id: context.tariffId,
        target_training_group_id: context.trainingGroupId,
        target_schedule_id: context.scheduleId,
        target_start_date: context.startDate,
        expected_offer_digest: context.offerDigest,
        idempotency_key: idempotencyKey,
      };
      if (paymentMethod === "sbp") {
        return createContextualGroupSaleBankOrderV2({
          ...strictTarget,
          ...(buyerEmail.trim() ? { buyer_email: buyerEmail.trim() } : {}),
        });
      }
      return createContextualGroupSaleManualV2({ ...strictTarget, payment_method: paymentMethod });
    }
    const exactTarget = {
      student_id: context.studentId,
      tariff_id: context.tariffId,
      discount_ids: [] as const,
      debt_ids: [] as const,
      target_training_group_id: context.trainingGroupId,
      target_schedule_id: context.scheduleId,
      target_start_date: context.startDate,
      ...(context.renewedFromSubscriptionId
        ? { renewed_from_subscription_id: context.renewedFromSubscriptionId }
        : {}),
      idempotency_key: idempotencyKey,
    };
    if (paymentMethod === "sbp") return createContextualGroupSaleBankOrder(exactTarget);
    return createContextualGroupSale({ ...exactTarget, payment_method: paymentMethod });
  }

  const exactSource = {
    student_id: context.studentId,
    renewed_from_subscription_id: context.renewedFromSubscriptionId,
    idempotency_key: idempotencyKey,
    ...(
      getExpectedRenewalOfferFields({
        renewal_target_tariff_id: context.renewalTargetTariffId,
        renewal_target_tariff_name: context.renewalTargetTariffName,
        renewal_target_price: context.renewalTargetPrice,
      }) ?? {}
    ),
  };
  if (paymentMethod === "sbp") return createStaffSubscriptionRenewalBankOrder(exactSource);
  return createManualSubscriptionRenewal({ ...exactSource, payment_method: paymentMethod });
}

export function ContextualCommercialSheet({
  open,
  onOpenChange,
  context,
  onAuthoritativeResult,
  onOfferChanged,
  initialPaymentMethod = "cash",
  freshCommand = false,
}: {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly context: ContextualCommercialContext | null;
  readonly onAuthoritativeResult?: (result: ContextualCommercialCommandResult) => void;
  /** A stale signed v2 offer must return to server-backed group selection. */
  readonly onOfferChanged?: () => void;
  /** A retry always starts a new command family, then keeps that key on lost responses. */
  readonly freshCommand?: boolean;
  readonly initialPaymentMethod?: ContextualCommercialPaymentMethod;
}) {
  const clubId = useAuthStore((state) => state.clubId);
  const accessToken = useAuthStore((state) => state.accessToken);
  const role = useAuthStore((state) => state.role);
  const queryClient = useQueryClient();
  const paymentCapabilitiesQuery = usePaymentCapabilities();
  const onlinePaymentsEnabled = hasOnlinePaymentsCapability(paymentCapabilitiesQuery);
  const availablePaymentMethods = onlinePaymentsEnabled ? onlinePaymentMethods : offlinePaymentMethods;
  const [paymentMethod, setPaymentMethod] = useState<ContextualCommercialPaymentMethod>(
    initialPaymentMethod,
  );
  const freshCommandRef = useRef(freshCommand);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [buyerEmail, setBuyerEmail] = useState("");

  function closeSheet() {
    setErrorMessage("");
    setIsSubmitting(false);
    onOpenChange(false);
  }

  async function submit() {
    if (!context || isSubmitting) return;
    if (
      context.kind === "subscription_renewal" &&
      !getExpectedRenewalOfferFields({
        renewal_target_tariff_id: context.renewalTargetTariffId,
        renewal_target_price: context.renewalTargetPrice,
      })
    ) {
      setErrorMessage("Актуальная цена продления недоступна. Обновите карточку клиента.");
      return;
    }
    if (paymentMethod === "sbp" && !onlinePaymentsEnabled) {
      setErrorMessage(getOnlinePaymentUnavailableMessage(paymentCapabilitiesQuery, "staff"));
      return;
    }
    if (paymentMethod === "sbp" && context.kind === "group_sale" && context.buyerEmailRequired && !buyerEmail.trim()) {
      setErrorMessage("Укажите email для фискального чека.");
      return;
    }
    const audience = role === "student" ? "student" : role === "parent" ? "parent" : "staff";
    const scope = commandScope(
      context,
      clubId,
      getAuthTokenSubject(accessToken),
      audience,
      paymentMethod,
    );
    if (freshCommandRef.current) {
      clearContextualCommercialCommandKey(scope);
      freshCommandRef.current = false;
    }
    const idempotencyKey = getOrCreateContextualCommercialCommandKey(scope);
    setIsSubmitting(true);
    setErrorMessage("");
    try {
      const result = await submitContextualCommand({ context, paymentMethod, idempotencyKey, buyerEmail });
      clearContextualCommercialCommandKey(scope);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["student", String(context.studentId)] }),
        queryClient.invalidateQueries({ queryKey: ["lead"] }),
        queryClient.invalidateQueries({ queryKey: ["leads"] }),
        queryClient.invalidateQueries({ queryKey: ["billing", "bank-payment-orders"] }),
        queryClient.invalidateQueries({ queryKey: ["billing", "debts", context.studentId] }),
        queryClient.invalidateQueries({ queryKey: ["retention-tasks"] }),
        context.kind === "group_sale"
          ? Promise.all(
              (context.slotScheduleIds?.length ? context.slotScheduleIds : [context.scheduleId]).map(
                (scheduleId) =>
                  queryClient.invalidateQueries({
                    queryKey: ["schedule", String(scheduleId), "students"],
                  }),
              ),
            )
          : Promise.resolve(),
      ]);
      onAuthoritativeResult?.(result);
      closeSheet();
    } catch (error) {
      // Retain the exact key so a transient failure replays the same command family.
      if (context.kind === "group_sale" && context.protocolVersion === "v2" && apiErrorCode(error) === "group_offer_changed") {
        clearContextualCommercialCommandKey(scope);
        setErrorMessage("Предложение изменилось. Выберите группу и дату старта заново.");
        setIsSubmitting(false);
        onOfferChanged?.();
        return;
      }
      if (context.kind === "subscription_renewal" && isRenewalOfferError(error)) {
        clearContextualCommercialCommandKey(scope);
        setErrorMessage(
          getRenewalOfferErrorMessage(error, "Не удалось создать оплату. Повторите попытку."),
        );
        setIsSubmitting(false);
        onOfferChanged?.();
        return;
      }
      setErrorMessage(getApiError(error, "Не удалось создать оплату. Повторите попытку."));
      setIsSubmitting(false);
    }
  }

  const title = context?.kind === "group_sale" ? "Проверка перед оформлением" : "Продлить абонемент";
  const renewalOfferLabel =
    context?.kind === "subscription_renewal"
      ? formatRenewalOfferLabel({
          renewal_target_tariff_id: context.renewalTargetTariffId,
          renewal_target_tariff_name: context.renewalTargetTariffName,
          renewal_target_price: context.renewalTargetPrice,
      })
      : null;
  const renewalOfferAvailable =
    context?.kind !== "subscription_renewal" ||
    Boolean(
      getExpectedRenewalOfferFields({
        renewal_target_tariff_id: context.renewalTargetTariffId,
        renewal_target_price: context.renewalTargetPrice,
      }),
    );
  const description =
    context?.kind === "group_sale"
      ? "Группа, расписание, дата старта и сумма зафиксированы сервером."
      : "Продление связано с выбранным абонементом. Сумму и остатки определит клуб.";

  return (
    <Sheet open={open} onOpenChange={(nextOpen) => (nextOpen ? onOpenChange(true) : closeSheet())}>
      <SheetContent side="bottom" className="max-h-[90vh] overflow-y-auto rounded-t-2xl">
        <SheetHeader>
          <SheetTitle>{title}</SheetTitle>
          <SheetDescription>{description}</SheetDescription>
        </SheetHeader>
        {context ? (
          <div className="space-y-4 px-4 pb-5">
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 text-sm">
              <dt className="text-muted-foreground">Клиент</dt>
              <dd>{context.studentName}</dd>
              {context.kind === "group_sale" ? (
                <>
                  <dt className="text-muted-foreground">Группа</dt>
                  <dd>{context.groupName}</dd>
                  <dt className="text-muted-foreground">Старт</dt>
                  <dd>{context.startDate}</dd>
                  <dt className="text-muted-foreground">Тариф</dt>
                  <dd>
                    {context.tariffName}
                    {tariffTermsLabel({
                      trainings_limit: context.trainingsLimit,
                      duration_days: context.durationDays,
                    })
                      ? ` · ${tariffTermsLabel({ trainings_limit: context.trainingsLimit, duration_days: context.durationDays })}`
                      : ""}
                  </dd>
                  {context.amount ? (
                    <>
                      <dt className="text-muted-foreground">Сумма</dt>
                      <dd>{formatRubles(context.amount)}</dd>
                    </>
                  ) : null}
                  {context.trainerName ? (
                    <>
                      <dt className="text-muted-foreground">Тренер</dt>
                      <dd>{context.trainerName}</dd>
                    </>
                  ) : null}
                  {context.locationName ? (
                    <>
                      <dt className="text-muted-foreground">Зал</dt>
                      <dd>{context.locationName}</dd>
                    </>
                  ) : null}
                  {context.scheduleLabel ? (
                    <>
                      <dt className="text-muted-foreground">Расписание</dt>
                      <dd>{context.scheduleLabel}</dd>
                    </>
                  ) : null}
                </>
              ) : (
                <>
                  <dt className="text-muted-foreground">Источник</dt>
                  <dd>{context.renewedFromSubscriptionName}</dd>
                  {renewalOfferLabel ? (
                    <>
                      <dt className="text-muted-foreground">Продление</dt>
                      <dd>{renewalOfferLabel}</dd>
                    </>
                  ) : (
                    <dd role="alert" className="ui-warning col-span-2">
                      Актуальная цена продления недоступна. Обновите карточку клиента.
                    </dd>
                  )}
                </>
              )}
            </dl>

            <fieldset className="space-y-2">
              <legend className="ui-field-label">Способ оплаты</legend>
              <div className="grid grid-cols-3 gap-2">
                {availablePaymentMethods.map((method) => (
                  <Button
                    key={method}
                    type="button"
                    variant={paymentMethod === method ? "default" : "outline"}
                    className="min-h-[44px]"
                    aria-pressed={paymentMethod === method}
                    onClick={() => {
                      setPaymentMethod(method);
                      setErrorMessage("");
                    }}
                  >
                    {methodLabels[method]}
                  </Button>
                ))}
              </div>
            </fieldset>

            {!onlinePaymentsEnabled ? (
              <p className="ui-muted-13">
                {getOnlinePaymentUnavailableMessage(paymentCapabilitiesQuery, "staff")}
              </p>
            ) : null}

            {context.kind === "group_sale" && paymentMethod === "sbp" && context.buyerEmailRequired ? (
              <label className="block space-y-1">
                <span className="ui-field-label">Email для чека</span>
                <input
                  type="email"
                  value={buyerEmail}
                  onChange={(event) => setBuyerEmail(event.target.value)}
                  className="ui-input w-full"
                  autoComplete="email"
                />
              </label>
            ) : null}

            {errorMessage ? <p role="alert" className="ui-warning">{errorMessage}</p> : null}
            <Button
              type="button"
              className="ui-brand-action min-h-[44px] w-full"
              disabled={
                isSubmitting ||
                !renewalOfferAvailable ||
                (paymentMethod === "sbp" && !onlinePaymentsEnabled)
              }
              onClick={() => void submit()}
            >
              {isSubmitting
                ? "Создаём..."
                : groupSaleSubmitLabel(context, paymentMethod)}
            </Button>
          </div>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

/** Unified group sales never degrade into the legacy payment sheet while capability is ambiguous. */
export function ContextualGroupSaleUnavailableSheet({
  open,
  onOpenChange,
  isRetrying = false,
  onRetry,
}: {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly isRetrying?: boolean;
  readonly onRetry?: () => void;
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="bottom" className="rounded-t-2xl">
        <SheetHeader>
          <SheetTitle>Оформить обучение</SheetTitle>
          <SheetDescription>
            Групповая продажа временно недоступна: клуб ещё не подтвердил точный контекст
            группы и старта.
          </SheetDescription>
        </SheetHeader>
        <div className="space-y-3 px-4 pb-5">
          <p role="alert" className="ui-warning">
            Оплата не будет создана без точной группы, расписания и даты старта.
          </p>
          {onRetry ? (
            <Button
              type="button"
              variant="outline"
              className="min-h-[44px] w-full"
              disabled={isRetrying}
              onClick={onRetry}
            >
              {isRetrying ? "Проверяем..." : "Повторить проверку"}
            </Button>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}

interface GroupSaleTariff {
  readonly id: number;
  readonly name: string;
  readonly price?: string | number;
  readonly trainings_limit?: number | null;
  readonly duration_days?: number | null;
  readonly is_active: boolean;
  readonly training_type?: { readonly kind?: string };
}

interface GroupSaleOccurrence {
  readonly schedule_id: number;
  readonly date: string;
}

interface GroupSaleOption {
  readonly training_group_id?: number | null;
  readonly schedule_id: number;
  readonly group_name: string;
  readonly trainer_name?: string;
  readonly location_name?: string;
  readonly schedule_label?: string;
  readonly slot_schedule_ids?: readonly number[];
  readonly weekly_schedule?: readonly {
    readonly schedule_id: number;
    readonly day_of_week: number;
    readonly start_time: string;
    readonly end_time: string;
    readonly trainer_name?: string;
    readonly location_name?: string;
  }[];
  readonly is_canonical_group_card?: boolean;
  readonly is_latest_trial_group?: boolean;
  readonly next_occurrence_date: string;
  readonly upcoming_occurrences?: readonly GroupSaleOccurrence[];
  /** Server-selects the business action; renewal requires a source subscription. */
  readonly group_membership_action: "new_admission" | "renewal";
  readonly renewed_from_subscription_id?: number | null;
}

interface GroupSaleOffer {
  readonly protocol_version: "v2";
  readonly student: { readonly id: number; readonly display_name: string };
  readonly tariff: {
    readonly id: number;
    readonly name: string;
    readonly price: string;
    readonly trainings_limit?: number | null;
    readonly duration_days?: number | null;
  };
  readonly group: {
    readonly id: number;
    readonly name: string;
    readonly responsible_trainer_name?: string;
    readonly location_name?: string;
    readonly weekly_schedule?: readonly {
      readonly day_of_week: number;
      readonly start_time: string;
      readonly end_time: string;
      readonly trainer_name?: string;
      readonly location_name?: string;
    }[];
  };
  readonly selected_occurrence: {
    readonly schedule_id: number;
    readonly date: string;
    readonly start_time?: string;
    readonly end_time?: string;
    readonly trainer_name?: string;
    readonly location_name?: string;
  };
  readonly expected_action: "new_admission";
  readonly buyer_email_required: boolean;
  readonly offer_digest: string;
}

function asArray<T>(value: T[] | { items?: T[] } | undefined): T[] {
  return Array.isArray(value) ? value : value?.items ?? [];
}

function isCanonicalNewAdmission(option: GroupSaleOption): option is GroupSaleOption & { training_group_id: number } {
  return (
    option.group_membership_action === "new_admission" &&
    option.is_canonical_group_card === true &&
    typeof option.training_group_id === "number" &&
    Number.isSafeInteger(option.training_group_id) &&
    option.training_group_id > 0
  );
}

function exactOccurrences(option: GroupSaleOption): readonly GroupSaleOccurrence[] {
  const candidates = option.upcoming_occurrences?.length
    ? option.upcoming_occurrences
    : [{ schedule_id: option.schedule_id, date: option.next_occurrence_date }];
  return candidates
    .filter(
      (occurrence) =>
        Number.isSafeInteger(occurrence.schedule_id) &&
        occurrence.schedule_id > 0 &&
        /^\d{4}-\d{2}-\d{2}$/.test(occurrence.date),
    )
    .sort(
      (left, right) =>
        left.date.localeCompare(right.date) || left.schedule_id - right.schedule_id,
    );
}

function offerScheduleLabel(offer: GroupSaleOffer) {
  const occurrence = offer.selected_occurrence;
  const hours = occurrence.start_time && occurrence.end_time
    ? ` · ${occurrence.start_time}–${occurrence.end_time}`
    : "";
  return `${occurrence.date}${hours}`;
}

const weekdayLabels = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const initialGroupCardLimit = 4;

function weeklyScheduleSummary(option: GroupSaleOption) {
  return (option.weekly_schedule ?? [])
    .map((slot) => `${weekdayLabels[slot.day_of_week] ?? ""} ${slot.start_time}–${slot.end_time}`.trim())
    .filter(Boolean)
    .join(", ");
}

function offerToContext(offer: GroupSaleOffer, option: GroupSaleOption): GroupSaleContext {
  return {
    kind: "group_sale",
    protocolVersion: "v2",
    offerDigest: offer.offer_digest,
    studentId: offer.student.id,
    studentName: offer.student.display_name,
    tariffId: offer.tariff.id,
    tariffName: offer.tariff.name,
    amount: offer.tariff.price,
    trainingsLimit: offer.tariff.trainings_limit,
    durationDays: offer.tariff.duration_days,
    trainingGroupId: offer.group.id,
    scheduleId: offer.selected_occurrence.schedule_id,
    startDate: offer.selected_occurrence.date,
    groupName: offer.group.name,
    trainerName: offer.selected_occurrence.trainer_name ?? offer.group.responsible_trainer_name,
    locationName: offer.selected_occurrence.location_name ?? offer.group.location_name,
    scheduleLabel: offerScheduleLabel(offer),
    slotScheduleIds: option.slot_schedule_ids,
    buyerEmailRequired: offer.buyer_email_required,
  };
}

function leadJourneyHint({
  leadStatus,
  trialDate,
}: {
  readonly leadStatus?: string | null;
  readonly trialDate?: string | null;
}) {
  const dateSuffix = trialDate ? ` · ${trialDate}` : "";
  if (leadStatus === "trial_done") return `Пробная проведена${dateSuffix}`;
  if (leadStatus === "trial_booked") return `Пробная назначена${dateSuffix}`;
  return null;
}

/** Flag-on picker: it accepts a server-provided canonical group/schedule/date only. */
export function ContextualGroupSalePickerSheet({
  open,
  onOpenChange,
  studentId,
  studentName,
  leadStatus,
  trialDate,
  onAuthoritativeResult,
  onRenewalConflict,
}: {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly studentId: number;
  readonly studentName: string;
  /** Read-only hint from the lead projection; it never filters candidates. */
  readonly leadStatus?: string | null;
  readonly trialDate?: string | null;
  readonly onAuthoritativeResult?: (result: ContextualCommercialCommandResult) => void;
  readonly onRenewalConflict?: () => void;
}) {
  const commercialCacheScope = useCommercialCacheScope();
  const [tariffId, setTariffId] = useState<number | null>(null);
  const [trainingGroupId, setTrainingGroupId] = useState<number | null>(null);
  const [selectedOccurrence, setSelectedOccurrence] = useState<GroupSaleOccurrence | null>(null);
  const [reviewRequested, setReviewRequested] = useState(false);
  const [showOtherGroups, setShowOtherGroups] = useState(false);
  const tariffsQuery = useQuery<GroupSaleTariff[] | { items?: GroupSaleTariff[] }>({
    queryKey: ["billing", "tariffs", "contextual-group-sale", ...commercialCacheScope],
    queryFn: () =>
      apiClient
        .get<GroupSaleTariff[] | { items?: GroupSaleTariff[] }>("/billing/tariffs/")
        .then((response) => response.data),
    enabled: open,
    staleTime: 60_000,
    retry: false,
  });
  const groupTariffs = asArray(tariffsQuery.data).filter(
    (tariff) => tariff.is_active && tariff.training_type?.kind === "group",
  );
  const selectedTariffId =
    tariffId ?? (groupTariffs.length === 1 ? groupTariffs[0]?.id ?? null : null);
  const groupOptionsQuery = useQuery<GroupSaleOption[]>({
    queryKey: [
      "billing",
      "group-enrollment-options",
      studentId,
      selectedTariffId,
      "contextual",
      ...commercialCacheScope,
    ],
    queryFn: () =>
      apiClient
        .get<GroupSaleOption[]>("/billing/group-enrollment-options/", {
          params: { student_id: studentId, tariff_id: selectedTariffId },
        })
        .then((response) => response.data),
    enabled: open && selectedTariffId !== null,
    staleTime: 30_000,
    retry: false,
  });

  const selectedTariff = groupTariffs.find((tariff) => tariff.id === selectedTariffId) ?? null;
  const allGroupOptions = groupOptionsQuery.data ?? [];
  const groupOptions = allGroupOptions
    .filter(isCanonicalNewAdmission)
    .sort((left, right) => {
      const recommendation = Number(Boolean(right.is_latest_trial_group)) - Number(Boolean(left.is_latest_trial_group));
      return recommendation || left.group_name.localeCompare(right.group_name, "ru") || left.training_group_id - right.training_group_id;
    });
  const renewalConflictOptions = allGroupOptions.filter(
    (option) =>
      option.is_canonical_group_card === true &&
      option.group_membership_action === "renewal" &&
      typeof option.training_group_id === "number",
  );
  const recommendedGroup = groupOptions.find((option) => option.is_latest_trial_group) ?? null;
  const otherGroups = recommendedGroup
    ? groupOptions.filter((option) => option.training_group_id !== recommendedGroup.training_group_id)
    : groupOptions;
  const initialGroupOptions = recommendedGroup
    ? [recommendedGroup, ...otherGroups.slice(0, initialGroupCardLimit - 1)]
    : groupOptions.slice(0, initialGroupCardLimit);
  const visibleGroupOptions = showOtherGroups ? groupOptions : initialGroupOptions;
  const hiddenGroupCount = groupOptions.length - initialGroupOptions.length;
  const selectedGroup = groupOptions.find((option) => option.training_group_id === trainingGroupId) ?? null;
  const offerQuery = useQuery<GroupSaleOffer>({
    queryKey: [
      "billing",
      "group-sale-offer",
      studentId,
      selectedTariff?.id,
      selectedGroup?.training_group_id,
      selectedOccurrence?.schedule_id,
      selectedOccurrence?.date,
      ...commercialCacheScope,
    ],
    queryFn: () =>
      apiClient
        .get<GroupSaleOffer>("/billing/group-sale-offers/preview/", {
          params: {
            student_id: studentId,
            tariff_id: selectedTariff?.id,
            target_training_group_id: selectedGroup?.training_group_id,
            target_schedule_id: selectedOccurrence?.schedule_id,
            target_start_date: selectedOccurrence?.date,
          },
        })
        .then((response) => response.data),
    enabled:
      open &&
      reviewRequested &&
      selectedTariff !== null &&
      selectedGroup !== null &&
      selectedOccurrence !== null,
    staleTime: 0,
    retry: false,
  });

  if (offerQuery.data && selectedGroup) {
    return (
      <ContextualCommercialSheet
        open
        onOpenChange={(nextOpen) => {
          if (nextOpen) return;
          setTrainingGroupId(null);
          setSelectedOccurrence(null);
          setReviewRequested(false);
          onOpenChange(false);
        }}
        context={offerToContext(offerQuery.data, selectedGroup)}
        onAuthoritativeResult={onAuthoritativeResult}
        onOfferChanged={() => {
          setTrainingGroupId(null);
          setSelectedOccurrence(null);
          setReviewRequested(false);
          setShowOtherGroups(false);
          void groupOptionsQuery.refetch();
        }}
      />
    );
  }

  return (
    <Sheet
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) {
          setTariffId(null);
          setTrainingGroupId(null);
          setSelectedOccurrence(null);
          setReviewRequested(false);
          setShowOtherGroups(false);
          onOpenChange(false);
        }
      }}
    >
      <SheetContent side="bottom" className="max-h-[90vh] overflow-y-auto rounded-t-2xl">
        <SheetHeader>
          <SheetTitle>Оформить обучение</SheetTitle>
          <SheetDescription>Оформляем: {studentName}</SheetDescription>
        </SheetHeader>
        <div className="space-y-4 px-4 pb-5">
          {leadJourneyHint({ leadStatus, trialDate }) ? (
            <p className="ui-muted-14">{leadJourneyHint({ leadStatus, trialDate })}</p>
          ) : null}
          {tariffsQuery.isError ? (
            <p role="status" className="ui-warning">Не удалось загрузить доступные тарифы. Повторите позже.</p>
          ) : (
            <fieldset className="space-y-2">
              <legend className="ui-field-label">Тариф</legend>
              {groupTariffs.map((tariff) => (
                <Button
                  key={tariff.id}
                  type="button"
                  variant={tariff.id === selectedTariffId ? "default" : "outline"}
                  wrap
                  className="min-h-[44px] min-w-0 w-full justify-start text-left"
                  aria-pressed={tariff.id === selectedTariffId}
                  onClick={() => {
                    setTariffId(tariff.id);
                    setTrainingGroupId(null);
                    setSelectedOccurrence(null);
                    setReviewRequested(false);
                    setShowOtherGroups(false);
                  }}
                >
                  {tariff.name}{tariff.price !== undefined ? ` · ${formatRubles(tariff.price)}` : ""}
                  {tariffTermsLabel(tariff) ? ` · ${tariffTermsLabel(tariff)}` : ""}
                </Button>
              ))}
              {!tariffsQuery.isLoading && groupTariffs.length === 0 ? (
                <p className="ui-muted-14">Для группового обучения нет доступного тарифа.</p>
              ) : null}
            </fieldset>
          )}

          {selectedTariffId !== null && groupOptionsQuery.isLoading ? <p className="ui-muted-14">Загрузка групп...</p> : null}
          {selectedTariffId !== null && groupOptionsQuery.isError ? (
            <p role="status" className="ui-warning">Не удалось проверить группы. Повторите позже.</p>
          ) : null}
          {selectedTariffId !== null && groupOptionsQuery.isSuccess ? (
            <fieldset className="space-y-2">
              <legend className="ui-field-label">Группа</legend>
              {visibleGroupOptions.map((option) => (
                <Button
                  key={option.training_group_id}
                  type="button"
                  variant={trainingGroupId === option.training_group_id ? "default" : "outline"}
                  wrap
                  className="min-h-[44px] min-w-0 w-full justify-start text-left"
                  aria-pressed={trainingGroupId === option.training_group_id}
                  onClick={() => {
                    setTrainingGroupId(option.training_group_id);
                    setSelectedOccurrence(exactOccurrences(option)[0] ?? null);
                    setReviewRequested(false);
                  }}
                >
                  <span>
                    {option.group_name}
                    {option.is_latest_trial_group ? " · Рекомендовано" : ""}
                    {option.trainer_name ? ` · ${option.trainer_name}` : ""}
                    {option.location_name ? ` · ${option.location_name}` : ""}
                    {weeklyScheduleSummary(option) ? ` · ${weeklyScheduleSummary(option)}` : ""}
                  </span>
                </Button>
              ))}
              {!groupOptions.length ? (
                <p className="ui-muted-14">Нет доступной группы с точной датой старта.</p>
              ) : null}
              {hiddenGroupCount > 0 ? (
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-[44px] w-full"
                  onClick={() => setShowOtherGroups((current) => !current)}
                >
                  {showOtherGroups
                    ? "Скрыть другие группы"
                    : `Другие группы (${hiddenGroupCount})`}
                </Button>
              ) : null}
            </fieldset>
          ) : null}

          {renewalConflictOptions.length ? (
            <section role="status" className="rounded-xl bg-amber-50 p-3 text-[14px] text-amber-950">
              <p className="font-semibold">Для одной из групп уже есть действующее членство.</p>
              <p className="mt-1">Новое оформление недоступно: продолжите абонемент в карточке ученика.</p>
              {onRenewalConflict ? (
                <Button
                  type="button"
                  variant="outline"
                  className="mt-3 min-h-[44px] w-full"
                  onClick={onRenewalConflict}
                >
                  Открыть продление ученика
                </Button>
              ) : null}
            </section>
          ) : null}

          {selectedGroup ? (
            <fieldset className="space-y-2">
              <legend className="ui-field-label">Точная дата старта</legend>
              {exactOccurrences(selectedGroup).map((occurrence) => (
                <Button
                  key={`${occurrence.schedule_id}:${occurrence.date}`}
                  type="button"
                  variant={
                    selectedOccurrence?.schedule_id === occurrence.schedule_id && selectedOccurrence.date === occurrence.date
                      ? "default"
                      : "outline"
                  }
                  className="min-h-[44px] w-full justify-start"
                  aria-pressed={
                    selectedOccurrence?.schedule_id === occurrence.schedule_id &&
                    selectedOccurrence.date === occurrence.date
                  }
                  onClick={() => {
                    setSelectedOccurrence(occurrence);
                    setReviewRequested(false);
                  }}
                >
                  {occurrence.date}
                </Button>
              ))}
              {!exactOccurrences(selectedGroup).length ? (
                <p className="ui-muted-14">Для этой группы нет точной даты старта.</p>
              ) : null}
              {selectedOccurrence ? (
                <Button
                  type="button"
                  className="ui-brand-action mt-2 min-h-[44px] w-full"
                  onClick={() => setReviewRequested(true)}
                >
                  Проверить условия
                </Button>
              ) : null}
            </fieldset>
          ) : null}

          {selectedOccurrence && offerQuery.isLoading ? <p className="ui-muted-14">Проверяем предложение...</p> : null}
          {selectedOccurrence && offerQuery.isError ? (
            <p role="alert" className="ui-warning">
              Предложение изменилось или недоступно. Обновите выбор группы и даты старта.
            </p>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CalendarPlus, RotateCcw, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  OnlinePaymentLinkPanel,
  type BankPaymentOrderLink,
} from "@/components/portal/online-payment-link-panel";
import apiClient from "@/api/custom-fetch";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import { toDateParamInTimeZone } from "@/lib/club-date";
import { cn, getApiError } from "@/lib/utils";
import {
  getCurrentSelfServicePersonalActorScope,
  type SelfServicePersonalCommandCard,
  type SelfServicePersonalOption,
  type SelfServicePersonalScope,
  useSelfServicePersonalJourney,
} from "@/api/self-service-personal";
import { PRIVATE_QUERY_SCOPE_MARKER } from "@/api/private-query-cache";

const COMMAND_ACTIONS = {
  openBankPaymentOrder: "open_bank_payment_order",
  cancelBankPaymentOrder: "cancel_bank_payment_order",
  retryBankPayment: "retry_bank_payment",
  viewBooking: "view_booking",
} as const;

function hasAction(card: SelfServicePersonalCommandCard, action: string) {
  return card.allowed_actions.includes(action as never);
}

function formatDateTime(value: string, timeZone: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.replace("T", " ").slice(0, 16);
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone,
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatPrice(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === "") return null;
  const amount = Number(value);
  if (!Number.isFinite(amount)) return `${value} ₽`;
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency: "RUB",
    maximumFractionDigits: 2,
  }).format(amount);
}

function statusLabel(card: SelfServicePersonalCommandCard, terminal: boolean) {
  const status = terminal ? card.status || card.order_status : card.order_status || card.status;
  const labels: Record<string, string> = {
    created: "Создано",
    pending: "Ожидает оплаты",
    pending_payment: "Ожидает оплаты",
    authorized: "Проверяем оплату",
    manual_review: "Оплата на проверке",
    approved: "Оплата подтверждена",
    fulfilled: "Запись подтверждена",
    booked: "Запись создана",
    cancelled: "Отменено",
    failed: "Оплата не подтверждена",
    expired: "Срок ссылки истёк",
    rejected: "Оплата отклонена",
  };
  return labels[status] ?? "Статус обновляется";
}

function commandErrorMessage(error: unknown, fallback: string) {
  const code =
    error && typeof error === "object"
      ? (error as { response?: { data?: { code?: unknown } } }).response?.data?.code
      : undefined;
  if (code === "personal_offer_changed") {
    return "Цена или условия изменились. Проверьте обновлённый вариант перед новой оплатой.";
  }
  return getApiError(error, fallback);
}

function useAuthoritativeClubTimeZone() {
  const clubId = useAuthStore((state) => state.clubId);
  const timeZone = useBrandingStore((state) => state.timeZone);
  const timeZoneStatus = useBrandingStore((state) => state.timeZoneStatus);
  const timeZoneClubId = useBrandingStore((state) => state.timeZoneClubId);
  const isTimeZoneAuthoritative = useBrandingStore((state) => state.isTimeZoneAuthoritative);
  const isAuthoritative =
    clubId !== null &&
    timeZoneStatus === "ready" &&
    isTimeZoneAuthoritative &&
    timeZoneClubId === clubId;
  return {
    timeZone: isAuthoritative ? timeZone : null,
    isError:
      timeZoneStatus === "failed" ||
      (timeZoneStatus === "ready" && (!isTimeZoneAuthoritative || timeZoneClubId !== clubId)),
  };
}

export function SelfServicePersonalUnavailableNotice({
  isError,
  className,
}: {
  isError: boolean;
  className?: string;
}) {
  return (
    <section
      aria-label="Доступность персональной записи"
      className={cn("rounded-[20px] bg-amber-50/80 p-4 ring-1 ring-amber-500/15", className)}
    >
      <p className="text-[14px] font-semibold text-amber-950">
        {isError
          ? "Персональная запись временно недоступна"
          : "Проверяем доступность персональной записи"}
      </p>
      <p className="mt-1 text-[13px] leading-5 text-amber-900/80">
        {isError
          ? "Не удалось подтвердить настройки клуба. Обновите страницу позже."
          : "Подождите подтверждения настроек клуба перед записью или оплатой."}
      </p>
    </section>
  );
}

export function SelfServicePersonalCommandCards({
  scope,
  enabled,
  onlinePaymentsEnabled,
  childName,
  className,
  hideWhenEmpty = false,
}: {
  scope: SelfServicePersonalScope;
  enabled: boolean;
  onlinePaymentsEnabled: boolean;
  childName?: string;
  className?: string;
  hideWhenEmpty?: boolean;
}) {
  const { timeZone, isError: timeZoneError } = useAuthoritativeClubTimeZone();
  const journey = useSelfServicePersonalJourney({
    scope,
    date: timeZone ? toDateParamInTimeZone(new Date(), timeZone) : "",
    enabled: enabled && Boolean(timeZone),
    onlinePaymentsEnabled,
    loadOptions: false,
    timeZone,
  });
  const [errorMessage, setErrorMessage] = useState("");
  const isMutating =
    journey.cancelMutation.isPending || journey.retryMutation.isPending;
  const refetchCommands = journey.commandsQuery.refetch;
  const cards = useMemo(() => {
    const byId = new Map<number, { card: SelfServicePersonalCommandCard; terminal: boolean }>();
    journey.commandsQuery.data?.live.forEach((card) => byId.set(card.command_id, { card, terminal: false }));
    journey.commandsQuery.data?.latest_terminal.forEach((card) =>
      byId.set(card.command_id, { card, terminal: true }),
    );
    return Array.from(byId.values());
  }, [journey.commandsQuery.data]);

  useEffect(() => {
    if (!enabled) return;
    const refreshFromServer = () => void refetchCommands();
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") refreshFromServer();
    };
    window.addEventListener("pageshow", refreshFromServer);
    window.addEventListener("focus", refreshFromServer);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.removeEventListener("pageshow", refreshFromServer);
      window.removeEventListener("focus", refreshFromServer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [enabled, refetchCommands]);

  if (!enabled) return null;
  if (!timeZone) return <SelfServicePersonalUnavailableNotice isError={timeZoneError} className={className} />;
  if (
    hideWhenEmpty &&
    (journey.commandsQuery.isLoading || journey.commandsQuery.isError || cards.length === 0)
  ) {
    return null;
  }

  return (
    <section
      aria-label={childName ? `Персональные тренировки: ${childName}` : "Персональные тренировки"}
      className={cn("space-y-2.5", className)}
    >
      <div className="space-y-1 px-1">
        <p className="ui-overline">Персональные тренировки{childName ? `: ${childName}` : ""}</p>
        <h2 className="portal-balanced-title text-[17px] font-semibold">Статус записи и оплаты</h2>
      </div>
      {journey.commandsQuery.isLoading ? (
        <div className="space-y-2" aria-label="Загружаем статус персональных тренировок">
          <Skeleton className="h-28 rounded-[20px]" />
          <Skeleton className="h-24 rounded-[20px]" />
        </div>
      ) : journey.commandsQuery.isError ? (
        <div className="rounded-[20px] bg-red-50 p-4 ring-1 ring-red-100">
          <p className="text-[14px] font-semibold text-red-950">Не удалось загрузить статус записи</p>
          <p className="mt-1 text-[13px] leading-5 text-red-900/75">
            Это не означает, что оплата подтверждена или отменена.
          </p>
          <Button
            type="button"
            variant="outline"
            className="mt-3 min-h-[44px] w-full bg-white"
            onClick={() => void journey.commandsQuery.refetch()}
          >
            <RotateCcw className="size-4" />
            Повторить
          </Button>
        </div>
      ) : cards.length === 0 ? (
        <div className="rounded-[20px] bg-white/90 p-4 text-center ring-1 ring-black/6">
          <p className="text-[14px] font-semibold">Нет активных или недавних персональных записей</p>
          <p className="ui-muted-detail">Здесь появятся ссылка СБП и итоговый статус записи.</p>
        </div>
      ) : (
        <div className="space-y-2.5">
          {cards.map(({ card, terminal }) => {
            const canOpenBank =
              onlinePaymentsEnabled &&
              !terminal &&
              hasAction(card, COMMAND_ACTIONS.openBankPaymentOrder) &&
              Boolean(card.bank_payment_order_id);
            const canCancel =
              !terminal && hasAction(card, COMMAND_ACTIONS.cancelBankPaymentOrder);
            const serverPaymentStatus = card.order_status || card.status;
            const canRetry =
              onlinePaymentsEnabled &&
              terminal &&
              ["cancelled", "failed", "expired"].includes(serverPaymentStatus) &&
              hasAction(card, COMMAND_ACTIONS.retryBankPayment);
            const hasBooking =
              hasAction(card, COMMAND_ACTIONS.viewBooking) && Boolean(card.booking_id);
            const amount = formatPrice(card.amount_snapshot);
            return (
              <article
                key={card.command_id}
                className="rounded-[20px] border border-black/6 bg-white/94 p-4 shadow-sm"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <p className="text-[15px] font-semibold">Персональная тренировка</p>
                    <p className="mt-1 text-[13px] text-muted-foreground">
                      {formatDateTime(card.starts_at, timeZone)}
                    </p>
                  </div>
                  <span className="rounded-full bg-black/[0.05] px-3 py-1 text-[12px] font-medium text-foreground">
                      {statusLabel(card, terminal)}
                  </span>
                </div>
                {amount ? (
                  <p className="mt-3 text-[14px] font-semibold text-foreground">К оплате: {amount}</p>
                ) : null}
                {hasBooking ? (
                  <p className="mt-3 text-[13px] text-emerald-700">Запись сохранена в расписании.</p>
                ) : null}
                {canOpenBank || canCancel || canRetry ? (
                  <div className="mt-3 grid gap-2 sm:grid-cols-2">
                    {canCancel ? (
                      <Button
                        type="button"
                        variant="outline"
                        className="min-h-[44px]"
                        disabled={isMutating}
                        onClick={() => {
                          setErrorMessage("");
                          journey.cancelMutation.mutate(card, {
                            onError: (error) =>
                              setErrorMessage(commandErrorMessage(error, "Не удалось отменить оплату")),
                          });
                        }}
                      >
                        <XCircle className="size-4" />
                        Отменить оплату
                      </Button>
                    ) : null}
                    {canRetry ? (
                      <Button
                        type="button"
                        className="min-h-[44px]"
                        disabled={isMutating}
                        onClick={() => {
                          setErrorMessage("");
                          journey.retryMutation.mutate(card, {
                            onError: (error) =>
                              setErrorMessage(commandErrorMessage(error, "Не удалось создать новую ссылку")),
                          });
                        }}
                      >
                        <RotateCcw className="size-4" />
                        Оплатить заново
                      </Button>
                    ) : null}
                  </div>
                ) : null}
                {canOpenBank ? (
                  <SelfServicePersonalBankOrderPanel card={card} scope={scope} />
                ) : null}
                {card.capability === "can_pay" && !onlinePaymentsEnabled ? (
                  <p className="mt-3 text-[13px] text-amber-800">
                    Онлайн-оплата сейчас недоступна. Обновите данные позже.
                  </p>
                ) : null}
              </article>
            );
          })}
        </div>
      )}
      {errorMessage ? (
        <p role="alert" aria-live="assertive" className="ui-error-14">
          {errorMessage}
        </p>
      ) : null}
    </section>
  );
}

export function SelfServicePersonalBookingSection({
  scope,
  enabled,
  onlinePaymentsEnabled,
  className,
}: {
  scope: SelfServicePersonalScope;
  enabled: boolean;
  onlinePaymentsEnabled: boolean;
  className?: string;
}) {
  const { timeZone, isError: timeZoneError } = useAuthoritativeClubTimeZone();
  const [date, setDate] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const selectedDate = date || (timeZone ? toDateParamInTimeZone(new Date(), timeZone) : "");
  const journey = useSelfServicePersonalJourney({
    scope,
    date: selectedDate,
    enabled: enabled && Boolean(timeZone),
    onlinePaymentsEnabled,
    timeZone,
  });

  if (!enabled) return null;
  if (!timeZone) return <SelfServicePersonalUnavailableNotice isError={timeZoneError} className={className} />;

  const submittingSlotId = journey.commandMutation.isPending
    ? journey.commandMutation.variables?.option.slot_id
    : null;
  return (
    <section aria-label="Запись на персональную тренировку" className={cn("space-y-3", className)}>
      <div className="space-y-1 px-1">
        <p className="ui-overline">Персоналка</p>
        <h2 className="portal-balanced-title text-[17px] font-semibold">Выберите свободный слот</h2>
        <p className="portal-pretty-text text-[13px] leading-5 text-muted-foreground">
          С подходящим абонементом запись создастся сразу. Иначе сначала покажем цену и
          предложим оплату через СБП.
        </p>
      </div>
      <label className="block text-[13px] font-medium text-foreground">
        Дата
        <input
          type="date"
          value={selectedDate}
          onChange={(event) => {
            setErrorMessage("");
            setDate(event.target.value);
          }}
          className="mt-1 min-h-[44px] w-full rounded-xl border border-input bg-background px-3 text-sm"
        />
      </label>
      {journey.optionsQuery.isLoading ? (
        <div className="space-y-2" aria-label="Загружаем персональные слоты">
          <Skeleton className="h-28 rounded-[20px]" />
          <Skeleton className="h-28 rounded-[20px]" />
        </div>
      ) : journey.optionsQuery.isError ? (
        <div className="rounded-[20px] bg-red-50 p-4 ring-1 ring-red-100">
          <p className="text-[14px] font-semibold text-red-950">Не удалось загрузить персональные слоты</p>
          <p className="mt-1 text-[13px] text-red-900/75">Попробуйте повторить загрузку выбранного дня.</p>
          <Button
            type="button"
            variant="outline"
            className="mt-3 min-h-[44px] w-full bg-white"
            onClick={() => void journey.optionsQuery.refetch()}
          >
            <RotateCcw className="size-4" />
            Повторить
          </Button>
        </div>
      ) : journey.optionsQuery.data?.length === 0 ? (
        <div className="rounded-[20px] bg-white/90 p-4 text-center ring-1 ring-black/6">
          <p className="text-[14px] font-semibold">На этот день нет персональных слотов</p>
          <p className="ui-muted-detail">Выберите другую дату или дождитесь публикации слота.</p>
        </div>
      ) : (
        <div className="space-y-2.5">
          {journey.optionsQuery.data?.map((option) => (
            <PersonalOptionCard
              key={`${option.slot_id}-${option.offer_digest}`}
              option={option}
              isPending={submittingSlotId === option.slot_id}
              disabled={submittingSlotId !== null}
              onlinePaymentsEnabled={onlinePaymentsEnabled}
              onCommand={() => {
                setErrorMessage("");
                journey.commandMutation.mutate(
                  { option },
                  {
                    onError: (error) =>
                      setErrorMessage(
                        commandErrorMessage(
                          error,
                          option.capability === "can_pay"
                            ? "Не удалось создать ссылку на оплату"
                            : "Не удалось записаться",
                        ),
                      ),
                  },
                );
              }}
            />
          ))}
        </div>
      )}
      {errorMessage ? (
        <p role="alert" aria-live="assertive" className="ui-error-14">
          {errorMessage}
        </p>
      ) : null}
    </section>
  );
}

function SelfServicePersonalBankOrderPanel({
  card,
  scope,
}: {
  card: SelfServicePersonalCommandCard;
  scope: SelfServicePersonalScope;
}) {
  const actorScope = getCurrentSelfServicePersonalActorScope();
  const orderId = card.bank_payment_order_id;
  const endpoint =
    scope.audience === "student" && orderId
      ? `/students/me/bank-payment-orders/${orderId}/`
      : scope.audience === "parent" && scope.childStudentId && orderId
        ? `/parents/children/${scope.childStudentId}/bank-payment-orders/${orderId}/`
        : null;
  const orderQuery = useQuery({
    queryKey: [
      "personal-availability",
      "self-service",
      "bank-payment-order",
      PRIVATE_QUERY_SCOPE_MARKER,
      actorScope?.clubId ?? "no-club",
      actorScope?.actorSubject ?? "unauthenticated",
      scope.audience,
      actorScope?.role ?? "no-role",
      scope.childStudentId ?? "self",
      orderId,
    ],
    queryFn: () => apiClient.get<BankPaymentOrderLink>(endpoint as string).then((response) => response.data),
    enabled: Boolean(endpoint) && actorScope !== null,
    staleTime: 30_000,
    retry: false,
  });

  if (orderQuery.isLoading) return <Skeleton className="mt-3 h-12 rounded-xl" />;
  if (orderQuery.isError || !orderQuery.data) {
    return (
      <p className="mt-3 text-[13px] text-amber-800">
        Не удалось получить защищённую ссылку СБП. Обновите статус записи позже.
      </p>
    );
  }
  return (
    <OnlinePaymentLinkPanel
      order={orderQuery.data}
      title="Онлайн-оплата персональной тренировки"
      subtitle="Ссылка и статус проверены в кабинете."
      className="mt-3 bg-black/[0.02] shadow-none"
    />
  );
}

function PersonalOptionCard({
  option,
  isPending,
  disabled,
  onlinePaymentsEnabled,
  onCommand,
}: {
  option: SelfServicePersonalOption;
  isPending: boolean;
  disabled: boolean;
  onlinePaymentsEnabled: boolean;
  onCommand: () => void;
}) {
  const timeZone = useBrandingStore((state) => state.timeZone);
  const canBook = option.capability === "can_book";
  const isPaymentOption = option.capability === "can_pay";
  const canPay = isPaymentOption && onlinePaymentsEnabled;
  const hasKnownCapability = canBook || isPaymentOption;
  const price = canPay ? formatPrice(option.offer_price) : null;
  const validPayOffer = canPay && Boolean(option.offer_digest) && Boolean(price);
  return (
    <article className="rounded-[20px] border border-black/6 bg-white/94 p-4 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[15px] font-semibold">{option.training_type_name}</p>
          <p className="mt-1 text-[13px] text-muted-foreground">
            {formatDateTime(option.starts_at, timeZone)} · {option.trainer_name}
          </p>
          <p className="mt-1 text-[13px] text-muted-foreground">{option.location_name}</p>
        </div>
        {canPay && price ? (
          <span className="rounded-full bg-black/[0.05] px-3 py-1 text-[12px] font-semibold">{price}</span>
        ) : null}
      </div>
      {canPay && option.offer_tariff_name ? (
        <p className="mt-3 text-[13px] text-muted-foreground">{option.offer_tariff_name}</p>
      ) : null}
      {canPay && option.offer_digest ? (
        <p className="mt-1 break-all text-[12px] text-muted-foreground">
          Условия предложения: {option.offer_digest}
        </p>
      ) : null}
      <Button
        type="button"
        className="mt-3 min-h-[44px] w-full"
        disabled={
          disabled ||
          !hasKnownCapability ||
          (isPaymentOption && (!onlinePaymentsEnabled || !validPayOffer))
        }
        onClick={onCommand}
      >
        <CalendarPlus className="size-4" />
        {isPending
          ? "Отправляем..."
          : canPay
            ? "Оплатить через СБП"
            : canBook
              ? "Записаться"
              : isPaymentOption
                ? "Онлайн-оплата недоступна"
              : "Недоступно"}
      </Button>
      {canPay && !validPayOffer ? (
        <p className="mt-2 text-[13px] text-amber-800">Цена этой тренировки пока обновляется. Обновите список позже.</p>
      ) : null}
      {!hasKnownCapability ? (
        <p className="mt-2 text-[13px] text-red-800">
          Конфигурация этого слота не поддерживается. Обновите список позже.
        </p>
      ) : null}
      {isPaymentOption && !onlinePaymentsEnabled ? (
        <p className="mt-2 text-[13px] text-amber-800">
          Онлайн-оплата сейчас недоступна. Обновите данные позже.
        </p>
      ) : null}
    </article>
  );
}

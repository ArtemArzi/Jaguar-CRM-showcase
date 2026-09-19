import {
  CalendarPlus,
  ChevronLeft,
  ChevronRight,
  Clock3,
  MapPin,
  RotateCcw,
  UserRound,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import {
  OnlinePaymentLinkPanel,
  type BankPaymentOrderLink,
} from "@/components/portal/online-payment-link-panel";
import {
  isManualReviewPersonalPaymentReservation,
  isVisiblePersonalPaymentReservation,
  type PersonalBookingPaymentReservation,
} from "@/components/portal/personal-payment-reservation-state";
import apiClient from "@/api/custom-fetch";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useBrandingStore } from "@/features/branding/use-branding";
import { APP_TIME_ZONE } from "@/lib/locale";
import { cn, formatRub } from "@/lib/utils";
import { useAuthStore } from "@/features/auth/auth-store";
import { usePrivateQueryScope } from "@/features/auth/private-query-scope";

const WRAPPING_BADGE_CLASS_NAME =
  "h-auto min-h-5 max-w-full min-w-0 shrink items-start justify-start whitespace-normal break-words py-1 text-left leading-snug";

export interface GuestBookingOption {
  schedule_id: number;
  date: string;
  start_time: string;
  end_time: string;
  group_name: string;
  trainer_name: string;
  location_name: string;
  training_type_id: number;
  training_type_name: string;
  booking_status: "can_book" | "already_booked" | "blocked";
  reason_code: string;
  financial_status: "subscription" | "trial_free" | "drop_in_debt" | "blocked";
  subscription_id: number | null;
  drop_in_price: string | null;
}

export interface GuestVisitOut {
  enrollment_id: number;
  student_id: number;
  display_name: string;
  schedule_id: number;
  created_from: string;
  is_guest_visit: boolean;
  starts_on: string | null;
  ends_on: string | null;
  created: boolean;
  already_member: boolean;
  origin: string;
  financial_preview: {
    code: string;
    message: string;
  };
}

export interface PersonalAvailabilityOption {
  slot_id: number;
  date: string;
  starts_at: string;
  ends_at: string;
  trainer_id: number;
  trainer_name: string;
  location_id: number;
  location_name: string;
  training_type_id: number;
  training_type_name: string;
  booking_status: "can_book" | "can_pay" | "blocked";
  reason_code: string;
  subscription_id: number | null;
  payment_tariff_id: number | null;
  payment_tariff_name: string;
  payment_amount: string;
  /** Slice 3 designated offer fields; payment_tariff_* remains flag-off compatibility. */
  offer_tariff_id?: number | null;
  offer_tariff_name?: string;
  offer_price?: string | number | null;
  offer_digest?: string;
  offer_error_code?: string;
}

export interface PersonalBookingOut {
  schedule_id: number;
  enrollment_id: number;
  availability_slot_id: number | null;
  student_id: number;
  trainer_id: number;
  trainer_name: string;
  location_id: number;
  location_name: string;
  training_type_id: number;
  training_type_name: string;
  starts_at: string;
  ends_at: string;
  created_from: string;
  created: boolean;
}

export type { PersonalBookingPaymentReservation } from "@/components/portal/personal-payment-reservation-state";

export interface GuestBookingDateOption {
  value: string;
  label: string;
  shortLabel: string;
}

interface GuestSelfBookingSectionProps {
  readonly ariaLabel: string;
  readonly eyebrow: string;
  readonly title: string;
  readonly description: string;
  readonly dateOptions: readonly GuestBookingDateOption[];
  readonly selectedDate: string;
  readonly onDateChange: (date: string) => void;
  readonly dateRangeLabel?: string;
  readonly onPreviousDateRange?: () => void;
  readonly onNextDateRange?: () => void;
  readonly onCurrentDateRange?: () => void;
  readonly options: readonly GuestBookingOption[];
  readonly isLoading?: boolean;
  readonly isError?: boolean;
  readonly onRetry?: () => void;
  readonly onBook: (option: GuestBookingOption) => void;
  readonly pendingScheduleId?: number | null;
  readonly bookedScheduleId?: number | null;
  readonly errorMessage?: string | null;
  readonly className?: string;
}

interface GuestSelfBookingSheetProps extends GuestSelfBookingSectionProps {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly subjectName?: string;
  readonly personalOptions: readonly PersonalAvailabilityOption[];
  readonly personalIsLoading?: boolean;
  readonly personalIsError?: boolean;
  readonly onPersonalRetry?: () => void;
  readonly onPersonalBook: (option: PersonalAvailabilityOption) => void;
  readonly personalPaymentReservations?: readonly PersonalBookingPaymentReservation[];
  readonly personalPaymentReservationsError?: boolean;
  readonly onPersonalPaymentReservationCancel?: (
    reservation: PersonalBookingPaymentReservation,
  ) => void;
  readonly pendingPersonalSlotId?: number | null;
  readonly pendingPersonalPaymentSlotId?: number | null;
  readonly bookedPersonalSlotId?: number | null;
  readonly cancelingPersonalPaymentReservationId?: number | null;
  readonly personalErrorMessage?: string | null;
  readonly onlinePaymentsEnabled?: boolean;
  readonly unifiedClientJourneyEnabled?: boolean;
}

function formatClockTime(value: string) {
  return value.slice(0, 5);
}

function formatDateTimeClock(value: string, timeZone = APP_TIME_ZONE) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    const timePart = value.includes("T") ? value.split("T")[1] : value.split(" ")[1];
    return timePart ? timePart.slice(0, 5) : value.slice(0, 5);
  }
  return new Intl.DateTimeFormat("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone,
  }).format(date);
}

function getDateTimeDurationLabel(start: string, end: string): string {
  const diffMinutes = Math.round((new Date(end).getTime() - new Date(start).getTime()) / 60_000);
  if (!Number.isFinite(diffMinutes) || diffMinutes <= 0) return "";
  if (diffMinutes < 60) return `${diffMinutes}м`;
  const hours = diffMinutes / 60;
  return hours === 1 ? "1ч" : `${hours.toFixed(1)}ч`;
}

function formatDateTimeDate(value: string, timeZone = APP_TIME_ZONE): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value.slice(0, 10);
  }
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "long",
    timeZone,
  }).format(date);
}

function financialLabel(option: GuestBookingOption) {
  if (option.financial_status === "subscription") return "По абонементу";
  if (option.financial_status === "trial_free") return "Пробное бесплатно";
  if (option.financial_status === "drop_in_debt") {
    return option.drop_in_price
      ? `Разовое посещение ${option.drop_in_price} ₽`
      : "Разовое посещение";
  }
  return "Оплата недоступна";
}

function statusLabel(option: GuestBookingOption, bookedScheduleId?: number | null) {
  if (bookedScheduleId === option.schedule_id) return "Запись создана";
  if (option.booking_status === "already_booked") return "Уже записан";
  if (option.reason_code === "drop_in_price_required") {
    return "Нужен абонемент или разовая цена";
  }
  if (option.booking_status === "blocked") return "Запись недоступна";
  return "";
}

function bookingActionLabel({
  option,
  isPending,
  isBooked,
}: {
  option: GuestBookingOption;
  isPending: boolean;
  isBooked: boolean;
}) {
  if (isPending) return "Записываем...";
  if (isBooked || option.booking_status === "already_booked") return "Уже записан";
  if (option.booking_status === "blocked") return "Недоступно";
  return "Записаться";
}

function personalStatusLabel(
  option: PersonalAvailabilityOption,
  bookedPersonalSlotId?: number | null,
) {
  if (bookedPersonalSlotId === option.slot_id) return "Запись создана";
  if (option.reason_code === "payment_required") return "Нужна оплата";
  if (option.reason_code === "subscription_not_available") return "Нужен абонемент";
  if (option.reason_code === "student_ineligible") return "Запись недоступна";
  if (option.booking_status === "blocked") return "Запись недоступна";
  return "";
}

function personalPaymentLabel(option: PersonalAvailabilityOption) {
  if (option.subscription_id) return "По абонементу";
  const tariffId = option.offer_tariff_id || option.payment_tariff_id;
  const tariffName = option.offer_tariff_name || option.payment_tariff_name;
  const price = option.offer_price || option.payment_amount;
  if (option.booking_status === "can_pay" && tariffId) {
    return price
      ? `${tariffName || "Оплата"} · ${formatRub(price)}`
      : tariffName || "Оплата онлайн";
  }
  return "Абонемент недоступен";
}

function personalOptionConflictKey(option: PersonalAvailabilityOption): string {
  return [
    option.trainer_id,
    option.location_id,
    option.training_type_id,
    option.payment_tariff_id ?? "",
    option.starts_at,
    option.ends_at,
  ].join(":");
}

function personalReservationConflictKey(reservation: PersonalBookingPaymentReservation): string {
  return [
    reservation.trainer_id,
    reservation.location_id,
    reservation.training_type_id,
    reservation.tariff_id,
    reservation.starts_at,
    reservation.ends_at,
  ].join(":");
}

function personalActionLabel({
  option,
  isPending,
  isPaymentPending,
  isBooked,
  canPay,
  hasPendingReservation,
  hasManualReviewReservation,
  paymentReservationsError,
}: {
  option: PersonalAvailabilityOption;
  isPending: boolean;
  isPaymentPending: boolean;
  isBooked: boolean;
  canPay: boolean;
  hasPendingReservation: boolean;
  hasManualReviewReservation: boolean;
  paymentReservationsError: boolean;
}) {
  if (isPending) return "Записываем...";
  if (isPaymentPending) return "Создаем ссылку...";
  if (isBooked) return "Записан";
  if (hasManualReviewReservation) return "На проверке";
  if (hasPendingReservation) return "Ссылка создана";
  if (paymentReservationsError && option.booking_status === "can_pay") return "Обновите экран";
  if (canPay) return "Оплатить";
  if (option.booking_status === "blocked") return "Недоступно";
  return "Записаться";
}

export function PersonalPaymentReservationLinkPanel({
  reservation,
  className,
  cancelingPaymentReservationId,
  onPaymentReservationCancel,
}: {
  readonly reservation: PersonalBookingPaymentReservation;
  readonly className?: string;
  readonly cancelingPaymentReservationId?: number | null;
  readonly onPaymentReservationCancel?: (reservation: PersonalBookingPaymentReservation) => void;
}) {
  const timeZone = useBrandingStore((s) => s.timeZone);
  const role = useAuthStore((state) => state.role);
  const slotLabel = `${formatDateTimeDate(reservation.starts_at, timeZone)}, ${formatDateTimeClock(
    reservation.starts_at,
    timeZone,
  )}-${formatDateTimeClock(reservation.ends_at, timeZone)}`;
  const orderPath = getActorPaymentOrderPath(role, reservation);

  if (!reservation.bank_payment_order_id || !orderPath) {
    return (
      <div
        className={cn(
          "rounded-2xl border border-amber-200 bg-amber-50 p-3 text-[13px] text-amber-900",
          className,
        )}
      >
        <p className="font-semibold">Статус оплаты недоступен</p>
        <p className="mt-1 text-[12px] leading-relaxed">
          Обновите экран или обратитесь в клуб. Данные ссылки для оплаты не показываются без точного статуса заказа.
        </p>
      </div>
    );
  }

  return (
    <div className={cn("space-y-2", className)}>
      {isManualReviewPersonalPaymentReservation(reservation) ? (
        <div className="rounded-xl bg-amber-50 p-3 text-[13px] leading-5 text-amber-950">
          <p className="font-semibold">Оплата под защищённой проверкой</p>
          <p className="mt-1">Не повторяйте оплату: клуб завершит сверку по защищённым данным банка.</p>
        </div>
      ) : null}
      <PersonalPaymentReservationExactOrderPanel
        reservation={reservation}
        orderPath={orderPath}
        slotLabel={slotLabel}
        cancelingPaymentReservationId={cancelingPaymentReservationId}
        onPaymentReservationCancel={onPaymentReservationCancel}
      />
    </div>
  );
}

function PersonalPaymentReservationExactOrderPanel({
  reservation,
  orderPath,
  slotLabel,
  className,
  cancelingPaymentReservationId,
  onPaymentReservationCancel,
}: {
  readonly reservation: PersonalBookingPaymentReservation;
  readonly orderPath: string;
  readonly slotLabel: string;
  readonly className?: string;
  readonly cancelingPaymentReservationId?: number | null;
  readonly onPaymentReservationCancel?: (reservation: PersonalBookingPaymentReservation) => void;
}) {
  const role = useAuthStore((state) => state.role);
  const audience = role === "parent" ? "parent" : role === "student" ? "student" : "staff";
  const { scope: privateScope, isReady: privateScopeReady } = usePrivateQueryScope(audience);
  const paymentOrderQuery = useQuery<BankPaymentOrderLink>({
    queryKey: [
      "payment-reservation",
      reservation.id,
      "bank-payment-order",
      reservation.bank_payment_order_id,
      ...privateScope,
    ],
    queryFn: () => apiClient.get<BankPaymentOrderLink>(orderPath).then((response) => response.data),
    enabled: privateScopeReady,
    staleTime: 0,
  });

  if (paymentOrderQuery.isLoading) {
    return <p role="status" className="ui-muted-status">Проверяем статус оплаты…</p>;
  }

  if (paymentOrderQuery.isError) {
    return (
      <div role="status" className="ui-warning-dark">
        <p>Не удалось проверить статус оплаты.</p>
        <Button type="button" variant="outline" className="mt-2 min-h-[44px]" onClick={() => void paymentOrderQuery.refetch()}>
          Повторить проверку
        </Button>
      </div>
    );
  }

  if (!paymentOrderQuery.data) {
    return (
      <p role="status" className="ui-muted-status">
        Статус оплаты пока недоступен. Обновите экран или обратитесь в клуб.
      </p>
    );
  }

  return (
    <OnlinePaymentLinkPanel
      order={paymentOrderQuery.data}
      className={className}
      title="Персоналка ожидает оплаты"
      subtitle={`${reservation.training_type_name} · ${slotLabel} · ${reservation.trainer_name}. Запись появится после оплаты.`}
      cancelLabel="Отменить ссылку"
      isCanceling={cancelingPaymentReservationId === reservation.id}
      isRefreshing={paymentOrderQuery.isFetching}
      onRefresh={() => void paymentOrderQuery.refetch()}
      onRequestRefresh={() => {
        void apiClient
          .post(`${orderPath}refresh/`, {})
          .finally(() => paymentOrderQuery.refetch());
      }}
      onCancel={
        onPaymentReservationCancel
          ? () => onPaymentReservationCancel(reservation)
          : undefined
      }
    />
  );
}

function getActorPaymentOrderPath(
  role: ReturnType<typeof useAuthStore.getState>["role"],
  reservation: PersonalBookingPaymentReservation,
) {
  if (!reservation.bank_payment_order_id) return null;
  if (role === "student") {
    return `/students/me/bank-payment-orders/${reservation.bank_payment_order_id}/`;
  }
  if (role === "parent") {
    return `/parents/children/${reservation.student_id}/bank-payment-orders/${reservation.bank_payment_order_id}/`;
  }
  if (role === "trainer" || role === "owner" || role === "admin") {
    return `/billing/bank-payment-orders/${reservation.bank_payment_order_id}/`;
  }
  return null;
}

function DateRangeControls({
  label,
  onPrevious,
  onNext,
  onCurrent,
}: {
  readonly label?: string;
  readonly onPrevious?: () => void;
  readonly onNext?: () => void;
  readonly onCurrent?: () => void;
}) {
  if (!label || (!onPrevious && !onNext && !onCurrent)) return null;

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 rounded-[24px] border border-black/8 bg-black/[0.03] p-1.5">
        <button
          type="button"
          className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-white text-foreground shadow-sm transition active:scale-[0.98] disabled:opacity-45"
          onClick={onPrevious}
          disabled={!onPrevious}
          aria-label="Предыдущая неделя"
        >
          <ChevronLeft size={18} />
        </button>

        <p className="min-w-0 flex-1 truncate text-center text-[13px] font-semibold leading-tight">
          {label}
        </p>

        <button
          type="button"
          className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-white text-foreground shadow-sm transition active:scale-[0.98] disabled:opacity-45"
          onClick={onNext}
          disabled={!onNext}
          aria-label="Следующая неделя"
        >
          <ChevronRight size={18} />
        </button>
      </div>

      {onCurrent ? (
        <button
          type="button"
          className="inline-flex min-h-[44px] w-full items-center justify-center rounded-2xl border border-black/8 bg-white/90 px-3 text-[12px] font-semibold text-foreground transition active:scale-[0.98]"
          onClick={onCurrent}
        >
          Текущая неделя
        </button>
      ) : null}
    </div>
  );
}

export function GuestSelfBookingSection({
  ariaLabel,
  eyebrow,
  title,
  description,
  dateOptions,
  selectedDate,
  onDateChange,
  dateRangeLabel,
  onPreviousDateRange,
  onNextDateRange,
  onCurrentDateRange,
  options,
  isLoading = false,
  isError = false,
  onRetry,
  onBook,
  pendingScheduleId = null,
  bookedScheduleId = null,
  errorMessage = null,
  className,
}: GuestSelfBookingSectionProps) {
  return (
    <section aria-label={ariaLabel} className={cn("space-y-3", className)}>
      <div className="space-y-1 px-1">
        <p className="ui-overline">
          {eyebrow}
        </p>
        <h2 className="portal-balanced-title text-[17px] font-semibold leading-tight">
          {title}
        </h2>
        <p className="portal-pretty-text text-[13px] leading-5 text-muted-foreground">
          {description}
        </p>
      </div>

      <DateRangeControls
        label={dateRangeLabel}
        onPrevious={onPreviousDateRange}
        onNext={onNextDateRange}
        onCurrent={onCurrentDateRange}
      />

      <div className="flex gap-2 overflow-x-auto pb-1">
        {dateOptions.map((date) => {
          const isSelected = date.value === selectedDate;
          return (
            <button
              key={date.value}
              type="button"
              onClick={() => onDateChange(date.value)}
              className={cn(
                "min-h-[44px] min-w-[76px] shrink-0 rounded-2xl px-3 text-left text-[12px] font-semibold ring-1 transition active:scale-[0.98]",
                isSelected
                  ? "bg-foreground text-background ring-foreground"
                  : "bg-white/90 text-foreground ring-black/8",
              )}
              aria-pressed={isSelected}
            >
              <span className="block">{date.shortLabel}</span>
              <span className="block text-[11px] font-medium opacity-70">{date.label}</span>
            </button>
          );
        })}
      </div>

      {isError ? (
        <div className="rounded-[20px] bg-red-50 p-4 ring-1 ring-red-100">
          <p className="text-[14px] font-semibold text-red-950">
            Не удалось загрузить варианты записи
          </p>
          <p className="mt-1 text-[13px] leading-5 text-red-900/70">
            Попробуйте повторить загрузку для выбранного дня.
          </p>
          {onRetry ? (
            <Button
              type="button"
              variant="outline"
              className="mt-3 min-h-[44px] w-full bg-white"
              onClick={onRetry}
              aria-label="Повторить загрузку вариантов записи"
            >
              <RotateCcw className="h-4 w-4" />
              Повторить
            </Button>
          ) : null}
        </div>
      ) : isLoading ? (
        <div className="space-y-2">
          {[1, 2].map((item) => (
            <Skeleton key={item} className="h-28 rounded-[20px]" />
          ))}
        </div>
      ) : options.length === 0 ? (
        <div className="rounded-[20px] bg-white/90 p-4 text-center ring-1 ring-black/6">
          <p className="text-[14px] font-semibold">На этот день нет доступных групп</p>
          <p className="ui-muted-detail">
            Выберите другой день или уточните расписание у тренера.
          </p>
        </div>
      ) : (
        <div className="space-y-2.5">
          {options.map((option) => {
            const isPending = pendingScheduleId === option.schedule_id;
            const isBooked = bookedScheduleId === option.schedule_id;
            const canBook = option.booking_status === "can_book" && !isBooked;
            const visibleStatus = statusLabel(option, bookedScheduleId);
            return (
              <div
                key={`${option.schedule_id}-${option.date}`}
                className="rounded-[20px] border border-black/6 bg-white/94 p-3.5 shadow-sm"
              >
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0 space-y-2">
                    <div>
                      <p className="text-[15px] font-semibold leading-tight">
                        {option.group_name}
                      </p>
                      <p className="mt-1 text-[12px] font-medium text-muted-foreground">
                        {option.training_type_name}
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Badge
                        variant="outline"
                        className={cn("border-black/8 bg-white/80", WRAPPING_BADGE_CLASS_NAME)}
                      >
                        {financialLabel(option)}
                      </Badge>
                      {visibleStatus ? (
                        <Badge
                          variant="outline"
                          className={cn(
                            "border-black/8 bg-black/[0.03]",
                            isBooked || option.booking_status === "already_booked"
                              ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                              : "",
                          )}
                        >
                          {visibleStatus}
                        </Badge>
                      ) : null}
                    </div>
                  </div>

                  <Button
                    type="button"
                    size="sm"
                    className="min-h-[44px] w-full shrink-0 sm:w-auto"
                    disabled={!canBook || isPending || pendingScheduleId !== null}
                    onClick={() => onBook(option)}
                  >
                    <CalendarPlus className="h-4 w-4" />
                    {bookingActionLabel({ option, isPending, isBooked })}
                  </Button>
                </div>

                <div className="mt-3 grid grid-cols-1 gap-1.5 text-[12px] text-muted-foreground sm:grid-cols-3">
                  <div className="ui-row-2">
                    <Clock3 size={14} className="shrink-0" />
                    <span>
                      {formatClockTime(option.start_time)}-{formatClockTime(option.end_time)}
                    </span>
                  </div>
                  <div className="ui-row-2">
                    <UserRound size={14} className="shrink-0" />
                    <span>{option.trainer_name}</span>
                  </div>
                  <div className="ui-row-2">
                    <MapPin size={14} className="shrink-0" />
                    <span>{option.location_name}</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {errorMessage ? (
        <p className="rounded-2xl bg-red-50 px-3 py-2 text-center text-[13px] text-red-700 ring-1 ring-red-100">
          {errorMessage}
        </p>
      ) : null}
    </section>
  );
}

function PersonalSelfBookingSection({
  ariaLabel,
  dateOptions,
  selectedDate,
  onDateChange,
  options,
  isLoading = false,
  isError = false,
  onRetry,
  onBook,
  dateRangeLabel,
  onPreviousDateRange,
  onNextDateRange,
  onCurrentDateRange,
  paymentReservations = [],
  paymentReservationsError = false,
  onPaymentReservationCancel,
  pendingSlotId = null,
  pendingPaymentSlotId = null,
  bookedSlotId = null,
  cancelingPaymentReservationId = null,
  errorMessage = null,
  onlinePaymentsEnabled = false,
  unifiedClientJourneyEnabled = false,
}: {
  readonly ariaLabel: string;
  readonly dateOptions: readonly GuestBookingDateOption[];
  readonly selectedDate: string;
  readonly onDateChange: (date: string) => void;
  readonly options: readonly PersonalAvailabilityOption[];
  readonly isLoading?: boolean;
  readonly isError?: boolean;
  readonly onRetry?: () => void;
  readonly onBook: (option: PersonalAvailabilityOption) => void;
  readonly dateRangeLabel?: string;
  readonly onPreviousDateRange?: () => void;
  readonly onNextDateRange?: () => void;
  readonly onCurrentDateRange?: () => void;
  readonly paymentReservations?: readonly PersonalBookingPaymentReservation[];
  readonly paymentReservationsError?: boolean;
  readonly onPaymentReservationCancel?: (reservation: PersonalBookingPaymentReservation) => void;
  readonly pendingSlotId?: number | null;
  readonly pendingPaymentSlotId?: number | null;
  readonly bookedSlotId?: number | null;
  readonly cancelingPaymentReservationId?: number | null;
  readonly errorMessage?: string | null;
  readonly onlinePaymentsEnabled?: boolean;
  readonly unifiedClientJourneyEnabled?: boolean;
}) {
  const timeZone = useBrandingStore((s) => s.timeZone);
  const visibleSlotIds = new Set(options.map((option) => option.slot_id));
  const visibleOptionConflictKeys = new Set(
    options.map((option) => personalOptionConflictKey(option)),
  );
  const visiblePaymentReservations = paymentReservations.filter(
    isVisiblePersonalPaymentReservation,
  );
  const reservationBySlotId = new Map<number, PersonalBookingPaymentReservation>();
  const reservationByConflictKey = new Map<string, PersonalBookingPaymentReservation>();
  for (const reservation of visiblePaymentReservations) {
    if (reservation.availability_slot_id != null) {
      reservationBySlotId.set(reservation.availability_slot_id, reservation);
    }
    reservationByConflictKey.set(personalReservationConflictKey(reservation), reservation);
  }
  const fallbackPaymentReservations = visiblePaymentReservations.filter(
    (reservation) =>
      (
        reservation.availability_slot_id == null ||
        !visibleSlotIds.has(reservation.availability_slot_id)
      ) &&
      !visibleOptionConflictKeys.has(personalReservationConflictKey(reservation)),
  );

  return (
    <section aria-label={ariaLabel} className="space-y-3">
      <div className="space-y-1 px-1">
        <p className="ui-overline">
          Персоналка
        </p>
        <h2 className="portal-balanced-title text-[17px] font-semibold leading-tight">
          Выберите свободный слот
        </h2>
        <p className="portal-pretty-text text-[13px] leading-5 text-muted-foreground">
          Если абонемент подходит, запись создастся сразу. Если абонемента нет, слот
          удержится после создания ссылки на оплату.
        </p>
      </div>

      <DateRangeControls
        label={dateRangeLabel}
        onPrevious={onPreviousDateRange}
        onNext={onNextDateRange}
        onCurrent={onCurrentDateRange}
      />

      {paymentReservationsError ? (
        <div className="rounded-[20px] bg-amber-50 p-4 ring-1 ring-amber-100">
          <p className="text-[14px] font-semibold text-amber-950">
            Не удалось проверить ожидающие оплаты
          </p>
          <p className="mt-1 text-[13px] leading-5 text-amber-900/75">
            Обновите экран перед новой оплатой, чтобы не создать дубль ссылки.
          </p>
        </div>
      ) : null}

      <div className="flex gap-2 overflow-x-auto pb-1">
        {dateOptions.map((date) => {
          const isSelected = date.value === selectedDate;
          return (
            <button
              key={date.value}
              type="button"
              onClick={() => onDateChange(date.value)}
              className={cn(
                "min-h-[44px] min-w-[76px] shrink-0 rounded-2xl px-3 text-left text-[12px] font-semibold ring-1 transition active:scale-[0.98]",
                isSelected
                  ? "bg-foreground text-background ring-foreground"
                  : "bg-white/90 text-foreground ring-black/8",
              )}
              aria-pressed={isSelected}
            >
              <span className="block">{date.shortLabel}</span>
              <span className="block text-[11px] font-medium opacity-70">{date.label}</span>
            </button>
          );
        })}
      </div>

      {isError ? (
        <div className="rounded-[20px] bg-red-50 p-4 ring-1 ring-red-100">
          <p className="text-[14px] font-semibold text-red-950">
            Не удалось загрузить персональные слоты
          </p>
          <p className="mt-1 text-[13px] leading-5 text-red-900/70">
            Попробуйте повторить загрузку для выбранного дня.
          </p>
          {onRetry ? (
            <Button
              type="button"
              variant="outline"
              className="mt-3 min-h-[44px] w-full bg-white"
              onClick={onRetry}
              aria-label="Повторить загрузку персональных слотов"
            >
              <RotateCcw className="h-4 w-4" />
              Повторить
            </Button>
          ) : null}
        </div>
      ) : isLoading ? (
        <div className="space-y-2">
          {[1, 2].map((item) => (
            <Skeleton key={item} className="h-28 rounded-[20px]" />
          ))}
        </div>
      ) : options.length === 0 && fallbackPaymentReservations.length === 0 ? (
        <div className="rounded-[20px] bg-white/90 p-4 text-center ring-1 ring-black/6">
          <p className="text-[14px] font-semibold">На этот день нет персональных слотов</p>
          <p className="ui-muted-detail">
            Выберите другой день или дождитесь публикации доступности тренеров.
          </p>
        </div>
      ) : (
        <div className="space-y-2.5">
          {fallbackPaymentReservations.map((reservation) => (
            <div
              key={reservation.id}
              role="group"
              aria-label={`Персональный слот ${reservation.training_type_name} ${formatDateTimeClock(reservation.starts_at, timeZone)} ${reservation.trainer_name}`}
              className="grid grid-cols-[56px_minmax(0,1fr)] gap-3 rounded-[20px] border border-black/6 bg-white/94 p-3 shadow-sm"
            >
              <div className="flex min-h-[72px] w-[56px] flex-col items-center justify-center rounded-2xl bg-foreground px-2 py-2 text-background">
                <span className="text-[16px] font-semibold leading-tight">
                  {formatDateTimeClock(reservation.starts_at, timeZone)}
                </span>
                <span className="text-[12px] leading-tight opacity-75">
                  {getDateTimeDurationLabel(reservation.starts_at, reservation.ends_at)}
                </span>
              </div>

              <div className="min-w-0 space-y-3">
                <div className="min-w-0 space-y-2">
                  <div>
                    <p className="text-[15px] font-semibold leading-tight">
                      {reservation.training_type_name}
                    </p>
                    <p className="mt-1 text-[12px] font-medium text-muted-foreground">
                      {reservation.trainer_name}
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Badge
                      variant="outline"
                      className={cn("border-black/8 bg-white/80", WRAPPING_BADGE_CLASS_NAME)}
                    >
                      {isManualReviewPersonalPaymentReservation(reservation)
                        ? "На проверке"
                        : "Ссылка уже создана"}
                    </Badge>
                    <Badge
                      variant="outline"
                      className="border-amber-200 bg-amber-50 text-amber-800"
                    >
                      {isManualReviewPersonalPaymentReservation(reservation)
                        ? "Проверяем оплату"
                        : "Ожидает оплаты"}
                    </Badge>
                  </div>
                </div>

                <div className="grid grid-cols-1 gap-1.5 text-[12px] text-muted-foreground sm:grid-cols-2">
                  <div className="ui-row-2">
                    <UserRound size={14} className="shrink-0" />
                    <span>{reservation.trainer_name}</span>
                  </div>
                  <div className="ui-row-2">
                    <MapPin size={14} className="shrink-0" />
                    <span>{reservation.location_name}</span>
                  </div>
                </div>

                <PersonalPaymentReservationLinkPanel
                  reservation={reservation}
                  className="bg-white/95 shadow-none"
                  cancelingPaymentReservationId={cancelingPaymentReservationId}
                  onPaymentReservationCancel={onPaymentReservationCancel}
                />
              </div>
            </div>
          ))}
          {options.map((option) => {
            const isPending = pendingSlotId === option.slot_id;
            const isPaymentPending = pendingPaymentSlotId === option.slot_id;
            const isBooked = bookedSlotId === option.slot_id;
            const pendingReservation =
              reservationBySlotId.get(option.slot_id) ??
              reservationByConflictKey.get(personalOptionConflictKey(option));
            const hasManualReviewReservation =
              pendingReservation !== undefined &&
              isManualReviewPersonalPaymentReservation(pendingReservation);
            const canBook =
              option.booking_status === "can_book" &&
              Boolean(option.subscription_id) &&
              !isBooked &&
              !pendingReservation;
            const displayedTariffId = unifiedClientJourneyEnabled
              ? option.offer_tariff_id || null
              : option.payment_tariff_id;
            const hasOfferConfigurationError =
              unifiedClientJourneyEnabled && Boolean(option.offer_error_code);
            const requiresOfferDigest = unifiedClientJourneyEnabled;
            const canPayForOption =
              option.booking_status === "can_pay" &&
              Boolean(displayedTariffId) &&
              (!requiresOfferDigest || Boolean(option.offer_digest)) &&
              !hasOfferConfigurationError &&
              !pendingReservation &&
              !paymentReservationsError;
            const canPay = canPayForOption && onlinePaymentsEnabled;
            const canAct = canBook || canPay;
            const visibleStatus = personalStatusLabel(option, bookedSlotId);
            return (
              <div
                key={`${option.slot_id}-${option.starts_at}`}
                role="group"
                aria-label={`Персональный слот ${option.training_type_name} ${formatDateTimeClock(option.starts_at, timeZone)} ${option.trainer_name}`}
                className="grid grid-cols-[56px_minmax(0,1fr)] gap-3 rounded-[20px] border border-black/6 bg-white/94 p-3 shadow-sm"
              >
                <div className="flex min-h-[72px] w-[56px] flex-col items-center justify-center rounded-2xl bg-foreground px-2 py-2 text-background">
                  <span className="text-[16px] font-semibold leading-tight">
                    {formatDateTimeClock(option.starts_at, timeZone)}
                  </span>
                  <span className="text-[12px] leading-tight opacity-75">
                    {getDateTimeDurationLabel(option.starts_at, option.ends_at)}
                  </span>
                </div>

                <div className="min-w-0 space-y-3">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0 space-y-2">
                      <div>
                        <p className="text-[15px] font-semibold leading-tight">
                          {option.training_type_name}
                        </p>
                        <p className="mt-1 text-[12px] font-medium text-muted-foreground">
                          {option.trainer_name}
                        </p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Badge
                          variant="outline"
                          className={cn("border-black/8 bg-white/80", WRAPPING_BADGE_CLASS_NAME)}
                        >
                          {pendingReservation
                            ? hasManualReviewReservation
                              ? "На проверке"
                              : "Ссылка уже создана"
                            : personalPaymentLabel(option)}
                        </Badge>
                        {visibleStatus ? (
                          <Badge
                            variant="outline"
                            className={cn(
                              "border-black/8 bg-black/[0.03]",
                              isBooked
                                ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                                : "",
                            )}
                          >
                            {visibleStatus}
                          </Badge>
                        ) : null}
                      </div>
                    </div>

                    <Button
                      type="button"
                      size="sm"
                      className="min-h-[44px] w-full shrink-0 sm:w-auto"
                      disabled={
                        !canAct ||
                        isPending ||
                        isPaymentPending ||
                        pendingSlotId !== null ||
                        pendingPaymentSlotId !== null
                      }
                      onClick={() => onBook(option)}
                    >
                      <CalendarPlus className="h-4 w-4" />
                      {personalActionLabel({
                        option,
                        isPending,
                        isPaymentPending,
                        isBooked,
                        canPay: canPayForOption,
                        hasPendingReservation: Boolean(pendingReservation),
                        hasManualReviewReservation,
                        paymentReservationsError,
                      })}
                    </Button>
                  </div>

                  <div className="grid grid-cols-1 gap-1.5 text-[12px] text-muted-foreground sm:grid-cols-2">
                    <div className="ui-row-2">
                      <UserRound size={14} className="shrink-0" />
                      <span>{option.trainer_name}</span>
                    </div>
                    <div className="ui-row-2">
                      <MapPin size={14} className="shrink-0" />
                      <span>{option.location_name}</span>
                    </div>
                  </div>
                  {hasOfferConfigurationError ? (
                    <p role="status" className="ui-warning">
                      Цена персональной тренировки пока не настроена. Обратитесь в клуб.
                    </p>
                  ) : null}
                  {pendingReservation ? (
                    <PersonalPaymentReservationLinkPanel
                      reservation={pendingReservation}
                      className="bg-white/95 shadow-none"
                      cancelingPaymentReservationId={cancelingPaymentReservationId}
                      onPaymentReservationCancel={onPaymentReservationCancel}
                    />
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {errorMessage ? (
        <p className="rounded-2xl bg-red-50 px-3 py-2 text-center text-[13px] text-red-700 ring-1 ring-red-100">
          {errorMessage}
        </p>
      ) : null}
    </section>
  );
}

export function GuestSelfBookingSheet({
  open,
  onOpenChange,
  subjectName,
  personalOptions,
  personalIsLoading = false,
  personalIsError = false,
  onPersonalRetry,
  onPersonalBook,
  personalPaymentReservations = [],
  personalPaymentReservationsError = false,
  onPersonalPaymentReservationCancel,
  pendingPersonalSlotId = null,
  pendingPersonalPaymentSlotId = null,
  bookedPersonalSlotId = null,
  cancelingPersonalPaymentReservationId = null,
  personalErrorMessage = null,
  onlinePaymentsEnabled = false,
  unifiedClientJourneyEnabled = false,
  ...sectionProps
}: GuestSelfBookingSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="bottom"
        showCloseButton={false}
        className="max-h-[92vh] overflow-y-auto rounded-t-2xl"
      >
        <SheetHeader>
          <SheetTitle>Записаться</SheetTitle>
          <SheetDescription>
            {subjectName ? `Запись для: ${subjectName}` : "Выберите занятие"}
          </SheetDescription>
        </SheetHeader>

        <div className="space-y-4 p-4 pt-0">
          <Tabs defaultValue="group" className="gap-4">
            <TabsList className="grid min-h-11 w-full grid-cols-2 rounded-2xl bg-black/[0.04] p-1">
              <TabsTrigger value="group" className="min-h-9 rounded-xl">
                Группа
              </TabsTrigger>
              <TabsTrigger value="personal" className="min-h-9 rounded-xl">
                Персоналка
              </TabsTrigger>
            </TabsList>

            <TabsContent value="group">
              <GuestSelfBookingSection {...sectionProps} />
            </TabsContent>

            <TabsContent value="personal">
              <PersonalSelfBookingSection
                ariaLabel="Запись на персональную тренировку"
                dateOptions={sectionProps.dateOptions}
                selectedDate={sectionProps.selectedDate}
                onDateChange={sectionProps.onDateChange}
                dateRangeLabel={sectionProps.dateRangeLabel}
                onPreviousDateRange={sectionProps.onPreviousDateRange}
                onNextDateRange={sectionProps.onNextDateRange}
                onCurrentDateRange={sectionProps.onCurrentDateRange}
                options={personalOptions}
                isLoading={personalIsLoading}
                isError={personalIsError}
                onRetry={onPersonalRetry}
                onBook={onPersonalBook}
                paymentReservations={personalPaymentReservations}
                paymentReservationsError={personalPaymentReservationsError}
                onPaymentReservationCancel={onPersonalPaymentReservationCancel}
                pendingSlotId={pendingPersonalSlotId}
                pendingPaymentSlotId={pendingPersonalPaymentSlotId}
                bookedSlotId={bookedPersonalSlotId}
                cancelingPaymentReservationId={cancelingPersonalPaymentReservationId}
                errorMessage={personalErrorMessage}
                onlinePaymentsEnabled={onlinePaymentsEnabled}
                unifiedClientJourneyEnabled={unifiedClientJourneyEnabled}
              />
            </TabsContent>
          </Tabs>

          <Button
            type="button"
            variant="outline"
            className="min-h-[44px] w-full"
            onClick={() => onOpenChange(false)}
          >
            Закрыть
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

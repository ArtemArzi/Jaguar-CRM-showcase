import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate, useSearchParams, useLocation } from "react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Phone,
  AlertTriangle,
  Award,
  Pencil,
  MessageSquareText,
  KeyRound,
  RotateCw,
  Copy,
  CalendarPlus,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import apiClient from "@/api/custom-fetch";
import { formatDateRu } from "@/lib/locale";
import { formatRelativeDate } from "@/lib/format";
import { getApiError } from "@/lib/utils";
import type { FeedbackAnswer, FeedbackResponse } from "@/features/feedback/types";
import { STATUS_CONFIG } from "../constants";
import type {
  AccountAccessIssue,
  AccountAccessSummary,
  PersonalBooking,
  StudentDetail as StudentDetailType,
  StudentSubscription,
  GradeProgress,
  AttendanceItem,
} from "../types";
import { PaymentSheet } from "../components/payment-sheet";
import { PersonalBookingSheet } from "../components/personal-booking-sheet";
import { PersonalBookingRescheduleSheet } from "../components/personal-booking-reschedule-sheet";
import { FreezeSheet } from "../components/freeze-sheet";
import { GradePromoteSheet } from "../components/grade-promote-sheet";
import { GradeAssignSheet } from "../components/grade-assign-sheet";
import { StudentNotes } from "../components/student-notes";
import { normalizeStudentSubscriptionsPayload } from "../lib/subscriptions";
import {
  OnlinePaymentLinkPanel,
  type BankPaymentOrderLink,
} from "@/components/portal/online-payment-link-panel";
import { isSubscriptionBankPaymentOrder } from "@/components/portal/payment-link-state";
import {
  formatRenewalOfferLabel,
  findRenewalOrderForSubscription,
  getExpectedRenewalOfferFields,
  isLiveRenewalOrder,
} from "@/components/portal/subscription-renewal-state";
import {
  usePersonalAvailabilityCapability,
  useUnifiedClientJourneyCapability,
} from "@/api/unified-client-journey";
import {
  PersonalCommercialReceiptList,
} from "../components/personal-commercial-context";
import {
  contextualRetryContextFromReceipt,
  getPersonalCommercialContext,
  personalCommercialContextQueryKey,
  type ContextualCommercialContext,
  type PersonalCommercialContext,
  type PersonalCommercialReceipt,
  useCommercialCacheScope,
} from "../components/personal-commercial-context-api";
import { ContextualCommercialSheet } from "../components/contextual-commercial-sheet";

interface GroupSaleManualAdmissionRouteState {
  readonly groupSaleManualAdmission?: {
    readonly studentId: number;
    readonly paymentId: number;
    readonly financeState: "pending_manual";
  };
}

const SUB_STATUS_LABEL: Record<string, string> = {
  active: "Активен",
  frozen: "Заморожен",
  expired: "Истёк",
  pending: "Ожидает",
  cancelled: "Отменён после возврата",
};

const ACTION_BUTTON_CLASS =
  "min-h-[44px] min-w-0 flex-1 basis-[calc(50%-0.25rem)] justify-center active:scale-[0.98] transition-transform";

function isPackageOwnerTracked(kind?: string) {
  return kind === "personal" || kind === "mini_group";
}

interface LeadResourceContext {
  readonly context: string | null;
  readonly resourceType: string;
  readonly resourceId: number;
}

interface LeadResourceAction {
  readonly kind: string;
  readonly label: string;
  readonly supporting_text: string;
  readonly target_resource_type: string | null;
  readonly target_resource_id: number | null;
  readonly context: string;
}

interface LeadActionContextPayload {
  readonly primary_action: LeadResourceAction | null;
  readonly secondary_capabilities: readonly LeadResourceAction[];
}

function leadResourceContextFromSearchParams(
  searchParams: URLSearchParams,
): LeadResourceContext | null {
  const resourceType = searchParams.get("resource_type");
  const resourceId = Number(searchParams.get("resource_id"));
  if (!resourceType || !Number.isSafeInteger(resourceId) || resourceId <= 0) return null;
  return { context: searchParams.get("context"), resourceType, resourceId };
}

function leadResourceLabel({ context, resourceType }: LeadResourceContext): string {
  if (resourceType === "bank_payment_order") return "Онлайн-оплата";
  if (resourceType === "payment") return "Оплата";
  if (resourceType === "personal_booking_reservation") return "Персональная запись и оплата";
  if (resourceType === "personal_drop_in_booking") return "Персональная запись";
  if (resourceType === "schedule_enrollment") {
    return context === "upcoming_trial" || context === "trial_done"
      ? "Пробная тренировка"
      : "Персональная запись";
  }
  return "Контекст заявки";
}

function accountAccessSummary(access: AccountAccessIssue): AccountAccessSummary {
  return {
    role: access.role,
    status: access.status,
    username: access.username,
    must_change_password: access.must_change_password,
    issued_at: access.issued_at,
    reset_at: access.reset_at,
  };
}

function copyCredential(value: string) {
  void navigator.clipboard?.writeText(value);
}

function AccountAccessPanel({
  accountAccess,
  isChild,
  canManageAccess,
  hasLinkedParent,
  parentPhone,
  canOpenAccess,
  eligibilityMessage,
  temporaryPassword,
  isOpening,
  isResetting,
  error,
  onParentPhoneChange,
  onOpen,
  onReset,
}: {
  readonly accountAccess: AccountAccessSummary | AccountAccessIssue | null;
  readonly isChild: boolean;
  readonly canManageAccess: boolean;
  readonly hasLinkedParent: boolean;
  readonly parentPhone: string;
  readonly canOpenAccess: boolean;
  readonly eligibilityMessage: string;
  readonly temporaryPassword: string | null;
  readonly isOpening: boolean;
  readonly isResetting: boolean;
  readonly error: unknown;
  readonly onParentPhoneChange: (value: string) => void;
  readonly onOpen: () => void;
  readonly onReset: () => void;
}) {
  return (
    <div className="rounded-xl bg-white p-4 ring-1 ring-foreground/5 space-y-3">
      <div className="ui-row-between">
        <div className="min-w-0">
          <div className="ui-row-2">
            <KeyRound size={16} className="text-foreground/70" />
            <p className="text-[15px] font-semibold text-foreground">
              Личный кабинет
            </p>
          </div>
          <p className="mt-1 text-[13px] text-muted-foreground">
            {accountAccess ? "Доступ открыт" : eligibilityMessage}
          </p>
        </div>
        <Badge variant={accountAccess ? "default" : "outline"}>
          {accountAccess ? "Открыт" : "Не открыт"}
        </Badge>
      </div>

      {accountAccess ? (
        <div className="space-y-2">
          <div className="flex items-center gap-2 rounded-lg bg-muted px-3 py-2">
            <span className="min-w-0 flex-1 break-all text-[13px] text-foreground">
              {accountAccess.username}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-8 w-8 shrink-0"
              onClick={() => copyCredential(accountAccess.username)}
              aria-label="Скопировать логин"
              title="Скопировать логин"
            >
              <Copy size={14} />
            </Button>
          </div>
          {temporaryPassword ? (
            <div className="space-y-1">
              <div className="flex items-center gap-2 rounded-lg bg-amber-50 px-3 py-2 text-amber-900">
                <span className="min-w-0 flex-1 break-all text-[13px]">
                  {temporaryPassword}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 shrink-0 text-amber-900"
                  onClick={() => copyCredential(temporaryPassword)}
                  aria-label="Скопировать временный пароль"
                  title="Скопировать временный пароль"
                >
                  <Copy size={14} />
                </Button>
              </div>
              <p className="ui-muted-12">
                Пароль виден только сейчас
              </p>
            </div>
          ) : null}
        </div>
      ) : null}

      {error ? (
        <p className="text-[13px] text-destructive">
          {getApiError(error, "Не удалось изменить доступ")}
        </p>
      ) : null}

      {accountAccess ? (
        <Button
          type="button"
          variant="outline"
          size="lg"
          wrap
          className="w-full"
          disabled={isResetting || !canManageAccess}
          onClick={onReset}
        >
          <RotateCw size={16} />
          {isResetting ? "Сбрасываем" : "Сбросить пароль"}
        </Button>
      ) : (
        <div className="space-y-3">
          {canManageAccess && isChild && !hasLinkedParent ? (
            <label className="block space-y-1">
              <span className="ui-muted-12">
                Телефон родителя
              </span>
              <input
                type="tel"
                value={parentPhone}
                onChange={(event) => onParentPhoneChange(event.target.value)}
                className="h-11 w-full rounded-lg border border-input bg-background px-3 text-[14px] outline-none focus:ring-2 focus:ring-ring"
              />
            </label>
          ) : null}
          {canManageAccess && isChild && hasLinkedParent ? (
            <p className="rounded-lg bg-muted px-3 py-2 text-[13px] text-muted-foreground">
              Родитель уже привязан
            </p>
          ) : null}
          <Button
            type="button"
            variant="default"
            size="lg"
            wrap
            className="w-full"
            disabled={
              isOpening ||
              !canManageAccess ||
              !canOpenAccess ||
              (isChild && !hasLinkedParent && !parentPhone.trim())
            }
            onClick={onOpen}
          >
            <KeyRound size={16} />
            {isOpening ? "Открываем" : "Открыть кабинет"}
          </Button>
        </div>
      )}
    </div>
  );
}

export function GradePromotionButtons({
  grades,
  openPromote,
}: {
  readonly grades: GradeProgress[] | undefined;
  readonly openPromote: (grade: GradeProgress) => void;
}) {
  if (!grades?.length) {
    return (
      <Button
        variant="outline"
        size="lg"
        wrap
        className={ACTION_BUTTON_CLASS}
        disabled
      >
        Повысить грейд
      </Button>
    );
  }

  if (grades.length === 1) {
    return (
      <Button
        variant="outline"
        size="lg"
        wrap
        className={ACTION_BUTTON_CLASS}
        onClick={() => openPromote(grades[0])}
      >
        Повысить грейд
      </Button>
    );
  }

  return (
    <>
      {grades.map((grade) => (
        <Button
          key={grade.student_grade_id}
          variant="outline"
          size="lg"
          wrap
          className={ACTION_BUTTON_CLASS}
          onClick={() => openPromote(grade)}
        >
          Повысить {grade.grade_system_name ?? "грейд"}
        </Button>
      ))}
    </>
  );
}

export function SubscriptionSection({
  subscriptions,
  isLoading,
  isError,
  bankPaymentOrders,
  isCancelingOrderId,
  cancelError,
  onCancelOrder,
  onRefreshOrders,
  onRefetchOrders,
  isRefreshingOrder,
  canStartContextualRenewal,
  onStartContextualRenewal,
}: {
  readonly subscriptions: StudentSubscription[] | undefined;
  readonly isLoading: boolean;
  readonly isError: boolean;
  readonly bankPaymentOrders?: BankPaymentOrderLink[];
  readonly isCancelingOrderId?: number | null;
  readonly cancelError?: string | null;
  readonly onCancelOrder?: (order: BankPaymentOrderLink) => void;
  readonly onRefreshOrders?: (order: BankPaymentOrderLink) => void;
  readonly onRefetchOrders?: () => void;
  readonly isRefreshingOrder?: boolean;
  readonly canStartContextualRenewal?: boolean;
  readonly onStartContextualRenewal?: (subscription: StudentSubscription) => void;
}) {
  if (isLoading) {
    return <Skeleton className="h-[80px] rounded-xl" />;
  }
  if (isError) {
    return (
      <p className="ui-error-14">
        Не удалось загрузить данные абонемента
      </p>
    );
  }
  const activeSubscriptions =
    subscriptions?.filter((subscription) => subscription.status === "active") ??
    [];
  const fallbackSubscriptions =
    subscriptions?.filter((subscription) =>
      ["frozen", "pending"].includes(subscription.status),
    ) ?? [];
  const currentSubscriptions = [
    ...activeSubscriptions,
    ...fallbackSubscriptions,
  ];
  const historicalSubscriptions =
    subscriptions?.filter((subscription) =>
      ["expired", "cancelled"].includes(subscription.status),
    ) ??
    [];
  const visibleSubscriptions =
    currentSubscriptions.length > 0 ? currentSubscriptions : historicalSubscriptions;
  const subscriptionPaymentOrders = (bankPaymentOrders ?? []).filter(
    isSubscriptionBankPaymentOrder,
  );
  const subscriptionRows = visibleSubscriptions.map((subscription) => {
    const exactAttachedOrder = subscriptionPaymentOrders.find(
      (order) =>
        order.renewed_from_subscription_id === subscription.id ||
        order.subscription_id === subscription.id,
    );
    return {
      subscription,
      bankPaymentOrder:
        exactAttachedOrder ??
        findRenewalOrderForSubscription(
          subscriptionPaymentOrders,
          subscription,
          visibleSubscriptions,
        ),
    };
  });
  const attachedBankPaymentOrderIds = new Set(
    subscriptionRows
      .map((row) => row.bankPaymentOrder?.id)
      .filter((orderId): orderId is number => typeof orderId === "number"),
  );
  const recentUnattachedBankPaymentOrders = subscriptionPaymentOrders.filter(
    (order) =>
      !isLiveRenewalOrder(order) &&
      !attachedBankPaymentOrderIds.has(order.id),
  );

  if (visibleSubscriptions.length === 0 && recentUnattachedBankPaymentOrders.length === 0) {
    return (
      <div className="ui-card">
        <p className="ui-muted-14">Нет активного абонемента</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {subscriptionRows.map(({ subscription, bankPaymentOrder }) => {
        const renewalOfferLabel = formatRenewalOfferLabel(subscription);
        return (
          <div
            key={subscription.id}
            className="rounded-xl bg-white p-4 ring-1 ring-foreground/5 space-y-2"
          >
            <p className="ui-title-16">
              {subscription.tariff_name}
            </p>
            <p className="ui-muted-14">
              {subscription.trainings_left != null
                ? `Осталось ${subscription.trainings_left}/${subscription.trainings_total ?? "~"} тренировок`
                : subscription.expires_at
                  ? `до ${formatDateRu(subscription.expires_at)}`
                  : "Без даты окончания"}
            </p>
            {isPackageOwnerTracked(subscription.training_type_kind) &&
            subscription.package_owner_trainer_name ? (
              <p className="ui-muted-13">
                Пакет тренера: {subscription.package_owner_trainer_name}
              </p>
            ) : null}
            <Badge
              variant={
                subscription.status === "active"
                  ? "default"
                  : subscription.status === "frozen" || subscription.status === "pending"
                    ? "outline"
                    : "destructive"
              }
            >
              {SUB_STATUS_LABEL[subscription.status] ?? subscription.status}
            </Badge>
            {subscription.freeze_status === "pending" ? (
              <Badge variant="outline" className="ml-2">
                Заявка на заморозку ждёт
              </Badge>
            ) : null}
            {bankPaymentOrder ? (
              <OnlinePaymentLinkPanel
                order={bankPaymentOrder}
                title="Онлайн-оплата"
                cancelLabel="Отменить оплату"
                isCanceling={isCancelingOrderId === bankPaymentOrder.id}
                isRefreshing={isRefreshingOrder}
                onCancel={onCancelOrder}
                onRefresh={onRefetchOrders}
                onRequestRefresh={() => onRefreshOrders?.(bankPaymentOrder)}
              />
            ) : null}
            {bankPaymentOrder && cancelError ? (
              <p role="alert" className="text-[13px] text-destructive">
                {cancelError}
              </p>
            ) : null}
            {canStartContextualRenewal &&
            !bankPaymentOrder &&
            ["active", "expired"].includes(subscription.status) ? (
              <div className="space-y-2">
                {renewalOfferLabel ? (
                  <p className="ui-muted-13">Продление: {renewalOfferLabel}</p>
                ) : null}
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-[44px] w-full"
                  onClick={() => onStartContextualRenewal?.(subscription)}
                >
                  Продлить абонемент
                </Button>
              </div>
            ) : null}
          </div>
        );
      })}
      {recentUnattachedBankPaymentOrders.map((order) => (
        <OnlinePaymentLinkPanel
          key={`recent-${order.id}`}
          order={order}
          title="Последняя онлайн-оплата"
          onRefresh={onRefetchOrders}
          onRequestRefresh={() => onRefreshOrders?.(order)}
          isRefreshing={isRefreshingOrder}
        />
      ))}
    </div>
  );
}

function formatBookingTimeRange(booking: PersonalBooking) {
  return `${booking.starts_at.slice(11, 16)} - ${booking.ends_at.slice(11, 16)}`;
}

function isTerminalDropInBooking(booking: PersonalBooking) {
  return booking.attendance_state === "cancelled" || booking.attendance_state === "no_show";
}

function terminalDropInFinancialMessage(booking: PersonalBooking) {
  const attendanceMessage =
    booking.attendance_state === "no_show" ? "Клиент не пришёл." : "Запись отменена.";
  if (booking.financial_state === "payment_pending") {
    return `${attendanceMessage} Связанная оплата ожидает решения владельца.`;
  }
  if (booking.financial_state === "paid") {
    return `${attendanceMessage} Подтверждённая оплата сохранена как неиспользованный кредит.`;
  }
  return `${attendanceMessage} Долг за непосещённую персоналку не создан.`;
}

function DropInBankPaymentOrderPanel({
  booking,
  isCanceling,
  onCancel,
}: {
  readonly booking: PersonalBooking;
  readonly isCanceling: boolean;
  readonly onCancel: (booking: PersonalBooking) => void;
}) {
  const orderId = booking.bank_payment_order_id;
  const orderQuery = useQuery<BankPaymentOrderLink>({
    queryKey: ["billing", "bank-payment-orders", "student-detail-drop-in", orderId],
    queryFn: () =>
      apiClient
        .get<BankPaymentOrderLink>(`/billing/bank-payment-orders/${orderId}/`)
        .then((response) => response.data),
    enabled: orderId !== null && orderId !== undefined,
    staleTime: 0,
  });

  if (orderId === null || orderId === undefined) return null;

  if (!orderQuery.data) {
    if (orderQuery.isError) {
      return (
        <div role="status" className="ui-warning-dark">
          <p>Не удалось проверить статус оплаты.</p>
          <Button
            type="button"
            variant="outline"
            className="mt-2 min-h-[44px]"
            onClick={() => void orderQuery.refetch()}
          >
            Повторить проверку
          </Button>
        </div>
      );
    }
    return (
      <p role="status" className="ui-muted-status">
        Проверяем статус оплаты…
      </p>
    );
  }

  return (
    <OnlinePaymentLinkPanel
      order={orderQuery.data}
      title="Ссылка на оплату персоналки"
      subtitle="Оплата останется привязана только к этой записи."
      cancelLabel="Отменить ссылку на оплату"
      isCanceling={isCanceling}
      isRefreshing={orderQuery.isFetching}
      onRefresh={() => void orderQuery.refetch()}
      onRequestRefresh={() => {
        void apiClient
          .post(`/billing/bank-payment-orders/${orderQuery.data.id}/refresh/`, {})
          .finally(() => orderQuery.refetch());
      }}
      onCancel={() => onCancel(booking)}
    />
  );
}

function PersonalBookingsSection({
  bookings,
  isLoading,
  isError,
  actionError,
  onOpenBooking,
  onOpenDropInPayment,
  isCancelingDropInPayment,
  onCancelDropInPayment,
  onCancelDropIn,
  onMarkNoShow,
  onReschedule,
}: {
  readonly bookings: PersonalBooking[] | undefined;
  readonly isLoading: boolean;
  readonly isError: boolean;
  readonly actionError: string | null;
  readonly onOpenBooking: () => void;
  readonly onOpenDropInPayment: (booking: PersonalBooking) => void;
  readonly isCancelingDropInPayment: boolean;
  readonly onCancelDropInPayment: (booking: PersonalBooking) => void;
  readonly onCancelDropIn: (booking: PersonalBooking) => void;
  readonly onMarkNoShow: (booking: PersonalBooking) => void;
  readonly onReschedule: (booking: PersonalBooking) => void;
}) {
  return (
    <section>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="text-sm font-medium text-foreground/70">Персоналки</h3>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="min-h-[44px]"
          onClick={onOpenBooking}
        >
          <CalendarPlus className="h-4 w-4" />
          Записать
        </Button>
      </div>

      {isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-[72px] rounded-xl" />
          <Skeleton className="h-[72px] rounded-xl" />
        </div>
      ) : isError ? (
        <p className="ui-error-14">
          Не удалось загрузить персоналки
        </p>
      ) : !bookings?.length ? (
        <div className="ui-card">
          <p className="ui-muted-14">
            Будущих персоналок пока нет
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {bookings.map((booking) => (
            <article
              key={booking.enrollment_id}
              aria-label={`Персоналка ${booking.starts_at.slice(0, 10)} ${formatBookingTimeRange(booking)}`}
              className="ui-card"
            >
              <div className="ui-row-between">
                <div className="min-w-0">
                  <p className="text-[15px] font-semibold text-foreground">
                    {formatDateRu(booking.starts_at.slice(0, 10))}
                  </p>
                  <p className="ui-muted-14">
                    {formatBookingTimeRange(booking)}
                  </p>
                </div>
                <Badge variant="secondary" className="shrink-0">
                  {booking.training_type_name}
                </Badge>
              </div>
              <p className="mt-2 text-[13px] text-muted-foreground">
                {booking.location_name}
                {booking.trainer_name ? `, ${booking.trainer_name}` : ""}
              </p>
              {booking.booking_kind === "drop_in" ? (
                <div className="mt-3 flex flex-col gap-2">
                  {booking.can_manage && booking.next_action_label && !isTerminalDropInBooking(booking) ? (
                    <p className="ui-caption-label">
                      Следующее действие: {booking.next_action_label}
                    </p>
                  ) : null}
                  {booking.can_manage && booking.can_mark_no_show && !isTerminalDropInBooking(booking) ? (
                    <Button
                      type="button"
                      variant="outline"
                      className="min-h-[44px] text-destructive"
                      onClick={() => onMarkNoShow(booking)}
                    >
                      Не пришёл
                    </Button>
                  ) : null}
                  {isTerminalDropInBooking(booking) ? (
                    <p className="ui-muted-13">
                      {terminalDropInFinancialMessage(booking)}
                    </p>
                  ) : booking.financial_state === "debt_open" ? (
                    <p className="text-[13px] font-medium text-destructive">Долг за персоналку</p>
                  ) : booking.financial_state === "payment_pending" ? (
                    <>
                      <p className="text-[13px] font-medium text-amber-800">
                        {booking.can_manage
                          ? booking.bank_payment_order_id
                            ? "Онлайн-оплата ожидает подтверждения владельцем."
                            : "Ручная оплата ожидает подтверждения владельцем."
                          : "Оплата ожидает подтверждения владельцем."}
                      </p>
                      {booking.can_manage &&
                      !booking.can_mark_no_show &&
                      booking.bank_payment_order_id ? (
                        <DropInBankPaymentOrderPanel
                          booking={booking}
                          isCanceling={isCancelingDropInPayment}
                          onCancel={onCancelDropInPayment}
                        />
                      ) : null}
                    </>
                  ) : booking.financial_state === "pay_at_club" ? (
                    <p className="ui-muted-13">Оплата в клубе: долг появится после check-in.</p>
                  ) : null}
                  {booking.can_manage &&
                  !booking.can_mark_no_show &&
                  !isTerminalDropInBooking(booking) &&
                  (booking.financial_state === "debt_open" || booking.financial_state === "pay_at_club") ? (
                    <Button
                      type="button"
                      variant="outline"
                      className="min-h-[44px]"
                      onClick={() => onOpenDropInPayment(booking)}
                    >
                      {booking.financial_state === "debt_open" ? "Принять оплату" : "Предоплата"}
                    </Button>
                  ) : null}
                  {booking.can_manage && booking.can_cancel && !isTerminalDropInBooking(booking) ? (
                    <Button
                      type="button"
                      variant="outline"
                      className="min-h-[44px] text-destructive"
                      onClick={() => onCancelDropIn(booking)}
                    >
                      Отменить запись
                    </Button>
                  ) : null}
                </div>
              ) : null}
              {booking.can_reschedule ? (
                <Button
                  type="button"
                  variant="outline"
                  className="mt-3 min-h-[44px] w-full"
                  onClick={() => onReschedule(booking)}
                >
                  Перенести
                </Button>
              ) : null}
            </article>
          ))}
        </div>
      )}
      {actionError ? (
        <p role="alert" className="mt-3 rounded-xl bg-destructive/10 p-3 text-[13px] text-destructive">
          {actionError}
        </p>
      ) : null}
    </section>
  );
}

function InfoSection({
  student,
  grades,
  lastVisitDate,
  studentLoading,
  gradesLoading,
  gradesError,
  canManageGrades,
  onRemoveGrade,
}: {
  readonly student: StudentDetailType | undefined;
  readonly grades: GradeProgress[] | undefined;
  readonly lastVisitDate: string | null;
  readonly studentLoading: boolean;
  readonly gradesLoading: boolean;
  readonly gradesError: boolean;
  readonly canManageGrades: boolean;
  readonly onRemoveGrade: (studentGradeId: number) => void;
}) {
  if (studentLoading || gradesLoading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-[20px] rounded" />
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex justify-between text-[14px]">
        <span className="ui-muted">Последний визит</span>
        <span className="text-foreground">{lastVisitDate ?? "Нет данных"}</span>
      </div>

      {gradesError ? (
        <p className="ui-error-14">
          Не удалось загрузить данные грейда
        </p>
      ) : !grades?.length ? (
        <div className="flex justify-between text-[14px]">
          <span className="ui-muted">Грейды</span>
          <span className="ui-muted">Не назначены</span>
        </div>
      ) : (
        grades.map((g) => (
          <div key={g.student_grade_id}>
            <div className="flex justify-between items-center text-[14px]">
              <div className="flex items-center gap-1.5">
                <span className="ui-muted">{g.grade_system_name}</span>
                {canManageGrades ? (
                  <button
                    type="button"
                    onClick={() => onRemoveGrade(g.student_grade_id)}
                    className="text-[12px] text-muted-foreground/50 hover:text-destructive transition-colors"
                    title="Убрать дисциплину"
                  >
                    ✕
                  </button>
                ) : null}
              </div>
              <span className="text-foreground">
                {g.current_grade?.name ?? "С нуля"}
              </span>
            </div>
            {g.next_grade && g.trainings_to_next != null && (
              <div className="mt-1">
                <div className="flex justify-between text-[12px] text-muted-foreground mb-1">
                  <span>{g.trainings_since_last_grade} тр.</span>
                  <span>{g.next_grade.min_trainings} до {g.next_grade.name}</span>
                </div>
                <div className="h-1.5 rounded-full bg-muted overflow-hidden">
                  <div
                    className="h-full rounded-full bg-[var(--branding-accent)]"
                    style={{
                      width: `${Math.min(100, (g.trainings_since_last_grade / g.next_grade.min_trainings) * 100)}%`,
                    }}
                  />
                </div>
              </div>
            )}
            {g.next_grade && g.trainings_to_next != null && g.trainings_to_next <= 0 && (
              <div className="flex items-center gap-2 rounded-lg bg-green-50 p-2 ring-1 ring-green-200 mt-1">
                <Award size={16} className="text-green-600 shrink-0" />
                <span className="text-[13px] text-green-800">
                  Готов к повышению до {g.next_grade.name}
                </span>
              </div>
            )}
          </div>
        ))
      )}

      {/* Contraindications */}
      {student?.contraindications && (
        <div className="flex items-center gap-2 rounded-lg bg-red-50 p-2 ring-1 ring-red-200">
          <AlertTriangle size={16} className="text-red-600 shrink-0" />
          <span className="text-[13px] text-red-800">
            {student.contraindications}
          </span>
        </div>
      )}
    </div>
  );
}

function AttendanceSection({
  attendance,
  isLoading,
  isError,
}: {
  readonly attendance: AttendanceItem[] | undefined;
  readonly isLoading: boolean;
  readonly isError: boolean;
}) {
  if (isLoading) {
    return (
      <div className="space-y-1">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-[28px] rounded" />
        ))}
      </div>
    );
  }
  if (isError) {
    return (
      <p className="ui-error-14">
        Не удалось загрузить данные посещений
      </p>
    );
  }
  if (!attendance?.length) {
    return (
      <p className="ui-muted-14">
        Ученик ещё не был на тренировках
      </p>
    );
  }

  return (
    <div className="space-y-1">
      {attendance.map((item) => (
        <div key={item.id} className="flex justify-between text-[14px] py-1.5">
          <span>{formatDateRu(item.date)}</span>
          <span className="ui-muted">{item.group_name}</span>
          <span className="ui-muted">{item.start_time}</span>
        </div>
      ))}
    </div>
  );
}

function formatFeedbackAnswer(answer: FeedbackAnswer) {
  if (answer.question_type === "rating" && answer.rating_value != null) {
    return `Оценка ${answer.rating_value}/5`;
  }

  if (answer.question_type === "yes_no" && answer.bool_value != null) {
    return answer.bool_value ? "Да" : "Нет";
  }

  return answer.text_value?.trim() ?? "";
}

function FeedbackSection({
  responses,
  isLoading,
  isError,
  canSendSurvey,
  isSending,
  isSent,
  isSendError,
  onSendSurvey,
}: {
  readonly responses: FeedbackResponse[] | undefined;
  readonly isLoading: boolean;
  readonly isError: boolean;
  readonly canSendSurvey: boolean;
  readonly isSending: boolean;
  readonly isSent: boolean;
  readonly isSendError: boolean;
  readonly onSendSurvey: () => void;
}) {
  return (
    <section>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="text-sm font-medium text-foreground/70">Опросы</h3>
        {canSendSurvey ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="min-h-[44px]"
            disabled={isSending}
            onClick={onSendSurvey}
          >
            <MessageSquareText className="h-4 w-4" />
            {isSending ? "Отправляем..." : "Отправить опрос"}
          </Button>
        ) : null}
      </div>

      {isSent ? (
        <p className="mb-2 rounded-xl bg-emerald-50 px-3 py-2 text-[13px] text-emerald-800 ring-1 ring-emerald-100">
          Опрос отправлен
        </p>
      ) : null}

      {isSendError ? (
        <p className="mb-2 rounded-xl bg-red-50 px-3 py-2 text-[13px] text-red-800 ring-1 ring-red-100">
          Не удалось отправить опрос
        </p>
      ) : null}

      {isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-[64px] rounded-xl" />
          <Skeleton className="h-[52px] rounded-xl" />
        </div>
      ) : isError ? (
        <p className="ui-error-14">
          Не удалось загрузить ответы по опросам
        </p>
      ) : !responses?.length ? (
        <div className="ui-card">
          <p className="ui-muted-14">
            Ответов по опросам пока нет
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {responses.slice(0, 3).map((response) => (
            <div
              key={response.id}
              className="ui-card"
            >
              <p className="ui-muted-12">
                {formatDateRu(response.submitted_at)}
              </p>
              <div className="mt-2 space-y-1">
                {response.answers
                  .map((answer) => ({
                    answer,
                    value: formatFeedbackAnswer(answer),
                  }))
                  .filter(({ value }) => Boolean(value))
                  .map(({ answer, value }) => (
                    <div key={answer.question_id} className="space-y-0.5">
                      {answer.question_text ? (
                        <p className="text-[12px] leading-5 text-muted-foreground">
                          {answer.question_text}
                        </p>
                      ) : null}
                      <p className="text-[14px] leading-6 text-foreground">
                        {value}
                      </p>
                    </div>
                  ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export default function StudentDetail() {
  const { studentId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const leadResourceContext = leadResourceContextFromSearchParams(searchParams);
  const leadContextRef = useRef<HTMLElement>(null);

  const exactLeadResource = useQuery<LeadResourceAction>({
    queryKey: [
      "lead",
      studentId,
      "exact-resource",
      leadResourceContext?.context,
      leadResourceContext?.resourceType,
      leadResourceContext?.resourceId,
    ],
    queryFn: async () => {
      const response = await apiClient.get<LeadActionContextPayload>(
        `/leads/${studentId}/action-context`,
      );
      const candidates = [
        response.data.primary_action,
        ...response.data.secondary_capabilities,
      ].filter((action): action is LeadResourceAction => action !== null);
      const match = candidates.find(
        (action) =>
          action.target_resource_type === leadResourceContext!.resourceType &&
          action.target_resource_id === leadResourceContext!.resourceId &&
          action.context === leadResourceContext!.context,
      );
      if (!match) throw new Error("Exact lead resource is no longer live");
      return match;
    },
    enabled: Boolean(studentId && leadResourceContext),
    retry: false,
    staleTime: 30_000,
  });

  useEffect(() => {
    if (exactLeadResource.data) leadContextRef.current?.focus();
  }, [exactLeadResource.data]);

  // Sheet states
  const [paymentOpen, setPaymentOpen] = useState(false);
  const [dropInPaymentBooking, setDropInPaymentBooking] = useState<PersonalBooking | null>(null);
  const [personalRescheduleBooking, setPersonalRescheduleBooking] =
    useState<PersonalBooking | null>(null);
  const [personalBookingOpen, setPersonalBookingOpen] = useState(false);
  const [retryBankPaymentReceipt, setRetryBankPaymentReceipt] =
    useState<PersonalCommercialReceipt | null>(null);
  const [commercialDebtReceipt, setCommercialDebtReceipt] =
    useState<PersonalCommercialReceipt | null>(null);
  const [contextualCommercialContext, setContextualCommercialContext] =
    useState<ContextualCommercialContext | null>(null);
  const [contextualCommercialRetry, setContextualCommercialRetry] = useState(false);
  const [contextualCommercialPaymentMethod, setContextualCommercialPaymentMethod] = useState<
    "cash" | "transfer" | "sbp"
  >("cash");
  const [freezeOpen, setFreezeOpen] = useState(false);
  const [gradeOpen, setGradeOpen] = useState(false);
  const [gradeAssignOpen, setGradeAssignOpen] = useState(false);
  const [accountAccessIssueState, setAccountAccessIssueState] = useState<{
    studentId: string | undefined;
    issue: AccountAccessIssue;
  } | null>(null);
  const [parentPhoneState, setParentPhoneState] = useState<{
    studentId: string | undefined;
    value: string;
  } | null>(null);
  const [selectedStudentGradeId, setSelectedStudentGradeId] = useState<number | null>(
    null,
  );
  const [dropInActionError, setDropInActionError] = useState<string | null>(null);
  const [manualGroupAdmissionNoticeState] = useState(() => {
    const notice = (location.state as GroupSaleManualAdmissionRouteState | null)
      ?.groupSaleManualAdmission;
    const routeStudentId = Number(studentId);
    return notice?.studentId === routeStudentId &&
      Number.isSafeInteger(notice.paymentId) &&
      notice.financeState === "pending_manual"
      ? { studentId: routeStudentId }
      : null;
  });
  const queryClient = useQueryClient();
  const commercialCacheScope = useCommercialCacheScope();
  const unifiedPersonalAvailabilityEnabled = usePersonalAvailabilityCapability();
  const unifiedClientJourneyEnabled = useUnifiedClientJourneyCapability();

  const manualGroupAdmissionRouteNotice = (location.state as GroupSaleManualAdmissionRouteState | null)
    ?.groupSaleManualAdmission;
  const hasManualGroupAdmissionNotice =
    manualGroupAdmissionRouteNotice?.studentId === Number(studentId) &&
    Number.isSafeInteger(manualGroupAdmissionRouteNotice.paymentId) &&
    manualGroupAdmissionRouteNotice.financeState === "pending_manual";
  const showManualGroupAdmissionNotice =
    manualGroupAdmissionNoticeState?.studentId === Number(studentId);

  useEffect(() => {
    if (!hasManualGroupAdmissionNotice) return;
    navigate(`${location.pathname}${location.search}${location.hash}`, {
      replace: true,
      state: null,
    });
  }, [hasManualGroupAdmissionNotice, location.hash, location.pathname, location.search, navigate]);

  const removeGradeMutation = useMutation({
    mutationFn: (studentGradeId: number) =>
      apiClient.delete(`/grades/student-grades/${studentGradeId}/`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["student", studentId, "grades"] });
    },
  });

  const invalidateDropInBookingQueries = () => {
    queryClient.invalidateQueries({ queryKey: ["student", studentId, "personal-bookings"] });
    queryClient.invalidateQueries({ queryKey: ["student", studentId] });
    queryClient.invalidateQueries({ queryKey: ["student", studentId, "subscriptions"] });
    queryClient.invalidateQueries({ queryKey: ["billing", "debts", Number(studentId)] });
    queryClient.invalidateQueries({ queryKey: ["trainer", "availability"] });
    queryClient.invalidateQueries({ queryKey: ["schedules"] });
  };

  const cancelDropInMutation = useMutation({
    mutationFn: (booking: PersonalBooking) => {
      if (!booking.can_manage) throw new Error("Нет прав для управления этой персоналкой");
      if (!booking.booking_id) throw new Error("Не найден идентификатор персоналки");
      return apiClient.post(`/personal-drop-in-bookings/${booking.booking_id}/cancel/`, {
        reason: "Отменено тренером в карточке ученика",
      });
    },
    onMutate: () => setDropInActionError(null),
    onSuccess: () => {
      setDropInActionError(null);
      invalidateDropInBookingQueries();
    },
    onError: (error: unknown) => {
      setDropInActionError(getApiError(error, "Не удалось отменить персоналку"));
    },
  });

  const noShowDropInMutation = useMutation({
    mutationFn: (booking: PersonalBooking) => {
      if (!booking.can_manage) throw new Error("Нет прав для управления этой персоналкой");
      if (!booking.booking_id) throw new Error("Не найден идентификатор персоналки");
      return apiClient.post(`/personal-drop-in-bookings/${booking.booking_id}/no-show/`, {
        reason: "Клиент не пришёл",
      });
    },
    onMutate: () => setDropInActionError(null),
    onSuccess: () => {
      setDropInActionError(null);
      invalidateDropInBookingQueries();
    },
    onError: (error: unknown) => {
      setDropInActionError(getApiError(error, "Не удалось отметить неявку"));
    },
  });

  const cancelDropInPaymentMutation = useMutation({
    mutationFn: (booking: PersonalBooking) => {
      if (!booking.can_manage) throw new Error("Нет прав для управления этой персоналкой");
      if (!booking.bank_payment_order_id) {
        throw new Error("Не найдена ссылка на оплату персоналки");
      }
      return apiClient.post(
        `/billing/bank-payment-orders/${booking.bank_payment_order_id}/cancel/`,
        {},
      );
    },
    onMutate: () => setDropInActionError(null),
    onSuccess: () => {
      setDropInActionError(null);
      invalidateDropInBookingQueries();
    },
    onError: (error: unknown) => {
      setDropInActionError(getApiError(error, "Не удалось отменить ссылку на оплату"));
    },
  });

  // 1. Student detail
  const {
    data: student,
    isLoading: studentLoading,
    isError: studentError,
  } = useQuery<StudentDetailType>({
    queryKey: ["student", studentId],
    queryFn: () =>
      apiClient.get(`/students/${studentId}/`).then((r) => r.data),
    staleTime: 60_000,
    enabled: !!studentId,
  });

  // 2. Active subscriptions
  const {
    data: subscriptions,
    isLoading: subsLoading,
    isError: subsError,
  } = useQuery<StudentSubscription[]>({
    queryKey: ["student", studentId, "subscriptions"],
    queryFn: () =>
      apiClient
        .get("/billing/subscriptions/", { params: { student_id: studentId } })
        .then((r) => normalizeStudentSubscriptionsPayload(r.data)),
    staleTime: 60_000,
    enabled: !!studentId,
  });

  const numericStudentId = Number(studentId);
  const {
    data: bankPaymentOrders,
    refetch: refetchBankPaymentOrders,
  } = useQuery<BankPaymentOrderLink[]>({
    queryKey: ["billing", "bank-payment-orders", numericStudentId],
    queryFn: () =>
      apiClient
        .get<BankPaymentOrderLink[] | { items?: BankPaymentOrderLink[] }>(
          "/billing/bank-payment-orders/",
          { params: { student_id: numericStudentId, status: "recent" } },
        )
        .then((response) =>
          Array.isArray(response.data) ? response.data : response.data.items ?? [],
        ),
    staleTime: 30_000,
    enabled: Number.isInteger(numericStudentId) && numericStudentId > 0,
  });

  const cancelBankPaymentOrderMutation = useMutation({
    mutationFn: async (order: BankPaymentOrderLink) => {
      const response = await apiClient.post<BankPaymentOrderLink>(
        `/billing/bank-payment-orders/${order.id}/cancel/`,
        {},
      );
      return response.data;
    },
    // Do not remove the order optimistically: a failed request must remain
    // visibly recoverable from this same subscription row.
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["student", studentId] }),
        queryClient.invalidateQueries({ queryKey: ["student", studentId, "subscriptions"] }),
        queryClient.invalidateQueries({
          queryKey: ["billing", "bank-payment-orders", numericStudentId],
        }),
        queryClient.invalidateQueries({ queryKey: ["billing", "debts", numericStudentId] }),
        queryClient.invalidateQueries({ queryKey: ["student", studentId, "personal-bookings"] }),
      ]);
    },
  });

  const refreshBankPaymentOrderMutation = useMutation({
    mutationFn: (order: BankPaymentOrderLink) =>
      apiClient.post(`/billing/bank-payment-orders/${order.id}/refresh/`, {}),
    onSettled: () => refetchBankPaymentOrders(),
  });

  const {
    data: personalBookings,
    isLoading: personalBookingsLoading,
    isError: personalBookingsError,
  } = useQuery<PersonalBooking[]>({
    queryKey: ["student", studentId, "personal-bookings"],
    queryFn: () =>
      apiClient
        .get(`/students/${studentId}/personal-bookings/`)
        .then((r) => r.data),
    staleTime: 60_000,
    enabled: !!studentId,
  });

  const commercialContext = useQuery<PersonalCommercialContext>({
    queryKey: personalCommercialContextQueryKey(numericStudentId, commercialCacheScope),
    queryFn: () => getPersonalCommercialContext(numericStudentId),
    enabled: Number.isSafeInteger(numericStudentId) && numericStudentId > 0,
    staleTime: 30_000,
    retry: false,
  });

  // 3. Grade progress
  const {
    data: grades,
    isLoading: gradesLoading,
    isError: gradesError,
  } = useQuery<GradeProgress[]>({
    queryKey: ["student", studentId, "grades"],
    queryFn: () =>
      apiClient
        .get(`/grades/students/${studentId}/progress/`)
        .then((r) => r.data),
    staleTime: 2 * 60_000,
    enabled: !!studentId,
  });

  // 4. Attendance history (D-08, US-14)
  const {
    data: attendance,
    isLoading: attendanceLoading,
    isError: attendanceError,
  } = useQuery<AttendanceItem[]>({
    queryKey: ["student", studentId, "checkins"],
    queryFn: () =>
      apiClient
        .get(`/students/${studentId}/checkins/`, { params: { limit: 10 } })
        .then((r) => r.data),
    staleTime: 2 * 60_000,
    enabled: !!studentId,
  });

  const {
    data: feedbackResponses,
    isLoading: feedbackLoading,
    isError: feedbackError,
  } = useQuery<FeedbackResponse[]>({
    queryKey: ["student", studentId, "feedback", "responses"],
    queryFn: () =>
      apiClient
        .get(`/feedback/students/${studentId}/responses/`)
        .then((r) => r.data),
    staleTime: 60_000,
    enabled: !!studentId,
  });

  const sendSurveyMutation = useMutation({
    mutationFn: () => apiClient.post(`/feedback/send-survey/${studentId}/`),
  });
  const currentAccountAccessIssueState = accountAccessIssueState;
  const currentParentPhoneState = parentPhoneState;
  const accountAccessIssue =
    currentAccountAccessIssueState && currentAccountAccessIssueState.studentId === studentId
      ? currentAccountAccessIssueState.issue
      : null;
  const effectiveParentPhone =
    currentParentPhoneState && currentParentPhoneState.studentId === studentId
      ? currentParentPhoneState.value
      : "";

  const storeAccountAccessIssue = (issue: AccountAccessIssue) => {
    setAccountAccessIssueState({ studentId, issue });
    queryClient.setQueryData<StudentDetailType>(["student", studentId], (current) =>
      current ? { ...current, account_access: accountAccessSummary(issue) } : current,
    );
  };

  const openAccountAccessMutation = useMutation({
    mutationFn: () =>
      apiClient.post(`/students/${studentId}/account-access/open/`, {
        ...(student?.is_child && !student.has_parent_user
          ? { parent_phone: effectiveParentPhone.trim() }
          : {}),
      }),
    onSuccess: async (response) => {
      storeAccountAccessIssue(response.data as AccountAccessIssue);
      await queryClient.invalidateQueries({ queryKey: ["student", studentId] });
    },
  });

  const resetAccountAccessMutation = useMutation({
    mutationFn: () => apiClient.post(`/students/${studentId}/account-access/reset/`),
    onSuccess: async (response) => {
      storeAccountAccessIssue(response.data as AccountAccessIssue);
      await queryClient.invalidateQueries({ queryKey: ["student", studentId] });
    },
  });

  // Derived data
  const lastVisitDate =
    attendance && attendance.length > 0
      ? formatRelativeDate(attendance[0].date)
      : null;

  const fullName = student
    ? `${student.first_name} ${student.last_name}`.trim()
    : "";

  const statusCfg = student
    ? STATUS_CONFIG[student.status] ?? {
        label: student.status,
        variant: "outline" as const,
      }
    : null;
  const currentAccountAccess = accountAccessIssue ?? student?.account_access ?? null;
  const temporaryPassword = accountAccessIssue?.temporary_password ?? null;
  const canManageSensitiveActions = student?.can_manage_sensitive_actions === true;
  const canManageAccountAccess = student?.can_manage_account_access ?? false;
  const canOpenAccountAccess = Boolean(
    canManageAccountAccess && student?.account_access_eligible,
  );
  const operationalAdmission = student?.operational_admission_v2 ?? student?.operational_admission ?? null;
  const accessEligibilityMessage = !canManageAccountAccess
      ? "Доступ может открыть ответственный тренер или администратор"
      : canOpenAccountAccess
      ? "Можно открыть кабинет"
      : "Доступ пока недоступен по данным сервера";

  // Grade progress for promote sheet
  const gradeProgress =
    grades?.find((grade) => grade.student_grade_id === selectedStudentGradeId) ??
    grades?.[0] ??
    null;

  const openGradePromote = (grade: GradeProgress) => {
    setSelectedStudentGradeId(grade.student_grade_id);
    setGradeOpen(true);
  };

  // Active subscription for freeze
  const activeSubscriptions =
    subscriptions?.filter((subscription) => subscription.status === "active") ?? [];
  const activeSub =
    activeSubscriptions.find((subscription) => subscription.freeze_status !== "pending") ??
    activeSubscriptions[0];
  const hasOnlyPendingFreezeAction = Boolean(
    activeSub && activeSub.freeze_status === "pending",
  );

  // 404 / error state
  if (studentError) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-4 px-4">
        <p className="ui-title-20">
          Ученик не найден
        </p>
        <Button variant="outline" onClick={() => navigate("/trainer/students")}>
          Назад к списку
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 px-4 pt-6 pb-8">
      {/* Back button */}
      <button
        type="button"
        onClick={() => navigate("/trainer/students")}
        className="flex items-center gap-1 text-[14px] text-muted-foreground active:scale-[0.98] active:opacity-70 transition-transform self-start"
      >
        <ArrowLeft size={18} />
        <span>Назад</span>
      </button>

      {/* Header: name + phone + edit */}
      {studentLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-[24px] w-[200px] rounded" />
          <Skeleton className="h-[20px] w-[120px] rounded" />
        </div>
      ) : (
        <div>
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <h1 className="text-lg font-semibold leading-tight text-foreground break-words">
                {fullName}
              </h1>
            </div>
            {student?.phone && (
              <a
                href={`tel:${student.phone}`}
                className="flex shrink-0 items-center justify-center rounded-full bg-[var(--branding-accent)] p-2 text-white active:scale-95 transition-transform"
              >
                <Phone size={16} />
              </a>
            )}
            <button
              type="button"
              aria-label="Редактировать ученика"
              onClick={() => navigate(`/trainer/students/${studentId}/edit`)}
              className="flex shrink-0 items-center justify-center rounded-full bg-muted p-2 text-muted-foreground active:scale-95 transition-transform"
            >
              <Pencil size={16} />
            </button>
          </div>
          {statusCfg && (
            <Badge variant={statusCfg.variant} className="mt-1">
              {statusCfg.label}
            </Badge>
          )}
        </div>
      )}

      {leadResourceContext && exactLeadResource.isLoading ? (
        <Skeleton className="h-[82px] rounded-xl" />
      ) : null}

      {leadResourceContext && exactLeadResource.isError ? (
        <section role="alert" className="rounded-xl bg-destructive/10 p-3 text-[13px] text-destructive">
          Точный контекст больше недоступен. Вернитесь в заявки и обновите следующее действие.
        </section>
      ) : null}

      {leadResourceContext && exactLeadResource.data ? (
        <section
          ref={leadContextRef}
          tabIndex={-1}
          role="status"
          aria-label="Точный контекст из заявки"
          className="rounded-xl bg-[var(--branding-accent)]/10 p-3 outline-none focus:ring-2 focus:ring-[var(--branding-accent)]"
        >
          <p className="text-[13px] font-semibold text-foreground">Открыт точный контекст из заявки</p>
          <p className="mt-1 text-[13px] text-muted-foreground">
            {leadResourceLabel(leadResourceContext)} #{leadResourceContext.resourceId}
          </p>
          <p className="mt-2 text-[14px] text-foreground">
            {exactLeadResource.data.supporting_text}
          </p>
          <p className="mt-1 text-[13px] font-medium text-foreground">
            Следующее действие: {exactLeadResource.data.label}
          </p>
        </section>
      ) : null}

      {showManualGroupAdmissionNotice && !studentLoading && student ? (
        <section
          role="status"
          className="rounded-xl bg-amber-50 p-3 text-[14px] font-semibold text-amber-950"
        >
          {fullName} оформлен в группу. Оплата ожидает подтверждения.
        </section>
      ) : null}

      {/* SUBSCRIPTION section (D-04) */}
      <section>
        <h3 className="ui-section-label">
          Абонемент
        </h3>
        <SubscriptionSection
          subscriptions={subscriptions}
          isLoading={subsLoading}
          isError={subsError}
          bankPaymentOrders={bankPaymentOrders}
          isCancelingOrderId={
            cancelBankPaymentOrderMutation.isPending
              ? cancelBankPaymentOrderMutation.variables?.id
              : null
          }
          cancelError={
            cancelBankPaymentOrderMutation.isError
              ? getApiError(
                  cancelBankPaymentOrderMutation.error,
                  "Не удалось отменить оплату. Попробуйте ещё раз.",
                )
              : null
          }
          onCancelOrder={(order) => cancelBankPaymentOrderMutation.mutate(order)}
          onRefreshOrders={(order) => refreshBankPaymentOrderMutation.mutate(order)}
          onRefetchOrders={() => void refetchBankPaymentOrders()}
          isRefreshingOrder={refreshBankPaymentOrderMutation.isPending}
          canStartContextualRenewal={
            unifiedClientJourneyEnabled && canManageSensitiveActions && !commercialContext.isError
          }
          onStartContextualRenewal={(subscription) =>
            {
              setContextualCommercialRetry(false);
              setContextualCommercialPaymentMethod("cash");
              setContextualCommercialContext({
                kind: "subscription_renewal",
                studentId: numericStudentId,
                studentName: fullName,
                renewedFromSubscriptionId: subscription.id,
                renewedFromSubscriptionName: subscription.tariff_name,
                renewalTargetTariffId: subscription.renewal_target_tariff_id,
                renewalTargetTariffName: subscription.renewal_target_tariff_name,
                renewalTargetPrice: subscription.renewal_target_price,
              });
            }
          }
        />
      </section>

      <PersonalBookingsSection
        bookings={personalBookings}
        isLoading={personalBookingsLoading}
        isError={personalBookingsError}
        actionError={dropInActionError}
        onOpenBooking={() => {
          if (unifiedPersonalAvailabilityEnabled) {
            navigate(`/trainer/availability?student_id=${studentId}`);
            return;
          }
          setPersonalBookingOpen(true);
        }}
        onOpenDropInPayment={(booking) => {
          if (!booking.can_manage) return;
          setPaymentOpen(false);
          setDropInPaymentBooking(booking);
        }}
        isCancelingDropInPayment={cancelDropInPaymentMutation.isPending}
        onCancelDropInPayment={(booking) => cancelDropInPaymentMutation.mutate(booking)}
        onCancelDropIn={(booking) => cancelDropInMutation.mutate(booking)}
        onMarkNoShow={(booking) => noShowDropInMutation.mutate(booking)}
        onReschedule={(booking) => {
          if (booking.can_reschedule) setPersonalRescheduleBooking(booking);
        }}
      />

      {commercialContext.isLoading || commercialContext.isError || commercialContext.data?.attempts.length ? (
        <section className="ui-col-2">
          <h3 className="ui-section-label">
            {commercialContext.data?.attempts.some((attempt) => attempt.kind !== "personal_staff_intent")
              ? "Коммерческий контекст оплаты"
              : "Персональная запись и оплата"}
          </h3>
          {commercialContext.isLoading ? (
            <Skeleton className="h-[154px] rounded-xl" />
          ) : commercialContext.isError ? (
            <p role="status" className="ui-warning">
              Коммерческий контекст сейчас недоступен. Обновите карточку клиента.
            </p>
          ) : commercialContext.data?.attempts.length ? (
            <PersonalCommercialReceiptList
              attempts={commercialContext.data.attempts}
              studentId={numericStudentId}
              onReceiptChanged={() => {
                void queryClient.invalidateQueries({
                  queryKey: personalCommercialContextQueryKey(numericStudentId, commercialCacheScope),
                });
                void queryClient.invalidateQueries({
                  queryKey: ["student", studentId, "personal-bookings"],
                });
                void queryClient.invalidateQueries({ queryKey: ["billing", "debts", numericStudentId] });
              }}
              onSettleExactDebt={setCommercialDebtReceipt}
              onRetryPersonalBankPayment={setRetryBankPaymentReceipt}
              canRetryContextualBankPayment={(receipt) =>
                contextualRetryContextFromReceipt({
                  receipt,
                  studentId: numericStudentId,
                  studentName: fullName,
                }) !== null
              }
              onRetryContextualBankPayment={(receipt) => {
                const retryContext = contextualRetryContextFromReceipt({
                  receipt,
                  studentId: numericStudentId,
                  studentName: fullName,
                });
                if (retryContext) {
                  const sourceSubscription =
                    retryContext.kind === "subscription_renewal"
                      ? subscriptions?.find(
                          (subscription) =>
                            subscription.id === retryContext.renewedFromSubscriptionId,
                        )
                      : null;
                  const receiptOffer =
                    retryContext.kind === "subscription_renewal"
                      ? getExpectedRenewalOfferFields({
                          renewal_target_tariff_id: retryContext.renewalTargetTariffId,
                          renewal_target_price: retryContext.renewalTargetPrice,
                        })
                      : null;
                  const receiptHasOfferProjection =
                    retryContext.kind === "subscription_renewal" &&
                    (receipt.renewal_target_tariff_id !== undefined ||
                      receipt.renewal_target_tariff_name !== undefined ||
                      receipt.renewal_target_price !== undefined);
                  const subscriptionOffer =
                    retryContext.kind === "subscription_renewal" && sourceSubscription
                      ? getExpectedRenewalOfferFields({
                          renewal_target_tariff_id: sourceSubscription.renewal_target_tariff_id,
                          renewal_target_price: sourceSubscription.renewal_target_price,
                        })
                      : null;
                  const retryContextWithOffer =
                    retryContext.kind === "subscription_renewal" &&
                    !receiptHasOfferProjection &&
                    !receiptOffer &&
                    subscriptionOffer
                      ? {
                          ...retryContext,
                          renewalTargetTariffId: subscriptionOffer.expected_target_tariff_id,
                          renewalTargetPrice: subscriptionOffer.expected_target_price,
                          renewalTargetTariffName: sourceSubscription?.renewal_target_tariff_name,
                        }
                      : retryContext;
                  setContextualCommercialRetry(true);
                  setContextualCommercialPaymentMethod(
                    receipt.payment_method === "transfer"
                      ? "transfer"
                      : receipt.payment_method === "sbp"
                        ? "sbp"
                        : "cash",
                  );
                  setContextualCommercialContext(retryContextWithOffer);
                }
              }}
            />
          ) : (
            <p className="ui-muted-14">Активных или последних завершённых попыток нет.</p>
          )}
        </section>
      ) : null}

      {!studentLoading && student ? (
        <section>
          {operationalAdmission ? (
            <div className="mb-3 rounded-xl bg-amber-50/80 p-3 ring-1 ring-amber-500/15">
              <p className="text-[14px] font-semibold text-amber-950">
                {operationalAdmission.payment_status === "pending"
                  ? "Оплата ожидает подтверждения"
                  : operationalAdmission.payment_status === "confirmed"
                    ? "Оплата подтверждена"
                    : operationalAdmission.payment_status === "rejected"
                      ? "Оплата отклонена"
                  : `Статус оплаты: ${operationalAdmission.payment_status}`}
              </p>
              {operationalAdmission.payment_status === "rejected" ? (
                <p className="mt-1 text-[13px] text-amber-900/80">
                  Запись отменена; будущая готовность к чекину снята.
                </p>
              ) : operationalAdmission.payment_status === "pending" ? (
                <p className="mt-1 text-[13px] text-amber-900/80">
                  {"kind" in operationalAdmission && operationalAdmission.kind === "personal"
                    ? `Персональная запись · ${operationalAdmission.checkin_ready ? "доступна для чекина" : "ожидает даты"} ${operationalAdmission.start_date}`
                    : operationalAdmission.checkin_ready
                      ? `Записан в ${operationalAdmission.group_label} · доступен для чекина с ${operationalAdmission.start_date}`
                      : `Записан в ${operationalAdmission.group_label} · чекин сейчас недоступен (старт ${operationalAdmission.start_date})`}
                </p>
              ) : (
                <p className="mt-1 text-[13px] text-amber-900/80">
                  {"kind" in operationalAdmission && operationalAdmission.kind === "personal"
                    ? `Персональная запись · ${operationalAdmission.start_date}`
                    : `Записан в ${operationalAdmission.group_label} · старт ${operationalAdmission.start_date}`}
                </p>
              )}
            </div>
          ) : null}
          <h3 className="ui-section-label">
            Доступ
          </h3>
          <AccountAccessPanel
            accountAccess={currentAccountAccess}
            isChild={student.is_child}
            canManageAccess={canManageAccountAccess}
            hasLinkedParent={student.has_parent_user}
            parentPhone={effectiveParentPhone}
            canOpenAccess={canOpenAccountAccess}
            eligibilityMessage={accessEligibilityMessage}
            temporaryPassword={temporaryPassword}
            isOpening={openAccountAccessMutation.isPending}
            isResetting={resetAccountAccessMutation.isPending}
            error={openAccountAccessMutation.error ?? resetAccountAccessMutation.error}
            onParentPhoneChange={(value) => setParentPhoneState({ studentId, value })}
            onOpen={() => openAccountAccessMutation.mutate()}
            onReset={() => resetAccountAccessMutation.mutate()}
          />
        </section>
      ) : null}

      {/* INFO section (D-05) */}
      <section>
        <h3 className="ui-section-label">
          Инфо
        </h3>
        <InfoSection
          student={student}
          grades={grades}
          lastVisitDate={lastVisitDate}
          studentLoading={studentLoading}
          gradesLoading={gradesLoading}
          gradesError={gradesError}
          canManageGrades={canManageSensitiveActions}
          onRemoveGrade={(id) => removeGradeMutation.mutate(id)}
        />
      </section>

      {/* ACTIONS section (D-07) */}
      {canManageSensitiveActions ? (
        <section>
          <h3 className="ui-section-label">
            Действия
          </h3>
          <div className="flex w-full flex-wrap gap-2">
            <Button
              type="button"
              size="lg"
              wrap
              className={ACTION_BUTTON_CLASS}
              onClick={() => setPaymentOpen(true)}
            >
              Принять оплату
            </Button>
            <Button
              variant="outline"
              size="lg"
              wrap
              className={ACTION_BUTTON_CLASS}
              disabled={!activeSub || hasOnlyPendingFreezeAction}
              onClick={() => setFreezeOpen(true)}
            >
              {hasOnlyPendingFreezeAction ? "Заявка ждёт" : "Заморозить"}
            </Button>
            <GradePromotionButtons grades={grades} openPromote={openGradePromote} />
            <Button
              variant="outline"
              size="lg"
              wrap
              className={ACTION_BUTTON_CLASS}
              onClick={() => setGradeAssignOpen(true)}
            >
              + Дисциплина
            </Button>
          </div>
        </section>
      ) : null}

      {/* ATTENDANCE section (D-08, US-14) */}
      <section>
        <h3 className="ui-section-label">
          Посещения
        </h3>
        <AttendanceSection
          attendance={attendance}
          isLoading={attendanceLoading}
          isError={attendanceError}
        />
      </section>

      <FeedbackSection
        responses={feedbackResponses}
        isLoading={feedbackLoading}
        isError={feedbackError}
        canSendSurvey={student?.can_manage_feedback ?? false}
        isSending={sendSurveyMutation.isPending}
        isSent={sendSurveyMutation.isSuccess}
        isSendError={sendSurveyMutation.isError}
        onSendSurvey={() => sendSurveyMutation.mutate()}
      />

      {/* NOTES section (D-06) */}
      <section>
        <h3 className="ui-section-label">
          Заметки
        </h3>
        <StudentNotes
          studentId={Number(studentId)}
          notes={student?.notes ?? []}
        />
      </section>

      {/* Action Sheets */}
      {paymentOpen && canManageSensitiveActions ? (
        <PaymentSheet
          open
          onOpenChange={setPaymentOpen}
          studentId={Number(studentId)}
          studentName={fullName}
        />
      ) : null}

      <ContextualCommercialSheet
        key={
          contextualCommercialContext?.kind === "group_sale"
            ? `group:${contextualCommercialContext.trainingGroupId}:${contextualCommercialContext.scheduleId}:${contextualCommercialContext.startDate}:${contextualCommercialPaymentMethod}:${contextualCommercialRetry}`
            : contextualCommercialContext?.kind === "subscription_renewal"
              ? `renewal:${contextualCommercialContext.renewedFromSubscriptionId}:${contextualCommercialPaymentMethod}:${contextualCommercialRetry}`
              : "none"
        }
        open={contextualCommercialContext !== null}
        onOpenChange={(open) => {
          if (!open) {
            setContextualCommercialContext(null);
            setContextualCommercialRetry(false);
            setContextualCommercialPaymentMethod("cash");
          }
        }}
        context={contextualCommercialContext}
        initialPaymentMethod={contextualCommercialPaymentMethod}
        freshCommand={contextualCommercialRetry}
        onOfferChanged={() => {
          setContextualCommercialContext(null);
          setContextualCommercialRetry(false);
          setContextualCommercialPaymentMethod("cash");
          void queryClient.invalidateQueries({
            queryKey: ["student", studentId, "subscriptions"],
          });
          void commercialContext.refetch();
        }}
      />

      {dropInPaymentBooking?.can_manage ? (
        <PaymentSheet
          open
          onOpenChange={(open) => {
            if (!open) setDropInPaymentBooking(null);
          }}
          studentId={Number(studentId)}
          studentName={fullName}
          dropInBookingId={dropInPaymentBooking.booking_id ?? null}
          requiredTariffId={dropInPaymentBooking.tariff_id ?? null}
          requiredDebtId={dropInPaymentBooking.debt_id ?? null}
          unifiedPersonalSettlement={unifiedPersonalAvailabilityEnabled}
        />
      ) : null}

      {commercialDebtReceipt?.booking_id &&
      commercialDebtReceipt?.debt_id &&
      commercialDebtReceipt.amount != null &&
      commercialDebtReceipt.amount !== "" ? (
        <PaymentSheet
          open
          onOpenChange={(open) => {
            if (!open) setCommercialDebtReceipt(null);
          }}
          studentId={Number(studentId)}
          studentName={fullName}
          dropInBookingId={commercialDebtReceipt.booking_id}
          requiredTariffId={commercialDebtReceipt.tariff_id ?? null}
          requiredDebtId={commercialDebtReceipt.debt_id}
          exactPersonalDebtSettlement={{
            bookingId: commercialDebtReceipt.booking_id,
            debtId: commercialDebtReceipt.debt_id,
            tariffName:
              commercialDebtReceipt.tariff_name ?? commercialDebtReceipt.training_type_name,
            amount: commercialDebtReceipt.amount,
          }}
          unifiedPersonalSettlement
        />
      ) : null}

      <PersonalBookingSheet
        open={personalBookingOpen}
        onOpenChange={setPersonalBookingOpen}
        studentId={Number(studentId)}
        studentName={fullName}
        subscriptions={subscriptions}
      />

      {personalRescheduleBooking?.can_reschedule ? (
        <PersonalBookingRescheduleSheet
          open
          onOpenChange={(open) => {
            if (!open) setPersonalRescheduleBooking(null);
          }}
          booking={personalRescheduleBooking}
          studentId={numericStudentId}
          studentName={fullName}
        />
      ) : null}

      {retryBankPaymentReceipt ? (
        <PersonalBookingSheet
          open
          onOpenChange={(open) => {
            if (!open) setRetryBankPaymentReceipt(null);
          }}
          studentId={Number(studentId)}
          studentName={fullName}
          subscriptions={subscriptions}
          retryBankPaymentReceipt={retryBankPaymentReceipt}
          onStaffIntentCreated={() => setRetryBankPaymentReceipt(null)}
        />
      ) : null}

      <FreezeSheet
        open={freezeOpen}
        onOpenChange={setFreezeOpen}
        subscriptionId={activeSub?.id ?? null}
        freezeStatus={activeSub?.freeze_status ?? null}
        studentName={fullName}
      />

      <GradePromoteSheet
        open={gradeOpen}
        onOpenChange={setGradeOpen}
        studentId={Number(studentId)}
        studentName={fullName}
        gradeProgress={gradeProgress}
      />

      <GradeAssignSheet
        open={gradeAssignOpen}
        onOpenChange={setGradeAssignOpen}
        studentId={Number(studentId)}
        assignedSystemIds={grades?.map((g) => g.grade_system_id) ?? []}
      />
    </div>
  );
}

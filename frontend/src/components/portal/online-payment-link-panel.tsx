import { useEffect, useId, useRef, useState } from "react";
import { Copy, ExternalLink, QrCode, RefreshCw, Send, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { cn, formatRub } from "@/lib/utils";
import {
  getPaymentActionMode,
  isConfirmedBankPaymentOrder,
  isFuturePaymentExpiry,
  isLiveBankPaymentOrder,
  type BankPaymentOrderLink,
} from "@/components/portal/payment-link-state";
import { formatRenewalOfferLabel } from "@/components/portal/subscription-renewal-state";
import { rememberPaymentResumeContext } from "@/features/payments/payment-resume-context";

export type { BankPaymentOrderLink } from "@/components/portal/payment-link-state";

const statusLabels: Record<string, string> = {
  created: "Создана",
  pending: "Ожидает оплаты",
  approved: "Оплачена",
  authorized: "Ожидает списания",
  expired: "Истекла",
  failed: "Ошибка",
  cancelled: "Отменена",
  manual_review: "Проверка",
  refunded: "Возвращена",
  refunded_partially: "Частичный возврат",
};

type ActionNotice = "" | "Ссылка скопирована" | "Не удалось скопировать ссылку" | "Не удалось отправить ссылку";

const unavailableStatusCopy: Record<string, string> = {
  approved: "Оплата подтверждена. Платить повторно не нужно.",
  manual_review: "Оплата проходит защищённую сверку с банком. Не создавайте новую попытку.",
  failed: "Оплата не подтверждена. Выберите другой способ или обратитесь в клуб.",
  expired: "Срок действия ссылки истёк. Запросите новую ссылку в кабинете.",
  cancelled: "Ссылка отменена. Если банк уже открыл оплату, не используйте её и обратитесь в клуб.",
  refunded: "Оплата возвращена полностью. Сумма и статус сохранены в истории клуба.",
  refunded_partially: "Часть оплаты возвращена. Подробности сохранены в истории клуба.",
};

function formatExpiresAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "срок уточняется";

  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function isSafePaymentUrl(value: string | undefined, provider: string): value is string {
  if (typeof value !== "string" || value.length === 0) return false;
  try {
    const parsed = new URL(value);
    const isLocalMock =
      provider === "mock" &&
      parsed.protocol === "http:" &&
      ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname);
    return (
      (parsed.protocol === "https:" || isLocalMock) &&
      Boolean(parsed.hostname) &&
      !parsed.username &&
      !parsed.password
    );
  } catch {
    return false;
  }
}

export function OnlinePaymentLinkPanel({
  order,
  className,
  title = "Ссылка на оплату",
  subtitle,
  cancelLabel = "Отменить",
  isCanceling = false,
  isRefreshing = false,
  unavailableReason,
  onCancel,
  onRefresh,
  onRequestRefresh,
  onPay,
}: {
  readonly order: BankPaymentOrderLink;
  readonly className?: string;
  readonly title?: string;
  readonly subtitle?: string;
  readonly cancelLabel?: string;
  readonly isCanceling?: boolean;
  readonly isRefreshing?: boolean;
  readonly unavailableReason?: string;
  readonly onCancel?: (order: BankPaymentOrderLink) => void;
  readonly onRefresh?: () => void;
  readonly onRequestRefresh?: () => void;
  readonly onPay?: (order: BankPaymentOrderLink) => void;
}) {
  const [copyState, setCopyState] = useState<ActionNotice>("");
  const [qrSvg, setQrSvg] = useState("");
  const [showQr, setShowQr] = useState(false);
  const [cancelSheetOpen, setCancelSheetOpen] = useState(false);
  const [expiryVersion, setExpiryVersion] = useState(0);
  const expiredRefreshKey = useRef<string | null>(null);
  const previousStatus = useRef(order.status);
  const confirmedHeadingRef = useRef<HTMLHeadingElement>(null);
  const qrRegionRef = useRef<HTMLDivElement>(null);
  const instanceId = useId();
  const qrRegionId = `${instanceId}-payment-qr-${order.id}`;
  const isLocallyExpired =
    ["created", "pending", "authorized"].includes(order.status) &&
    !isFuturePaymentExpiry(order.expires_at);
  const statusLabel = isLocallyExpired
    ? statusLabels.expired
    : statusLabels[order.status] ?? "Статус уточняется";
  const actionMode = getPaymentActionMode(order);
  const isLive = isLiveBankPaymentOrder(order);
  const canCancel = isLive && Boolean(onCancel) && order.can_cancel === true;
  const paymentUrl = isSafePaymentUrl(order.provider_payment_url, order.provider ?? "")
    ? order.provider_payment_url
    : null;
  const hasStaffActions = actionMode === "staff" && paymentUrl !== null;
  const hasSelfServiceAction = actionMode === "self_service" && paymentUrl !== null;
  const renewalSourceName = order.renewal_source_tariff_name?.trim();
  const renewalTargetLabel = formatRenewalOfferLabel(order);
  const fulfillmentCopy =
    order.fulfillment_state === "fulfillment_pending"
      ? "Оплата принята, но абонемент или запись ещё активируются. Обновите статус через несколько секунд."
      : order.fulfillment_state === "fulfilled"
        ? "Покупка уже отражена в кабинете."
        : "";
  const canRefreshFulfillment =
    order.status === "approved" &&
    order.fulfillment_state === "fulfillment_pending" &&
    Boolean(onRefresh);
  const refreshHandler = canRefreshFulfillment
    ? onRefresh
    : onRequestRefresh || onRefresh;
  const canRefreshOrder =
    Boolean(refreshHandler) &&
    (order.can_request_refresh === true || canRefreshFulfillment);

  useEffect(() => {
    if (!isLive) return;
    const delay = new Date(order.expires_at).getTime() - Date.now() + 100;
    if (delay <= 0) {
      if (expiredRefreshKey.current !== order.expires_at) {
        expiredRefreshKey.current = order.expires_at;
        setExpiryVersion((current) => current + 1);
        onRefresh?.();
      }
      return;
    }
    const timer = window.setTimeout(() => {
      setExpiryVersion((current) => current + 1);
      onRefresh?.();
    }, Math.min(delay, 24 * 60 * 60_000));
    return () => window.clearTimeout(timer);
  }, [expiryVersion, isLive, onRefresh, order.expires_at]);

  useEffect(() => {
    if (!showQr || !paymentUrl || order.can_show_qr !== true) return;
    let cancelled = false;
    import("uqr")
      .then(({ renderSVG }) =>
        renderSVG(paymentUrl, {
          ecc: "M",
          border: 1,
        }),
      )
      .then((svg) => {
        if (!cancelled) setQrSvg(svg);
      })
      .catch(() => {
        if (!cancelled) setQrSvg("");
      });
    return () => {
      cancelled = true;
    };
  }, [order.can_show_qr, paymentUrl, showQr]);

  useEffect(() => {
    const qrRegion = qrRegionRef.current;
    if (showQr && typeof qrRegion?.scrollIntoView === "function") {
      qrRegion.scrollIntoView({ block: "nearest" });
    }
  }, [showQr]);

  useEffect(() => {
    if (isConfirmedBankPaymentOrder(order) && previousStatus.current !== order.status) {
      confirmedHeadingRef.current?.focus();
    }
    previousStatus.current = order.status;
  }, [order]);

  async function copyPaymentUrl() {
    if (!paymentUrl || order.can_copy !== true) return false;
    try {
      await navigator.clipboard.writeText(paymentUrl);
      setCopyState("Ссылка скопирована");
      return true;
    } catch {
      setCopyState("Не удалось скопировать ссылку");
      return false;
    }
  }

  async function sharePaymentUrl() {
    if (!paymentUrl || order.can_share !== true) return;
    if (typeof navigator.share !== "function") {
      await copyPaymentUrl();
      return;
    }
    try {
      await navigator.share({
        title: "Оплата через СБП",
        text: "Ссылка для оплаты через СБП",
        url: paymentUrl,
      });
      setCopyState("");
    } catch {
      const copied = await copyPaymentUrl();
      if (!copied) setCopyState("Не удалось отправить ссылку");
    }
  }

  function handleCancelConfirm() {
    if (!canCancel || isCanceling) return;
    onCancel?.(order);
    setCancelSheetOpen(false);
  }

  return (
    <section
      aria-label={title}
      className={cn(
        "scroll-mb-24 space-y-3 rounded-xl bg-white/86 p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] ring-1 ring-black/8",
        className,
      )}
    >
      <div className="ui-row-between">
        <div className="min-w-0">
          <p className="break-words text-[13px] font-semibold text-foreground">{title}</p>
          {subtitle ? (
            <p className="mt-0.5 break-words text-[12px] text-muted-foreground">{subtitle}</p>
          ) : null}
          <p className="mt-0.5 break-words text-[12px] text-muted-foreground">
            Ссылка действует до {formatExpiresAt(order.expires_at)}
          </p>
        </div>
        <Badge variant="secondary" className="shrink-0">
          {statusLabel}
        </Badge>
      </div>

      <p aria-atomic="true" aria-live="polite" role="status" className="sr-only">
        {[statusLabel, copyState, fulfillmentCopy].filter(Boolean).join(". ")}
      </p>

      <div className="rounded-lg bg-black/[0.035] px-3 py-2">
        <p className="ui-muted-12">Назначение</p>
        <p className="mt-0.5 break-words text-[14px] font-semibold text-foreground">
          {order.purpose_snapshot || "Онлайн-оплата"}
        </p>
      </div>

      {order.renewed_from_subscription_id && (renewalSourceName || renewalTargetLabel) ? (
        <div className="space-y-1 rounded-lg bg-black/[0.035] px-3 py-2 text-[13px]">
          {renewalSourceName ? (
            <p className="break-words text-muted-foreground">
              Купленный абонемент: <span className="font-medium text-foreground">{renewalSourceName}</span>
            </p>
          ) : null}
          {renewalTargetLabel ? (
            <p className="break-words text-muted-foreground">
              Продление: <span className="font-medium text-foreground">{renewalTargetLabel}</span>
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="flex items-center justify-between gap-3 rounded-lg bg-black/[0.035] px-3 py-2">
        <span className="ui-muted-12">Сумма</span>
        <span className="ui-title-14">
          {order.currency === "RUB" ? formatRub(order.amount_snapshot) : `${order.amount_snapshot} ${order.currency}`}
        </span>
      </div>

      {hasSelfServiceAction ? (
        <a
          href={paymentUrl}
          onClick={() => {
            rememberPaymentResumeContext(order);
            onPay?.(order);
          }}
          className="inline-flex min-h-[44px] w-full items-center justify-center gap-2 rounded-xl bg-[var(--branding-accent)] px-4 text-[14px] font-semibold text-white transition active:scale-[0.99]"
        >
          <ExternalLink className="size-4" />
          Оплатить через СБП
        </a>
      ) : null}

      {hasStaffActions ? (
        <div className="grid min-w-0 grid-cols-1 gap-2">
          {order.can_share === true ? (
            <Button type="button" className="min-h-[44px]" onClick={sharePaymentUrl} wrap>
              <Send className="size-4" />
              Отправить ссылку
            </Button>
          ) : null}
          {order.can_copy === true ? (
            <Button type="button" variant="outline" className="min-h-[44px]" onClick={copyPaymentUrl} wrap>
              <Copy className="size-4" />
              Скопировать ссылку
            </Button>
          ) : null}
          {order.can_show_qr === true ? (
            <Button
              type="button"
              variant="outline"
              className="min-h-[44px] bg-white/70"
              onClick={() => setShowQr((current) => !current)}
              aria-expanded={showQr}
              aria-controls={qrRegionId}
              wrap
            >
              <QrCode className="size-4" />
              {showQr ? "Скрыть QR" : "Показать QR"}
            </Button>
          ) : null}
          <a
            href={paymentUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex min-h-[44px] items-center justify-center gap-2 rounded-xl border border-input bg-background px-4 text-[14px] font-semibold text-foreground"
          >
            <ExternalLink className="size-4" />
            Открыть предпросмотр
          </a>
          {showQr ? (
            <div
              ref={qrRegionRef}
              id={qrRegionId}
              className="flex scroll-mt-24 items-center justify-center rounded-xl bg-white p-3 ring-1 ring-black/8"
            >
              {qrSvg ? (
                <div
                  role="img"
                  aria-label="QR-код ссылки на оплату"
                  className="flex aspect-square w-32 items-center justify-center [&_svg]:h-full [&_svg]:w-full"
                  dangerouslySetInnerHTML={{ __html: qrSvg }}
                />
              ) : (
                <div className="flex min-h-32 items-center justify-center text-center text-[12px] leading-5 text-muted-foreground">
                  QR готовится
                </div>
              )}
            </div>
          ) : null}
        </div>
      ) : null}

      {!hasSelfServiceAction && !hasStaffActions ? (
        <div className="rounded-xl bg-black/[0.035] px-3 py-2 text-[13px] leading-5 text-muted-foreground">
          {isLive
            ? unavailableReason || "Онлайн-оплата сейчас недоступна. Обновите статус или обратитесь к сотруднику клуба."
            : (isLocallyExpired ? unavailableStatusCopy.expired : unavailableStatusCopy[order.status]) ||
              `Действия по ссылке недоступны. Статус оплаты: ${statusLabel}.`}
        </div>
      ) : null}

      {canRefreshOrder ? (
        <Button
          type="button"
          variant="outline"
          className="min-h-[44px] w-full"
          onClick={refreshHandler}
          disabled={isRefreshing}
          wrap
        >
          <RefreshCw className={cn("size-4", isRefreshing && "animate-spin")} />
          {isRefreshing
            ? "Обновляем статус..."
            : canRefreshFulfillment
              ? "Обновить данные"
              : "Обновить статус"}
        </Button>
      ) : null}

      {isConfirmedBankPaymentOrder(order) ? (
        <div className="space-y-1">
          <h3 ref={confirmedHeadingRef} tabIndex={-1} className="scroll-mt-24 text-[14px] font-semibold text-emerald-800">
            Оплата подтверждена
          </h3>
          {order.fulfillment_state === "fulfillment_pending" ? (
            <p className="text-[13px] leading-5 text-amber-800">
              {fulfillmentCopy}
            </p>
          ) : order.fulfillment_state === "fulfilled" ? (
            <p className="text-[13px] leading-5 text-emerald-800">
              {fulfillmentCopy}
            </p>
          ) : null}
        </div>
      ) : null}

      {canCancel ? (
        <Button
          type="button"
          variant="ghost"
          className="min-h-[44px] w-full text-red-700 hover:bg-red-50 hover:text-red-800"
          disabled={isCanceling}
          onClick={() => setCancelSheetOpen(true)}
          wrap
        >
          <XCircle className="size-4" />
          {isCanceling ? "Отмена..." : cancelLabel}
        </Button>
      ) : null}

      {canCancel ? (
        <Sheet open={cancelSheetOpen} onOpenChange={setCancelSheetOpen}>
          <SheetContent side="bottom" showCloseButton={false} className="rounded-t-2xl pb-[max(1.5rem,env(safe-area-inset-bottom))]">
            <SheetHeader>
              <SheetTitle>Отменить оплату?</SheetTitle>
              <SheetDescription>
                Отмена доступна только до отправки ссылки в банк. Уже выданную банком ссылку локальная отмена не отзывает.
              </SheetDescription>
            </SheetHeader>
            <SheetFooter className="flex-row gap-3 pt-0">
              <Button type="button" variant="outline" className="min-h-[44px] flex-1" disabled={isCanceling} onClick={() => setCancelSheetOpen(false)}>
                Назад
              </Button>
              <Button type="button" variant="destructive" className="min-h-[44px] flex-1" disabled={isCanceling} onClick={handleCancelConfirm} wrap>
                {isCanceling ? "Отмена..." : cancelLabel}
              </Button>
            </SheetFooter>
          </SheetContent>
        </Sheet>
      ) : null}
    </section>
  );
}

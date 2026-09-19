import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import apiClient from "@/api/custom-fetch";
import {
  getSelfServicePersonalCommandsQueryKey,
  selfServicePersonalCommandsQueryKey,
} from "@/api/self-service-personal";
import { useAuthStore } from "@/features/auth/auth-store";
import {
  OnlinePaymentLinkPanel,
  type BankPaymentOrderLink,
} from "@/components/portal/online-payment-link-panel";
import {
  clearPaymentResumeContext,
  getPaymentResumeContext,
  PAYMENT_RESUME_STORAGE_KEY,
} from "./payment-resume-context";

type PaymentReturnStatus =
  | "checking"
  | "approved"
  | "manual_review"
  | "failed"
  | "expired"
  | "cancelled"
  | "refunded"
  | "refunded_partially"
  | "unavailable";

interface PaymentReturnProjection {
  status: PaymentReturnStatus;
}

const statusCopy: Record<PaymentReturnStatus, { title: string; description: string }> = {
  checking: { title: "Проверяем оплату", description: "Банк мог уже принять платёж. Подтверждение появится только после проверки в клубе." },
  approved: { title: "Оплата подтверждена", description: "Статус обновлён по данным клуба." },
  manual_review: { title: "Оплата на проверке", description: "Новая попытка не нужна. Клуб завершит проверку по защищённым данным банка." },
  failed: { title: "Оплата не подтверждена", description: "Попробуйте другой способ или обратитесь в клуб." },
  expired: { title: "Срок ссылки истёк", description: "Откройте кабинет, чтобы проверить доступные способы оплаты." },
  cancelled: { title: "Ссылка отменена", description: "Откройте кабинет, чтобы проверить доступные способы оплаты." },
  refunded: { title: "Платёж возвращён", description: "Подробности доступны после входа в кабинет." },
  refunded_partially: { title: "Часть платежа возвращена", description: "Подробности доступны после входа в кабинет." },
  unavailable: { title: "Статус оплаты недоступен", description: "Откройте кабинет или обратитесь в клуб." },
};

const pollingDelays = [1_000, 2_000, 4_000, 6_000, 8_000] as const;
const PAYMENT_RETURN_BROWSER_BINDING_KEY = "jaguar-payment-return-browser";
export const PAYMENT_RETURN_PENDING_EXCHANGE_KEY = "jaguar-payment-return-pending";
const PENDING_EXCHANGE_TTL_MS = 30 * 60_000;

function normalizeStatus(value: unknown): PaymentReturnStatus {
  return typeof value === "string" && value in statusCopy
    ? (value as PaymentReturnStatus)
    : "unavailable";
}

function getOrCreateBrowserBinding(): string | null {
  try {
    const existing = localStorage.getItem(PAYMENT_RETURN_BROWSER_BINDING_KEY);
    if (existing && /^[A-Za-z0-9_-]{32,128}$/.test(existing)) return existing;
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    const raw = Array.from(bytes, (value) => String.fromCharCode(value)).join("");
    const binding = btoa(raw).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
    localStorage.setItem(PAYMENT_RETURN_BROWSER_BINDING_KEY, binding);
    return binding;
  } catch {
    return null;
  }
}

function rememberPendingExchange(state: string, browserBinding: string) {
  if (!/^[A-Za-z0-9_-]{16,1024}$/.test(state)) return;
  try {
    sessionStorage.setItem(
      PAYMENT_RETURN_PENDING_EXCHANGE_KEY,
      JSON.stringify({ state, browserBinding, expiresAt: Date.now() + PENDING_EXCHANGE_TTL_MS }),
    );
  } catch {
    // The URL is already sanitized; in-memory bounded retry remains available.
  }
}

function loadPendingExchange(browserBinding: string): string | null {
  try {
    const raw = sessionStorage.getItem(PAYMENT_RETURN_PENDING_EXCHANGE_KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw) as Record<string, unknown>;
    if (
      typeof stored.state !== "string" ||
      !/^[A-Za-z0-9_-]{16,1024}$/.test(stored.state) ||
      stored.browserBinding !== browserBinding ||
      typeof stored.expiresAt !== "number" ||
      stored.expiresAt <= Date.now()
    ) {
      sessionStorage.removeItem(PAYMENT_RETURN_PENDING_EXCHANGE_KEY);
      return null;
    }
    return stored.state;
  } catch {
    return null;
  }
}

function clearPendingExchange() {
  try {
    sessionStorage.removeItem(PAYMENT_RETURN_PENDING_EXCHANGE_KEY);
  } catch {
    // Best effort only; the stored record is browser-bound and expires.
  }
}

export default function PaymentReturnPage() {
  const location = useLocation();
  const queryClient = useQueryClient();
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const accessToken = useAuthStore((state) => state.accessToken);
  const authRole = useAuthStore((state) => state.role);
  const authClubId = useAuthStore((state) => state.clubId);
  const [status, setStatus] = useState<PaymentReturnStatus>("checking");
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState("");
  const [resumeMessage, setResumeMessage] = useState("");
  const exchangeStarted = useRef(false);
  const rawStateRef = useRef<string | null>(null);
  const latestStatus = useRef<PaymentReturnStatus>("checking");
  const statusRequestGeneration = useRef(0);
  const [manualCooldownUntil, setManualCooldownUntil] = useState(0);
  const [resumeAttempt, setResumeAttempt] = useState(0);
  const [resumeRetryExhausted, setResumeRetryExhausted] = useState(false);
  const [resumedOrder, setResumedOrder] = useState<BankPaymentOrderLink | null>(null);
  const [resumedRefreshEndpoint, setResumedRefreshEndpoint] = useState("");
  const confirmedHeadingRef = useRef<HTMLHeadingElement>(null);
  const previousStatus = useRef(status);

  const clearResumedOrderState = useCallback(() => {
    setResumedOrder(null);
    setResumedRefreshEndpoint("");
    setResumeMessage("");
    setResumeRetryExhausted(false);
    setResumeAttempt(0);
  }, []);

  const invalidateSelfServiceCommands = useCallback(
    (scope?: { audience: "student" | "parent"; childStudentId?: number }) =>
      queryClient.invalidateQueries({
        queryKey: scope
          ? getSelfServicePersonalCommandsQueryKey(scope)
          : selfServicePersonalCommandsQueryKey,
      }),
    [queryClient],
  );

  const invalidatePaymentReturnCommercialState = useCallback(() => {
    // A redirect is only a browser hint. Re-read each role-scoped server projection instead.
    return Promise.all([
      queryClient.invalidateQueries({ queryKey: ["student"] }),
      queryClient.invalidateQueries({ queryKey: ["parent", "child"] }),
      queryClient.invalidateQueries({ queryKey: ["billing", "bank-payment-orders"] }),
      queryClient.invalidateQueries({ queryKey: ["lead"] }),
    ]);
  }, [queryClient]);

  const updateStatus = useCallback((nextStatus: PaymentReturnStatus, publish = false) => {
    const changed = latestStatus.current !== nextStatus;
    latestStatus.current = nextStatus;
    setStatus(nextStatus);
    if (publish && changed && typeof BroadcastChannel !== "undefined") {
      const channel = new BroadcastChannel("jaguar-payment-return");
      // Cross-tab data is an untrusted browser signal, not payment evidence.
      // It may only prompt the other tab to re-read the server-scoped status.
      channel.postMessage({ type: "status-changed" });
      channel.close();
    }
  }, []);

  const readStatus = useCallback(async (publish = true) => {
    const requestGeneration = ++statusRequestGeneration.current;
    setIsRefreshing(true);
    try {
      const response = await apiClient.get<PaymentReturnProjection>("/billing/payment-returns/status/", {
        withCredentials: true,
      });
      const nextStatus = normalizeStatus(response.data?.status);
      if (requestGeneration !== statusRequestGeneration.current) return null;
      setRefreshError("");
      updateStatus(nextStatus, publish);
      void invalidateSelfServiceCommands();
      void invalidatePaymentReturnCommercialState();
      return nextStatus;
    } catch (error) {
      if (requestGeneration !== statusRequestGeneration.current) return null;
      const statusCode = (error as { response?: { status?: number } })?.response?.status;
      if (statusCode === 404) updateStatus("unavailable", publish);
      else setRefreshError("Не удалось обновить статус. Повторим проверку автоматически.");
      return null;
    } finally {
      if (requestGeneration === statusRequestGeneration.current) setIsRefreshing(false);
    }
  }, [invalidatePaymentReturnCommercialState, invalidateSelfServiceCommands, updateStatus]);

  useEffect(() => {
    const queryState = new URLSearchParams(location.search).get("state");
    const browserBinding = getOrCreateBrowserBinding();
    const rawState = queryState || (browserBinding ? loadPendingExchange(browserBinding) : null);
    if (queryState) {
      if (browserBinding) rememberPendingExchange(queryState, browserBinding);
      window.history.replaceState(window.history.state, "", "/payments/return");
    }
    if (rawState) rawStateRef.current = rawState;
    if (exchangeStarted.current) return;
    exchangeStarted.current = true;
    if (!rawState) {
      void readStatus(false);
      return;
    }
    if (!browserBinding) {
      setRefreshError("Не удалось безопасно открыть возврат оплаты. Обновите страницу или обратитесь в клуб.");
      updateStatus("unavailable", false);
      return;
    }
    let cancelled = false;
    let retryTimer: number | undefined;
    const exchange = (attempt: number) => {
      const state = rawStateRef.current;
      if (!state || cancelled) return;
      void apiClient
        .post<PaymentReturnProjection>(
          "/billing/payment-returns/exchange/",
          { state, browser_binding: browserBinding },
          { withCredentials: true },
        )
        .then((response) => {
          if (cancelled) return;
          clearPendingExchange();
          statusRequestGeneration.current += 1;
          setIsRefreshing(false);
          setRefreshError("");
          updateStatus(normalizeStatus(response.data?.status), true);
          void invalidateSelfServiceCommands();
          void invalidatePaymentReturnCommercialState();
        })
        .catch(() => {
          if (cancelled) return;
          if (attempt < 2) {
            retryTimer = window.setTimeout(() => exchange(attempt + 1), pollingDelays[attempt]);
          } else {
            setRefreshError("Не удалось связать возврат с этой оплатой. Повторите позже или откройте кабинет.");
            updateStatus("unavailable", false);
          }
        });
    };
    exchange(0);
    return () => {
      cancelled = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [
    invalidatePaymentReturnCommercialState,
    invalidateSelfServiceCommands,
    location.search,
    readStatus,
    updateStatus,
  ]);

  useEffect(() => {
    const handlePageShow = () => void readStatus();
    const handleVisibility = () => {
      if (document.visibilityState === "visible") void readStatus();
    };
    const handleFocus = () => void readStatus();
    window.addEventListener("pageshow", handlePageShow);
    window.addEventListener("focus", handleFocus);
    document.addEventListener("visibilitychange", handleVisibility);
    const channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("jaguar-payment-return");
    if (channel) {
      channel.onmessage = (event) => {
        if ((event.data as { type?: unknown } | null)?.type === "status-changed") {
          void readStatus(false);
        }
      };
    }
    return () => {
      window.removeEventListener("pageshow", handlePageShow);
      window.removeEventListener("focus", handleFocus);
      document.removeEventListener("visibilitychange", handleVisibility);
      channel?.close();
    };
  }, [readStatus, updateStatus]);

  useEffect(() => {
    if (status !== "checking" && status !== "manual_review") return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = (index: number) => {
      if (index >= pollingDelays.length || cancelled) return;
      timer = window.setTimeout(async () => {
        const nextStatus = await readStatus();
        if (nextStatus === "checking" || nextStatus === "manual_review") poll(index + 1);
        if (nextStatus === null) poll(index + 1);
      }, pollingDelays[index]);
    };
    poll(0);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [readStatus, status]);

  useEffect(() => {
    if (!manualCooldownUntil) return;
    const delay = manualCooldownUntil - Date.now();
    if (delay <= 0) {
      setManualCooldownUntil(0);
      return;
    }
    const timer = window.setTimeout(() => setManualCooldownUntil(0), delay);
    return () => window.clearTimeout(timer);
  }, [manualCooldownUntil]);

  useEffect(() => {
    const clearWhenResumeContextIsUnavailable = () => {
      if (!isAuthenticated || !accessToken || !getPaymentResumeContext()) {
        clearResumedOrderState();
      }
    };
    clearWhenResumeContextIsUnavailable();
    const handleStorage = (event: StorageEvent) => {
      if (event.key === PAYMENT_RESUME_STORAGE_KEY || event.key === null) {
        clearWhenResumeContextIsUnavailable();
      }
    };
    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, [accessToken, authClubId, authRole, clearResumedOrderState, isAuthenticated]);

  useEffect(() => {
    if (!isAuthenticated || !accessToken) {
      clearResumedOrderState();
      return;
    }
    if (status === "checking") return;
    const context = getPaymentResumeContext();
    if (!context) {
      clearResumedOrderState();
      return;
    }
    setResumedRefreshEndpoint(context.refreshEndpoint);
    let cancelled = false;
    let retryTimer: number | undefined;
    const retryResume = () => {
      if (resumeAttempt >= pollingDelays.length - 1) {
        setResumeRetryExhausted(true);
        return false;
      }
      setResumeRetryExhausted(false);
      retryTimer = window.setTimeout(
        () => setResumeAttempt((current) => current + 1),
        pollingDelays[resumeAttempt],
      );
      return true;
    };
    void apiClient
      .get<BankPaymentOrderLink>(context.endpoint)
      .then(async (response) => {
        if (cancelled) return;
        const order = response.data;
        setResumedOrder(order);
        if (context.role === "student") {
          await queryClient.invalidateQueries({ queryKey: ["student"] });
        } else if (context.role === "parent") {
          await queryClient.invalidateQueries({ queryKey: ["parent", "child"] });
        }
        if (context.role === "student") {
          await invalidateSelfServiceCommands({ audience: "student" });
        } else if (context.role === "parent" && context.childId) {
          await invalidateSelfServiceCommands({
            audience: "parent",
            childStudentId: context.childId,
          });
        }
        await queryClient.invalidateQueries({ queryKey: ["payment-reservation"] });
        if (cancelled) return;
        if (order.status === "approved" && order.fulfillment_state === "fulfillment_pending") {
          const willRetry = retryResume();
          setResumeMessage(
            willRetry
              ? "Оплата подтверждена. Абонемент или запись ещё обновляются — проверим снова автоматически."
              : "Оплата подтверждена, но данные кабинета ещё обновляются. Автоматическая проверка завершена — обновите данные кабинета вручную.",
          );
          return;
        }
        setResumeRetryExhausted(false);
        if (order.status === "approved" && order.fulfillment_state === "fulfilled") {
          setResumeMessage("Оплата и связанные данные кабинета подтверждены.");
          clearPaymentResumeContext();
          clearResumedOrderState();
        } else if (order.status === "manual_review") {
          setResumeMessage("Оплата находится на защищённой сверке. Новая попытка не нужна.");
        } else if (["created", "pending", "authorized"].includes(order.status)) {
          setResumeMessage("Точный статус оплаты обновлён в кабинете. Проверка продолжается.");
        } else {
          setResumeMessage("Точный статус оплаты обновлён в кабинете.");
          clearPaymentResumeContext();
          clearResumedOrderState();
        }
      })
      .catch((error) => {
        if (cancelled) return;
        const statusCode = (error as { response?: { status?: number } })?.response?.status;
        if (statusCode === 403 || statusCode === 404) {
          setResumeRetryExhausted(false);
          clearPaymentResumeContext();
          clearResumedOrderState();
          return;
        }
        const willRetry = retryResume();
        setResumeMessage(
          willRetry
            ? "Не удалось обновить подробности. Повторим после восстановления связи."
            : "Не удалось обновить подробности автоматически. Проверьте связь и обновите данные кабинета вручную.",
        );
      });
    return () => {
      cancelled = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [
    accessToken,
    authClubId,
    authRole,
    clearResumedOrderState,
    invalidateSelfServiceCommands,
    isAuthenticated,
    queryClient,
    resumeAttempt,
    status,
  ]);

  useEffect(() => {
    if (status === "approved" && previousStatus.current !== "approved") confirmedHeadingRef.current?.focus();
    previousStatus.current = status;
  }, [status]);

  const copy = statusCopy[status];
  const canManuallyRefresh = Date.now() >= manualCooldownUntil;
  return (
    <main className="flex min-h-dvh items-center justify-center bg-background px-4 pb-[max(1rem,env(safe-area-inset-bottom))] pt-4">
      <section aria-label="Статус оплаты" className="w-full max-w-md space-y-4 rounded-2xl bg-card p-5 shadow-sm ring-1 ring-border">
        <p aria-atomic="true" aria-live="polite" role="status" className="sr-only">{copy.title}</p>
        <h1 ref={confirmedHeadingRef} tabIndex={-1} className="scroll-mt-24 text-xl font-semibold text-foreground">{copy.title}</h1>
        <p className="text-sm leading-6 text-muted-foreground">{copy.description}</p>
        {refreshError ? <p role="status" className="text-sm text-amber-800">{refreshError}</p> : null}
        {resumeMessage ? <p className="text-sm text-muted-foreground">{resumeMessage}</p> : null}
        {resumedOrder ? (
          <OnlinePaymentLinkPanel
            order={resumedOrder}
            title="Последняя онлайн-оплата"
            onRefresh={() => setResumeAttempt((current) => current + 1)}
            onRequestRefresh={() => {
              if (!resumedRefreshEndpoint) return;
              void apiClient
                .post(resumedRefreshEndpoint, {})
                .finally(() => setResumeAttempt((current) => current + 1));
            }}
          />
        ) : null}
        {resumeRetryExhausted ? (
          <button
            type="button"
            className="min-h-[44px] w-full rounded-xl border border-input px-4 text-sm font-semibold text-foreground"
            onClick={() => {
              setResumeRetryExhausted(false);
              setResumeMessage("Обновляем точные данные оплаты и кабинета...");
              setResumeAttempt(0);
            }}
          >
            Обновить данные кабинета
          </button>
        ) : null}
        <button
          type="button"
          className="min-h-[44px] w-full rounded-xl border border-input px-4 text-sm font-semibold text-foreground disabled:opacity-50"
          disabled={isRefreshing || !canManuallyRefresh}
          onClick={() => {
            setManualCooldownUntil(Date.now() + 3_000);
            void readStatus();
          }}
        >
          {isRefreshing ? "Обновляем статус..." : "Обновить статус"}
        </button>
        <Link to={isAuthenticated ? "/app" : "/login"} state={isAuthenticated ? undefined : { from: { pathname: "/payments/return" } }} className="inline-flex min-h-[44px] w-full items-center justify-center rounded-xl bg-[var(--branding-accent)] px-4 text-sm font-semibold text-white">
          {isAuthenticated ? "Открыть кабинет" : "Войти в кабинет"}
        </Link>
      </section>
    </main>
  );
}

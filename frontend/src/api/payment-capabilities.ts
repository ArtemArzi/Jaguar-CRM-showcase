import { useEffect, useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import apiClient from "@/api/custom-fetch";
import { useAuthStore } from "@/features/auth/auth-store";
import { decodeJwtPayload } from "@/lib/jwt";

export interface PaymentCapabilities {
  online_payments_enabled: boolean;
  payment_mode?: "sbp";
  payment_modes?: readonly "sbp"[];
  payment_creation_enabled?: boolean;
  payment_reconciliation_enabled?: boolean;
  payment_unavailable_reason?: string;
  can_create_payment_order?: boolean;
  can_request_payment_reconciliation?: boolean;
  training_group_rollout_mode?: string;
  training_group_payment_selection_mode?: TrainingGroupPaymentSelectionMode;
  canonical_group_selection_enabled?: boolean;
}

export const ENABLED_SBP_PAYMENT_CAPABILITIES = {
  online_payments_enabled: true,
  payment_mode: "sbp",
  payment_modes: ["sbp"],
  payment_creation_enabled: true,
  can_create_payment_order: true,
} as const satisfies PaymentCapabilities;

export type TrainingGroupPaymentSelectionMode = "legacy" | "canonical" | "disabled";

export const paymentCapabilitiesQueryKey = ["billing", "payment-capabilities"] as const;

export interface PaymentCapabilitiesActorScope {
  clubId: number;
  actorSubject: string;
}

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function actorSubjectFromToken(token: string | null): string | null {
  if (!token) return null;
  const payload = decodeJwtPayload(token);
  const subject = payload?.sub ?? payload?.user_id;
  if (typeof subject === "string" && subject.length > 0 && subject.length <= 128) return subject;
  if (isPositiveSafeInteger(subject)) return String(subject);
  return null;
}

function toPaymentCapabilitiesActorScope({
  accessToken,
  clubId,
  isAuthenticated,
}: Pick<
  ReturnType<typeof useAuthStore.getState>,
  "accessToken" | "clubId" | "isAuthenticated"
>): PaymentCapabilitiesActorScope | null {
  const actorSubject = actorSubjectFromToken(accessToken);
  if (!isAuthenticated || !isPositiveSafeInteger(clubId) || !actorSubject) return null;
  return { clubId, actorSubject };
}

export function getCurrentPaymentCapabilitiesActorScope(): PaymentCapabilitiesActorScope | null {
  return toPaymentCapabilitiesActorScope(useAuthStore.getState());
}

export function getPaymentCapabilitiesQueryKey(
  actorScope: PaymentCapabilitiesActorScope | null = getCurrentPaymentCapabilitiesActorScope(),
) {
  return [
    ...paymentCapabilitiesQueryKey,
    actorScope?.clubId ?? "no-club",
    actorScope?.actorSubject ?? "unauthenticated",
  ] as const;
}

function usePaymentCapabilitiesActorScope(): PaymentCapabilitiesActorScope | null {
  const accessToken = useAuthStore((state) => state.accessToken);
  const clubId = useAuthStore((state) => state.clubId);
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  return useMemo(
    () => toPaymentCapabilitiesActorScope({ accessToken, clubId, isAuthenticated }),
    [accessToken, clubId, isAuthenticated],
  );
}

function useClearStalePaymentCapabilities(actorScope: PaymentCapabilitiesActorScope | null) {
  const queryClient = useQueryClient();
  useEffect(() => {
    queryClient.removeQueries({
      predicate: (query) => {
        if (
          query.queryKey[0] !== paymentCapabilitiesQueryKey[0] ||
          query.queryKey[1] !== paymentCapabilitiesQueryKey[1]
        ) {
          return false;
        }
        if (!actorScope) return true;
        return (
          query.queryKey[2] !== actorScope.clubId ||
          query.queryKey[3] !== actorScope.actorSubject
        );
      },
    });
  }, [actorScope, queryClient]);
}

export function usePaymentCapabilities() {
  const actorScope = usePaymentCapabilitiesActorScope();
  useClearStalePaymentCapabilities(actorScope);
  return useQuery({
    queryKey: getPaymentCapabilitiesQueryKey(actorScope),
    queryFn: async () =>
      (await apiClient.get<PaymentCapabilities>("/billing/payment-capabilities/")).data,
    enabled: actorScope !== null,
    staleTime: 30_000,
  });
}

export function hasCanonicalTrainingGroupSelectionCapability(query: {
  data?: unknown;
  isSuccess: boolean;
  isRefetchError?: boolean;
}) {
  return getTrainingGroupPaymentSelectionMode(query) === "canonical";
}

export function getTrainingGroupPaymentSelectionMode(query: {
  data?: unknown;
  isSuccess: boolean;
  isRefetchError?: boolean;
}): TrainingGroupPaymentSelectionMode {
  const data = query.data as Partial<PaymentCapabilities> | undefined;
  if (!query.isSuccess || query.isRefetchError) {
    return "disabled";
  }
  const mode = data?.training_group_payment_selection_mode;
  return mode === "legacy" || mode === "canonical" || mode === "disabled"
    ? mode
    : "disabled";
}

export function hasOnlinePaymentsCapability(query: {
  data?: unknown;
  isSuccess: boolean;
  isRefetchError?: boolean;
}) {
  const data = query.data as Partial<PaymentCapabilities> | undefined;
  return (
    query.isSuccess &&
    !query.isRefetchError &&
    data?.online_payments_enabled === true &&
    data.payment_mode === "sbp" &&
    Array.isArray(data.payment_modes) &&
    data.payment_modes.length === 1 &&
    data.payment_modes[0] === "sbp" &&
    data.payment_creation_enabled === true &&
    data.can_create_payment_order === true
  );
}

export function getOnlinePaymentUnavailableMessage(
  query: {
    data?: unknown;
    isSuccess: boolean;
    isRefetchError?: boolean;
  },
  audience: "staff" | "self_service",
) {
  if (hasOnlinePaymentsCapability(query)) return "";
  if (audience === "staff") {
    const data = query.data as Partial<PaymentCapabilities> | undefined;
    const reason = data?.payment_unavailable_reason || "online_payment_unavailable";
    const staffReasons: Record<string, string> = {
      mock_provider: "СБП работает только в тестовом режиме.",
      unknown_provider: "Платёжный провайдер не настроен.",
      api_origin_invalid: "Адрес API банка настроен некорректно.",
      credentials_missing: "Не настроены реквизиты доступа к банку.",
      merchant_identity_missing: "Не настроена торговая точка СБП.",
      payment_mode_invalid: "На сервере разрешён неподдерживаемый способ оплаты.",
      webhook_verification_not_ready: "Защищённое подтверждение банка ещё не готово.",
      return_origin_invalid: "Адрес возврата после оплаты настроен некорректно.",
      fiscalization_undecided: "Не завершена настройка чеков.",
      retailer_readback_missing: "Нет подтверждённой проверки торговой точки.",
      retailer_readback_stale: "Проверка торговой точки устарела.",
      retailer_not_ready: "Торговая точка пока не готова принимать СБП.",
      creation_disabled: "Создание новых ссылок временно отключено.",
      reconciliation_disabled: "Защищённая сверка платежей временно отключена.",
      online_payment_unavailable: "Онлайн-оплата сейчас недоступна.",
    };
    return staffReasons[reason] || "Онлайн-оплата сейчас недоступна. Проверьте готовность интеграции.";
  }
  return "Онлайн-оплата сейчас недоступна. Выберите другой способ или обратитесь в клуб.";
}

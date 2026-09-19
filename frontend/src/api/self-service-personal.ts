import { useEffect, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import apiClient from "@/api/custom-fetch";
import { useAuthStore } from "@/features/auth/auth-store";
import { toDateParamInTimeZone } from "@/lib/club-date";
import { decodeJwtPayload } from "@/lib/jwt";
import { PRIVATE_QUERY_SCOPE_MARKER } from "@/api/private-query-cache";

export type SelfServicePersonalCapability = "can_book" | "can_pay";

export interface SelfServicePersonalOption {
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
  capability: SelfServicePersonalCapability;
  offer_tariff_name: string;
  offer_price: string | number | null;
  offer_digest: string;
}

export type SelfServicePersonalAllowedAction =
  | "open_bank_payment_order"
  | "cancel_bank_payment_order"
  | "retry_bank_payment"
  | "view_booking";

export interface SelfServicePersonalCommandCard {
  command_id: number;
  slot_id: number;
  capability: SelfServicePersonalCapability;
  status: string;
  starts_at: string;
  ends_at: string;
  booking_id?: number | null;
  reservation_id?: number | null;
  bank_payment_order_id?: number | null;
  provider_payment_url: string;
  amount_snapshot: string;
  order_status: string;
  allowed_actions: readonly SelfServicePersonalAllowedAction[];
}

export interface SelfServicePersonalCommands {
  live: SelfServicePersonalCommandCard[];
  latest_terminal: SelfServicePersonalCommandCard[];
}

export interface SelfServicePersonalScope {
  audience: "student" | "parent";
  childStudentId?: number | null;
}

export interface SelfServicePersonalActorScope {
  clubId: number;
  actorSubject: string;
  role: "student" | "parent";
}

const SELF_SERVICE_PERSONAL_BASE_PATH = "/personal-availability/self-service";
const COMMAND_KEY_STORAGE_KEY = "jaguar-self-service-personal-command-keys";
const COMMAND_KEY_TTL_MS = 2 * 60 * 60_000;
const inMemoryCommandKeys = new Map<string, { key: string; expiresAt: number }>();

export const selfServicePersonalCommandsQueryKey = [
  "personal-availability",
  "self-service",
  "commands",
] as const;

export const selfServicePersonalQueryKey = [
  "personal-availability",
  "self-service",
] as const;

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

function toSelfServicePersonalActorScope({
  accessToken,
  clubId,
  isAuthenticated,
  role,
}: Pick<
  ReturnType<typeof useAuthStore.getState>,
  "accessToken" | "clubId" | "isAuthenticated" | "role"
>): SelfServicePersonalActorScope | null {
  const actorSubject = actorSubjectFromToken(accessToken);
  if (
    !isAuthenticated ||
    !isPositiveSafeInteger(clubId) ||
    !actorSubject ||
    (role !== "student" && role !== "parent")
  ) {
    return null;
  }
  return { clubId, actorSubject, role };
}

export function getCurrentSelfServicePersonalActorScope(): SelfServicePersonalActorScope | null {
  return toSelfServicePersonalActorScope(useAuthStore.getState());
}

function isCurrentSelfServicePersonalActorScope(actorScope: SelfServicePersonalActorScope | null) {
  const current = getCurrentSelfServicePersonalActorScope();
  return (
    actorScope !== null &&
    current?.clubId === actorScope.clubId &&
    current?.actorSubject === actorScope.actorSubject &&
    current?.role === actorScope.role
  );
}

function useSelfServicePersonalActorScope(): SelfServicePersonalActorScope | null {
  const accessToken = useAuthStore((state) => state.accessToken);
  const clubId = useAuthStore((state) => state.clubId);
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const role = useAuthStore((state) => state.role);
  return useMemo(
    () => toSelfServicePersonalActorScope({ accessToken, clubId, isAuthenticated, role }),
    [accessToken, clubId, isAuthenticated, role],
  );
}

function isSelfServicePersonalQueryKey(queryKey: readonly unknown[]) {
  return (
    queryKey[0] === selfServicePersonalQueryKey[0] &&
    queryKey[1] === selfServicePersonalQueryKey[1] &&
    (queryKey[2] === "commands" ||
      queryKey[2] === "options" ||
      queryKey[2] === "bank-payment-order")
  );
}

function useClearStaleSelfServicePersonalQueries(
  actorScope: SelfServicePersonalActorScope | null,
  queryClient: ReturnType<typeof useQueryClient>,
) {
  useEffect(() => {
    queryClient.removeQueries({
      predicate: (query) => {
        if (!isSelfServicePersonalQueryKey(query.queryKey)) return false;
        if (!actorScope) return true;
        return (
          query.queryKey[3] !== PRIVATE_QUERY_SCOPE_MARKER ||
          query.queryKey[4] !== actorScope.clubId ||
          query.queryKey[5] !== actorScope.actorSubject ||
          query.queryKey[7] !== actorScope.role
        );
      },
    });
  }, [actorScope, queryClient]);
}

export function getSelfServicePersonalCommandsQueryKey(
  scope: SelfServicePersonalScope,
  actorScope: SelfServicePersonalActorScope | null = getCurrentSelfServicePersonalActorScope(),
) {
  return [
    ...selfServicePersonalCommandsQueryKey,
    PRIVATE_QUERY_SCOPE_MARKER,
    actorScope?.clubId ?? "no-club",
    actorScope?.actorSubject ?? "unauthenticated",
    scope.audience,
    actorScope?.role ?? "no-role",
    scope.childStudentId ?? "self",
  ] as const;
}

export function getSelfServicePersonalOptionsQueryKey(
  scope: SelfServicePersonalScope,
  date: string,
  actorScope: SelfServicePersonalActorScope | null = getCurrentSelfServicePersonalActorScope(),
) {
  return [
    ...selfServicePersonalQueryKey,
    "options",
    PRIVATE_QUERY_SCOPE_MARKER,
    actorScope?.clubId ?? "no-club",
    actorScope?.actorSubject ?? "unauthenticated",
    scope.audience,
    actorScope?.role ?? "no-role",
    scope.childStudentId ?? "self",
    date,
  ] as const;
}

function scopeParams(scope: SelfServicePersonalScope) {
  return scope.audience === "parent" && scope.childStudentId
    ? { child_student_id: scope.childStudentId }
    : undefined;
}

function commandBody({
  scope,
  idempotencyKey,
  offerDigest,
}: {
  scope: SelfServicePersonalScope;
  idempotencyKey: string;
  offerDigest?: string;
}) {
  return {
    ...(scope.audience === "parent" && scope.childStudentId
      ? { child_student_id: scope.childStudentId }
      : {}),
    ...(offerDigest ? { offer_digest: offerDigest } : {}),
    idempotency_key: idempotencyKey,
  };
}

export async function fetchSelfServicePersonalOptions({
  scope,
  date,
}: {
  scope: SelfServicePersonalScope;
  date: string;
}) {
  const response = await apiClient.get<SelfServicePersonalOption[]>(
    `${SELF_SERVICE_PERSONAL_BASE_PATH}/options/`,
    { params: { date, ...scopeParams(scope) } },
  );
  return response.data;
}

export async function fetchSelfServicePersonalCommands(scope: SelfServicePersonalScope) {
  const response = await apiClient.get<SelfServicePersonalCommands>(
    `${SELF_SERVICE_PERSONAL_BASE_PATH}/commands/`,
    { params: scopeParams(scope) },
  );
  return response.data;
}

function createCommandKey() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `personal-self-service-${crypto.randomUUID()}`;
  }
  return `personal-self-service-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function commandKeyScope(scope: SelfServicePersonalScope) {
  const auth = useAuthStore.getState();
  const actorScope = toSelfServicePersonalActorScope(auth);
  return `${actorScope?.clubId ?? "club"}:${scope.audience}:${actorScope?.role ?? "no-role"}:${actorScope?.actorSubject ?? "unauthenticated"}:${scope.childStudentId ?? "self"}`;
}

function readStoredCommandKeys() {
  const now = Date.now();
  const collected = new Map(inMemoryCommandKeys);
  try {
    const raw = localStorage.getItem(COMMAND_KEY_STORAGE_KEY);
    if (raw) {
      const stored = JSON.parse(raw) as Record<string, { key?: unknown; expiresAt?: unknown }>;
      Object.entries(stored).forEach(([identity, value]) => {
        if (
          typeof value.key === "string" &&
          value.key.length <= 120 &&
          typeof value.expiresAt === "number" &&
          value.expiresAt > now
        ) {
          collected.set(identity, { key: value.key, expiresAt: value.expiresAt });
        }
      });
    }
  } catch {
    // The in-memory record still protects an ambiguous click in this session.
  }
  for (const [identity, value] of collected) {
    if (value.expiresAt <= now) collected.delete(identity);
  }
  return collected;
}

function writeStoredCommandKeys(keys: Map<string, { key: string; expiresAt: number }>) {
  inMemoryCommandKeys.clear();
  keys.forEach((value, identity) => inMemoryCommandKeys.set(identity, value));
  try {
    localStorage.setItem(
      COMMAND_KEY_STORAGE_KEY,
      JSON.stringify(Object.fromEntries(keys)),
    );
  } catch {
    // Persistent storage is best effort; the in-memory key remains replay-safe per tab.
  }
}

function persistentCommandKey(identity: string) {
  const keys = readStoredCommandKeys();
  const existing = keys.get(identity);
  if (existing) return existing.key;
  const key = createCommandKey();
  keys.set(identity, { key, expiresAt: Date.now() + COMMAND_KEY_TTL_MS });
  writeStoredCommandKeys(keys);
  return key;
}

function clearPersistentCommandKey(identity: string) {
  const keys = readStoredCommandKeys();
  if (!keys.delete(identity)) return;
  writeStoredCommandKeys(keys);
}

function isPersonalOfferChanged(error: unknown) {
  return (
    error &&
    typeof error === "object" &&
    (error as { response?: { data?: { code?: unknown } } }).response?.data?.code ===
      "personal_offer_changed"
  );
}

function optionCommandIdentity(scope: SelfServicePersonalScope, option: SelfServicePersonalOption) {
  return `${commandKeyScope(scope)}:slot:${option.slot_id}:${option.capability}:${option.offer_digest}`;
}

function retryCommandIdentity(
  scope: SelfServicePersonalScope,
  card: SelfServicePersonalCommandCard,
  option: SelfServicePersonalOption,
) {
  return `${commandKeyScope(scope)}:retry:${card.command_id}:${option.slot_id}:${option.offer_digest}`;
}

export function useSelfServicePersonalCommands({
  scope,
  enabled,
}: {
  scope: SelfServicePersonalScope;
  enabled: boolean;
}) {
  const actorScope = useSelfServicePersonalActorScope();
  return useQuery({
    queryKey: getSelfServicePersonalCommandsQueryKey(scope, actorScope),
    queryFn: () => fetchSelfServicePersonalCommands(scope),
    enabled: enabled && actorScope !== null,
    staleTime: 30_000,
    retry: false,
  });
}

export function useSelfServicePersonalJourney({
  scope,
  date,
  enabled,
  onlinePaymentsEnabled,
  loadOptions = true,
  timeZone,
}: {
  scope: SelfServicePersonalScope;
  date: string;
  enabled: boolean;
  onlinePaymentsEnabled: boolean;
  loadOptions?: boolean;
  timeZone?: string | null;
}) {
  const queryClient = useQueryClient();
  const actorScope = useSelfServicePersonalActorScope();
  useClearStaleSelfServicePersonalQueries(actorScope, queryClient);
  const optionsQueryKey = getSelfServicePersonalOptionsQueryKey(scope, date, actorScope);
  const commandsQueryKey = getSelfServicePersonalCommandsQueryKey(scope, actorScope);
  const optionsQuery = useQuery({
    queryKey: optionsQueryKey,
    queryFn: () => fetchSelfServicePersonalOptions({ scope, date }),
    enabled: enabled && actorScope !== null && loadOptions && Boolean(date),
    staleTime: 30_000,
    retry: false,
  });
  const commandsQuery = useSelfServicePersonalCommands({ scope, enabled: enabled && actorScope !== null });

  const invalidateJourney = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: commandsQueryKey }),
      queryClient.invalidateQueries({ queryKey: optionsQueryKey }),
    ]);
  };

  const refetchStaleOption = async (
    option: SelfServicePersonalOption,
    requestDate = option.date,
  ) => {
    const refreshedOptions = await fetchSelfServicePersonalOptions({
      scope,
      date: requestDate,
    });
    if (!isCurrentSelfServicePersonalActorScope(actorScope)) return refreshedOptions;
    queryClient.setQueryData(
      getSelfServicePersonalOptionsQueryKey(scope, requestDate, actorScope),
      refreshedOptions,
    );
    // A completed authoritative options read proves that the rejected command
    // fingerprint is stale. Until that read succeeds, retain it for safe replay.
    clearPersistentCommandKey(optionCommandIdentity(scope, option));
    return refreshedOptions;
  };

  const commandMutation = useMutation({
    mutationFn: async ({
      option,
    }: {
      option: SelfServicePersonalOption;
    }) => {
      if (option.capability !== "can_book" && option.capability !== "can_pay") {
        throw new Error("Personal option capability is unavailable");
      }
      if (option.capability === "can_pay" && !onlinePaymentsEnabled) {
        throw new Error("Online payments are unavailable");
      }
      const commandIdentity = optionCommandIdentity(scope, option);
      try {
        const response = await apiClient.post<SelfServicePersonalCommandCard>(
          `${SELF_SERVICE_PERSONAL_BASE_PATH}/slots/${option.slot_id}/command/`,
          commandBody({
            scope,
            idempotencyKey: persistentCommandKey(commandIdentity),
            offerDigest: option.capability === "can_pay" ? option.offer_digest : undefined,
          }),
        );
        return { card: response.data, commandIdentity };
      } catch (error) {
        if (isPersonalOfferChanged(error)) {
          try {
            await refetchStaleOption(option, date);
          } catch {
            // Keep the server's stale-offer result visible; its key remains replay-safe.
          }
        }
        throw error;
      }
    },
    onSuccess: async ({ commandIdentity }) => {
      clearPersistentCommandKey(commandIdentity);
      await invalidateJourney();
    },
  });

  const cancelMutation = useMutation({
    mutationFn: async (card: SelfServicePersonalCommandCard) => {
      const response = await apiClient.post<SelfServicePersonalCommandCard>(
        `${SELF_SERVICE_PERSONAL_BASE_PATH}/commands/${card.command_id}/cancel/`,
        scopeParams(scope) ?? {},
      );
      return response.data;
    },
    onSuccess: invalidateJourney,
  });

  const retryMutation = useMutation({
    mutationFn: async (card: SelfServicePersonalCommandCard) => {
      if (!onlinePaymentsEnabled) {
        throw new Error("Online payments are unavailable");
      }
      if (!timeZone) {
        throw new Error("Club time zone is unavailable");
      }
      const retryDate = toDateParamInTimeZone(new Date(card.starts_at), timeZone);
      const freshOptions = await fetchSelfServicePersonalOptions({ scope, date: retryDate });
      if (!isCurrentSelfServicePersonalActorScope(actorScope)) {
        throw new Error("Self-service actor scope changed");
      }
      queryClient.setQueryData(
        getSelfServicePersonalOptionsQueryKey(scope, retryDate, actorScope),
        freshOptions,
      );
      const freshOption = freshOptions.find(
        (option) =>
          option.slot_id === card.slot_id &&
          option.date === retryDate &&
          option.capability === "can_pay",
      );
      if (!freshOption?.offer_digest) {
        throw new Error("Personal payment option is no longer available");
      }
      const commandIdentity = retryCommandIdentity(scope, card, freshOption);
      try {
        const response = await apiClient.post<SelfServicePersonalCommandCard>(
          `${SELF_SERVICE_PERSONAL_BASE_PATH}/slots/${freshOption.slot_id}/command/`,
          commandBody({
            scope,
            idempotencyKey: persistentCommandKey(commandIdentity),
            offerDigest: freshOption.offer_digest,
          }),
        );
        return { card: response.data, commandIdentity };
      } catch (error) {
        if (isPersonalOfferChanged(error)) {
          try {
            await refetchStaleOption(freshOption, retryDate);
          } catch {
            // Keep the server's stale-offer result visible; its key remains replay-safe.
          }
        }
        throw error;
      }
    },
    onSuccess: async ({ commandIdentity }) => {
      clearPersistentCommandKey(commandIdentity);
      await invalidateJourney();
    },
  });

  return {
    optionsQuery,
    commandsQuery,
    commandMutation,
    cancelMutation,
    retryMutation,
  };
}

export function createStableCommandKeyFactory(scope?: SelfServicePersonalScope) {
  const keys = new Map<string, string>();
  return (option: SelfServicePersonalOption) => {
    const identity = `${option.slot_id}:${option.capability}:${option.offer_digest}`;
    const existing = keys.get(identity);
    if (existing) return existing;
    const next = scope
      ? persistentCommandKey(optionCommandIdentity(scope, option))
      : createCommandKey();
    keys.set(identity, next);
    return next;
  };
}

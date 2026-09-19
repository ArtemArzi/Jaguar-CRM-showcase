export const CONTEXTUAL_COMMERCIAL_COMMAND_STORAGE_PREFIX = "jaguar-contextual-commercial-command";
const memoryKeys = new Map<string, string>();

export interface ContextualCommercialCommandScope {
  readonly clubId: number | null;
  /** Browser-only cache identity; it is never sent in a commercial payload. */
  readonly actorSubject: string | null;
  readonly audience: "staff" | "student" | "parent";
  readonly kind: "group_sale" | "subscription_renewal";
  readonly studentId: number;
  readonly paymentMethod: "cash" | "transfer" | "sbp";
  readonly protocolVersion?: "v1" | "v2";
  /** v1 group commands need the chosen tariff to distinguish commercial terms. */
  readonly tariffId?: number;
  /** Generic staff payments retain these explicit commercial choices on replay. */
  readonly discountIds?: readonly number[];
  readonly debtIds?: readonly number[];
  readonly trainingGroupId?: number;
  readonly scheduleId?: number;
  readonly startDate?: string;
  readonly renewedFromSubscriptionId?: number;
  /** Signed v2 offers define a distinct command family from a prior quote. */
  readonly offerDigest?: string;
}

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function normalizedIds(values: readonly number[] | undefined): string {
  return [...new Set((values ?? []).filter(isPositiveSafeInteger))].sort((left, right) => left - right).join(",");
}

export function contextualCommercialCommandFingerprint(
  scope: ContextualCommercialCommandScope,
): string {
  const clubId = isPositiveSafeInteger(scope.clubId) ? scope.clubId : "no-club";
  const actorSubject = scope.actorSubject?.trim() || "anonymous";
  const exactContext =
    scope.kind === "group_sale"
      ? [
          scope.tariffId ?? "",
          normalizedIds(scope.discountIds),
          normalizedIds(scope.debtIds),
          scope.trainingGroupId ?? "",
          scope.scheduleId ?? "",
          scope.startDate ?? "",
          scope.renewedFromSubscriptionId ?? "",
          scope.offerDigest ?? "",
        ]
      : [scope.renewedFromSubscriptionId ?? ""];
  return [
    clubId,
    scope.audience,
    actorSubject,
    scope.kind,
    scope.studentId,
    scope.paymentMethod,
    scope.protocolVersion ?? "v1",
    ...exactContext,
  ].join(":");
}

function storageKey(scope: ContextualCommercialCommandScope) {
  return `${CONTEXTUAL_COMMERCIAL_COMMAND_STORAGE_PREFIX}:${contextualCommercialCommandFingerprint(scope)}`;
}

function createCommandKey() {
  try {
    const bytes = crypto.getRandomValues(new Uint8Array(18));
    return `commercial-${Array.from(bytes, (value) => value.toString(36)).join("")}`;
  } catch {
    return `commercial-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
  }
}

function validCommandKey(value: string | null): value is string {
  return value !== null && /^commercial-[A-Za-z0-9_-]{12,128}$/.test(value);
}

/**
 * The key is retained until the server returns the authoritative command result.
 * It contains only club-scoped numeric identifiers and no client amount or PII.
 */
export function getOrCreateContextualCommercialCommandKey(
  scope: ContextualCommercialCommandScope,
  storage: Pick<Storage, "getItem" | "setItem"> | null =
    typeof localStorage === "undefined" ? null : localStorage,
): string {
  const key = storageKey(scope);
  const stored = storage?.getItem(key) ?? memoryKeys.get(key) ?? null;
  if (validCommandKey(stored)) return stored;

  const created = createCommandKey();
  memoryKeys.set(key, created);
  try {
    storage?.setItem(key, created);
  } catch {
    // In-memory replay survives the active UI even when browser storage is unavailable.
  }
  return created;
}

export function clearContextualCommercialCommandKey(
  scope: ContextualCommercialCommandScope,
  storage: Pick<Storage, "removeItem"> | null =
    typeof localStorage === "undefined" ? null : localStorage,
) {
  const key = storageKey(scope);
  memoryKeys.delete(key);
  try {
    storage?.removeItem(key);
  } catch {
    // A stale persisted key is still protected by the server fingerprint and TTL policy.
  }
}

/** Clears retained command keys on a browser identity transition (logout or actor change). */
export function clearAllContextualCommercialCommandKeys(
  storage: Pick<Storage, "length" | "key" | "removeItem"> | null =
    typeof localStorage === "undefined" ? null : localStorage,
) {
  memoryKeys.clear();
  if (!storage) return;
  const keys: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key?.startsWith(`${CONTEXTUAL_COMMERCIAL_COMMAND_STORAGE_PREFIX}:`)) keys.push(key);
  }
  for (const key of keys) {
    try {
      storage.removeItem(key);
    } catch {
      // Retained browser storage must not make identity cleanup throw.
    }
  }
}

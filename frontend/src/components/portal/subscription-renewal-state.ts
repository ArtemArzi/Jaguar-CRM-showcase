import {
  isPayableBankPaymentOrder,
  isSubscriptionBankPaymentOrder,
  type BankPaymentOrderLink,
} from "./payment-link-state";
import { getApiError, formatRub } from "@/lib/utils";

export type RenewalOfferPrice = string | number;

export interface RenewalTargetOffer {
  readonly renewal_target_tariff_id?: number | null;
  readonly renewal_target_tariff_name?: string | null;
  readonly renewal_target_price?: RenewalOfferPrice | null;
}

export interface ExpectedRenewalOffer {
  readonly expected_target_tariff_id: number;
  readonly expected_target_price: RenewalOfferPrice;
}

function isRenewalOfferPrice(value: unknown): value is RenewalOfferPrice {
  if (typeof value === "number") return Number.isFinite(value);
  return typeof value === "string" && value.trim().length > 0 && Number.isFinite(Number(value));
}

export function getExpectedRenewalOfferFields(
  offer: RenewalTargetOffer,
): ExpectedRenewalOffer | null {
  const tariffId = offer.renewal_target_tariff_id;
  const price = offer.renewal_target_price;
  if (
    typeof tariffId !== "number" ||
    !Number.isSafeInteger(tariffId) ||
    tariffId <= 0 ||
    !isRenewalOfferPrice(price)
  ) {
    return null;
  }
  return {
    expected_target_tariff_id: tariffId,
    expected_target_price: price,
  };
}

export function formatRenewalOfferLabel(offer: RenewalTargetOffer): string | null {
  const name = typeof offer.renewal_target_tariff_name === "string"
    ? offer.renewal_target_tariff_name.trim()
    : "";
  const price = isRenewalOfferPrice(offer.renewal_target_price)
    ? formatRub(offer.renewal_target_price)
    : "";
  const label = [name, price].filter(Boolean).join(" · ");
  return label || null;
}

const RENEWAL_OFFER_ERROR_CODES = new Set([
  "renewal_offer_required",
  "renewal_offer_stale",
  "renewal_tariff_mismatch",
  "idempotency_conflict",
]);

function renewalOfferErrorCode(error: unknown): string | null {
  const code = (error as { response?: { data?: { code?: unknown } } })?.response?.data?.code;
  return typeof code === "string" ? code : null;
}

export function isRenewalOfferError(error: unknown): boolean {
  const code = renewalOfferErrorCode(error);
  return code !== null && RENEWAL_OFFER_ERROR_CODES.has(code);
}

export function getRenewalOfferErrorMessage(error: unknown, fallback: string): string {
  if (isRenewalOfferError(error)) {
    return "Цена или версия тарифа продления изменилась. Предложение обновлено — проверьте сумму и повторите действие.";
  }
  return getApiError(error, fallback);
}

export interface RenewalSubscriptionSummary {
  id: number;
  tariff_id?: number | null;
  status: string;
}

const ENTITLEMENT_SUBSCRIPTION_STATUSES = new Set(["active", "frozen"]);
const FALLBACK_SUBSCRIPTION_STATUSES = new Set(["expired", "cancelled"]);
export function isLiveRenewalOrder(order: BankPaymentOrderLink): boolean {
  return (
    isSubscriptionBankPaymentOrder(order) &&
    isPayableBankPaymentOrder(order) &&
    (Boolean(order.tariff_id) || Boolean(order.renewed_from_subscription_id)) &&
    Boolean(order.provider_payment_url)
  );
}

export function selectEntitlementSubscriptions<T extends RenewalSubscriptionSummary>(
  subscriptions: T[],
): T[] {
  const currentSubscriptions = subscriptions.filter((subscription) =>
    ENTITLEMENT_SUBSCRIPTION_STATUSES.has(subscription.status),
  );
  if (currentSubscriptions.length > 0) return currentSubscriptions;

  return subscriptions.filter((subscription) =>
    FALLBACK_SUBSCRIPTION_STATUSES.has(subscription.status),
  );
}

export function findRenewalOrderForSubscription<T extends RenewalSubscriptionSummary>(
  orders: BankPaymentOrderLink[],
  subscription: T,
  entitlementSubscriptions: T[],
): BankPaymentOrderLink | null {
  const exactRenewalSource = orders.find(
    (order) =>
      isLiveRenewalOrder(order) && order.renewed_from_subscription_id === subscription.id,
  );
  if (exactRenewalSource) return exactRenewalSource;

  if (!subscription.tariff_id) return null;

  const matchingOrders = orders.filter(
    (order) => isLiveRenewalOrder(order) && order.tariff_id === subscription.tariff_id,
  );
  const exactOrder = matchingOrders.find(
    (order) => order.subscription_id === subscription.id,
  );
  if (exactOrder) {
    return exactOrder;
  }

  const orderBelongsToAnotherVisibleSubscription = matchingOrders.some((order) =>
    entitlementSubscriptions.some((visibleSubscription) => visibleSubscription.id === order.subscription_id),
  );
  if (orderBelongsToAnotherVisibleSubscription) {
    return null;
  }

  return matchingOrders[0] ?? null;
}

export function hasLiveRenewalOrderForTariff(
  orders: BankPaymentOrderLink[],
  tariffId?: number | null,
): boolean {
  return orders.some((order) => isLiveRenewalOrder(order) && order.tariff_id === tariffId);
}

export function hasUnresolvedRenewalOrderForTariff(
  orders: BankPaymentOrderLink[],
  tariffId?: number | null,
): boolean {
  return orders.some(
    (order) =>
      order.tariff_id === tariffId &&
      (isLiveRenewalOrder(order) || order.status === "manual_review"),
  );
}

export function hasUnresolvedRenewalOrderForSource(
  orders: BankPaymentOrderLink[],
  renewedFromSubscriptionId: number,
): boolean {
  return orders.some(
    (order) =>
      order.renewed_from_subscription_id === renewedFromSubscriptionId &&
      (isLiveRenewalOrder(order) || order.status === "manual_review"),
  );
}

export function selectPendingRenewalOrders<T extends RenewalSubscriptionSummary>(
  orders: BankPaymentOrderLink[],
  subscriptions: T[],
): BankPaymentOrderLink[] {
  const pendingSubscriptions = subscriptions.filter((subscription) => subscription.status === "pending");
  const subscriptionIds = new Set(subscriptions.map((subscription) => subscription.id));
  const pendingSubscriptionIds = new Set(pendingSubscriptions.map((subscription) => subscription.id));
  const pendingTariffIds = new Set(
    pendingSubscriptions
      .map((subscription) => subscription.tariff_id)
      .filter((tariffId): tariffId is number => typeof tariffId === "number"),
  );
  const seen = new Set<number>();
  const result: BankPaymentOrderLink[] = [];

  for (const order of orders) {
    if (!isLiveRenewalOrder(order)) continue;
    if (order.renewed_from_subscription_id && subscriptionIds.has(order.renewed_from_subscription_id)) {
      if (seen.has(order.id)) continue;
      seen.add(order.id);
      result.push(order);
      continue;
    }
    const isKnownSubscriptionOrder = subscriptionIds.has(order.subscription_id);
    const isPendingSubscriptionOrder = pendingSubscriptionIds.has(order.subscription_id);
    const matchesPendingTariff = pendingTariffIds.has(order.tariff_id);
    if (!isKnownSubscriptionOrder && !matchesPendingTariff) continue;
    if (!isPendingSubscriptionOrder && !matchesPendingTariff) continue;
    if (seen.has(order.id)) continue;
    seen.add(order.id);
    result.push(order);
  }

  return result;
}

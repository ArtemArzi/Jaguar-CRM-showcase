import type { StudentSubscription } from "../types";

interface BillingTariffPayload {
  name?: string;
  trainings_limit?: number | null;
  training_type?: {
    id?: number;
    name?: string;
    kind?: string;
  } | null;
}

interface BillingSubscriptionPayload {
  id: number;
  tariff?: BillingTariffPayload | null;
  tariff_name?: string;
  renewal_target_tariff_id?: number | null;
  renewal_target_tariff_name?: string | null;
  renewal_target_price?: string | number | null;
  training_type_kind?: string;
  package_owner_trainer_id?: number | null;
  package_owner_trainer_name?: string;
  trainings_used: number;
  trainings_total?: number | null;
  trainings_left: number | null;
  expires_at: string | null;
  status: string;
  paid_amount?: string | number;
  freeze_status?: string | null;
  scope?: string;
  location_id?: number | null;
  has_components?: boolean;
  booking_entitlements?: BillingSubscriptionBookingEntitlementPayload[];
  booking_date?: string | null;
}

interface BillingSubscriptionBookingEntitlementPayload {
  training_type_id: number;
  training_type_name?: string;
  training_type_kind?: string;
  credits_left?: number | null;
  weekly_limit?: number | null;
  weekly_used?: number | null;
  scope?: string;
  location_id?: number | null;
}

type PaginatedPayload<T> = T[] | { items?: T[] };

function getItems<T>(payload: PaginatedPayload<T>): T[] {
  return Array.isArray(payload) ? payload : payload.items ?? [];
}

export function normalizeStudentSubscription(
  subscription: BillingSubscriptionPayload,
): StudentSubscription {
  const bookingEntitlements = subscription.booking_entitlements?.map((entitlement) => ({
    training_type_id: entitlement.training_type_id,
    training_type_name: entitlement.training_type_name ?? "",
    training_type_kind: entitlement.training_type_kind ?? "",
    credits_left: entitlement.credits_left ?? null,
    weekly_limit: entitlement.weekly_limit ?? null,
    weekly_used: entitlement.weekly_used ?? null,
    scope: entitlement.scope ?? "club",
    location_id: entitlement.location_id ?? null,
  }));
  const hasRenewalTargetFields =
    subscription.renewal_target_tariff_id !== undefined ||
    subscription.renewal_target_tariff_name !== undefined ||
    subscription.renewal_target_price !== undefined;

  return {
    id: subscription.id,
    tariff_name:
      subscription.tariff_name ?? subscription.tariff?.name ?? "Абонемент",
    ...(hasRenewalTargetFields
      ? {
          renewal_target_tariff_id: subscription.renewal_target_tariff_id ?? null,
          renewal_target_tariff_name: subscription.renewal_target_tariff_name ?? "",
          renewal_target_price: subscription.renewal_target_price ?? null,
        }
      : {}),
    training_type_id: subscription.tariff?.training_type?.id ?? null,
    training_type_name: subscription.tariff?.training_type?.name ?? "",
    trainings_used: subscription.trainings_used,
    trainings_total:
      subscription.trainings_total ?? subscription.tariff?.trainings_limit ?? null,
    trainings_left: subscription.trainings_left,
    expires_at: subscription.expires_at,
    status: subscription.status,
    paid_amount: subscription.paid_amount,
    freeze_status: subscription.freeze_status ?? null,
    training_type_kind:
      subscription.training_type_kind ?? subscription.tariff?.training_type?.kind ?? "",
    package_owner_trainer_id: subscription.package_owner_trainer_id ?? null,
    package_owner_trainer_name: subscription.package_owner_trainer_name ?? "",
    ...(subscription.scope !== undefined ||
    subscription.location_id !== undefined ||
    subscription.booking_date !== undefined
      ? {
          scope: subscription.scope ?? "club",
          location_id: subscription.location_id ?? null,
          booking_date: subscription.booking_date ?? null,
        }
      : {}),
    ...(subscription.has_components !== undefined || bookingEntitlements !== undefined
      ? {
          has_components: subscription.has_components ?? Boolean(bookingEntitlements?.length),
          booking_entitlements: bookingEntitlements ?? [],
        }
      : {}),
  };
}

export function normalizeStudentSubscriptionsPayload(
  payload: PaginatedPayload<BillingSubscriptionPayload>,
): StudentSubscription[] {
  return getItems(payload).map(normalizeStudentSubscription);
}

export interface BankPaymentOrderLink {
  id: number;
  payment_id?: number;
  subscription_id: number;
  student_id?: number;
  tariff_id: number;
  target_schedule_id?: number | null;
  target_start_date?: string | null;
  target_training_group_id?: number | null;
  target_group_membership_id?: number | null;
  renewed_from_subscription_id?: number | null;
  renewal_source_tariff_id?: number | null;
  renewal_source_tariff_name?: string | null;
  renewal_target_tariff_id?: number | null;
  renewal_target_tariff_name?: string | null;
  renewal_target_price?: string | number | null;
  debt_ids: number[];
  source?: string;
  provider?: string;
  status: string;
  amount_snapshot: string | number;
  currency: string;
  purpose_snapshot?: string;
  provider_payment_link_id?: string;
  provider_payment_url?: string;
  provider_payment_modes?: string[];
  provider_status?: string;
  expires_at: string;
  paid_at?: string | null;
  payment_status?: string;
  subscription_status?: string;
  is_renewal?: boolean;
  intent_kind?: "subscription" | "renewal" | "personal_booking" | "personal_drop_in";
  personal_booking_reservation_id?: number | null;
  personal_drop_in_booking_id?: number | null;
  fulfillment_state?: string;
  can_pay?: boolean;
  can_share?: boolean;
  can_copy?: boolean;
  can_show_qr?: boolean;
  can_request_refresh?: boolean;
  can_cancel?: boolean;
}

export type PaymentActionMode = "self_service" | "staff" | "unavailable";

const LIVE_BANK_PAYMENT_ORDER_STATUSES = new Set([
  "created",
  "pending",
  "authorized",
]);

export const TERMINAL_BANK_PAYMENT_ORDER_STATUSES = new Set([
  "approved",
  "failed",
  "expired",
  "cancelled",
  "refunded",
  "refunded_partially",
]);

export function isFuturePaymentExpiry(value: string): boolean {
  const expiresAt = new Date(value);
  if (Number.isNaN(expiresAt.getTime())) return false;
  return expiresAt.getTime() > Date.now();
}

export function isLiveBankPaymentOrder(order: BankPaymentOrderLink): boolean {
  return (
    LIVE_BANK_PAYMENT_ORDER_STATUSES.has(order.status) &&
    isFuturePaymentExpiry(order.expires_at)
  );
}

export function isPayableBankPaymentOrder(order: BankPaymentOrderLink): boolean {
  return (
    isLiveBankPaymentOrder(order) &&
    order.can_pay === true &&
    Boolean(order.provider_payment_url)
  );
}

export function isPersonalBankPaymentOrder(order: BankPaymentOrderLink): boolean {
  return (
    order.intent_kind === "personal_booking" ||
    order.intent_kind === "personal_drop_in" ||
    order.personal_booking_reservation_id != null ||
    order.personal_drop_in_booking_id != null
  );
}

export function isSubscriptionBankPaymentOrder(order: BankPaymentOrderLink): boolean {
  return !isPersonalBankPaymentOrder(order);
}

export function getPaymentActionMode(order: BankPaymentOrderLink): PaymentActionMode {
  if (!isLiveBankPaymentOrder(order)) return "unavailable";
  if (order.can_pay === true && order.provider_payment_url) return "self_service";
  if (
    order.provider_payment_url &&
    (order.can_share === true || order.can_copy === true || order.can_show_qr === true)
  ) {
    return "staff";
  }
  return "unavailable";
}

export function isConfirmedBankPaymentOrder(order: BankPaymentOrderLink): boolean {
  return order.status === "approved";
}

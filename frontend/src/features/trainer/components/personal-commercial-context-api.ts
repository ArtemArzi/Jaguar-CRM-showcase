import apiClient from "@/api/custom-fetch";
import { getAuthTokenSubject } from "@/features/auth/auth-store";
import { privateQueryScope, type PrivateQueryScope } from "@/api/private-query-cache";
import { usePrivateQueryScope } from "@/features/auth/private-query-scope";
import type { RenewalOfferPrice } from "@/components/portal/subscription-renewal-state";

export type PersonalStaffIntentPaymentMethod =
  | "entitlement"
  | "cash"
  | "transfer"
  | "sbp"
  | "pay_at_visit";

/** Mirrors the frozen backend markers; this is a route contract, not a rollout flag. */
export type PersonalStaffCommandProtocol = "v1" | "v2";

export const PERSONAL_STAFF_SLOT_INTENT_V1_ROUTE = "/personal-availability/slots";
export const PERSONAL_STAFF_SLOT_INTENT_V2_ROUTE = "/personal-availability/v2/slots";
export const PERSONAL_STAFF_DIRECT_INTENT_V1_ROUTE = "/personal-availability/staff-intents/direct/";
export const PERSONAL_STAFF_DIRECT_INTENT_V2_ROUTE = "/personal-availability/v2/staff-intents/direct/";
/** These strict endpoints must never be replaced with the legacy group-sale routes. */
export const GROUP_SALE_MANUAL_V2_ROUTE = "/billing/v2/group-sales/manual/";
export const GROUP_SALE_BANK_ORDER_V2_ROUTE = "/billing/v2/group-sales/bank-orders/";

export type CommercialReceiptKind =
  | "personal_staff_intent"
  | "group_sale"
  | "renewal";

export interface PersonalCommercialReceipt {
  readonly kind: CommercialReceiptKind;
  readonly booking_id?: number | null;
  readonly reservation_id?: number | null;
  readonly payment_id?: number | null;
  readonly subscription_id?: number | null;
  readonly bank_payment_order_id?: number | null;
  readonly debt_id?: number | null;
  readonly slot_id?: number | null;
  readonly schedule_id?: number | null;
  readonly enrollment_id?: number | null;
  readonly starts_at?: string | null;
  readonly ends_at?: string | null;
  readonly training_type_id: number;
  readonly training_type_name: string;
  readonly tariff_id?: number | null;
  readonly tariff_name?: string | null;
  readonly trainer_id: number;
  readonly trainer_name: string;
  readonly location_id: number;
  readonly location_name: string;
  readonly amount: string | number | null;
  readonly payment_method: PersonalStaffIntentPaymentMethod;
  readonly status: string;
  readonly provider_payment_url?: string | null;
  readonly allowed_actions: readonly string[];
  readonly resource_route: string;
  /** Exact flag-on group target; absent only on older personal receipts. */
  readonly training_group_id?: number | null;
  readonly group_membership_id?: number | null;
  readonly group_name?: string | null;
  readonly start_date?: string | null;
  readonly target_start_date?: string | null;
  /** Exact lineage for renewal attempts. */
  readonly renewed_from_subscription_id?: number | null;
  readonly renewed_from_subscription_name?: string | null;
  /** Current retry target; absent when the source has no available revision offer. */
  readonly renewal_target_tariff_id?: number | null;
  readonly renewal_target_tariff_name?: string | null;
  readonly renewal_target_price?: RenewalOfferPrice | null;
  /** Present only on strict v2 personal command responses. */
  readonly workspace_state?: "lead" | "student";
  readonly finance_state?:
    | "pending_manual"
    | "provider_pending"
    | "confirmed"
    | "rejected"
    | "cancelled"
    | "expired"
    | "failed";
  readonly command_replayed?: boolean;
  readonly created_at?: string | null;
  readonly attempted_at?: string | null;
}

export interface PersonalCommercialContext {
  readonly student_id?: number;
  readonly lead_id?: number;
  readonly attempts: readonly PersonalCommercialReceipt[];
}

export type CommercialCacheScope = PrivateQueryScope;

export function getCommercialCacheScope(
  clubId: number | null,
  accessToken: string | null,
  role: string | null = null,
): CommercialCacheScope {
  return privateQueryScope({
    clubId,
    actorSubject: getAuthTokenSubject(accessToken),
    audience: "staff",
    role,
  });
}

export function useCommercialCacheScope(): CommercialCacheScope {
  return usePrivateQueryScope("staff").scope;
}

export interface CreatePersonalStaffIntentPayload {
  readonly student_id: number;
  readonly payment_method: PersonalStaffIntentPaymentMethod;
  readonly subscription_id?: number;
  readonly offer_digest?: string;
  /** One server-validated discount, selected only for a paid staff intent. */
  readonly discount_id?: number | null;
  readonly idempotency_key: string;
}

export interface CreateDirectPersonalStaffIntentPayload extends CreatePersonalStaffIntentPayload {
  readonly trainer_id: number;
  readonly starts_at: string;
  readonly ends_at: string;
  readonly location_id: number;
  readonly training_type_id: number;
  readonly protocolVersion?: PersonalStaffCommandProtocol;
}

export interface ReplacePersonalPaymentMethodPayload {
  readonly reservation_id?: number;
  readonly payment_id?: number;
  readonly replacement_payment_method: "cash" | "transfer" | "pay_at_visit";
  readonly reason: string;
  readonly idempotency_key: string;
}

export function replacePersonalPaymentMethod({
  studentId,
  payload,
}: {
  readonly studentId: number;
  readonly payload: ReplacePersonalPaymentMethodPayload;
}): Promise<PersonalCommercialReceipt> {
  return apiClient
    .post<PersonalCommercialReceipt>(
      `/students/${studentId}/personal-commercial-attempts/replace-payment-method/`,
      payload,
    )
    .then((response) => response.data);
}

export interface GroupSaleContext {
  readonly kind: "group_sale";
  readonly studentId: number;
  readonly studentName: string;
  readonly tariffId: number;
  readonly tariffName: string;
  readonly trainingGroupId: number;
  readonly scheduleId: number;
  readonly startDate: string;
  readonly groupName: string;
  /** A v2 context comes solely from the server-signed offer preview. */
  readonly protocolVersion?: "v2";
  readonly offerDigest?: string;
  readonly amount?: string;
  readonly trainingsLimit?: number | null;
  readonly durationDays?: number | null;
  readonly trainerName?: string;
  readonly locationName?: string;
  readonly scheduleLabel?: string;
  readonly slotScheduleIds?: readonly number[];
  readonly buyerEmailRequired?: boolean;
  /** Present only when the server selected an exact subscription as group-renewal lineage. */
  readonly renewedFromSubscriptionId?: number;
}

export interface SubscriptionRenewalContext {
  readonly kind: "subscription_renewal";
  readonly studentId: number;
  readonly studentName: string;
  readonly renewedFromSubscriptionId: number;
  readonly renewedFromSubscriptionName: string;
  readonly renewalTargetTariffId?: number | null;
  readonly renewalTargetTariffName?: string | null;
  readonly renewalTargetPrice?: RenewalOfferPrice | null;
}

export type ContextualCommercialContext = GroupSaleContext | SubscriptionRenewalContext;

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function hasExactDate(value: string | null | undefined): value is string {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value);
}

/** Returns a fresh typed retry context only when the receipt has every required exact reference. */
export function contextualRetryContextFromReceipt({
  receipt,
  studentId,
  studentName,
}: {
  readonly receipt: PersonalCommercialReceipt;
  readonly studentId: number;
  readonly studentName: string;
}): ContextualCommercialContext | null {
  if (!isPositiveSafeInteger(studentId)) return null;

  if (receipt.kind === "renewal") {
    if (!isPositiveSafeInteger(receipt.renewed_from_subscription_id)) return null;
    return {
      kind: "subscription_renewal",
      studentId,
      studentName,
      renewedFromSubscriptionId: receipt.renewed_from_subscription_id,
      renewedFromSubscriptionName:
        receipt.renewed_from_subscription_name ||
        receipt.tariff_name ||
        `Абонемент #${receipt.renewed_from_subscription_id}`,
      renewalTargetTariffId: receipt.renewal_target_tariff_id,
      renewalTargetTariffName: receipt.renewal_target_tariff_name,
      renewalTargetPrice: receipt.renewal_target_price,
    };
  }

  if (receipt.kind !== "group_sale") return null;
  const startDate = receipt.target_start_date || receipt.start_date;
  const groupName = receipt.group_name || receipt.training_type_name;
  const tariffName = receipt.tariff_name || receipt.training_type_name;
  if (
    !isPositiveSafeInteger(receipt.tariff_id) ||
    !isPositiveSafeInteger(receipt.training_group_id) ||
    !isPositiveSafeInteger(receipt.schedule_id) ||
    !hasExactDate(startDate) ||
    !groupName.trim() ||
    !tariffName.trim()
  ) {
    return null;
  }
  return {
    kind: "group_sale",
    studentId,
    studentName,
    tariffId: receipt.tariff_id,
    tariffName,
    trainingGroupId: receipt.training_group_id,
    scheduleId: receipt.schedule_id,
    startDate,
    groupName,
  };
}

export interface CreateGroupSalePayload {
  readonly student_id: number;
  readonly tariff_id: number;
  readonly payment_method: "cash" | "transfer";
  readonly discount_ids: readonly number[];
  readonly debt_ids: readonly [];
  readonly target_training_group_id: number;
  readonly target_schedule_id: number;
  readonly target_start_date: string;
  readonly renewed_from_subscription_id?: number;
  readonly idempotency_key: string;
}

export interface CreateGroupSaleBankOrderPayload {
  readonly student_id: number;
  readonly tariff_id: number;
  readonly discount_ids: readonly [];
  readonly debt_ids: readonly [];
  readonly target_training_group_id: number;
  readonly target_schedule_id: number;
  readonly target_start_date: string;
  readonly renewed_from_subscription_id?: number;
  readonly idempotency_key: string;
}

export interface CreateGroupSaleManualV2Payload {
  readonly protocol_version: "v2";
  readonly student_id: number;
  readonly tariff_id: number;
  readonly payment_method: "cash" | "transfer";
  readonly target_training_group_id: number;
  readonly target_schedule_id: number;
  readonly target_start_date: string;
  readonly expected_offer_digest: string;
  readonly idempotency_key: string;
}

export interface CreateGroupSaleBankOrderV2Payload {
  readonly protocol_version: "v2";
  readonly student_id: number;
  readonly tariff_id: number;
  readonly target_training_group_id: number;
  readonly target_schedule_id: number;
  readonly target_start_date: string;
  readonly expected_offer_digest: string;
  readonly buyer_email?: string;
  readonly idempotency_key: string;
}

export interface CreateManualRenewalPayload {
  readonly student_id: number;
  readonly renewed_from_subscription_id: number;
  readonly payment_method: "cash" | "transfer";
  readonly idempotency_key: string;
  readonly discount_ids?: readonly number[];
  readonly expected_target_tariff_id?: number;
  readonly expected_target_price?: RenewalOfferPrice;
}

export interface CreateStaffRenewalBankOrderPayload {
  readonly student_id: number;
  readonly renewed_from_subscription_id: number;
  readonly idempotency_key: string;
  readonly expected_target_tariff_id?: number;
  readonly expected_target_price?: RenewalOfferPrice;
}

export interface CreateSelfServiceRenewalPayload {
  readonly renewed_from_subscription_id: number;
  readonly idempotency_key: string;
  readonly expected_target_tariff_id?: number;
  readonly expected_target_price?: RenewalOfferPrice;
}

/** Command endpoint responses are not the projection used for commercial cards. */
export interface ContextualCommercialCommandResult {
  readonly id?: number;
  readonly payment_id?: number;
  readonly subscription_id?: number;
  readonly bank_payment_order_id?: number;
  readonly command_replayed?: boolean;
  readonly workspace_state?: "lead" | "student";
  // Terminal provider/manual outcomes are server-owned and deliberately typed
  // so callers never infer conversion from a client-side amount or status.
  readonly finance_state?:
    | "pending_manual"
    | "provider_pending"
    | "confirmed"
    | "rejected"
    | "cancelled"
    | "expired"
    | "failed";
  readonly payment_status?: string;
  readonly bank_payment_order_status?: string | null;
  readonly fulfillment_state?: string;
  readonly provider_payment_url?: string | null;
  readonly allowed_actions?: readonly string[];
}

export function createPersonalStaffIntent({
  slotId,
  protocolVersion,
  payload,
}: {
  readonly slotId: number;
  readonly protocolVersion: PersonalStaffCommandProtocol;
  readonly payload: CreatePersonalStaffIntentPayload;
}): Promise<PersonalCommercialReceipt> {
  if (
    protocolVersion === "v2" &&
    !["cash", "transfer", "sbp"].includes(payload.payment_method)
  ) {
    throw new Error("v2_personal_staff_payment_method_required");
  }
  const route =
    protocolVersion === "v2"
      ? `${PERSONAL_STAFF_SLOT_INTENT_V2_ROUTE}/${slotId}/staff-intents/`
      : `${PERSONAL_STAFF_SLOT_INTENT_V1_ROUTE}/${slotId}/staff-intents/`;
  return apiClient
    .post<PersonalCommercialReceipt>(
      route,
      protocolVersion === "v2" ? { ...payload, protocol_version: "v2" } : payload,
    )
    .then((response) => response.data);
}

export function createDirectPersonalStaffIntent(
  { protocolVersion = "v1", ...payload }: CreateDirectPersonalStaffIntentPayload,
): Promise<PersonalCommercialReceipt> {
  if (
    protocolVersion === "v2" &&
    !["cash", "transfer", "sbp"].includes(payload.payment_method)
  ) {
    throw new Error("v2_personal_staff_payment_method_required");
  }
  return apiClient
    .post<PersonalCommercialReceipt>(
      protocolVersion === "v2"
        ? PERSONAL_STAFF_DIRECT_INTENT_V2_ROUTE
        : PERSONAL_STAFF_DIRECT_INTENT_V1_ROUTE,
      protocolVersion === "v2" ? { ...payload, protocol_version: "v2" } : payload,
    )
    .then((response) => response.data);
}

export function createContextualGroupSale(
  payload: CreateGroupSalePayload,
): Promise<ContextualCommercialCommandResult> {
  return apiClient
    .post<ContextualCommercialCommandResult>("/billing/payments/", payload)
    .then((response) => response.data);
}

export function createContextualGroupSaleBankOrder(
  payload: CreateGroupSaleBankOrderPayload,
): Promise<ContextualCommercialCommandResult> {
  return apiClient
    .post<ContextualCommercialCommandResult>("/billing/bank-payment-orders/", payload)
    .then((response) => response.data);
}

export function createContextualGroupSaleManualV2(
  payload: CreateGroupSaleManualV2Payload,
): Promise<ContextualCommercialCommandResult> {
  return apiClient
    .post<ContextualCommercialCommandResult>(GROUP_SALE_MANUAL_V2_ROUTE, payload)
    .then((response) => response.data);
}

export function createContextualGroupSaleBankOrderV2(
  payload: CreateGroupSaleBankOrderV2Payload,
): Promise<ContextualCommercialCommandResult> {
  return apiClient
    .post<ContextualCommercialCommandResult>(GROUP_SALE_BANK_ORDER_V2_ROUTE, payload)
    .then((response) => response.data);
}

export function createManualSubscriptionRenewal(
  payload: CreateManualRenewalPayload,
): Promise<ContextualCommercialCommandResult> {
  return apiClient
    .post<ContextualCommercialCommandResult>("/billing/payments/renewals/", payload)
    .then((response) => response.data);
}

export function createStaffSubscriptionRenewalBankOrder(
  payload: CreateStaffRenewalBankOrderPayload,
): Promise<ContextualCommercialCommandResult> {
  return apiClient
    .post<ContextualCommercialCommandResult>("/billing/bank-payment-orders/", payload)
    .then((response) => response.data);
}

export function createStudentSubscriptionRenewal(
  payload: CreateSelfServiceRenewalPayload,
) {
  return apiClient
    .post("/students/me/bank-payment-orders/", payload)
    .then((response) => response.data);
}

export function createParentSubscriptionRenewal({
  studentId,
  payload,
}: {
  readonly studentId: number;
  readonly payload: CreateSelfServiceRenewalPayload;
}) {
  return apiClient
    .post(`/parents/children/${studentId}/bank-payment-orders/`, payload)
    .then((response) => response.data);
}

export function getPersonalCommercialContext(
  studentId: number,
): Promise<PersonalCommercialContext> {
  return apiClient
    .get<PersonalCommercialContext>(`/students/${studentId}/commercial-context/`)
    .then((response) => response.data);
}

export function getLeadPersonalCommercialContext(
  leadId: number,
): Promise<PersonalCommercialContext> {
  return apiClient
    .get<PersonalCommercialContext>(`/leads/${leadId}/commercial-context/`)
    .then((response) => response.data);
}

export const personalCommercialContextQueryKey = (
  studentId: number,
  cacheScope: CommercialCacheScope,
) => ["student", String(studentId), "commercial-context", ...cacheScope] as const;

export const leadPersonalCommercialContextQueryKey = (
  leadId: number,
  cacheScope: CommercialCacheScope,
) => ["lead", String(leadId), "commercial-context", ...cacheScope] as const;

export const personalCommercialBankOrderQueryKey = (
  bankPaymentOrderId: number,
  cacheScope: CommercialCacheScope,
) => ["billing", "bank-payment-orders", "personal-commercial", bankPaymentOrderId, ...cacheScope] as const;

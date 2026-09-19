import apiClient from "@/api/custom-fetch";
import type {
  TrainerPersonalAvailabilityGeneratePayload,
  TrainerPersonalAvailabilityGenerateResult,
  TrainerPersonalAvailabilitySlot,
} from "../types";

export interface PersonalOffer {
  tariffId?: number;
  tariffName?: string;
  /** Established field: the ordinary (pre-discount) price. */
  price?: string | number;
  /** Explicit server-owned monetary evidence. Never derive it in the client. */
  baseAmount?: string | number;
  discountId?: number;
  discountName?: string;
  discountType?: "percent" | "fixed";
  discountValue?: string | number;
  discountAmount?: string | number;
  payableAmount?: string | number;
  /** The catalog row that supplied an optional trainer-specific ordinary price. */
  trainerId?: number;
  /** A digest only exists for an immutable, slot-bound offer. */
  digest?: string;
  errorCode?: string;
}

export interface PersonalOfferResponse {
  tariff_id?: number | null;
  tariff_name?: string;
  price?: string | number | null;
  offer_tariff_id?: number | null;
  offer_tariff_name?: string;
  offer_price?: string | number | null;
  offer_base_amount?: string | number | null;
  offer_discount_id?: number | null;
  offer_discount_name?: string | null;
  offer_discount_type?: "percent" | "fixed" | "" | null;
  offer_discount_value?: string | number | null;
  offer_discount_amount?: string | number | null;
  offer_payable_amount?: string | number | null;
  offer_trainer_id?: number | null;
  offer_digest?: string;
  offer_error_code?: string;
}

export const personalOfferQueryKey = ({
  trainingTypeId,
  locationId,
}: {
  trainingTypeId: number;
  locationId: number;
}) => ["trainer", "personal-offer", trainingTypeId, locationId] as const;

export const directPersonalOfferQueryKey = ({
  trainerId,
  startsAt,
  endsAt,
  locationId,
  trainingTypeId,
  discountId = null,
}: {
  trainerId: number;
  startsAt: string;
  endsAt: string;
  locationId: number;
  trainingTypeId: number;
  discountId?: number | null;
}) =>
  [
    "trainer",
    "personal-direct-offer",
    trainerId,
    startsAt,
    endsAt,
    locationId,
    trainingTypeId,
    discountId ?? "no-discount",
  ] as const;

export const fixedPersonalOfferQueryKey = ({
  slotId,
  trainerId,
  locationId,
  trainingTypeId,
  discountId = null,
}: {
  slotId: number;
  trainerId?: number;
  locationId: number;
  trainingTypeId: number;
  discountId?: number | null;
}) =>
  [
    "trainer",
    "personal-fixed-offer",
    slotId,
    trainerId ?? "unknown-trainer",
    locationId,
    trainingTypeId,
    discountId ?? "no-discount",
  ] as const;

function hasValue(value: unknown): value is string | number {
  return value !== null && value !== undefined && value !== "";
}

/**
 * Accept both the preview route's compact fields and the slot/availability
 * offer fields while Slice 3 rolls out. The caller still decides whether an
 * acceptance digest is required; a preview never manufactures one.
 */
export function normalizePersonalOffer(response: PersonalOfferResponse): PersonalOffer | null {
  const tariffId = response.offer_tariff_id ?? response.tariff_id;
  const tariffName = response.offer_tariff_name ?? response.tariff_name;
  const price = response.offer_price ?? response.price;
  if (!tariffId || !tariffName || price === null || price === undefined) {
    return response.offer_error_code ? { errorCode: response.offer_error_code } : null;
  }
  const baseAmount = hasValue(response.offer_base_amount) ? response.offer_base_amount : price;
  const discountAmount = hasValue(response.offer_discount_amount)
    ? response.offer_discount_amount
    : 0;
  const payableAmount = hasValue(response.offer_payable_amount)
    ? response.offer_payable_amount
    : price;
  const discountType = response.offer_discount_type;
  return {
    tariffId,
    tariffName,
    price,
    baseAmount,
    discountId: response.offer_discount_id ?? undefined,
    discountName: response.offer_discount_name || undefined,
    discountType: discountType === "percent" || discountType === "fixed" ? discountType : undefined,
    discountValue: hasValue(response.offer_discount_value)
      ? response.offer_discount_value
      : undefined,
    discountAmount,
    payableAmount,
    trainerId: response.offer_trainer_id ?? undefined,
    digest: response.offer_digest || undefined,
    errorCode: response.offer_error_code || undefined,
  };
}

export function getPersonalOfferFromSlot(slot: TrainerPersonalAvailabilitySlot): PersonalOffer | null {
  return normalizePersonalOffer(slot);
}

export function previewTrainerPersonalOffer({
  trainingTypeId,
  locationId,
  trainerId,
  slotId,
  discountId,
}: {
  trainingTypeId: number;
  locationId: number;
  trainerId?: number;
  slotId?: number;
  discountId?: number | null;
}): Promise<PersonalOffer | null> {
  return apiClient
    .get<PersonalOfferResponse>("/personal-availability/offers/", {
      params: {
        training_type_id: trainingTypeId,
        location_id: locationId,
        ...(trainerId ? { trainer_id: trainerId } : {}),
        ...(slotId ? { slot_id: slotId } : {}),
        ...(discountId ? { discount_id: discountId } : {}),
      },
    })
    .then((response) => normalizePersonalOffer(response.data));
}

export function getDirectPersonalOffer({
  trainerId,
  startsAt,
  endsAt,
  locationId,
  trainingTypeId,
  discountId,
}: {
  trainerId: number;
  startsAt: string;
  endsAt: string;
  locationId: number;
  trainingTypeId: number;
  discountId?: number | null;
}): Promise<PersonalOffer | null> {
  return apiClient
    .get<PersonalOfferResponse>("/personal-availability/direct-offer/", {
      params: {
        trainer_id: trainerId,
        starts_at: startsAt,
        ends_at: endsAt,
        location_id: locationId,
        training_type_id: trainingTypeId,
        ...(discountId ? { discount_id: discountId } : {}),
      },
    })
    .then((response) => normalizePersonalOffer(response.data));
}

export function listTrainerAvailability({
  dateFrom,
  dateTo,
  trainerId,
}: {
  dateFrom: string;
  dateTo: string;
  trainerId?: number;
}): Promise<TrainerPersonalAvailabilitySlot[]> {
  return apiClient
    .get("/personal-availability/slots/", {
      params: {
        date_from: dateFrom,
        date_to: dateTo,
        ...(trainerId ? { trainer_id: trainerId } : {}),
      },
    })
    .then((response) => response.data);
}

export function generateTrainerAvailability(
  payload: TrainerPersonalAvailabilityGeneratePayload,
): Promise<TrainerPersonalAvailabilityGenerateResult> {
  return apiClient
    .post("/personal-availability/slots/generate/", payload)
    .then((response) => response.data);
}

export function blockTrainerAvailabilitySlot({
  slotId,
  reason,
}: {
  slotId: number;
  reason: string;
}): Promise<TrainerPersonalAvailabilitySlot> {
  return apiClient
    .post(`/personal-availability/slots/${slotId}/block/`, { reason })
    .then((response) => response.data);
}

export function unblockTrainerAvailabilitySlot(
  slotId: number,
): Promise<TrainerPersonalAvailabilitySlot> {
  return apiClient
    .post(`/personal-availability/slots/${slotId}/unblock/`, {})
    .then((response) => response.data);
}

export function cancelTrainerAvailabilitySlot(
  slotId: number,
): Promise<TrainerPersonalAvailabilitySlot> {
  return apiClient
    .post(`/personal-availability/slots/${slotId}/cancel/`, {})
    .then((response) => response.data);
}

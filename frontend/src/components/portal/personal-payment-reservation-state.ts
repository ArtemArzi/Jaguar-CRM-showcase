
export interface PersonalBookingPaymentReservation {
  id: number;
  student_id: number;
  trainer_id: number;
  trainer_name: string;
  location_id: number;
  location_name: string;
  training_type_id: number;
  training_type_name: string;
  tariff_id: number;
  tariff_name: string;
  availability_slot_id: number | null;
  starts_at: string;
  ends_at: string;
  status: string;
  expires_at: string;
  payment_id: number | null;
  bank_payment_order_id: number | null;
  subscription_id: number | null;
  schedule_id: number | null;
  enrollment_id: number | null;
  provider_payment_url: string;
  amount_snapshot: string | number;
  order_status: string;
  can_cancel: boolean;
  created_at: string;
}

export function isLivePersonalPaymentReservation(
  reservation: PersonalBookingPaymentReservation,
): boolean {
  const expiresAt = new Date(reservation.expires_at).getTime();
  return (
    reservation.status === "pending_payment" &&
    reservation.bank_payment_order_id !== null &&
    Number.isFinite(expiresAt) &&
    expiresAt > Date.now()
  );
}

export function isManualReviewPersonalPaymentReservation(
  reservation: PersonalBookingPaymentReservation,
): boolean {
  return reservation.status === "manual_review";
}

export function isVisiblePersonalPaymentReservation(
  reservation: PersonalBookingPaymentReservation,
): boolean {
  return (
    isLivePersonalPaymentReservation(reservation) ||
    isManualReviewPersonalPaymentReservation(reservation)
  );
}

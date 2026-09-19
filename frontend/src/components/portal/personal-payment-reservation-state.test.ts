import { describe, expect, it } from "vitest";
import {
  isLivePersonalPaymentReservation,
  isManualReviewPersonalPaymentReservation,
  isVisiblePersonalPaymentReservation,
  type PersonalBookingPaymentReservation,
} from "./personal-payment-reservation-state";

function reservation(
  overrides: Partial<PersonalBookingPaymentReservation> = {},
): PersonalBookingPaymentReservation {
  return {
    id: 31,
    student_id: 7,
    trainer_id: 3,
    trainer_name: "Ivan Petrov",
    location_id: 2,
    location_name: "Main hall",
    training_type_id: 8,
    training_type_name: "Персональная тренировка",
    tariff_id: 12,
    tariff_name: "Разовая персоналка",
    availability_slot_id: 56,
    starts_at: "2026-04-13T10:00:00+05:00",
    ends_at: "2026-04-13T11:00:00+05:00",
    status: "pending_payment",
    expires_at: "2099-04-13T11:15:00+05:00",
    payment_id: null,
    bank_payment_order_id: 91,
    subscription_id: null,
    schedule_id: null,
    enrollment_id: null,
    provider_payment_url: "https://pay.example/personal-31",
    amount_snapshot: "2000.00",
    order_status: "pending",
    can_cancel: true,
    created_at: "2026-04-13T08:45:00+05:00",
    ...overrides,
  };
}

describe("personal-payment-reservation-state", () => {
  it("keeps only pending reservations with a bound order live", () => {
    expect(isLivePersonalPaymentReservation(reservation())).toBe(true);
    expect(isLivePersonalPaymentReservation(reservation({ status: "cancelled" }))).toBe(false);
    expect(
      isLivePersonalPaymentReservation(reservation({ bank_payment_order_id: null })),
    ).toBe(false);
    expect(
      isLivePersonalPaymentReservation(reservation({ expires_at: "2026-01-01T00:00:00Z" })),
    ).toBe(false);
  });

  it("keeps manual review reservations visible but not payable", () => {
    const review = reservation({
      status: "manual_review",
      can_cancel: false,
      order_status: "approved",
    });

    expect(isManualReviewPersonalPaymentReservation(review)).toBe(true);
    expect(isLivePersonalPaymentReservation(review)).toBe(false);
    expect(isVisiblePersonalPaymentReservation(review)).toBe(true);
  });
});

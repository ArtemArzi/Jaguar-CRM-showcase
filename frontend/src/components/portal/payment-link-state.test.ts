import { describe, expect, it } from "vitest";
import {
  isPersonalBankPaymentOrder,
  isPayableBankPaymentOrder,
  isSubscriptionBankPaymentOrder,
  type BankPaymentOrderLink,
} from "./payment-link-state";

function order(overrides: Partial<BankPaymentOrderLink> = {}): BankPaymentOrderLink {
  return {
    id: 1,
    subscription_id: 2,
    tariff_id: 3,
    debt_ids: [],
    status: "pending",
    amount_snapshot: "2000.00",
    currency: "RUB",
    provider_payment_link_id: "pay-1",
    provider_payment_url: "https://pay.example/1",
    provider_status: "pending",
    expires_at: "2099-06-30T12:00:00Z",
    paid_at: null,
    can_pay: true,
    can_cancel: true,
    ...overrides,
  };
}

describe("payment-link-state", () => {
  it.each(["created", "pending", "authorized"])(
    "treats %s orders with explicit payment capability as payable",
    (status) => {
      expect(isPayableBankPaymentOrder(order({ status }))).toBe(true);
    },
  );

  it.each(["approved", "paid", "cancelled", "expired", "failed"])(
    "treats %s orders as terminal",
    (status) => {
      expect(isPayableBankPaymentOrder(order({ status }))).toBe(false);
    },
  );

  it("does not treat orders without provider urls as payable", () => {
    expect(isPayableBankPaymentOrder(order({ provider_payment_url: "" }))).toBe(false);
  });

  it("fails closed when the backend omits payment capability", () => {
    expect(isPayableBankPaymentOrder(order({ can_pay: undefined }))).toBe(false);
  });

  it("does not treat expired orders as payable", () => {
    expect(isPayableBankPaymentOrder(order({ expires_at: "2026-01-01T00:00:00Z" }))).toBe(false);
  });

  it("does not treat invalid expiry values as payable", () => {
    expect(isPayableBankPaymentOrder(order({ expires_at: "not-a-date" }))).toBe(false);
  });

  it.each([
    { intent_kind: "personal_booking" as const },
    { intent_kind: "personal_drop_in" as const },
    { personal_booking_reservation_id: 41 },
    { personal_drop_in_booking_id: 42 },
  ])("keeps personal payment intents out of subscription surfaces", (origin) => {
    const personalOrder = order(origin);

    expect(isPersonalBankPaymentOrder(personalOrder)).toBe(true);
    expect(isSubscriptionBankPaymentOrder(personalOrder)).toBe(false);
  });
});

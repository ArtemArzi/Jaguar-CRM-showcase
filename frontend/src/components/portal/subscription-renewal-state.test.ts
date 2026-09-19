import { describe, expect, it } from "vitest";
import type { BankPaymentOrderLink } from "./online-payment-link-panel";
import {
  formatRenewalOfferLabel,
  findRenewalOrderForSubscription,
  getExpectedRenewalOfferFields,
  getRenewalOfferErrorMessage,
  isRenewalOfferError,
  selectEntitlementSubscriptions,
  selectPendingRenewalOrders,
} from "./subscription-renewal-state";

function order(overrides: Partial<BankPaymentOrderLink>): BankPaymentOrderLink {
  return {
    id: 1,
    subscription_id: 10,
    tariff_id: 5,
    debt_ids: [],
    status: "pending",
    amount_snapshot: "5000.00",
    currency: "RUB",
    provider_payment_link_id: "jgr-1-test",
    provider_payment_url: "https://pay.example/jgr-1-test",
    can_pay: true,
    provider_status: "CREATED",
    expires_at: "2099-06-28T12:00:00Z",
    paid_at: null,
    ...overrides,
  };
}

describe("subscription renewal state", () => {
  it("formats the server-selected target offer and builds its expected fingerprint", () => {
    const offer = {
      renewal_target_tariff_id: 9,
      renewal_target_tariff_name: "Base 2026",
      renewal_target_price: "6500.00",
    };

    expect(formatRenewalOfferLabel(offer)).toBe("Base 2026 · 6 500 ₽");
    expect(getExpectedRenewalOfferFields(offer)).toEqual({
      expected_target_tariff_id: 9,
      expected_target_price: "6500.00",
    });
  });

  it("requires both target identity and price before sending an offer fingerprint", () => {
    expect(
      getExpectedRenewalOfferFields({
        renewal_target_tariff_id: 9,
        renewal_target_tariff_name: "Base 2026",
      }),
    ).toBeNull();
    expect(formatRenewalOfferLabel({ renewal_target_tariff_name: "Base 2026" })).toBe(
      "Base 2026",
    );
  });

  it("recognizes stale renewal offers so callers can refresh instead of retrying silently", () => {
    const error = { response: { data: { code: "renewal_offer_stale" } } };

    expect(isRenewalOfferError(error)).toBe(true);
    expect(getRenewalOfferErrorMessage(error, "fallback")).toMatch(/изменилась/);
    expect(isRenewalOfferError({ response: { data: { code: "idempotency_conflict" } } })).toBe(true);
    expect(isRenewalOfferError(new Error("offline"))).toBe(false);
  });

  it("keeps pending renewals out of entitlement cards when an active subscription exists", () => {
    const active = { id: 1, tariff_id: 5, status: "active" };
    const pending = { id: 2, tariff_id: 5, status: "pending" };

    expect(selectEntitlementSubscriptions([active, pending])).toEqual([active]);
  });

  it("attaches a live renewal order to the visible subscription by tariff", () => {
    const active = { id: 1, tariff_id: 5, status: "active" };
    const pendingOrder = order({ id: 7, subscription_id: 2, tariff_id: 5 });

    expect(findRenewalOrderForSubscription([pendingOrder], active, [active])).toEqual(
      pendingOrder,
    );
  });

  it("returns pending renewal orders as standalone payment blocks", () => {
    const active = { id: 1, tariff_id: 5, status: "active" };
    const pending = { id: 2, tariff_id: 5, status: "pending" };
    const pendingOrder = order({ id: 7, subscription_id: pending.id, tariff_id: 5 });
    const staleOrder = order({
      id: 8,
      subscription_id: pending.id,
      tariff_id: 5,
      status: "expired",
    });

    expect(selectPendingRenewalOrders([pendingOrder, staleOrder], [active, pending])).toEqual([
      pendingOrder,
    ]);
  });

  it("never attaches a personal payment order to a subscription renewal", () => {
    const active = { id: 1, tariff_id: 5, status: "active" };
    const pending = { id: 2, tariff_id: 5, status: "pending" };
    const personalOrder = order({
      id: 9,
      subscription_id: pending.id,
      tariff_id: 5,
      intent_kind: "personal_booking",
      personal_booking_reservation_id: 77,
    });

    expect(findRenewalOrderForSubscription([personalOrder], active, [active])).toBeNull();
    expect(selectPendingRenewalOrders([personalOrder], [active, pending])).toEqual([]);
  });

  it("keeps a cancelled subscription visible only when there is no current entitlement", () => {
    const active = { id: 1, tariff_id: 5, status: "active" };
    const cancelled = { id: 2, tariff_id: 5, status: "cancelled" };

    expect(selectEntitlementSubscriptions([cancelled])).toEqual([cancelled]);
    expect(selectEntitlementSubscriptions([active, cancelled])).toEqual([active]);
  });
});

import { describe, expect, it } from "vitest";
import { getParentSubscriptionAlert } from "./subscription-alerts";

describe("getParentSubscriptionAlert", () => {
  it("explains a cancelled subscription as a refund outcome with no entitlement", () => {
    expect(
      getParentSubscriptionAlert({
        hasSubscription: true,
        remaining: 5,
        total: 8,
        status: "cancelled",
        freezeStatus: null,
      }),
    ).toMatchObject({
      title: "Абонемент отменён",
      description: "Оплата возвращена, оставшиеся занятия недоступны.",
      badge: "Возврат",
      tone: "danger",
      requiresAttention: true,
      remainingText: null,
    });
  });
});

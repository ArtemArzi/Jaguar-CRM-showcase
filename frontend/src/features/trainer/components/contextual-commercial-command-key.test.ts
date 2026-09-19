import { describe, expect, it } from "vitest";
import {
  clearAllContextualCommercialCommandKeys,
  clearContextualCommercialCommandKey,
  contextualCommercialCommandFingerprint,
  getOrCreateContextualCommercialCommandKey,
} from "./contextual-commercial-command-key";

function storage() {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    getItem: (key: string) => values.get(key) ?? null,
    key: (index: number) => [...values.keys()][index] ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  };
}

describe("contextual commercial command key", () => {
  it("replays an unchanged exact context and rotates when a commercial edit changes it", () => {
    const saved = storage();
    const group = {
      clubId: 7,
      actorSubject: "staff-7",
      audience: "staff" as const,
      kind: "group_sale" as const,
      studentId: 41,
      paymentMethod: "cash" as const,
      tariffId: 17,
      discountIds: [9],
      debtIds: [11],
      trainingGroupId: 15,
      scheduleId: 19,
      startDate: "2099-02-01",
    };
    const first = getOrCreateContextualCommercialCommandKey(group, saved);

    expect(getOrCreateContextualCommercialCommandKey(group, saved)).toBe(first);
    expect(
      getOrCreateContextualCommercialCommandKey({ ...group, paymentMethod: "transfer" }, saved),
    ).not.toBe(first);
    expect(
      getOrCreateContextualCommercialCommandKey({ ...group, startDate: "2099-02-08" }, saved),
    ).not.toBe(first);
    expect(
      getOrCreateContextualCommercialCommandKey({ ...group, tariffId: 23 }, saved),
    ).not.toBe(first);
    expect(
      getOrCreateContextualCommercialCommandKey({ ...group, discountIds: [10] }, saved),
    ).not.toBe(first);
    expect(
      getOrCreateContextualCommercialCommandKey({ ...group, debtIds: [12] }, saved),
    ).not.toBe(first);
  });

  it("isolates keys by tenant and releases a key only after its authoritative result", () => {
    const saved = storage();
    const renewal = {
      clubId: 7,
      actorSubject: "student-41",
      audience: "student" as const,
      kind: "subscription_renewal" as const,
      studentId: 41,
      paymentMethod: "sbp" as const,
      renewedFromSubscriptionId: 55,
    };
    const first = getOrCreateContextualCommercialCommandKey(renewal, saved);

    expect(contextualCommercialCommandFingerprint(renewal)).not.toBe(
      contextualCommercialCommandFingerprint({ ...renewal, clubId: 8 }),
    );
    clearContextualCommercialCommandKey(renewal, saved);
    expect(getOrCreateContextualCommercialCommandKey(renewal, saved)).not.toBe(first);
  });

  it("separates same-club keys for different subjects and student-parent audiences", () => {
    const saved = storage();
    const studentScope = {
      clubId: 7,
      actorSubject: "student-user",
      audience: "student" as const,
      kind: "subscription_renewal" as const,
      studentId: 41,
      paymentMethod: "sbp" as const,
      renewedFromSubscriptionId: 55,
    };
    const parentScope = {
      ...studentScope,
      actorSubject: "parent-user",
      audience: "parent" as const,
    };

    expect(getOrCreateContextualCommercialCommandKey(parentScope, saved)).not.toBe(
      getOrCreateContextualCommercialCommandKey(studentScope, saved),
    );
    expect(contextualCommercialCommandFingerprint({ ...studentScope, actorSubject: "staff-7", audience: "staff" })).not.toBe(
      contextualCommercialCommandFingerprint({ ...studentScope, actorSubject: "staff-8", audience: "staff" }),
    );
  });

  it("purges retained command keys when the browser identity changes", () => {
    const saved = storage();
    const scope = {
      clubId: 7,
      actorSubject: "student-user",
      audience: "student" as const,
      kind: "subscription_renewal" as const,
      studentId: 41,
      paymentMethod: "sbp" as const,
      renewedFromSubscriptionId: 55,
    };
    const first = getOrCreateContextualCommercialCommandKey(scope, saved);

    clearAllContextualCommercialCommandKeys(saved);

    expect(getOrCreateContextualCommercialCommandKey(scope, saved)).not.toBe(first);
  });
});

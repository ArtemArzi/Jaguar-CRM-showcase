import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import {
  PAYMENT_RESUME_STORAGE_KEY,
  getPaymentResumeContext,
} from "./payment-resume-context";

function store(value: Record<string, unknown>) {
  localStorage.setItem(PAYMENT_RESUME_STORAGE_KEY, JSON.stringify(value));
}

function tokenFor(subject: string) {
  return `header.${btoa(JSON.stringify({ sub: subject }))}.signature`;
}

describe("payment resume context", () => {
  beforeEach(() => {
    localStorage.removeItem(PAYMENT_RESUME_STORAGE_KEY);
    useAuthStore.setState({
      accessToken: tokenFor("student-17"),
      role: "student",
      clubId: 7,
      isAuthenticated: true,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("derives only the exact student endpoint from a matching, unexpired context", () => {
    store({ orderId: 44, role: "student", clubId: 7, actorSubject: "student-17", expiresAt: Date.now() + 60_000 });

    expect(getPaymentResumeContext()).toEqual({
      orderId: 44,
      role: "student",
      endpoint: "/students/me/bank-payment-orders/44/",
      refreshEndpoint: "/students/me/bank-payment-orders/44/refresh/",
    });
  });

  it("rejects stale, mismatched, and malformed contexts instead of constructing a URL", () => {
    store({ orderId: 44, role: "student", clubId: 7, actorSubject: "student-17", expiresAt: Date.now() - 1 });
    expect(getPaymentResumeContext()).toBeNull();
    expect(localStorage.getItem(PAYMENT_RESUME_STORAGE_KEY)).toBeNull();

    store({ orderId: 44, role: "parent", clubId: 7, actorSubject: "student-17", expiresAt: Date.now() + 60_000, childId: 8 });
    expect(getPaymentResumeContext()).toBeNull();

    store({ orderId: "44", role: "student", clubId: 7, actorSubject: "student-17", expiresAt: Date.now() + 60_000 });
    expect(getPaymentResumeContext()).toBeNull();
  });

  it("requires a positive child id before deriving a parent endpoint", () => {
    useAuthStore.setState({ accessToken: tokenFor("parent-23"), role: "parent", clubId: 7, isAuthenticated: true });
    store({ orderId: 44, role: "parent", clubId: 7, actorSubject: "parent-23", expiresAt: Date.now() + 60_000 });
    expect(getPaymentResumeContext()).toBeNull();

    store({ orderId: 44, role: "parent", clubId: 7, childId: 8, actorSubject: "parent-23", expiresAt: Date.now() + 60_000 });
    expect(getPaymentResumeContext()).toEqual({
      orderId: 44,
      role: "parent",
      childId: 8,
      endpoint: "/parents/children/8/bank-payment-orders/44/",
      refreshEndpoint: "/parents/children/8/bank-payment-orders/44/refresh/",
    });
  });
});

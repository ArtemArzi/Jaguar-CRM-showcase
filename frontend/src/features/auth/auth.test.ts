import { describe, it, expect, beforeEach, vi } from "vitest";
import { SERVER_REFRESH_TOKEN_SENTINEL, useAuthStore } from "./auth-store";
import { decodeJwtPayload } from "@/lib/jwt";

function tokenFor(subject: string) {
  return `header.${btoa(JSON.stringify({ sub: subject }))}.signature`;
}

describe("auth-store", () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      role: null,
      clubId: null,
      trainerId: null,
      studentId: null,
      isAuthenticated: false,
    });
  });

  it("setTokens marks user as authenticated", () => {
    useAuthStore.getState().setTokens("access123", "refresh456");
    const state = useAuthStore.getState();
    expect(state.accessToken).toBe("access123");
    expect(state.refreshToken).toBe(SERVER_REFRESH_TOKEN_SENTINEL);
    expect(state.isAuthenticated).toBe(true);
  });

  it("does not keep the raw refresh token in persistent auth storage", () => {
    useAuthStore.getState().setTokens("access123", "raw-refresh-token");
    const state = useAuthStore.getState();
    const persisted = localStorage.getItem("jaguar-auth") ?? "";

    expect(state.accessToken).toBe("access123");
    expect(state.refreshToken).not.toBe("raw-refresh-token");
    expect(persisted).not.toContain("raw-refresh-token");
    expect(state.isAuthenticated).toBe(true);
  });

  it("setUserInfo stores role and clubId", () => {
    useAuthStore.getState().setUserInfo("trainer", 42);
    const state = useAuthStore.getState();
    expect(state.role).toBe("trainer");
    expect(state.clubId).toBe(42);
  });

  it("setUserInfo clears role-bound profile ids before a new membership resolves", () => {
    useAuthStore.setState({ trainerId: 9, studentId: 17 });

    useAuthStore.getState().setUserInfo("trainer", 42);

    const state = useAuthStore.getState();
    expect(state.trainerId).toBeNull();
    expect(state.studentId).toBeNull();
  });

  it("logout clears auth state and the generic payment-return session", () => {
    const fetchMock = vi.fn(() => Promise.resolve(new Response()));
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem("jaguar-payment-resume", "stale");
    useAuthStore.getState().setTokens("a", "b");
    useAuthStore.getState().setUserInfo("owner", 1);
    useAuthStore.getState().logout();
    const state = useAuthStore.getState();
    expect(state.accessToken).toBeNull();
    expect(state.refreshToken).toBeNull();
    expect(state.role).toBeNull();
    expect(state.clubId).toBeNull();
    expect(state.isAuthenticated).toBe(false);
    expect(localStorage.getItem("jaguar-payment-resume")).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/billing/payment-returns/session/",
      expect.objectContaining({ method: "DELETE", credentials: "same-origin", keepalive: true }),
    );
    vi.unstubAllGlobals();
  });

  it("clears a payment resume context when the authenticated subject changes", () => {
    useAuthStore.getState().setTokens(tokenFor("actor-a"));
    localStorage.setItem("jaguar-payment-resume", "bound-to-actor-a");

    useAuthStore.getState().setTokens(tokenFor("actor-b"));

    expect(localStorage.getItem("jaguar-payment-resume")).toBeNull();
  });
});

describe("decodeJwtPayload", () => {
  it("returns null for empty input", () => {
    expect(decodeJwtPayload("")).toBeNull();
  });

  it("returns null for null/undefined coerced to string", () => {
    expect(decodeJwtPayload(null as unknown as string)).toBeNull();
  });

  it("returns null for malformed JWT", () => {
    expect(decodeJwtPayload("not-a-jwt")).toBeNull();
    expect(decodeJwtPayload("a.b")).toBeNull();
  });

  it("decodes valid JWT payload", () => {
    const payload = { role: "trainer", club_id: 1, exp: 9999999999 };
    const encoded = btoa(JSON.stringify(payload));
    const token = `eyJhbGciOiJIUzI1NiJ9.${encoded}.sig`;
    const result = decodeJwtPayload(token);
    expect(result).toEqual(payload);
  });
});

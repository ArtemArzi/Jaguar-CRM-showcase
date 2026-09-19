import { create } from "zustand";
import { persist } from "zustand/middleware";
import { decodeJwtPayload } from "@/lib/jwt";
import { clearAllContextualCommercialCommandKeys } from "@/api/contextual-commercial-command-key";
import { purgePrivateQueryCaches } from "@/api/private-query-cache";

export const SERVER_REFRESH_TOKEN_SENTINEL = "__jaguar_server_refresh_cookie__";
const AUTH_STORAGE_KEY = "jaguar-auth";
const PAYMENT_RESUME_STORAGE_KEY = "jaguar-payment-resume";

export type UserRole = "trainer" | "student" | "parent" | "owner" | "admin";
export type StudentBootstrapStatus = "idle" | "loading" | "resolved" | "failed";
export type AuthBootstrapStatus = "idle" | "loading" | "ready";

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  role: UserRole | null;
  clubId: number | null;
  trainerId: number | null;
  studentId: number | null;
  authBootstrapStatus: AuthBootstrapStatus;
  studentBootstrapStatus: StudentBootstrapStatus;
  studentBootstrapError: string | null;
  isAuthenticated: boolean;
  setTokens: (access: string, refresh?: string | null) => void;
  setUserInfo: (role: UserRole, clubId: number) => void;
  setTrainerId: (id: number | null) => void;
  setStudentId: (id: number) => void;
  startAuthBootstrap: () => void;
  resolveAuthBootstrap: () => void;
  startStudentBootstrap: () => void;
  resolveStudentBootstrap: (id: number) => void;
  failStudentBootstrap: (message: string) => void;
  resetStudentBootstrap: () => void;
  logout: () => void;
}

type PersistedAuthState = Partial<AuthState> & {
  refreshToken?: string | null;
};

function toServerRefreshSentinel(refresh: string | null | undefined) {
  return refresh ? SERVER_REFRESH_TOKEN_SENTINEL : null;
}

export function sanitizePersistedAuthStorage(
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> = localStorage,
) {
  const raw = storage.getItem(AUTH_STORAGE_KEY);
  if (!raw) return;

  try {
    const parsed = JSON.parse(raw) as { state?: Record<string, unknown> };
    if (!parsed.state || typeof parsed.state !== "object") return;

    const persistedRefresh = parsed.state.refreshToken;
    if (
      typeof persistedRefresh === "string" &&
      persistedRefresh !== SERVER_REFRESH_TOKEN_SENTINEL
    ) {
      parsed.state.refreshToken = SERVER_REFRESH_TOKEN_SENTINEL;
      storage.setItem(AUTH_STORAGE_KEY, JSON.stringify(parsed));
    }
  } catch {
    storage.removeItem(AUTH_STORAGE_KEY);
  }
}

function clearServerRefreshCookie() {
  if (typeof fetch !== "function") return;
  try {
    void fetch("/api/auth/logout/", {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
    }).catch(() => undefined);
  } catch {
    // Logout must clear client state even if the network is already gone.
  }
}

function clearPaymentResumeStorage() {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.removeItem(PAYMENT_RESUME_STORAGE_KEY);
  } catch {
    // Local resume state must never keep logout from completing.
  }
}

function clearPaymentReturnSession() {
  if (typeof fetch !== "function") return;
  try {
    void fetch("/api/billing/payment-returns/session/", {
      method: "DELETE",
      credentials: "same-origin",
      keepalive: true,
    }).catch(() => undefined);
  } catch {
    // Logout must clear local state even when this best-effort request cannot start.
  }
}

export function getAuthTokenSubject(token: string | null): string | null {
  if (!token) return null;
  const payload = decodeJwtPayload(token);
  const value = payload?.sub ?? payload?.user_id;
  if (typeof value === "string" && value.length > 0) return value;
  if (typeof value === "number" && Number.isSafeInteger(value) && value > 0) return String(value);
  return null;
}

if (typeof window !== "undefined") {
  sanitizePersistedAuthStorage(window.localStorage);
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      accessToken: null,
      refreshToken: null,
      role: null,
      clubId: null,
      trainerId: null,
      studentId: null,
      authBootstrapStatus: "idle",
      studentBootstrapStatus: "idle",
      studentBootstrapError: null,
      isAuthenticated: false,
      setTokens: (access, refresh) =>
        set((current) => {
          const previousSubject = getAuthTokenSubject(current.accessToken);
          const nextSubject = getAuthTokenSubject(access);
          if (previousSubject !== nextSubject) {
            clearPaymentResumeStorage();
            clearAllContextualCommercialCommandKeys();
            purgePrivateQueryCaches();
          }
          return {
            accessToken: access,
            refreshToken: toServerRefreshSentinel(refresh ?? SERVER_REFRESH_TOKEN_SENTINEL),
            authBootstrapStatus: "ready",
            isAuthenticated: true,
          };
        }),
      setUserInfo: (role, clubId) =>
        set((current) => {
          if (current.role !== role || current.clubId !== clubId) {
            clearPaymentResumeStorage();
            clearAllContextualCommercialCommandKeys();
            purgePrivateQueryCaches();
          }
          return {
            role,
            clubId,
            trainerId: null,
            studentId: null,
            studentBootstrapStatus: role === "student" ? "idle" : "resolved",
            studentBootstrapError: null,
          };
        }),
      setTrainerId: (id) => set({ trainerId: id }),
      setStudentId: (id) => set({ studentId: id }),
      startAuthBootstrap: () =>
        set({
          authBootstrapStatus: "loading",
        }),
      resolveAuthBootstrap: () =>
        set({
          authBootstrapStatus: "ready",
        }),
      startStudentBootstrap: () =>
        set({
          studentBootstrapStatus: "loading",
          studentBootstrapError: null,
        }),
      resolveStudentBootstrap: (id) =>
        set({
          studentId: id,
          studentBootstrapStatus: "resolved",
          studentBootstrapError: null,
        }),
      failStudentBootstrap: (message) =>
        set({
          studentBootstrapStatus: "failed",
          studentBootstrapError: message,
        }),
      resetStudentBootstrap: () =>
        set({
          studentBootstrapStatus: "idle",
          studentBootstrapError: null,
        }),
      logout: () => {
        clearServerRefreshCookie();
        clearPaymentResumeStorage();
        clearPaymentReturnSession();
        clearAllContextualCommercialCommandKeys();
        purgePrivateQueryCaches();
        set({
          accessToken: null,
          refreshToken: null,
          role: null,
          clubId: null,
          trainerId: null,
          studentId: null,
          authBootstrapStatus: "idle",
          studentBootstrapStatus: "idle",
          studentBootstrapError: null,
          isAuthenticated: false,
        });
      },
    }),
    {
      name: AUTH_STORAGE_KEY,
      partialize: (state) => ({
        refreshToken: toServerRefreshSentinel(state.refreshToken),
        role: state.role,
        clubId: state.clubId,
        trainerId: state.trainerId,
        studentId: state.studentId,
        isAuthenticated: state.isAuthenticated,
      }),
      merge: (persistedState, currentState) => {
        const persisted = (persistedState ?? {}) as PersistedAuthState;
        return {
          ...currentState,
          ...persisted,
          accessToken: null,
          refreshToken: toServerRefreshSentinel(persisted.refreshToken),
          authBootstrapStatus: "idle",
        };
      },
    },
  ),
);

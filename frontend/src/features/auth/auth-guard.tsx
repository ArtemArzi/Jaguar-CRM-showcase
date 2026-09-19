/* eslint-disable react-refresh/only-export-components -- test reset helper must share module state */
import { useEffect } from "react";
import { Navigate, useLocation } from "react-router";
import { useAuthStore, type UserRole } from "@/features/auth/auth-store";
import { refreshAccessToken } from "@/api/custom-fetch";
import { ROLE_ROUTES } from "@/lib/roles";
import { decodeJwtPayload } from "@/lib/jwt";
import {
  isDocumentRoute,
  navigateToDocumentRoute,
} from "@/lib/document-routes";
import type { ReactNode } from "react";

interface AuthGuardProps {
  children: ReactNode;
  role?: UserRole;
}

let sessionRestorePromise: Promise<void> | null = null;

export function resetAuthGuardSessionRestoreForTests() {
  sessionRestorePromise = null;
}

export function AuthGuard({ children, role }: AuthGuardProps) {
  const {
    isAuthenticated,
    role: userRole,
    logout,
    accessToken,
    refreshToken,
    authBootstrapStatus,
    startAuthBootstrap,
    resolveAuthBootstrap,
  } = useAuthStore();
  const location = useLocation();

  const needsRefresh =
    isAuthenticated &&
    Boolean(refreshToken) &&
    (!accessToken || isTokenExpired(accessToken));

  useEffect(() => {
    if (!needsRefresh || !refreshToken) return;
    startAuthBootstrap();
    if (!sessionRestorePromise) {
      sessionRestorePromise = refreshAccessToken(refreshToken)
        .then(() => {
          resolveAuthBootstrap();
        })
        .finally(() => {
          sessionRestorePromise = null;
        });
    }
    sessionRestorePromise
      .catch(() => {
        logout();
      });
  }, [needsRefresh, refreshToken, logout, resolveAuthBootstrap, startAuthBootstrap]);

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  if (needsRefresh || authBootstrapStatus === "loading") {
    return <SessionRestoreScreen />;
  }

  // No access token and no refresh token -- force login
  if (!accessToken && !refreshToken) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  // Token expired and no refresh token -- force login
  if (accessToken && isTokenExpired(accessToken) && !refreshToken) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  if (role && userRole !== role) {
    const target = ROLE_ROUTES[userRole ?? ""] ?? "/login";
    if (isDocumentRoute(target)) {
      return <DocumentRedirect to={target} />;
    }
    return <Navigate to={target} replace />;
  }

  return <>{children}</>;
}

function DocumentRedirect({ to }: { to: string }) {
  useEffect(() => {
    navigateToDocumentRoute(to);
  }, [to]);

  return <SessionRestoreScreen />;
}

function SessionRestoreScreen() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-[#F5F2ED] px-6">
      <div className="flex max-w-sm flex-col items-center gap-3 text-center">
        <div className="h-10 w-10 animate-spin rounded-full border-4 border-neutral-200 border-t-neutral-700" />
        <div className="text-sm font-medium text-neutral-900">
          Восстанавливаем сессию...
        </div>
        <div className="text-sm text-neutral-600">
          Защищённые разделы откроются автоматически, как только проверим вход.
        </div>
      </div>
    </div>
  );
}

function isTokenExpired(token: string): boolean {
  const payload = decodeJwtPayload(token);
  if (!payload || typeof payload.exp !== "number") return true;
  return payload.exp * 1000 < Date.now();
}

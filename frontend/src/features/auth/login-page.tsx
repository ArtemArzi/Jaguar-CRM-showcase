import { useEffect, useState } from "react";
import { useNavigate, useLocation } from "react-router";
import axios from "axios";
import { storeRefreshTokenCookie } from "@/api/custom-fetch";
import { useAuthStore, type UserRole } from "@/features/auth/auth-store";
import { ROLE_ROUTES } from "@/lib/roles";
import { decodeJwtPayload } from "@/lib/jwt";
import {
  isDocumentRoute,
  navigateToDocumentRoute,
} from "@/lib/document-routes";

const LOGIN_URL = "/_allauth/app/v1/auth/login";

export default function LoginPage() {
  const [loginIdentifier, setLoginIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const { setTokens, setUserInfo, logout } = useAuthStore();

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("reset") !== "1") return;

    logout();
    localStorage.removeItem("jaguar-auth");
    window.history.replaceState(null, "", "/login");
  }, [logout]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const identifier = loginIdentifier.trim();
      const loginPayload = identifier.includes("@")
        ? { email: identifier, password }
        : { username: identifier, password };
      const response = await axios.post(LOGIN_URL, loginPayload);

      if (!response.data?.meta?.access_token || !response.data?.meta?.refresh_token) {
        setError("Не удалось войти. Попробуйте ещё раз.");
        return;
      }
      const { access_token, refresh_token } = response.data.meta;

      // Validate payload BEFORE storing tokens (P0-B: malformed JWT must not set isAuthenticated)
      const payload = decodeJwtPayload(access_token);
      const fromLocation = (
        location.state as {
          from?: { pathname: string; search?: string; hash?: string };
        } | null
      )?.from;
      const from = fromLocation
        ? `${fromLocation.pathname}${fromLocation.search ?? ""}${fromLocation.hash ?? ""}`
        : undefined;
      const isParentInviteReturn = fromLocation?.pathname.startsWith("/parent-invite/") ?? false;
      const isAppEntryReturn = fromLocation?.pathname === "/app";
      const hasMembershipClaims = Boolean(
        payload &&
        typeof payload.role === "string" &&
        typeof payload.club_id === "number",
      );
      if (
        !payload ||
        (!hasMembershipClaims && !isParentInviteReturn && !isAppEntryReturn)
      ) {
        setError("Не удалось подтвердить доступ. Попробуйте войти ещё раз.");
        return;
      }
      const VALID_ROLES: UserRole[] = [
        "trainer",
        "student",
        "parent",
        "owner",
        "admin",
      ];
      if (hasMembershipClaims && !VALID_ROLES.includes(payload.role as UserRole)) {
        setError("Для этого аккаунта пока не настроен кабинет.");
        return;
      }

      await storeRefreshTokenCookie(refresh_token, access_token);
      setTokens(access_token);
      if (hasMembershipClaims) {
        const role = payload.role as UserRole;
        const clubId = payload.club_id as number;
        setUserInfo(role, clubId);
      } else {
        useAuthStore.setState({
          role: null,
          clubId: null,
          trainerId: null,
          studentId: null,
          studentBootstrapStatus: "idle",
          studentBootstrapError: null,
        });
      }

      // Role-based redirect
      const target =
        from ??
        (hasMembershipClaims
          ? ROLE_ROUTES[payload.role as UserRole] ?? "/login"
          : "/login");
      if (isDocumentRoute(target)) {
        navigateToDocumentRoute(target);
        return;
      }
      navigate(target, { replace: true });
    } catch (err) {
      if (
        axios.isAxiosError(err) &&
        [400, 401].includes(err.response?.status ?? 0)
      ) {
        setError("Неверный логин или пароль");
      } else {
        setError("Не удалось подключиться. Попробуйте ещё раз.");
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-neutral-100 px-5 py-8">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm rounded-lg bg-white p-6 shadow-md sm:p-8"
      >
        <h1 className="mb-2 text-center text-2xl font-bold">Вход в кабинет</h1>
        <p className="mb-6 text-center text-sm leading-5 text-neutral-600">
          Введите телефон или email и пароль, который выдал клуб.
        </p>
        {error && (
          <div className="bg-red-50 text-red-700 p-3 rounded mb-4 text-sm">
            {error}
          </div>
        )}
        <div className="mb-4">
          <label className="block text-sm font-medium mb-1" htmlFor="email">
            Телефон или email
          </label>
          <input
            type="text"
            id="email"
            aria-label="Email или телефон"
            value={loginIdentifier}
            onChange={(e) => setLoginIdentifier(e.target.value)}
            required
            autoComplete="username"
            className="w-full border rounded px-3 py-2 focus:outline-none focus:ring-2 focus:ring-neutral-400"
          />
        </div>
        <div className="mb-6">
          <label className="block text-sm font-medium mb-1" htmlFor="password">
            Пароль
          </label>
          <input
            type="password"
            id="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            autoComplete="current-password"
            className="w-full border rounded px-3 py-2 focus:outline-none focus:ring-2 focus:ring-neutral-400"
          />
        </div>
        <button
          type="submit"
          disabled={loading}
          className="w-full py-2 rounded text-white font-medium bg-gray-900 hover:bg-gray-800 disabled:opacity-50"
          style={{ backgroundColor: "var(--branding-primary, #000)" }}
        >
          {loading ? "Входим..." : "Войти"}
        </button>
      </form>
    </div>
  );
}

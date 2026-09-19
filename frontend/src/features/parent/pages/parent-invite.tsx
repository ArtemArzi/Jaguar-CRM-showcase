import { useEffect, useRef, useState } from "react";
import { useNavigate, useLocation, useParams } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2 } from "lucide-react";
import apiClient, { refreshAccessToken } from "@/api/custom-fetch";
import { useAuthStore } from "@/features/auth/auth-store";
import { decodeJwtPayload } from "@/lib/jwt";

const INVITE_STORAGE_KEY = "jaguar-parent-invite-token";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const ERROR_COPY: Record<string, string> = {
  invite_expired: "Срок действия приглашения истёк.",
  invite_used: "Это приглашение уже использовано.",
  invite_not_found: "Ссылка приглашения не найдена или введена с ошибкой.",
  invite_access_not_ready: "Приглашение принято, но доступ к клубу не обновился. Выйдите и войдите снова.",
  already_member: "Этот аккаунт уже состоит в клубе.",
  parent_already_linked: "У ребёнка уже привязан родитель.",
  not_child: "Приглашение доступно только для детской анкеты.",
  student_deleted: "Анкета ребёнка больше недоступна.",
};

interface AcceptInviteResponse {
  student_id: number;
  student_name: string;
  club_id: number;
  club_name: string;
  access_token: string | null;
}

export default function ParentInvitePage() {
  const { token: routeToken } = useParams();
  const token = routeToken || sessionStorage.getItem(INVITE_STORAGE_KEY) || "";
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const submittedRef = useRef(false);
  const [errorMessage, setErrorMessage] = useState("");
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const isMalformedToken = !token || !UUID_RE.test(token);

  useEffect(() => {
    if (token) {
      sessionStorage.setItem(INVITE_STORAGE_KEY, token);
    }
  }, [token]);

  useEffect(() => {
    if (isMalformedToken) return;

    if (!isAuthenticated) {
      navigate("/login", { replace: true, state: { from: location } });
      return;
    }

    if (submittedRef.current) return;
    submittedRef.current = true;

    let cancelled = false;

    async function acceptInvite() {
      try {
        const response = await apiClient.post<AcceptInviteResponse>(
          "/parents/accept-invite/",
          { token },
        );
        const { refreshToken, setTokens, setUserInfo } = useAuthStore.getState();
        await applyInviteMembershipState({
          accessToken: response.data.access_token,
          refreshToken,
          expectedClubId: response.data.club_id,
          setTokens,
          setUserInfo,
        });
        void queryClient.invalidateQueries({ queryKey: ["parent", "children"] });
        sessionStorage.removeItem(INVITE_STORAGE_KEY);
        if (!cancelled) {
          navigate("/parent", { replace: true });
        }
      } catch (error) {
        submittedRef.current = false;
        if (!cancelled) {
          setErrorMessage(getInviteErrorMessage(error));
        }
      }
    }

    void acceptInvite();

    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, isMalformedToken, location, navigate, queryClient, token]);

  const isError = isMalformedToken || Boolean(errorMessage);
  const message = isMalformedToken
    ? ERROR_COPY.invite_not_found
    : errorMessage || "Привязываем кабинет родителя...";

  return (
    <main className="flex min-h-screen items-center justify-center bg-neutral-100 px-4 py-8">
      <section
        aria-live="polite"
        className="w-full max-w-md border border-neutral-200 bg-white p-6 shadow-sm"
      >
        <div className="mb-4 flex items-center gap-3">
          <span
            className="flex h-10 w-10 items-center justify-center rounded-full"
            style={{
              backgroundColor: isError ? "#FFF1F0" : "#E8F5E9",
              color: isError ? "#C45A3B" : "#2E7D32",
            }}
          >
            {isError ? (
              <AlertTriangle className="h-5 w-5" aria-hidden="true" />
            ) : (
              <CheckCircle2 className="h-5 w-5" aria-hidden="true" />
            )}
          </span>
          <div>
            <h1 className="text-lg font-semibold text-neutral-950">
              Приглашение родителя
            </h1>
            <p className="text-sm text-neutral-500">
              Доступ к кабинету ребёнка
            </p>
          </div>
        </div>
        <p className="text-sm leading-6 text-neutral-700">{message}</p>
        {isError && (
          <button
            type="button"
            onClick={() => navigate("/login", { state: { from: location } })}
            className="mt-5 min-h-11 w-full border border-neutral-900 px-4 text-sm font-semibold text-neutral-950"
          >
            Войти другим аккаунтом
          </button>
        )}
      </section>
    </main>
  );
}

class InviteAccessError extends Error {
  constructor() {
    super("invite_access_not_ready");
  }
}

async function applyInviteMembershipState({
  accessToken,
  refreshToken,
  expectedClubId,
  setTokens,
  setUserInfo,
}: {
  accessToken: string | null;
  refreshToken: string | null;
  expectedClubId: number;
  setTokens: (access: string, refresh?: string | null) => void;
  setUserInfo: (role: "parent", clubId: number) => void;
}) {
  let nextAccessToken = accessToken;

  if (!nextAccessToken && refreshToken) {
    try {
      nextAccessToken = await refreshAccessToken(refreshToken);
    } catch {
      throw new InviteAccessError();
    }
  }

  if (!nextAccessToken) {
    throw new InviteAccessError();
  }

  const payload = decodeJwtPayload(nextAccessToken);
  if (payload?.role !== "parent" || payload.club_id !== expectedClubId) {
    throw new InviteAccessError();
  }

  setTokens(nextAccessToken);
  setUserInfo("parent", expectedClubId);
}

function getInviteErrorMessage(error: unknown): string {
  if (error instanceof InviteAccessError) {
    return ERROR_COPY.invite_access_not_ready;
  }
  if (isApiError(error)) {
    const code = error.response?.data?.code;
    if (code && ERROR_COPY[code]) {
      return ERROR_COPY[code];
    }
    if (error.response?.status === 422) {
      return ERROR_COPY.invite_not_found;
    }
  }
  return "Не удалось принять приглашение. Попробуйте ещё раз.";
}

function isApiError(error: unknown): error is {
  response?: { status?: number; data?: { code?: string } };
} {
  return typeof error === "object" && error !== null && "response" in error;
}

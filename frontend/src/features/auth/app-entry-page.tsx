import { useEffect } from "react";
import { Link, Navigate, useLocation } from "react-router";
import { ArrowRight, KeyRound, ShieldCheck, Smartphone } from "lucide-react";
import { refreshAccessToken } from "@/api/custom-fetch";
import { useAuthStore, type UserRole } from "@/features/auth/auth-store";
import { useKioskStore } from "@/features/kiosk/lib/kiosk-store";
import { buttonVariants } from "@/components/ui/button-variants";
import { decodeJwtPayload } from "@/lib/jwt";
import { ROLE_ROUTES } from "@/lib/roles";
import { cn } from "@/lib/utils";

const DASHBOARD_ROLES: UserRole[] = ["owner", "admin"];
let appEntryRestorePromise: Promise<void> | null = null;

export default function AppEntryPage() {
  const location = useLocation();
  const isKioskActivated = useKioskStore((s) => s.isActivated);
  const {
    accessToken,
    authBootstrapStatus,
    isAuthenticated,
    logout,
    refreshToken,
    role,
    resolveAuthBootstrap,
    startAuthBootstrap,
  } = useAuthStore();
  const needsRefresh =
    isAuthenticated &&
    Boolean(refreshToken) &&
    (!accessToken || isTokenExpired(accessToken));

  useEffect(() => {
    if (!needsRefresh || !refreshToken) return;
    startAuthBootstrap();
    if (!appEntryRestorePromise) {
      appEntryRestorePromise = refreshAccessToken(refreshToken)
        .then(() => {
          resolveAuthBootstrap();
        })
        .finally(() => {
          appEntryRestorePromise = null;
        });
    }
    appEntryRestorePromise.catch(() => {
      logout();
    });
  }, [
    logout,
    needsRefresh,
    refreshToken,
    resolveAuthBootstrap,
    startAuthBootstrap,
  ]);

  if (needsRefresh || authBootstrapStatus === "loading") {
    return <AppEntryRestoreScreen />;
  }

  if (isKioskActivated && !isAuthenticated) {
    return <Navigate to="/kiosk" replace />;
  }

  if (isAuthenticated && role && DASHBOARD_ROLES.includes(role)) {
    return <DashboardEntry />;
  }

  if (isAuthenticated && role) {
    return <Navigate to={ROLE_ROUTES[role] ?? "/login"} replace />;
  }

  if (isAuthenticated && !role) {
    return <PendingMembershipEntry />;
  }

  return (
    <main className="min-h-screen bg-[#F5F2ED] px-5 py-6 text-neutral-950">
      <section className="mx-auto flex min-h-[calc(100vh-3rem)] w-full max-w-md flex-col justify-between">
        <div className="pt-5">
          <div className="mb-8 flex items-center gap-3">
            <div
              className="flex h-12 w-12 items-center justify-center rounded-lg text-white"
              style={{ backgroundColor: "var(--branding-primary, #000000)" }}
            >
              <ShieldCheck className="h-6 w-6" aria-hidden="true" />
            </div>
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-neutral-500">
                CRM Jaguar
              </p>
              <h1 className="text-[22px] font-semibold leading-tight">
                Кабинет клуба
              </h1>
            </div>
          </div>

          <div className="space-y-4">
            <h2 className="text-[30px] font-semibold leading-[1.08] tracking-normal">
              Один вход для тренировок, оплат и расписания
            </h2>
            <p className="text-[15px] leading-6 text-neutral-600">
              Откройте кабинет с телефона. Если вы уже входили на этом
              устройстве, система сразу вернёт вас в нужный раздел.
            </p>
          </div>
        </div>

        <div className="space-y-5 pb-4">
          <div className="grid gap-2 text-[13px] text-neutral-600">
            <div className="ui-row-2">
              <Smartphone className="h-4 w-4 text-neutral-500" aria-hidden="true" />
              <span>Быстрый вход с телефона после первой авторизации</span>
            </div>
            <div className="ui-row-2">
              <KeyRound className="h-4 w-4 text-neutral-500" aria-hidden="true" />
              <span>Роль определяется автоматически после входа</span>
            </div>
          </div>

          <Link
            to="/login"
            state={{ from: location }}
            className={cn(
              buttonVariants({ size: "lg" }),
              "min-h-12 w-full justify-between px-5 text-[15px]",
            )}
          >
            Войти в кабинет
            <ArrowRight className="h-5 w-5" aria-hidden="true" />
          </Link>
        </div>
      </section>
    </main>
  );
}

function AppEntryRestoreScreen() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-[#F5F2ED] px-6">
      <section className="flex max-w-sm flex-col items-center gap-3 text-center">
        <div className="h-10 w-10 animate-spin rounded-full border-4 border-neutral-200 border-t-neutral-800" />
        <h1 className="text-[18px] font-semibold text-neutral-950">
          Восстанавливаем вход
        </h1>
        <p className="text-[14px] leading-6 text-neutral-600">
          Сейчас проверим сохранённую сессию и откроем нужный кабинет.
        </p>
      </section>
    </main>
  );
}

function DashboardEntry() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-[#F5F2ED] px-5 py-8 text-neutral-950">
      <section className="w-full max-w-md space-y-5">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-neutral-500">
            Кабинет клуба
          </p>
          <h1 className="mt-2 text-[24px] font-semibold leading-tight">
            Панель управления клубом
          </h1>
          <p className="mt-3 text-[15px] leading-6 text-neutral-600">
            Здесь владелец или администратор управляет учениками, оплатами,
            расписанием и заявками. Кабинеты тренеров, учеников и родителей
            откроются автоматически после входа под их ролью.
          </p>
        </div>
        <a
          href="/dashboard/login/"
          className={cn(
            buttonVariants({ size: "lg" }),
            "min-h-12 w-full justify-between px-5",
          )}
        >
          Открыть панель управления
          <ArrowRight className="h-5 w-5" aria-hidden="true" />
        </a>
      </section>
    </main>
  );
}

function isTokenExpired(token: string): boolean {
  const payload = decodeJwtPayload(token);
  if (!payload || typeof payload.exp !== "number") return true;
  return payload.exp * 1000 < Date.now();
}

function PendingMembershipEntry() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-[#F5F2ED] px-5 py-8 text-neutral-950">
      <section className="w-full max-w-md space-y-5">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-neutral-500">
            Кабинет клуба
          </p>
          <h1 className="mt-2 text-[24px] font-semibold leading-tight">
            Нужна ссылка-приглашение
          </h1>
          <p className="mt-3 text-[15px] leading-6 text-neutral-600">
            Вы вошли, но аккаунт ещё не привязан к клубу. Попросите
            администратора выдать приглашение или войдите другим аккаунтом.
          </p>
        </div>
        <Link
          to="/login?reset=1"
          className={cn(
            buttonVariants({ variant: "outline", size: "lg" }),
            "min-h-12 w-full justify-between px-5",
          )}
        >
          Войти другим аккаунтом
          <ArrowRight className="h-5 w-5" aria-hidden="true" />
        </Link>
      </section>
    </main>
  );
}

import { lazy, Suspense, useState } from "react";
import { useNavigate } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { LogOut, Banknote, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import apiClient from "@/api/custom-fetch";
import { getInitials, formatRub } from "@/lib/utils";
import type { EarningSummary } from "../types";

const NotificationPreferences = lazy(
  () => import("@/features/notifications/components/notification-preferences"),
);

interface TrainerMe {
  id: number;
  first_name: string;
  last_name: string;
  student_count?: number;
}

export default function Profile() {
  const [showConfirm, setShowConfirm] = useState(false);
  const navigate = useNavigate();
  const trainerId = useAuthStore((s) => s.trainerId);
  const clubName = useBrandingStore((s) => s.clubName);

  const { data: trainerInfo } = useQuery<TrainerMe>({
    queryKey: ["trainer", "me"],
    queryFn: () => apiClient.get("/trainers/me/").then((r) => r.data),
    staleTime: 5 * 60_000,
  });

  const { data: earnings } = useQuery<EarningSummary>({
    queryKey: ["trainer", "earnings", trainerId],
    queryFn: () =>
      apiClient
        .get(`/trainers/${trainerId}/earnings/summary/`)
        .then((r) => r.data),
    enabled: !!trainerId,
    staleTime: 5 * 60_000,
  });

  function handleLogout() {
    useAuthStore.getState().logout();
    navigate("/login", { replace: true });
  }

  const fullName = trainerInfo
    ? `${trainerInfo.first_name} ${trainerInfo.last_name}`.trim()
    : "";
  const displayName = fullName || "Тренер";

  return (
    <div className="flex flex-col min-h-[calc(100vh-80px)]">
      {/* Header with avatar */}
      <div
        className="flex flex-col items-center gap-3 px-4 pt-8 pb-6"
        style={{ backgroundColor: "var(--branding-primary, #000)" }}
      >
        <div className="flex h-20 w-20 items-center justify-center rounded-full bg-[var(--branding-accent)] text-white text-[28px] font-semibold">
          {getInitials(displayName)}
        </div>
        <div className="text-center">
          <h1 className="text-[22px] font-semibold text-white">
            {displayName}
          </h1>
          <span className="inline-block mt-1 px-3 py-0.5 rounded-full bg-white/20 text-[13px] text-white/80">
            Тренер
          </span>
        </div>
      </div>

      {/* Content */}
      <div className="flex flex-col gap-4 p-4 flex-1">
        <h2 className="sr-only">Профиль</h2>
        {/* Salary navigation card */}
        <button
          type="button"
          onClick={() => navigate("/trainer/salary")}
          className="flex items-center gap-4 rounded-xl bg-white p-4 ring-1 ring-foreground/5 text-left w-full active:scale-[0.98] transition-transform"
        >
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[var(--branding-accent)]/10">
            <Banknote size={20} className="text-[var(--branding-accent)]" />
          </div>
          <div className="flex-1">
            <p className="ui-title-16">
              Мой заработок
            </p>
            <p className="ui-muted-14">
              {earnings
                ? `${formatRub(earnings.total_amount)} за текущий месяц`
                : "Посмотреть начисления"}
            </p>
          </div>
          <ChevronRight size={20} className="text-muted-foreground shrink-0" />
        </button>

        {/* Notification preferences */}
        <Suspense fallback={null}>
          <NotificationPreferences role="trainer" />
        </Suspense>

        {/* Club info */}
        {clubName && (
          <div className="ui-card">
            <p className="text-[12px] font-semibold uppercase tracking-wider text-muted-foreground">
              Клуб
            </p>
            <p className="mt-1 break-words text-[16px] leading-snug text-foreground">
              {clubName}
            </p>
          </div>
        )}

        {/* Spacer */}
        <div className="flex-1" />

        {/* Logout */}
        {showConfirm ? (
          <div className="flex flex-col gap-3 rounded-xl bg-white p-4 ring-1 ring-foreground/5">
            <p className="text-[16px] font-semibold text-foreground text-center">
              Вы уверены?
            </p>
            <div className="flex gap-3">
              <Button
                variant="outline"
                className="flex-1"
                onClick={() => setShowConfirm(false)}
              >
                Отмена
              </Button>
              <Button
                variant="destructive"
                className="flex-1"
                onClick={handleLogout}
              >
                Выйти
              </Button>
            </div>
          </div>
        ) : (
          <Button
            variant="destructive"
            className="w-full"
            onClick={() => setShowConfirm(true)}
          >
            <LogOut size={18} className="mr-2" />
            Выйти из аккаунта
          </Button>
        )}
      </div>
    </div>
  );
}

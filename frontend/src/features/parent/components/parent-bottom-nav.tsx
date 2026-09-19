import { useMemo } from "react";
import { Link, useLocation } from "react-router";
import { CalendarDays, CreditCard, Settings, UsersRound } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import apiClient from "@/api/custom-fetch";
import { cn } from "@/lib/utils";
import { useBrandingStore } from "@/features/branding/use-branding";
import { usePrivateQueryScope } from "@/features/auth/private-query-scope";
import {
  getContrastingAccent,
  isDarkBackground,
} from "@/features/branding/use-contrast-color";

interface ParentNavChild {
  id: number;
}

function currentChildId(pathname: string): number | null {
  const match = /^\/parent\/child\/(\d+)/.exec(pathname);
  return match ? Number(match[1]) : null;
}

export function ParentBottomNav() {
  const { scope: parentPrivateScope, isReady: parentPrivateReady } = usePrivateQueryScope("parent");
  const location = useLocation();
  const primaryColor = useBrandingStore((s) => s.primaryColor);
  const accentColor = useBrandingStore((s) => s.accentColor);
  const dark = useMemo(() => isDarkBackground(primaryColor), [primaryColor]);
  const activeColor = useMemo(
    () => getContrastingAccent(primaryColor, accentColor),
    [primaryColor, accentColor],
  );
  const inactiveClass = dark ? "text-white/50" : "text-gray-900/40";

  const { data: children = [] } = useQuery<ParentNavChild[]>({
    queryKey: ["parent", "children", ...parentPrivateScope],
    queryFn: () =>
      apiClient.get<ParentNavChild[]>("/parents/children/").then((r) => r.data),
    staleTime: 5 * 60_000,
    enabled: parentPrivateReady,
  });

  const selectedChildId = currentChildId(location.pathname);
  const activeChildId = selectedChildId ?? children[0]?.id ?? null;
  const childTarget = activeChildId ? `/parent/child/${activeChildId}` : "/parent";
  const items = [
    { to: "/parent", icon: UsersRound, label: "Дети", isActive: location.pathname === "/parent" && location.hash !== "#settings" },
    {
      to: activeChildId ? `${childTarget}#groups` : "/parent",
      icon: CalendarDays,
      label: "Занятия",
      isActive:
        location.pathname === childTarget &&
        (location.hash === "#groups" || location.hash === ""),
    },
    {
      to: activeChildId ? `${childTarget}#subscription` : "/parent",
      icon: CreditCard,
      label: "Оплата",
      isActive: location.pathname === childTarget && location.hash === "#subscription",
    },
    {
      to: "/parent#settings",
      icon: Settings,
      label: "Настройки",
      isActive: location.pathname === "/parent" && location.hash === "#settings",
    },
  ] as const;

  return (
    <nav
      aria-label="Нижняя навигация родителя"
      className="fixed bottom-0 left-0 right-0 z-40 border-t border-black/6 bg-[var(--branding-primary)]/96 backdrop-blur-sm"
      style={{ paddingBottom: "calc(env(safe-area-inset-bottom, 0px) + 10px)" }}
    >
      <div className="mx-auto flex max-w-screen-sm items-center justify-between gap-2 px-3 pt-2">
        {items.map(({ to, icon: Icon, label, isActive }) => (
          <Link
            key={label}
            to={to}
            className={cn(
              "flex min-h-[56px] min-w-0 flex-1 flex-col items-center justify-center gap-1 rounded-2xl px-2 py-2 text-center transition",
              isActive
                ? "font-semibold shadow-[0_8px_24px_rgba(0,0,0,0.14)]"
                : `${inactiveClass} font-normal`,
            )}
            style={
              isActive
                ? {
                    color: activeColor,
                    backgroundColor: dark
                      ? "rgba(255,255,255,0.12)"
                      : "rgba(255,255,255,0.7)",
                  }
                : undefined
            }
          >
            <Icon
              size={20}
              className={isActive ? undefined : inactiveClass}
              style={isActive ? { color: activeColor } : undefined}
            />
            <span className="text-[11px] font-medium leading-tight">{label}</span>
          </Link>
        ))}
      </div>
    </nav>
  );
}

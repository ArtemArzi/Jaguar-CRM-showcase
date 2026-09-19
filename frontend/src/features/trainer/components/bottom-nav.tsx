import { useMemo } from "react";
import { NavLink } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { CalendarDays, Users, CheckSquare, User, UserPlus } from "lucide-react";
import { cn } from "@/lib/utils";
import apiClient from "@/api/custom-fetch";
import { useBrandingStore } from "@/features/branding/use-branding";
import { isDarkBackground, getContrastingAccent } from "@/features/branding/use-contrast-color";

const NAV_ITEMS = [
  { to: "/trainer", icon: CalendarDays, label: "Расписание", end: true },
  { to: "/trainer/leads", icon: UserPlus, label: "Заявки", end: false },
  { to: "/trainer/students", icon: Users, label: "Ученики", end: false },
  { to: "/trainer/tasks", icon: CheckSquare, label: "Задачи", end: false },
  { to: "/trainer/profile", icon: User, label: "Профиль", end: false },
] as const;

export function BottomNav() {
  const primaryColor = useBrandingStore((s) => s.primaryColor);
  const accentColor = useBrandingStore((s) => s.accentColor);
  const dark = useMemo(() => isDarkBackground(primaryColor), [primaryColor]);
  const activeColor = useMemo(
    () => getContrastingAccent(primaryColor, accentColor),
    [primaryColor, accentColor],
  );
  const inactiveClass = dark ? "text-white/50" : "text-gray-900/40";

  const { data: taskCount } = useQuery<number>({
    queryKey: ["task-badge-count"],
    queryFn: () =>
      apiClient
        .get("/retention/tasks/", { params: { resolved: false, limit: 1, offset: 0 } })
        .then((r) => {
          if (!Array.isArray(r.data) && typeof r.data.count === "number") {
            return r.data.count;
          }
          const items = r.data.items ?? r.data;
          return Array.isArray(items) ? items.length : 0;
        }),
    staleTime: 60_000,
  });

  return (
    <nav
      className="fixed bottom-0 left-0 right-0 z-40 grid items-stretch bg-[var(--branding-primary)]"
      style={{
        height: "calc(64px + env(safe-area-inset-bottom, 0px))",
        paddingBottom: "calc(16px + env(safe-area-inset-bottom, 0px))",
        gridTemplateColumns: `repeat(${NAV_ITEMS.length}, minmax(0, 1fr))`,
      }}
    >
      {NAV_ITEMS.map(({ to, icon: Icon, label, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          aria-label={label}
          title={label}
          className={({ isActive }) =>
            cn(
              "relative flex min-h-[48px] min-w-0 flex-col items-center justify-center gap-0.5 px-0.5",
              isActive ? "font-semibold" : `${inactiveClass} font-normal`,
            )
          }
          style={({ isActive }) => (isActive ? { color: activeColor } : undefined)}
        >
          {({ isActive }) => (
            <>
              <span className="relative flex h-6 w-6 shrink-0 items-center justify-center">
                <Icon
                  size={22}
                  className={isActive ? undefined : inactiveClass}
                  style={isActive ? { color: activeColor } : undefined}
                />
                {to === "/trainer/tasks" && taskCount != null && taskCount > 0 && (
                  <span
                    aria-hidden="true"
                    className="absolute -right-2 -top-1 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-bold leading-none text-white"
                  >
                    {taskCount > 99 ? "99+" : taskCount}
                  </span>
                )}
              </span>
              <span className="block max-w-full whitespace-nowrap text-center text-[11px] leading-[12px]">
                {label}
              </span>
            </>
          )}
        </NavLink>
      ))}
    </nav>
  );
}

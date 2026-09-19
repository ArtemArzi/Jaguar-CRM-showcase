import { useMemo } from "react";
import { NavLink } from "react-router";
import { Home, CalendarDays, CheckCircle, User } from "lucide-react";
import { cn } from "@/lib/utils";
import { useBrandingStore } from "@/features/branding/use-branding";
import { isDarkBackground, getContrastingAccent } from "@/features/branding/use-contrast-color";

const NAV_ITEMS = [
  { to: "/student", icon: Home, label: "Главная", end: true },
  {
    to: "/student/schedule",
    icon: CalendarDays,
    label: "Расписание",
    end: false,
  },
  {
    to: "/student/attendance",
    icon: CheckCircle,
    label: "Посещения",
    end: false,
  },
  { to: "/student/profile", icon: User, label: "Профиль", end: false },
] as const;

export function StudentBottomNav() {
  const primaryColor = useBrandingStore((s) => s.primaryColor);
  const accentColor = useBrandingStore((s) => s.accentColor);
  const dark = useMemo(() => isDarkBackground(primaryColor), [primaryColor]);
  const activeColor = useMemo(
    () => getContrastingAccent(primaryColor, accentColor),
    [primaryColor, accentColor],
  );
  const inactiveClass = dark ? "text-white/50" : "text-gray-900/40";

  return (
    <nav
      className="fixed bottom-0 left-0 right-0 z-40 border-t border-black/6 bg-[var(--branding-primary)]/96 backdrop-blur-sm"
      style={{ paddingBottom: "calc(env(safe-area-inset-bottom, 0px) + 10px)" }}
    >
      <div className="mx-auto flex max-w-screen-sm items-center justify-between gap-2 px-3 pt-2">
        {NAV_ITEMS.map(({ to, icon: Icon, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              cn(
                "flex min-h-[56px] min-w-0 flex-1 flex-col items-center justify-center gap-1 rounded-2xl px-2 py-2 text-center transition",
                isActive
                  ? "font-semibold shadow-[0_8px_24px_rgba(0,0,0,0.14)]"
                  : `${inactiveClass} font-normal`,
              )
            }
            style={({ isActive }) =>
              isActive
                ? {
                    color: activeColor,
                    backgroundColor: dark ? "rgba(255,255,255,0.12)" : "rgba(255,255,255,0.7)",
                  }
                : undefined
            }
          >
            {({ isActive }) => (
              <>
                <Icon
                  size={20}
                  className={isActive ? undefined : inactiveClass}
                  style={isActive ? { color: activeColor } : undefined}
                />
                <span className="text-[11px] font-medium leading-tight">
                  {label}
                </span>
              </>
            )}
          </NavLink>
        ))}
      </div>
    </nav>
  );
}

import { useMemo } from "react";
import { Swords } from "lucide-react";
import { cn } from "@/lib/utils";
import { useBrandingStore } from "@/features/branding/use-branding";
import {
  getContrastingAccent,
  isDarkBackground,
} from "@/features/branding/use-contrast-color";

export function StudentBrandedHeader() {
  const clubName = useBrandingStore((s) => s.clubName);
  const logoUrl = useBrandingStore((s) => s.logoUrl);
  const primaryColor = useBrandingStore((s) => s.primaryColor);
  const accentColor = useBrandingStore((s) => s.accentColor);

  const dark = useMemo(() => isDarkBackground(primaryColor), [primaryColor]);
  const textClass = dark ? "text-white" : "text-neutral-950";
  const emblemColor = useMemo(
    () => getContrastingAccent(primaryColor, accentColor),
    [primaryColor, accentColor],
  );
  const brandLabel = clubName.trim().toUpperCase();

  return (
    <header
      className="sticky top-0 z-30 border-b border-black/6 bg-[var(--branding-primary)]/95 backdrop-blur-sm"
      data-student-shell-header
    >
      <div className="mx-auto flex h-16 max-w-screen-sm items-center gap-3 px-5">
        {logoUrl ? (
          <img
            src={logoUrl}
            alt=""
            className="h-10 w-10 shrink-0 rounded-2xl object-contain ring-1 ring-white/12"
          />
        ) : (
          <div
            className={cn(
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl ring-1",
              dark ? "bg-white/12 ring-white/14" : "bg-black/6 ring-black/8",
            )}
          >
            <Swords size={18} style={{ color: emblemColor }} />
          </div>
        )}

        <div className="min-w-0 flex-1">
          <p
            className={cn(
              "truncate text-[13px] font-bold tracking-[0.18em] sm:text-[14px]",
              textClass,
            )}
          >
            {brandLabel}
          </p>
        </div>
      </div>
    </header>
  );
}

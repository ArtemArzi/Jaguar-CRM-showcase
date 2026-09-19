import { Users } from "lucide-react";
import { useBrandingStore } from "@/features/branding/use-branding";
import {
  getContrastingAccent,
  isDarkBackground,
} from "@/features/branding/use-contrast-color";
import { cn } from "@/lib/utils";

export function ParentBrandedHeader() {
  const clubName = useBrandingStore((s) => s.clubName);
  const logoUrl = useBrandingStore((s) => s.logoUrl);
  const primaryColor = useBrandingStore((s) => s.primaryColor);
  const accentColor = useBrandingStore((s) => s.accentColor);

  const dark = isDarkBackground(primaryColor);
  const textClass = dark ? "text-white" : "text-neutral-950";
  const emblemColor = getContrastingAccent(primaryColor, accentColor);
  const brandLabel = clubName.trim().toUpperCase();

  return (
    <header
      aria-label="Кабинет родителя"
      className="sticky top-0 z-30 border-b border-black/6 bg-[var(--branding-primary)]/95 backdrop-blur-sm"
      data-parent-shell-header
    >
      <div className="mx-auto flex h-16 max-w-screen-sm items-center gap-3 px-5">
        {logoUrl ? (
          <img
            src={logoUrl}
            alt=""
            className="h-10 w-10 shrink-0 rounded-2xl bg-white/8 object-contain p-1 ring-1 ring-white/14"
          />
        ) : (
          <div
            className={cn(
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl ring-1",
              dark ? "bg-white/12 ring-white/14" : "bg-black/6 ring-black/8",
            )}
          >
            <Users size={18} style={{ color: emblemColor }} />
          </div>
        )}

        <div className="min-w-0 flex-1">
          <p
            className={cn(
              "truncate text-[11px] font-bold tracking-[0.18em] opacity-[0.82]",
              textClass,
            )}
          >
            {brandLabel}
          </p>
          <p className={cn("truncate text-[14px] font-semibold leading-tight", textClass)}>
            Кабинет родителя
          </p>
        </div>
      </div>
    </header>
  );
}

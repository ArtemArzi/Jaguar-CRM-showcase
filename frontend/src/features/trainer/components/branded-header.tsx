import { useMemo } from "react";
import { Swords } from "lucide-react";
import { useBrandingStore } from "@/features/branding/use-branding";
import { isDarkBackground } from "@/features/branding/use-contrast-color";

export function BrandedHeader() {
  const clubName = useBrandingStore((s) => s.clubName);
  const logoUrl = useBrandingStore((s) => s.logoUrl);
  const primaryColor = useBrandingStore((s) => s.primaryColor);
  const dark = useMemo(() => isDarkBackground(primaryColor), [primaryColor]);
  const textClass = dark ? "text-white" : "text-gray-900";

  return (
    <header className="flex h-14 items-center gap-3 px-4 bg-[var(--branding-primary)]">
      {logoUrl ? (
        <img
          src={logoUrl}
          alt=""
          className="h-8 w-8 shrink-0 rounded object-contain"
        />
      ) : (
        <div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${dark ? "bg-white/20" : "bg-black/10"}`}>
          <Swords size={18} className={textClass} />
        </div>
      )}
      <h1 className={`truncate text-[15px] font-bold uppercase tracking-[0.06em] ${textClass}`}>
        {clubName}
      </h1>
    </header>
  );
}

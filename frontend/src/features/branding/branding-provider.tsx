import { useEffect, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import apiClient from "@/api/custom-fetch";

function applyBrandingCSS(data: {
  primary_color: string;
  accent_color: string;
  club_name_display?: string;
  logo_url?: string;
  timezone?: string;
}) {
  const root = document.documentElement;
  root.style.setProperty("--branding-primary", data.primary_color);
  root.style.setProperty("--branding-accent", data.accent_color);
  root.style.setProperty(
    "--branding-name",
    data.club_name_display || "",
  );
  if (data.logo_url) {
    root.style.setProperty("--branding-logo", `url(${data.logo_url})`);
  }
}

export function BrandingProvider({ children }: { children: ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const clubId = useAuthStore((s) => s.clubId);
  const setBranding = useBrandingStore((s) => s.setBranding);
  const markTimeZonePending = useBrandingStore((s) => s.markTimeZonePending);
  const markTimeZoneFailed = useBrandingStore((s) => s.markTimeZoneFailed);

  const { data, isError } = useQuery({
    queryKey: ["branding", clubId],
    queryFn: () => apiClient.get("/billing/settings/").then((r) => r.data),
    enabled: isAuthenticated,
    staleTime: 5 * 60_000,
    retry: false,
  });

  useEffect(() => {
    if (!isAuthenticated || clubId === null) {
      markTimeZonePending(null);
      return;
    }
    if (isError) {
      markTimeZoneFailed(clubId);
      return;
    }
    if (!data) {
      markTimeZonePending(clubId);
      return;
    }
    setBranding(data, clubId);
    applyBrandingCSS(data);
  }, [clubId, data, isAuthenticated, isError, markTimeZoneFailed, markTimeZonePending, setBranding]);

  return <>{children}</>;
}

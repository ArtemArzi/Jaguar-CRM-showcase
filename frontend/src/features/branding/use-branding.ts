import { create } from "zustand";
import { APP_TIME_ZONE } from "@/lib/locale";

export type BrandingTimeZoneStatus = "pending" | "ready" | "failed";

interface BrandingPayload {
  primary_color: string;
  accent_color: string;
  club_name_display: string;
  logo_url: string;
  timezone?: string;
}

function hasSupportedTimeZone(timeZone: string | undefined): timeZone is string {
  if (!timeZone) return false;
  try {
    new Intl.DateTimeFormat("en-CA", { timeZone }).format();
    return true;
  } catch {
    return false;
  }
}

interface BrandingState {
  primaryColor: string;
  accentColor: string;
  clubName: string;
  logoUrl: string;
  timeZone: string;
  timeZoneStatus: BrandingTimeZoneStatus;
  timeZoneClubId: number | null;
  isTimeZoneAuthoritative: boolean;
  setBranding: (branding: BrandingPayload, clubId: number | null) => void;
  markTimeZonePending: (clubId: number | null) => void;
  markTimeZoneFailed: (clubId: number | null) => void;
}

export const useBrandingStore = create<BrandingState>()((set) => ({
  primaryColor: "#000000",
  accentColor: "#FF6B00",
  clubName: "CRM Jaguar",
  logoUrl: "",
  timeZone: APP_TIME_ZONE,
  timeZoneStatus: "pending",
  timeZoneClubId: null,
  isTimeZoneAuthoritative: false,
  setBranding: (b, clubId) =>
    set({
      primaryColor: b.primary_color,
      accentColor: b.accent_color,
      clubName: b.club_name_display || "CRM Jaguar",
      logoUrl: b.logo_url,
      timeZone: b.timezone || APP_TIME_ZONE,
      timeZoneStatus: hasSupportedTimeZone(b.timezone) ? "ready" : "failed",
      timeZoneClubId: clubId,
      isTimeZoneAuthoritative: hasSupportedTimeZone(b.timezone),
    }),
  markTimeZonePending: (clubId) =>
    set({
      timeZone: APP_TIME_ZONE,
      timeZoneStatus: "pending",
      timeZoneClubId: clubId,
      isTimeZoneAuthoritative: false,
    }),
  markTimeZoneFailed: (clubId) =>
    set({
      timeZone: APP_TIME_ZONE,
      timeZoneStatus: "failed",
      timeZoneClubId: clubId,
      isTimeZoneAuthoritative: false,
    }),
}));

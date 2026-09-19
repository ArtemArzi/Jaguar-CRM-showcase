import { describe, it, expect, beforeEach } from "vitest";
import { APP_TIME_ZONE } from "@/lib/locale";
import { useBrandingStore } from "./use-branding";

describe("branding-store", () => {
  beforeEach(() => {
    useBrandingStore.setState({
      primaryColor: "#000000",
      accentColor: "#FF6B00",
      clubName: "CRM Jaguar",
      logoUrl: "",
      timeZone: APP_TIME_ZONE,
      timeZoneStatus: "pending",
      timeZoneClubId: null,
      isTimeZoneAuthoritative: false,
    });
  });

  it("setBranding updates all fields", () => {
    useBrandingStore.getState().setBranding({
      primary_color: "#1E40AF",
      accent_color: "#F59E0B",
      club_name_display: "Fight Club",
      logo_url: "https://example.com/logo.png",
      timezone: "Asia/Yekaterinburg",
    }, 1);
    const state = useBrandingStore.getState();
    expect(state.primaryColor).toBe("#1E40AF");
    expect(state.accentColor).toBe("#F59E0B");
    expect(state.clubName).toBe("Fight Club");
    expect(state.logoUrl).toBe("https://example.com/logo.png");
    expect(state.timeZone).toBe("Asia/Yekaterinburg");
    expect(state.isTimeZoneAuthoritative).toBe(true);
    expect(state.timeZoneClubId).toBe(1);
  });

  it("uses default club name when display name is empty", () => {
    useBrandingStore.getState().setBranding({
      primary_color: "#000",
      accent_color: "#FFF",
      club_name_display: "",
      logo_url: "",
    }, 1);
    expect(useBrandingStore.getState().clubName).toBe("CRM Jaguar");
    expect(useBrandingStore.getState().timeZone).toBe(APP_TIME_ZONE);
    expect(useBrandingStore.getState().isTimeZoneAuthoritative).toBe(false);
  });
});

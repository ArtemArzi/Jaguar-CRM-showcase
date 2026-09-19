import { lazy, Suspense, useEffect, useState } from "react";
import { useKioskStore } from "@/features/kiosk/lib/kiosk-store";
import { fetchBranding } from "@/features/kiosk/lib/kiosk-api";
import { useKioskNav } from "@/features/kiosk/kiosk-router";
import { SyncIndicator } from "@/features/kiosk/components/sync-indicator";
import { useOfflineSync } from "@/features/kiosk/hooks/use-offline-sync";
import NumpadHome from "@/features/kiosk/pages/numpad-home";
import MatchSelect from "@/features/kiosk/pages/match-select";
import ResultScreen from "@/features/kiosk/pages/result-screen";
import CascadeFeedback from "@/features/kiosk/components/cascade-feedback";

const PinActivation = lazy(
  () => import("@/features/kiosk/pages/pin-activation"),
);

function KioskLoading() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-[#1A1A1A]">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-neutral-600 border-t-white" />
    </div>
  );
}

// ── KioskBrandingProvider ─────────────────────────
// Fetches branding via device token (X-Kiosk-Token), NOT JWT.
// Sets --branding-primary, --branding-accent, --branding-logo CSS vars.

function useKioskBranding() {
  const isActivated = useKioskStore((s) => s.isActivated);
  const [logoUrl, setLogoUrl] = useState("");

  useEffect(() => {
    if (!isActivated) {
      // Apply defaults
      document.documentElement.style.setProperty(
        "--branding-primary",
        "#000000",
      );
      document.documentElement.style.setProperty(
        "--branding-accent",
        "#ff6b00",
      );
      document.documentElement.style.setProperty("--branding-logo", "");
      return;
    }

    fetchBranding()
      .then((data) => {
        document.documentElement.style.setProperty(
          "--branding-primary",
          data.primary_color || "#000000",
        );
        document.documentElement.style.setProperty(
          "--branding-accent",
          data.accent_color || "#ff6b00",
        );
        const rawLogo = data.logo_url || "";
        const logo = rawLogo.startsWith("http") ? rawLogo : "";
        document.documentElement.style.setProperty(
          "--branding-logo",
          logo ? `url(${logo})` : "",
        );
        setLogoUrl(logo);
      })
      .catch(() => {
        // Fallback defaults
        document.documentElement.style.setProperty(
          "--branding-primary",
          "#000000",
        );
        document.documentElement.style.setProperty(
          "--branding-accent",
          "#ff6b00",
        );
      });
  }, [isActivated]);

  return { logoUrl: isActivated ? logoUrl : "" };
}

// ── KioskShell ────────────────────────────────────

export default function KioskShell() {
  const { logoUrl } = useKioskBranding();
  const clubName = useKioskStore((s) => s.clubName);
  const {
    nav,
    goToNumpad,
    goToMatches,
    goToResult,
    goToFeedback,
    goToErrorFeedback,
    onActivated,
    setSchedules,
  } = useKioskNav();

  const isActivated = useKioskStore((s) => s.isActivated);
  const {
    status,
    pendingCount,
    syncError,
    rejectedCheckins,
    acknowledgeRejected,
  } = useOfflineSync(isActivated);

  return (
    <div className="min-h-screen bg-[#1A1A1A] text-white">
      {isActivated && (
        <SyncIndicator
          status={status}
          pendingCount={pendingCount}
          syncError={syncError}
          rejectedCheckins={rejectedCheckins}
          onAcknowledgeRejected={acknowledgeRejected}
        />
      )}

      <Suspense fallback={<KioskLoading />}>
        {!isActivated && <PinActivation onActivated={onActivated} />}

        {isActivated && nav.screen === "pin" && (
          <PinActivation onActivated={onActivated} />
        )}

        {isActivated && nav.screen === "numpad" && (
          <NumpadHome
            logoUrl={logoUrl}
            clubName={clubName ?? ""}
            goToMatches={goToMatches}
            goToResult={goToResult}
            goToFeedback={goToFeedback}
            goToErrorFeedback={goToErrorFeedback}
            schedules={nav.schedules}
            setSchedules={setSchedules}
          />
        )}

        {isActivated && nav.screen === "matches" && (
          <MatchSelect
            matches={nav.matches}
            phoneSuffix={nav.phoneSuffix}
            schedules={nav.schedules}
            goToNumpad={goToNumpad}
            goToResult={goToResult}
            goToFeedback={goToFeedback}
            goToErrorFeedback={goToErrorFeedback}
          />
        )}

        {isActivated && nav.screen === "result" && nav.selectedStudent && (
          <ResultScreen
            student={nav.selectedStudent}
            schedules={nav.schedules}
            kioskOptions={nav.kioskOptions}
            goToFeedback={goToFeedback}
            goToNumpad={goToNumpad}
          />
        )}

        {isActivated && nav.screen === "feedback" && nav.selectedStudent && (
          <CascadeFeedback
            student={nav.selectedStudent}
            result={nav.checkinResult}
            error={nav.checkinError}
            goToNumpad={goToNumpad}
          />
        )}
      </Suspense>
    </div>
  );
}

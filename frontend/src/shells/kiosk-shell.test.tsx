import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/features/kiosk/lib/kiosk-api", () => ({
  fetchBranding: vi.fn().mockResolvedValue({
    primary_color: "#000000",
    accent_color: "#ff6b00",
    club_name_display: "Jaguar",
    logo_url: "",
  }),
}));

const offlineSyncState = vi.hoisted(() => ({
  syncError: null as string | null,
  rejectedCheckins: [] as Array<{
    stable_key: string;
    student_id: number;
    schedule_id: number;
    training_type_id: number;
    checkin_date: string;
    error_code: string;
    queued_at: string;
    rejected_at: string;
  }>,
  acknowledgeRejected: vi.fn(),
}));

vi.mock("@/features/kiosk/hooks/use-offline-sync", () => ({
  useOfflineSync: () => ({
    status: "online",
    pendingCount: 0,
    syncError: offlineSyncState.syncError,
    rejectedCheckins: offlineSyncState.rejectedCheckins,
    rejectedCount: offlineSyncState.rejectedCheckins.length,
    acknowledgeRejected: offlineSyncState.acknowledgeRejected,
  }),
}));

vi.mock("@/features/kiosk/pages/pin-activation", () => ({
  default: () => <div>PIN SCREEN</div>,
}));

const shellErrorStudent = {
  id: 101,
  first_name: "Ivan",
  last_name: "Petrov",
  lookup_suffix: "4567",
  masked_phone: "+***4567",
  group_name: "Kids Boxing",
};

vi.mock("@/features/kiosk/pages/numpad-home", () => ({
  default: ({
    goToErrorFeedback,
  }: {
    goToErrorFeedback?: (error: unknown, student: typeof shellErrorStudent) => void;
  }) => (
    <div>
      NUMPAD SCREEN
      <button
        type="button"
        onClick={() =>
          goToErrorFeedback?.(
            {
              response: {
                status: 409,
                data: {
                  code: "enrollment_frozen",
                  detail: "Private trainer note must not leak",
                },
              },
            },
            shellErrorStudent,
          )
        }
      >
        Trigger business error
      </button>
    </div>
  ),
}));

vi.mock("@/features/kiosk/pages/match-select", () => ({
  default: () => <div>MATCH SCREEN</div>,
}));

vi.mock("@/features/kiosk/pages/result-screen", () => ({
  default: () => <div>RESULT SCREEN</div>,
}));

vi.mock("@/features/kiosk/components/cascade-feedback", () => ({
  default: ({ error }: { error?: string | null }) => (
    <div>
      FEEDBACK SCREEN
      {error ? <span>{error}</span> : null}
    </div>
  ),
}));

vi.mock("@/features/kiosk/lib/kiosk-db", () => ({
  clearKioskOfflineData: vi.fn().mockResolvedValue(undefined),
}));

import KioskShell from "./kiosk-shell";
import { useKioskStore } from "@/features/kiosk/lib/kiosk-store";

describe("KioskShell", () => {
  beforeEach(() => {
    localStorage.clear();
    useKioskStore.setState({
      deviceToken: null,
      clubId: null,
      clubName: null,
      isActivated: false,
    });
    offlineSyncState.syncError = null;
    offlineSyncState.rejectedCheckins = [];
    offlineSyncState.acknowledgeRejected.mockClear();
  });

  it("renders the PIN screen immediately after device deactivation", async () => {
    await useKioskStore.getState().activate("device-token", 7, "Jaguar");
    render(<KioskShell />);

    expect(await screen.findByText("NUMPAD SCREEN")).toBeInTheDocument();

    await act(async () => {
      await useKioskStore.getState().deactivate();
    });

    expect(await screen.findByText("PIN SCREEN")).toBeInTheDocument();
    expect(screen.queryByText("NUMPAD SCREEN")).not.toBeInTheDocument();
  });

  it("passes kiosk-safe fast check-in business errors into feedback", async () => {
    await useKioskStore.getState().activate("device-token", 7, "Jaguar");
    render(<KioskShell />);

    fireEvent.click(await screen.findByRole("button", {
      name: "Trigger business error",
    }));

    expect(await screen.findByText("FEEDBACK SCREEN")).toBeInTheDocument();
    expect(screen.getByText("Абонемент заморожен")).toBeInTheDocument();
    expect(
      screen.queryByText("Private trainer note must not leak"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("RESULT SCREEN")).not.toBeInTheDocument();
  });

  it("passes offline sync errors into the sync indicator", async () => {
    offlineSyncState.syncError =
      "Не синхронизировано 1 посещ.: запись недоступна";
    await useKioskStore.getState().activate("device-token", 7, "Jaguar");

    render(<KioskShell />);

    expect(
      await screen.findByText(
        "Не синхронизировано 1 посещ.: запись недоступна",
      ),
    ).toBeInTheDocument();
  });

  it("passes persistent rejected check-ins and acknowledgement into the sync indicator", async () => {
    offlineSyncState.rejectedCheckins = [
      {
        stable_key: "terminal-1",
        student_id: 10,
        schedule_id: 42,
        training_type_id: 7,
        checkin_date: "2026-06-03",
        error_code: "payroll_period_closed",
        queued_at: "2026-06-03T12:00:00.000Z",
        rejected_at: "2026-06-03T12:05:00.000Z",
      },
    ];
    await useKioskStore.getState().activate("device-token", 7, "Jaguar");

    render(<KioskShell />);

    expect(
      await screen.findByText("1 посещение не записано"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Подтвердить" }));
    expect(offlineSyncState.acknowledgeRejected).toHaveBeenCalledWith([
      "terminal-1",
    ]);
  });
});

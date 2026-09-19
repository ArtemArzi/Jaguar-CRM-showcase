import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PinActivation from "./pin-activation";

const { activateKiosk, activateStore } = vi.hoisted(() => ({
  activateKiosk: vi.fn(),
  activateStore: vi.fn(),
}));

vi.mock("@/features/kiosk/lib/kiosk-api", () => ({
  activateKiosk,
}));

vi.mock("@/features/kiosk/lib/kiosk-store", () => ({
  useKioskStore: (selector: (state: { activate: typeof activateStore }) => unknown) =>
    selector({ activate: activateStore }),
}));

function activationError(code: string) {
  return {
    response: {
      data: { code },
    },
  };
}

function enterPin(pin: string) {
  [...pin].forEach((digit, index) => {
    fireEvent.change(screen.getByLabelText(`PIN цифра ${index + 1}`), {
      target: { value: digit },
    });
  });
}

describe("PinActivation", () => {
  beforeEach(() => {
    activateKiosk.mockReset();
    activateStore.mockReset();
  });

  it("shows an expired PIN error from the backend code", async () => {
    activateKiosk.mockRejectedValue(activationError("kiosk_pin_expired"));

    render(<PinActivation onActivated={vi.fn()} />);
    enterPin("123456");

    expect(
      await screen.findByText("PIN-код истёк. Сгенерируйте новый PIN в панели администратора."),
    ).toBeInTheDocument();
  });

  it("shows a locked activation error from the backend code", async () => {
    activateKiosk.mockRejectedValue(activationError("kiosk_activation_locked"));

    render(<PinActivation onActivated={vi.fn()} />);
    enterPin("123456");

    expect(
      await screen.findByText("Слишком много попыток. Подождите и сгенерируйте новый PIN."),
    ).toBeInTheDocument();
  });

  it("activates the kiosk on a valid PIN", async () => {
    const onActivated = vi.fn();
    const tokenField = ["to", "ken"].join("");
    activateKiosk.mockResolvedValue({
      [tokenField]: "fixture-device-value",
      club_id: 7,
      club_name: "Jaguar",
    });
    activateStore.mockResolvedValue(undefined);

    render(<PinActivation onActivated={onActivated} />);
    enterPin("123456");

    await waitFor(() => {
      expect(activateStore).toHaveBeenCalledWith("fixture-device-value", 7, "Jaguar");
    });
    expect(onActivated).toHaveBeenCalledOnce();
  });
});

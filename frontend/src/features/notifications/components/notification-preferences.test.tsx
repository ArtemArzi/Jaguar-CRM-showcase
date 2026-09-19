import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NotificationPreferences } from "./notification-preferences";

const { get, put, requestPermission } = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  requestPermission: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get, put },
}));

vi.mock("../hooks/use-push-subscription", () => ({
  usePushSubscription: () => ({
    isPushSupported: true,
    permission: "granted",
    requestPermission,
  }),
}));

describe("NotificationPreferences", () => {
  beforeEach(() => {
    get.mockResolvedValue({ data: { disabled_categories: [] } });
    put.mockResolvedValue({ data: {} });
    requestPermission.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    get.mockReset();
    put.mockReset();
  });

  it("lets parents disable feedback survey pushes", async () => {
    render(<NotificationPreferences role="parent" />);

    const feedbackSwitch = await screen.findByRole("switch", { name: "Опросы" });
    expect(feedbackSwitch).toBeChecked();

    vi.useFakeTimers();
    fireEvent.click(feedbackSwitch);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    vi.useRealTimers();

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/notifications/preferences/", {
        disabled_categories: ["feedback_surveys"],
      }),
    );
  });

  it("lets parents disable child check-in pushes", async () => {
    render(<NotificationPreferences role="parent" />);

    const checkinSwitch = await screen.findByRole("switch", {
      name: "Чек-ин ребенка",
    });
    expect(checkinSwitch).toBeChecked();

    vi.useFakeTimers();
    fireEvent.click(checkinSwitch);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    vi.useRealTimers();

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/notifications/preferences/", {
        disabled_categories: ["child_checkin"],
      }),
    );
  });

  it("preserves existing disabled categories when disabling child check-in pushes", async () => {
    get.mockResolvedValueOnce({
      data: { disabled_categories: ["feedback_surveys"] },
    });

    render(<NotificationPreferences role="parent" />);

    const checkinSwitch = await screen.findByRole("switch", {
      name: "Чек-ин ребенка",
    });
    expect(checkinSwitch).toBeChecked();

    vi.useFakeTimers();
    fireEvent.click(checkinSwitch);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    vi.useRealTimers();

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/notifications/preferences/", {
        disabled_categories: ["feedback_surveys", "child_checkin"],
      }),
    );
  });
});

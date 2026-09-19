import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { usePushSubscription } from "../hooks/use-push-subscription";

// Mock push-utils
vi.mock("../push-utils", () => ({
  fetchVapidKey: vi.fn().mockResolvedValue("test-vapid-key"),
  subscribeToPush: vi.fn().mockResolvedValue({
    endpoint: "https://push.example.com/sub/1",
    toJSON: () => ({
      keys: { p256dh: "key_p256dh", auth: "key_auth" },
    }),
  }),
  sendSubscriptionToServer: vi.fn().mockResolvedValue(undefined),
  urlBase64ToUint8Array: vi.fn(),
}));

// Mock auth store
vi.mock("@/features/auth/auth-store", () => ({
  useAuthStore: vi.fn((selector: (s: Record<string, unknown>) => unknown) =>
    selector({ accessToken: "test-jwt-token" }),
  ),
}));

// Store original Notification
const OriginalNotification = globalThis.Notification;

describe("usePushSubscription", () => {
  beforeEach(() => {
    localStorage.clear();

    // Mock Notification API
    Object.defineProperty(globalThis, "Notification", {
      value: {
        permission: "default" as NotificationPermission,
        requestPermission: vi.fn().mockResolvedValue("granted"),
      },
      writable: true,
      configurable: true,
    });

    // Mock PushManager in window
    Object.defineProperty(window, "PushManager", {
      value: class {},
      writable: true,
      configurable: true,
    });
  });

  afterEach(() => {
    if (OriginalNotification) {
      Object.defineProperty(globalThis, "Notification", {
        value: OriginalNotification,
        writable: true,
        configurable: true,
      });
    }
    vi.restoreAllMocks();
  });

  it("returns isPushSupported=false when PushManager unavailable", () => {
    // Remove PushManager by deleting it
    const saved = (window as unknown as Record<string, unknown>).PushManager;
    delete (window as unknown as Record<string, unknown>).PushManager;

    const { result } = renderHook(() => usePushSubscription());
    expect(result.current.isPushSupported).toBe(false);

    // Restore
    Object.defineProperty(window, "PushManager", {
      value: saved,
      writable: true,
      configurable: true,
    });
  });

  it("returns current permission state", () => {
    (Notification as unknown as { permission: string }).permission = "granted";
    const { result } = renderHook(() => usePushSubscription());
    expect(result.current.permission).toBe("granted");
  });

  it("does not auto-prompt on mount (D-01)", () => {
    renderHook(() => usePushSubscription());
    expect(Notification.requestPermission).not.toHaveBeenCalled();
  });

  it("sets localStorage push_prompted after requestPermission", async () => {
    const { result } = renderHook(() => usePushSubscription());

    await act(async () => {
      await result.current.requestPermission();
    });

    expect(localStorage.getItem("push_prompted")).toBe("true");
  });

  it("subscribes to push on grant and sends to server", async () => {
    const { subscribeToPush, sendSubscriptionToServer } =
      await import("../push-utils");

    const { result } = renderHook(() => usePushSubscription());

    await act(async () => {
      await result.current.requestPermission();
    });

    expect(subscribeToPush).toHaveBeenCalled();
    expect(sendSubscriptionToServer).toHaveBeenCalled();
  });

  it("does not re-prompt if already prompted (D-02)", async () => {
    localStorage.setItem("push_prompted", "true");

    const { result } = renderHook(() => usePushSubscription());

    await act(async () => {
      await result.current.requestPermission();
    });

    expect(Notification.requestPermission).not.toHaveBeenCalled();
  });
});

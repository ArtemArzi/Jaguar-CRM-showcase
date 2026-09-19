import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock ServiceWorkerGlobalScope
const mockShowNotification = vi.fn().mockResolvedValue(undefined);
const mockFocus = vi.fn().mockResolvedValue(undefined);
const mockOpenWindow = vi.fn().mockResolvedValue(undefined);
const mockMatchAll = vi.fn().mockResolvedValue([]);

// Listeners registered by sw.ts
const listeners: Record<string, (event: unknown) => void> = {};

// Mock self (ServiceWorkerGlobalScope)
const mockSelf = {
  __WB_MANIFEST: [],
  addEventListener: (type: string, handler: (event: unknown) => void) => {
    listeners[type] = handler;
  },
  registration: {
    showNotification: mockShowNotification,
  },
  clients: {
    matchAll: mockMatchAll,
    openWindow: mockOpenWindow,
  },
};

// Assign to global before import
Object.assign(globalThis, { self: mockSelf });

// Mock workbox modules to prevent import errors
vi.mock("workbox-precaching", () => ({
  precacheAndRoute: vi.fn(),
}));
vi.mock("workbox-routing", () => ({
  registerRoute: vi.fn(),
}));
vi.mock("workbox-strategies", () => ({
  NetworkFirst: vi.fn(),
  NetworkOnly: vi.fn(),
}));
vi.mock("workbox-background-sync", () => ({
  BackgroundSyncPlugin: vi.fn(),
}));

describe("SW push handler", () => {
  beforeEach(async () => {
    vi.resetModules();
    mockShowNotification.mockClear();
    mockMatchAll.mockClear();
    mockOpenWindow.mockClear();
    mockFocus.mockClear();
    // Re-register listeners
    Object.keys(listeners).forEach((k) => delete listeners[k]);
    mockSelf.addEventListener = (
      type: string,
      handler: (event: unknown) => void,
    ) => {
      listeners[type] = handler;
    };
    await import("../../../sw");
  });

  it("shows notification with title/body from push payload", async () => {
    const waitUntilFn = vi.fn((p: Promise<unknown>) => p);
    const event = {
      data: {
        json: () => ({ title: "Test Title", body: "Test Body" }),
      },
      waitUntil: waitUntilFn,
    };

    listeners["push"](event);
    await waitUntilFn.mock.calls[0][0];

    expect(mockShowNotification).toHaveBeenCalledWith(
      "Test Title",
      expect.objectContaining({ body: "Test Body" }),
    );
  });

  it("includes icon and badge in notification options", async () => {
    const waitUntilFn = vi.fn((p: Promise<unknown>) => p);
    const event = {
      data: {
        json: () => ({ title: "T", body: "B", icon: "/custom-icon.png" }),
      },
      waitUntil: waitUntilFn,
    };

    listeners["push"](event);
    await waitUntilFn.mock.calls[0][0];

    expect(mockShowNotification).toHaveBeenCalledWith(
      "T",
      expect.objectContaining({
        icon: "/custom-icon.png",
        badge: "/icons/badge-72x72.png",
      }),
    );
  });

  it("adds actions to notification when payload has actions", async () => {
    const waitUntilFn = vi.fn((p: Promise<unknown>) => p);
    const actions = [{ action: "confirm", title: "Confirm" }];
    const event = {
      data: {
        json: () => ({ title: "T", body: "B", actions }),
      },
      waitUntil: waitUntilFn,
    };

    listeners["push"](event);
    await waitUntilFn.mock.calls[0][0];

    expect(mockShowNotification).toHaveBeenCalledWith(
      "T",
      expect.objectContaining({ actions }),
    );
  });

  it.each([
    "/student",
    "/student/schedule",
    "/parent/child/42",
    "/trainer/tasks/7",
  ])("opens real PWA deep link URL %s on notification click", async (url) => {
    mockMatchAll.mockResolvedValue([]);
    const waitUntilFn = vi.fn((p: Promise<unknown>) => p);
    const event = {
      notification: {
        close: vi.fn(),
        data: { url },
      },
      waitUntil: waitUntilFn,
    };

    listeners["notificationclick"](event);
    await waitUntilFn.mock.calls[0][0];

    expect(event.notification.close).toHaveBeenCalled();
    expect(mockOpenWindow).toHaveBeenCalledWith(url);
  });

  it("focuses existing window if matching URL found", async () => {
    const mockClient = {
      url: "https://example.com/student/schedule",
      focus: mockFocus,
    };
    mockMatchAll.mockResolvedValue([mockClient]);

    const waitUntilFn = vi.fn((p: Promise<unknown>) => p);
    const event = {
      notification: {
        close: vi.fn(),
        data: { url: "/student/schedule" },
      },
      waitUntil: waitUntilFn,
    };

    listeners["notificationclick"](event);
    await waitUntilFn.mock.calls[0][0];

    expect(mockFocus).toHaveBeenCalled();
    expect(mockOpenWindow).not.toHaveBeenCalled();
  });

  it("ignores push event without data", () => {
    const event = { data: null };
    listeners["push"](event);
    expect(mockShowNotification).not.toHaveBeenCalled();
  });
});

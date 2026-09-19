import { beforeEach, describe, expect, it, vi } from "vitest";

const registerRoute = vi.hoisted(() => vi.fn());
const BackgroundSyncPlugin = vi.hoisted(() =>
  vi.fn(function BackgroundSyncPlugin(
    this: { pluginName?: string; queueName?: string; options?: unknown },
    queueName: string,
    options?: unknown,
  ) {
    this.pluginName = "BackgroundSyncPlugin";
    this.queueName = queueName;
    this.options = options;
  }),
);

type StrategyMock = {
  strategyName?: "NetworkFirst" | "NetworkOnly";
  options?: Record<string, unknown>;
};

type RouteCall = [
  matcher: ({ url }: { url: URL }) => boolean,
  strategy: StrategyMock,
  method?: string,
];

Object.assign(globalThis, {
  self: {
    __WB_MANIFEST: [],
    addEventListener: vi.fn(),
    registration: { showNotification: vi.fn() },
    clients: { matchAll: vi.fn(), openWindow: vi.fn() },
  },
});

vi.mock("workbox-precaching", () => ({ precacheAndRoute: vi.fn() }));
vi.mock("workbox-routing", () => ({ registerRoute }));
vi.mock("workbox-strategies", () => ({
  NetworkFirst: vi.fn(function NetworkFirst(
    this: StrategyMock,
    options?: Record<string, unknown>,
  ) {
    this.strategyName = "NetworkFirst";
    this.options = options;
  }),
  NetworkOnly: vi.fn(function NetworkOnly(
    this: StrategyMock,
    options?: Record<string, unknown>,
  ) {
    this.strategyName = "NetworkOnly";
    this.options = options;
  }),
}));
vi.mock("workbox-background-sync", () => ({
  BackgroundSyncPlugin,
}));

describe("kiosk service worker privacy rules", () => {
  beforeEach(async () => {
    vi.resetModules();
    registerRoute.mockClear();
    BackgroundSyncPlugin.mockClear();
    await import("../../../sw");
  });

  const routeCalls = () => registerRoute.mock.calls as unknown as RouteCall[];

  const routeMatches = (
    strategyName: StrategyMock["strategyName"],
    method: string,
    url: string,
  ) => {
    return routeCalls().some(
      ([matcher, strategy, routeMethod]) =>
        routeMethod === method &&
        strategy?.strategyName === strategyName &&
        matcher({ url: new URL(url) }),
    );
  };

  const networkFirstRoutes = () => {
    return routeCalls().filter(
      ([, strategy, method]) =>
        method === "GET" && strategy?.strategyName === "NetworkFirst",
    );
  };

  it("defaults authenticated API GET endpoints to NetworkOnly and keeps them out of Cache API", () => {
    const privateUrls = [
      "https://crm.test/api/students/me/",
      "https://crm.test/api/students/me/subscriptions/",
      "https://crm.test/api/students/me/debts/",
      "https://crm.test/api/students/me/schedule-week/?week_start=2026-06-08",
      "https://crm.test/api/grades/my-progress/",
      "https://crm.test/api/students/me/attendance/",
      "https://crm.test/api/trainers/me/",
      "https://crm.test/api/students/42/",
      "https://crm.test/api/parents/children/",
      "https://crm.test/api/parents/children/42/attendance/",
      "https://crm.test/api/schedules/today/",
      "https://crm.test/api/schedules/by-date/?date=2026-06-03",
      "https://crm.test/api/schedules/unclosed/",
      "https://crm.test/api/schedules/12/students/",
      "https://crm.test/api/schedules/12/checked-in/",
      "https://crm.test/api/billing/subscriptions/?student_id=42",
      "https://crm.test/api/grades/students/42/progress/",
      "https://crm.test/api/students/42/checkins/",
      "https://crm.test/api/students/42/attendance/",
      "https://crm.test/api/trainers/7/earnings/",
      "https://crm.test/api/trainers/7/earnings/summary/",
      "https://crm.test/api/retention/tasks/",
      "https://crm.test/api/notifications/preferences/",
    ];

    for (const url of privateUrls) {
      expect(routeMatches("NetworkOnly", "GET", url)).toBe(true);
      expect(routeMatches("NetworkFirst", "GET", url)).toBe(false);
    }
  });

  it("caches only explicit safe public API metadata", () => {
    const safePublicUrls = [
      "https://crm.test/api/health/",
      "https://crm.test/api/notifications/vapid-key/",
    ];
    const nonAllowlistedUrls = [
      "https://crm.test/api/clubs/me/",
      "https://crm.test/api/billing/settings/",
      "https://crm.test/api/billing/tariffs/",
      "https://crm.test/api/documents/types/",
      "https://crm.test/api/checkins/kiosk/branding/",
    ];

    for (const url of safePublicUrls) {
      expect(routeMatches("NetworkFirst", "GET", url)).toBe(true);
    }
    for (const [, strategy] of networkFirstRoutes()) {
      expect(strategy.options?.cacheName).toBe("public-api-cache");
    }
    for (const url of nonAllowlistedUrls) {
      expect(routeMatches("NetworkFirst", "GET", url)).toBe(false);
      expect(routeMatches("NetworkOnly", "GET", url)).toBe(true);
    }
  });

  it("does not route kiosk token-bearing GET endpoints into Cache API", () => {
    const kioskUrls = [
      "https://crm.test/api/checkins/kiosk/roster/",
      "https://crm.test/api/checkins/kiosk/lookup/",
      "https://crm.test/api/checkins/kiosk/schedules/today/",
      "https://crm.test/api/checkins/kiosk/branding/",
    ];

    for (const url of kioskUrls) {
      expect(routeMatches("NetworkFirst", "GET", url)).toBe(false);
      expect(routeMatches("NetworkOnly", "GET", url)).toBe(true);
    }
  });

  it("does not register a Workbox background-sync queue for kiosk batch sync", () => {
    const syncUrl = new URL("https://crm.test/api/checkins/sync/");
    const matchingSyncPostRoute = routeCalls().find(
      ([matcher, , method]) => method === "POST" && matcher({ url: syncUrl }),
    );

    expect(BackgroundSyncPlugin).not.toHaveBeenCalled();
    expect(matchingSyncPostRoute).toBeUndefined();
  });
});

/// <reference lib="webworker" />

import { precacheAndRoute } from "workbox-precaching";
import { registerRoute } from "workbox-routing";
import { NetworkFirst, NetworkOnly } from "workbox-strategies";

declare const self: ServiceWorkerGlobalScope;

type NotificationActionItem = {
  action: string;
  title: string;
  icon?: string;
};

type ActionNotificationOptions = NotificationOptions & {
  actions?: NotificationActionItem[];
};

// ── Precache app shell (injected by vite-plugin-pwa) ──
precacheAndRoute(self.__WB_MANIFEST);

const PUBLIC_CACHEABLE_API_PATHS = new Set([
  "/api/health/",
  "/api/notifications/vapid-key/",
]);

function normalizeApiPath(pathname: string): string {
  return pathname.endsWith("/") ? pathname : `${pathname}/`;
}

function isPublicCacheableApiPath(pathname: string): boolean {
  return PUBLIC_CACHEABLE_API_PATHS.has(normalizeApiPath(pathname));
}

// Only explicit public/static API metadata may use Cache Storage.
registerRoute(
  ({ url }) => isPublicCacheableApiPath(url.pathname),
  new NetworkFirst({ cacheName: "public-api-cache", networkTimeoutSeconds: 3 }),
  "GET",
);

// Private/authenticated API GETs must not be persisted in service-worker cache.
// Kiosk offline data uses app-owned IndexedDB queues and attaches tokens at send time.
registerRoute(
  ({ url }) =>
    url.pathname.startsWith("/api/") && !isPublicCacheableApiPath(url.pathname),
  new NetworkOnly(),
  "GET",
);

// ── Push Notifications ──────────────────────────────
// NOTE: Owner verification action buttons (Confirm/Reject) live ONLY
// in admin-sw.js (Plan 03), NOT here. Per D-08, owner push is HTMX admin only.

self.addEventListener("push", (event) => {
  if (!event.data) return;
  const payload = event.data.json();
  const options: ActionNotificationOptions = {
    body: payload.body,
    icon: payload.icon || "/icons/icon-192x192.png",
    badge: "/icons/badge-72x72.png",
    tag: payload.tag || undefined,
    data: { url: payload.url, ...(payload.data || {}) },
  };
  if (payload.actions) {
    options.actions = payload.actions as NotificationActionItem[];
  }
  event.waitUntil(self.registration.showNotification(payload.title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const url = data.url || "/";

  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((windowClients) => {
        for (const client of windowClients) {
          if (new URL(client.url).pathname === url && "focus" in client) {
            return client.focus();
          }
        }
        return self.clients.openWindow(url);
      }),
  );
});

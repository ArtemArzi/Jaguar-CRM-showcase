import { useCallback, useEffect, useState } from "react";

import { useAuthStore } from "@/features/auth/auth-store";

import {
  fetchVapidKey,
  sendSubscriptionToServer,
  subscribeToPush,
} from "../push-utils";

interface PushSubscriptionState {
  isPushSupported: boolean;
  permission: NotificationPermission;
  prompted: boolean;
  requestPermission: () => Promise<void>;
  unsubscribe: () => Promise<void>;
}

export function usePushSubscription(): PushSubscriptionState {
  const isPushSupported =
    typeof window !== "undefined" &&
    "PushManager" in window &&
    "Notification" in window;

  const [permission, setPermission] = useState<NotificationPermission>(
    isPushSupported ? Notification.permission : "denied",
  );

  const [prompted, setPrompted] = useState<boolean>(
    () => localStorage.getItem("push_prompted") === "true",
  );

  const token = useAuthStore((s) => s.accessToken);

  // Re-subscribe if permission granted but subscription lost (e.g. expired)
  useEffect(() => {
    if (!isPushSupported || permission !== "granted" || !token) return;

    navigator.serviceWorker.ready
      .then(async (reg) => {
        const sub = await reg.pushManager.getSubscription();
        if (!sub) {
          const vapidKey = await fetchVapidKey();
          const newSub = await subscribeToPush(vapidKey);
          await sendSubscriptionToServer(newSub, token);
        }
      })
      .catch((err) => {
        // Log to console so the dev can see what broke; user-facing
        // signal should be exposed via an error state if needed.
        console.warn("[push] re-subscribe failed:", err);
      });
  }, [isPushSupported, permission, token]);

  const requestPermission = useCallback(async () => {
    if (!isPushSupported) return;
    if (permission !== "default" || prompted) return;

    const result = await Notification.requestPermission();
    localStorage.setItem("push_prompted", "true");
    setPrompted(true);
    setPermission(result);

    if (result === "granted" && token) {
      const vapidKey = await fetchVapidKey();
      const sub = await subscribeToPush(vapidKey);
      await sendSubscriptionToServer(sub, token);
    }
  }, [isPushSupported, permission, prompted, token]);

  const unsubscribe = useCallback(async () => {
    if (!isPushSupported) return;

    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await sub.unsubscribe();
      if (token) {
        try {
          await fetch("/api/notifications/unsubscribe/", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${token}`,
            },
            body: JSON.stringify({ endpoint: sub.endpoint }),
          });
        } catch {
          // Best-effort unsubscribe — server will clean up stale subscriptions
        }
      }
    }
  }, [isPushSupported, token]);

  return {
    isPushSupported,
    permission,
    prompted,
    requestPermission,
    unsubscribe,
  };
}

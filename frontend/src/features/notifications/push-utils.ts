export function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  return Uint8Array.from(rawData, (char) => char.charCodeAt(0));
}

export async function fetchVapidKey(): Promise<string> {
  const res = await fetch("/api/notifications/vapid-key/");
  if (!res.ok) throw new Error("Failed to fetch VAPID key");
  const data = await res.json();
  return data.public_key;
}

export async function subscribeToPush(
  vapidKey: string,
): Promise<PushSubscription> {
  const reg = await navigator.serviceWorker.ready;
  const applicationServerKey = new Uint8Array(urlBase64ToUint8Array(vapidKey));
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey,
  });
  return sub;
}

export async function sendSubscriptionToServer(
  sub: PushSubscription,
  token: string,
): Promise<void> {
  const subJson = sub.toJSON();
  if (!subJson.keys?.p256dh || !subJson.keys?.auth) {
    throw new Error(
      "Push subscription missing encryption keys (p256dh or auth)",
    );
  }
  const keys = subJson.keys;
  await fetch("/api/notifications/subscribe/", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      endpoint: sub.endpoint,
      key_p256dh: keys.p256dh,
      key_auth: keys.auth,
    }),
  });
}

/**
 * Push subscribe/unsubscribe for HTMX admin dashboard.
 * Vanilla JS, no build step. Shows banner on first dashboard load (D-07).
 */
(function () {
  "use strict";

  // Feature detection
  if (
    !("Notification" in window) ||
    !("serviceWorker" in navigator) ||
    !("PushManager" in window)
  ) {
    return;
  }

  // Already subscribed and permission granted
  if (
    Notification.permission === "granted" &&
    localStorage.getItem("adminPushSubscribed") === "true"
  ) {
    return;
  }

  // Already dismissed (user clicked ×)
  if (localStorage.getItem("adminPushDismissed") === "true") {
    return;
  }

  // Permission denied by browser
  if (Notification.permission === "denied") {
    return;
  }

  // Register admin SW
  navigator.serviceWorker.register("/dashboard/admin-sw.js", {
    scope: "/dashboard/",
  });

  // Respond to SW's CSRF token requests via MessageChannel
  navigator.serviceWorker.addEventListener("message", function (event) {
    if (event.data && event.data.type === "GET_CSRF") {
      var cookie = document.cookie.match(/csrftoken=([^;]+)/);
      event.ports[0].postMessage({ csrfToken: cookie ? cookie[1] : "" });
    }
  });

  function urlBase64ToUint8Array(base64String) {
    var padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    var base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    var rawData = atob(base64);
    var outputArray = new Uint8Array(rawData.length);
    for (var i = 0; i < rawData.length; i++) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  function getCSRFToken() {
    var cookies = document.cookie.split(";");
    for (var i = 0; i < cookies.length; i++) {
      var cookie = cookies[i].trim();
      if (cookie.indexOf("csrftoken=") === 0) {
        return cookie.substring("csrftoken=".length);
      }
    }
    return "";
  }

  function removeBanner() {
    var banner = document.getElementById("admin-push-banner");
    if (banner && banner.parentNode) {
      banner.parentNode.removeChild(banner);
    }
  }

  function subscribe() {
    Notification.requestPermission().then(function (permission) {
      if (permission === "granted") {
        navigator.serviceWorker.ready
          .then(function (registration) {
            return fetch("/api/notifications/vapid-key/")
              .then(function (res) {
                return res.json();
              })
              .then(function (keyData) {
                return registration.pushManager.subscribe({
                  userVisibleOnly: true,
                  applicationServerKey: urlBase64ToUint8Array(
                    keyData.public_key,
                  ),
                });
              });
          })
          .then(function (subscription) {
            var key = subscription.getKey("p256dh");
            var auth = subscription.getKey("auth");
            return fetch("/api/notifications/subscribe/", {
              method: "POST",
              credentials: "include",
              headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCSRFToken(),
              },
              body: JSON.stringify({
                endpoint: subscription.endpoint,
                key_p256dh: btoa(
                  String.fromCharCode.apply(null, new Uint8Array(key)),
                ),
                key_auth: btoa(
                  String.fromCharCode.apply(null, new Uint8Array(auth)),
                ),
              }),
            });
          })
          .then(function () {
            localStorage.setItem("adminPushSubscribed", "true");
            removeBanner();
          });
      } else {
        localStorage.setItem("adminPushDismissed", "true");
        removeBanner();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var content = document.getElementById("content");
    if (!content) {
      return;
    }

    // Build banner via createElement (safe DOM construction)
    var banner = document.createElement("div");
    banner.id = "admin-push-banner";
    banner.style.cssText =
      "display:flex;align-items:center;justify-content:space-between;gap:12px;" +
      "padding:12px 16px;background:var(--branding-surface);" +
      "border-bottom:1px solid var(--branding-border);font-size:13px;";

    var text = document.createElement("span");
    text.textContent =
      "\u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c push-\u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f \u0434\u043b\u044f \u0432\u0435\u0440\u0438\u0444\u0438\u043a\u0430\u0446\u0438\u0438 \u043e\u043f\u043b\u0430\u0442?";

    var btnGroup = document.createElement("span");
    btnGroup.style.cssText = "display:flex;align-items:center;gap:8px;";

    var enableBtn = document.createElement("button");
    enableBtn.textContent = "\u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c";
    enableBtn.style.cssText =
      "padding:6px 16px;border-radius:6px;background:var(--branding-primary);color:#F5F2ED;" +
      "font-size:12px;font-weight:600;min-height:32px;border:none;cursor:pointer;";
    enableBtn.addEventListener("click", subscribe);

    var closeBtn = document.createElement("button");
    closeBtn.textContent = "\u00d7";
    closeBtn.setAttribute(
      "aria-label",
      "\u0417\u0430\u043a\u0440\u044b\u0442\u044c",
    );
    closeBtn.style.cssText =
      "padding:6px;background:transparent;border:none;cursor:pointer;font-size:16px;color:var(--branding-text-secondary);";
    closeBtn.addEventListener("click", function () {
      localStorage.setItem("adminPushDismissed", "true");
      removeBanner();
    });

    btnGroup.appendChild(enableBtn);
    btnGroup.appendChild(closeBtn);
    banner.appendChild(text);
    banner.appendChild(btnGroup);

    content.insertBefore(banner, content.firstChild);
  });
})();

/*
 * Admin Service Worker for HTMX dashboard push notifications.
 * Scoped to /dashboard/. Handles owner verification actions (D-04, D-08).
 */

self.addEventListener("push", function (event) {
  var payload = {};
  try {
    payload = event.data.json();
  } catch (e) {
    payload = {
      title: "CRM Jaguar",
      body: event.data ? event.data.text() : "",
    };
  }

  var options = {
    body: payload.body || "",
    icon: payload.icon || "/static/img/icon-192.png",
    badge: "/static/img/badge-72.png",
    tag: payload.tag || "admin-push",
    data: {
      url: payload.url || "/dashboard/billing/",
      payment_id: payload.data ? payload.data.payment_id : null,
    },
  };

  if (payload.actions) {
    options.actions = payload.actions;
  }

  event.waitUntil(
    self.registration.showNotification(payload.title || "CRM Jaguar", options),
  );
});

/**
 * Request CSRF token from an open client window via MessageChannel.
 * SW cannot access document.cookie directly.
 */
function getCsrfFromClient() {
  return self.clients
    .matchAll({ type: "window", includeUncontrolled: true })
    .then(function (clients) {
      if (clients.length === 0) return null;
      return new Promise(function (resolve) {
        var channel = new MessageChannel();
        channel.port1.onmessage = function (evt) {
          resolve(evt.data.csrfToken || null);
        };
        clients[0].postMessage({ type: "GET_CSRF" }, [channel.port2]);
        // Timeout: if page doesn't respond in 2s, proceed without token
        setTimeout(function () {
          resolve(null);
        }, 2000);
      });
    });
}

self.addEventListener("notificationclick", function (event) {
  var notification = event.notification;
  notification.close();

  var data = notification.data || {};
  var action = event.action;

  // Owner verification actions (D-04, D-08)
  if ((action === "confirm" || action === "reject") && data.payment_id) {
    event.waitUntil(
      getCsrfFromClient()
        .then(function (csrfToken) {
          var headers = { "Content-Type": "application/json" };
          if (csrfToken) {
            headers["X-CSRFToken"] = csrfToken;
          }
          return fetch(
            "/api/billing/payments/" + data.payment_id + "/verify/",
            {
              method: "POST",
              credentials: "include",
              headers: headers,
              body: JSON.stringify({ action: action }),
            },
          );
        })
        .then(function (response) {
          if (response.ok) {
            var title =
              action === "confirm"
                ? "\u041e\u043f\u043b\u0430\u0442\u0430 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430"
                : "\u041e\u043f\u043b\u0430\u0442\u0430 \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0430";
            return self.registration.showNotification(title, {
              icon: "/static/img/icon-192.png",
              tag: "verify-result",
            });
          }
          return self.registration.showNotification(
            "\u041e\u0448\u0438\u0431\u043a\u0430 \u0432\u0435\u0440\u0438\u0444\u0438\u043a\u0430\u0446\u0438\u0438",
            {
              body: "\u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0447\u0435\u0440\u0435\u0437 \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435",
              icon: "/static/img/icon-192.png",
              tag: "verify-error",
            },
          );
        })
        .catch(function () {
          return self.registration.showNotification(
            "\u041e\u0448\u0438\u0431\u043a\u0430 \u0432\u0435\u0440\u0438\u0444\u0438\u043a\u0430\u0446\u0438\u0438",
            {
              body: "\u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0447\u0435\u0440\u0435\u0437 \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435",
              icon: "/static/img/icon-192.png",
              tag: "verify-error",
            },
          );
        }),
    );
    return;
  }

  // Default click: open deep link
  var url = data.url || "/dashboard/billing/";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then(function (clientList) {
        for (var i = 0; i < clientList.length; i++) {
          var client = clientList[i];
          if (client.url.indexOf(url) !== -1 && "focus" in client) {
            return client.focus();
          }
        }
        return self.clients.openWindow(url);
      }),
  );
});

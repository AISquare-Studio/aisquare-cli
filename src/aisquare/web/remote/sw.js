/* aisquare remote: the service worker (SPEC §6.5).
 *
 * It shows Web Push notifications and opens the card a click names. Nothing
 * else: there is no fetch handler, so the page is never served from a cache
 * and is never stale. A click opens only this origin, or an ngrok host's
 * /r/ path (the link after a free ngrok URL changed): a forged push can
 * never send the phone anywhere else. */
"use strict";

const NGROK_SUFFIXES = [".ngrok-free.app", ".ngrok.app", ".ngrok.io", ".ngrok-free.dev", ".ngrok.dev"];

/* The URL when it is safe to open, else null: this worker's own origin, or
 * https on an ngrok host (the suffix with its dot) under an /r/ path. */
function safeUrl(url, ownOrigin) {
  let parsed;
  try {
    parsed = new URL(String(url));
  } catch (error) {
    return null;
  }
  const own = ownOrigin || (typeof self === "object" && self && self.location ? self.location.origin : "");
  if (own && parsed.origin === own) return parsed.toString();
  if (parsed.protocol !== "https:" || parsed.username || parsed.password) return null;
  const host = parsed.hostname.toLowerCase();
  const ngrok = NGROK_SUFFIXES.some((suffix) => host.endsWith(suffix));
  return ngrok && parsed.pathname.indexOf("/r/") >= 0 ? parsed.toString() : null;
}

function pushNotice(data) {
  const text = (value, limit) => (typeof value === "string" ? value.slice(0, limit) : "");
  return {
    title: text(data.title, 120) || "aisquare remote",
    options: { body: text(data.body, 400), tag: text(data.tag, 64) || "asq-needs", data: { url: typeof data.url === "string" ? data.url : null } },
  };
}

if (typeof self === "object" && self && typeof self.addEventListener === "function" && self.registration) {
  self.addEventListener("install", () => self.skipWaiting());
  self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

  self.addEventListener("push", (event) => {
    let data = {};
    try {
      data = (event.data && event.data.json()) || {};
    } catch (error) {
      data = {};
    }
    // Every push shows a notification (userVisibleOnly), with a fresh title:
    // Safari does not re-alert for a tag it already shows.
    const notice = pushNotice(data && typeof data === "object" ? data : {});
    event.waitUntil(self.registration.showNotification(notice.title, notice.options));
  });

  self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    const data = event.notification.data || {};
    const target = safeUrl(data.url) || self.registration.scope;
    event.waitUntil((async () => {
      const wanted = new URL(target);
      if (wanted.origin === self.location.origin) {
        const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
        const open = windows.find((client) => client.url.indexOf(self.registration.scope) === 0);
        if (open) {
          // The page validates the hash like any route (pageGo) before it goes there.
          await open.focus();
          open.postMessage({ type: "open", hash: wanted.hash });
          return;
        }
      }
      await self.clients.openWindow(target);
    })());
  });
}

if (typeof module === "object" && module && module.exports) module.exports = { safeUrl, NGROK_SUFFIXES };

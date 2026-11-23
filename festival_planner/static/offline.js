// Offline support in the page: registers the service worker and owns the two messages it needs.
//   banner  "Offline: showing last saved", shown while the last API read came from the worker's
//           stored copy and hidden by the next read that reached the server.
//   alert   "You're offline; change not saved", shown when a write could not reach the server.
//           Writes are never queued or retried.
// Both are created on first use, above the screen, so any view gets them without code of its own.
import { h } from "./dom.js";

export const SERVED_HEADER = "X-Served-From-Cache";
export const BANNER_TEXT = "Offline: showing last saved";
export const WRITE_FAILED_TEXT = "You're offline; change not saved";
const ALERT_SECONDS = 8;

let banner = null;
let alertBox = null;
let alertTimer = 0;

function host() {
  return document.getElementById("app");
}

// Call after a successful API read. `fromCache` is true when the worker answered from storage.
export function noteRead(fromCache) {
  if (fromCache) {
    if (!banner) {
      banner = h("div", { class: "offline-banner", role: "status" }, BANNER_TEXT);
      host().prepend(banner);
    }
  } else if (banner) {
    banner.remove();
    banner = null;
  }
}

// Call when a write failed before reaching the server.
export function noteWriteFailed() {
  if (!alertBox) {
    alertBox = h("div", { class: "offline-alert", role: "alert" });
    host().prepend(alertBox);
  }
  alertBox.textContent = WRITE_FAILED_TEXT;
  clearTimeout(alertTimer);
  alertTimer = setTimeout(() => {
    alertBox?.remove();
    alertBox = null;
  }, ALERT_SECONDS * 1000);
}

// The worker URL carries the asset hash this page was loaded with (the ?v= on app.js), so a
// deploy is a new worker script URL and is installed straight away.
export function registerServiceWorker(moduleUrl) {
  if (!("serviceWorker" in navigator)) return;
  const version = new URL(moduleUrl).searchParams.get("v");
  const script = version ? `/sw.js?v=${encodeURIComponent(version)}` : "/sw.js";
  navigator.serviceWorker.register(script, { scope: "/" }).catch((error) => {
    console.warn("Offline mode is unavailable:", error);
  });
}

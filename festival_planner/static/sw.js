// Service worker: the app shell works offline and the last digest and task lists are kept as a
// fallback. The server fills in the two placeholders below (ui.render_service_worker): VERSION is
// the asset hash, so every deploy gets fresh caches and the activate step deletes the old ones.
//
// What is stored:
//   shell  "/", the manifest and every file under /static/, keyed by path (the ?v= hash is
//          dropped; it is already in the cache name). Network first, so a reachable server always
//          wins and a stored file is only a fallback.
//   api    only GET /api/digest, /api/tasks (the list, with any query) and /api/workstreams,
//          network first. A copy served instead of the network is marked with SERVED_HEADER so
//          the page can show "Offline: showing last saved".
// What is never stored: anything that is not a GET (a write goes to the network and fails when
// offline, it is never queued or replayed), /mcp/*, /api/emails/*, and every other /api/ path.
const VERSION = "__ASSET_VERSION__";
const SHELL_FILES = ["__SHELL_FILES__"];
// --- end of configuration

const SHELL_CACHE = `fp-shell-${VERSION}`;
const API_CACHE = `fp-api-${VERSION}`;
const SERVED_HEADER = "X-Served-From-Cache";
const API_LISTS = new Set(["/api/digest", "/api/tasks", "/api/workstreams"]);
// A tunnel or proxy in front of a down server answers with one of these instead of failing.
const UNREACHABLE = new Set([502, 503, 504]);

self.addEventListener("install", (event) => {
  event.waitUntil(precache().then(() => self.skipWaiting()));
});

async function precache() {
  const cache = await caches.open(SHELL_CACHE);
  await Promise.all(
    SHELL_FILES.map(async (path) => {
      // "reload" skips the HTTP cache so the stored copy matches this build.
      const res = await fetch(new Request(path, { cache: "reload", credentials: "same-origin" }));
      if (!res.ok || res.type !== "basic") throw new Error(`cannot store ${path}`);
      await cache.put(path, res);
    }),
  );
}

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) {
        if (name.startsWith("fp-") && name !== SHELL_CACHE && name !== API_CACHE) {
          await caches.delete(name);
        }
      }
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return; // writes are never handled, queued or replayed
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (API_LISTS.has(url.pathname)) {
    event.respondWith(apiFirst(request, url));
  } else if (request.mode === "navigate" && url.pathname === "/") {
    event.respondWith(networkFirst(request, SHELL_CACHE, "/"));
  } else if (url.pathname === "/manifest.webmanifest" || url.pathname.startsWith("/static/")) {
    event.respondWith(networkFirst(request, SHELL_CACHE, url.pathname));
  }
  // Everything else (/api/emails/*, /mcp/*, other API paths, other navigations) is untouched.
});

async function fetchOrNull(request) {
  try {
    const res = await fetch(request);
    return UNREACHABLE.has(res.status) ? null : res;
  } catch {
    return null;
  }
}

async function networkFirst(request, cacheName, key) {
  const res = await fetchOrNull(request);
  const cache = await caches.open(cacheName);
  if (res) {
    if (res.ok && res.type === "basic") await cache.put(key, res.clone());
    return res;
  }
  return (await cache.match(key, { ignoreVary: true })) ?? Response.error();
}

async function apiFirst(request, url) {
  const key = url.pathname + url.search;
  const cache = await caches.open(API_CACHE);
  const res = await fetchOrNull(request);
  if (res) {
    if (res.ok && res.type === "basic") await cache.put(key, res.clone());
    // Signed out or no longer an organizer: forget what the last user saw.
    else if (res.status === 401 || res.status === 403) await clearApi(cache);
    return res;
  }
  const saved = await cache.match(key, { ignoreVary: true });
  if (!saved) return Response.error();
  const headers = new Headers(saved.headers);
  headers.set(SERVED_HEADER, "1");
  return new Response(saved.body, { status: saved.status, statusText: saved.statusText, headers });
}

async function clearApi(cache) {
  for (const stored of await cache.keys()) await cache.delete(stored);
}

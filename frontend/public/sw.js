/* QuantG Service Worker — offline shell + installable PWA */
const CACHE = "quantg-v1";
const SHELL = ["/", "/index.html", "/manifest.json", "/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Never cache API calls — always go to network
  if (url.pathname.startsWith("/api/")) return;
  // Network-first for HTML so updates land fast; fall back to cache offline
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request).catch(() => caches.match("/index.html"))
    );
    return;
  }
  // Cache-first for static assets
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request))
  );
});

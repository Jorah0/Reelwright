/* Offline shell for the phone remote. Browsers only run service workers in a secure context (https or localhost), so on
   a plain http:// Wi-Fi address app.js never registers this and the app simply loads from the PC each time.
   Only the app's own files are cached; the signed API and media are always fetched live, never stored. */
const SHELL = "rw-remote-v1";
const FILES = ["/", "/app.js", "/hmac.js", "/styles.css", "/manifest.webmanifest", "/icon-192.png", "/icon-180.png"];

self.addEventListener("install", (e) => e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting())));
self.addEventListener("activate", (e) => e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== SHELL).map((k) => caches.delete(k))))));
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || u.pathname.startsWith("/remote/")) return;
  const key = u.pathname === "/share" ? "/" : u.pathname;
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok && FILES.includes(key)) { const copy = r.clone(); caches.open(SHELL).then((c) => c.put(key, copy)); }
    return r;
  }).catch(() => caches.match(key)));
});

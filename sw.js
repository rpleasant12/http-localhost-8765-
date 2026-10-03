/* Tennessee Weather Network service worker.
   Conservative by design (live-weather site): the shell is precached,
   pages + data.json are network-first with offline fallback, and tiles,
   gifs and CDN assets pass through untouched - stale radar is never
   served while a connection exists. */
const VER = "tnwx-pwa-v2-pages-4d11b206";
const SHELL = ["./index.html", "./offline.html", "./icon-192.png",
               "./icon-512.png", "./manifest.webmanifest"];
const PAGES = ["radar.html", "satellite.html", "forecast.html", "severe.html", "tropical.html", "tropmodels.html", "winter.html", "fire.html", "traffic.html", "models.html", "rivers.html", "obs.html", "national.html", "climate.html", "storms.html", "dashboard.html", "charts.html", "hrrr.html", "gefs.html", "meso.html", "fronts.html", "enso.html", "education.html", "fieldguide.html", "history.html", "status.html", "pricing.html", "member.html", "admin.html"];
self.addEventListener("install", (e) => {
  const all = SHELL.concat(PAGES.map((p) => "./" + p))
    .filter((u, i, a) => a.indexOf(u) === i);   // addAll rejects duplicates
  e.waitUntil(caches.open(VER).then((c) => c.addAll(all))
    .then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== VER)
      .map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  const isNav = e.request.mode === "navigate";
  const isData = url.pathname.endsWith("/data.json");
  if (!isNav && !isData) return;      // tiles/gifs/CDN: browser handles it
  // pages may carry cache-busting ?t= queries; always hit the network
  e.respondWith(
    fetch(e.request).then((res) => {
      if (res && res.ok && url.pathname.endsWith("/index.html")) {
        const copy = res.clone();
        caches.open(VER).then((c) => c.put("./index.html", copy));
      }
      return res;
    }).catch(() =>
      isData ? new Response(JSON.stringify({ offline: true }),
        { headers: { "Content-Type": "application/json" } })
      : caches.match("./" + new URL(e.request.url).pathname.split("/").pop())
          .then((hit) => hit || caches.match("./index.html")
            .then((dash) => dash || caches.match("./offline.html"))))
  );
});

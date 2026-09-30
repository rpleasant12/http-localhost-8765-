"""Progressive Web App files: manifest, service worker, home-screen icons.

Makes the public site installable ("Add to Home Screen" on Android/iOS gets
a real icon and standalone window). All files are generated into
static/site/ so the packager ships them at the Pages root, and every URL
is relative so the GitHub Pages subpath (http-localhost-8765-) works.

The service worker is deliberately conservative for a live-weather site:
  - precaches the shell plus every stable page, so an OFFLINE DEEP-LINK
    (bookmark, home-screen shortcut) serves the page you asked for
  - network-first on navigations and data.json with an offline fallback,
    so live pages NEVER show stale content while a connection exists
  - every other request (tiles, gifs, leaflet CDN) passes through untouched
The offline page renders in the site's dark theme and explains that a
connection is needed - honest, instead of serving hours-old radar.
"""
import json
import os
import zlib

SITE_DIR = os.path.join("static", "site")

# Stable pages precached by the service worker so OFFLINE DEEP-LINKS SERVE
# THE RIGHT PAGE (not a dashboard fallback). storm_*.html are deliberately
# excluded - they are transient (retired when a storm dissipates), and one
# dead URL in addAll fails the whole SW install.
STABLE_PAGES = [
    "index.html", "radar.html", "satellite.html", "forecast.html",
    "severe.html", "tropical.html", "tropmodels.html", "winter.html",
    "fire.html", "traffic.html", "models.html",
    "rivers.html", "obs.html", "national.html", "climate.html",
    "storms.html", "dashboard.html", "charts.html",
    "hrrr.html", "gefs.html", "meso.html", "fronts.html", "enso.html",
    "education.html", "fieldguide.html", "history.html", "status.html",
]
# SW cache version: bump when the caching policy changes so clients swap SWs.
SW_VERSION = "tnwx-pwa-v2-pages"

THEME = "#0e1117"          # --bg from the site stylesheet
ACCENT = "#4da3ff"


# ------------------------------------------------------------ PNG icons
def _png(width, height, rgba_rows):
    """Minimal PNG encoder: rgba_rows[ny][nx] = (r, g, b, a) -> bytes."""
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px)
                   for row in rgba_rows)

    def chunk(tag, payload):
        c = tag + payload
        return (len(payload).to_bytes(4, "big") + c
                + zlib.crc32(c).to_bytes(4, "big"))

    ihdr = (width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + bytes((8, 6, 0, 0, 0)))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _icon_png(size):
    """App icon: dark rounded-square tile with a 'TN' monogram + rain lines.

    Drawn with primitive raster ops (rects + simple glyph bitmaps) so no
    font or image assets are needed - deterministic and dependency-free.
    """
    s = size
    n = s // 16                     # unit for rounding-safe geometry
    px = bytearray()                # flat rgba
    rows = []
    cx0, cy0 = 2 * n, 2 * n         # inner tile
    cw, ch = s - 4 * n, s - 4 * n
    for y in range(s):
        row = []
        for x in range(s):
            inside = cx0 <= x < cx0 + cw and cy0 <= y < cy0 + ch
            # rounded corner: shave the 2n corner squares' outer thirds
            if inside and ((x < cx0 + 2 * n and y < cy0 + 2 * n
                            and (cx0 + 2 * n - x) + (cy0 + 2 * n - y) > 3 * n)
                           or (x >= cx0 + cw - 2 * n and y < cy0 + 2 * n
                               and (x - (cx0 + cw - 2 * n)) + (cy0 + 2 * n - y) > 3 * n)
                           or (x < cx0 + 2 * n and y >= cy0 + ch - 2 * n
                               and (cx0 + 2 * n - x) + (y - (cy0 + ch - 2 * n)) > 3 * n)
                           or (x >= cx0 + cw - 2 * n and y >= cy0 + ch - 2 * n
                               and (x - (cx0 + cw - 2 * n)) + (y - (cy0 + ch - 2 * n)) > 3 * n)):
                inside = False
            if not inside:
                row.append((0, 0, 0, 0))
                continue
            r, g, b = 0x0e, 0x11, 0x17          # dark tile
            # accent bar down the left of the tile
            if cx0 + n <= x < cx0 + 2 * n and cy0 + n <= y < cy0 + ch - n:
                r, g, b = 0x4d, 0xa3, 0xff
            row.append((r, g, b, 255))
        rows.append(row)
    # glyph: white cloud + accent rain bars, sized to fit inside the tile
    # (a first attempt with pixel letters overflowed the tile edge - all
    # glyph rects are clipped to the canvas and sized within 2n..14n)
    def _rect(x0, y0, x1, y1, col):
        for y in range(max(0, y0), min(s, y1)):
            for x in range(max(0, x0), min(s, x1)):
                rows[y][x] = col

    white = (0xe8, 0xed, 0xf4, 255)
    blue = (0x4d, 0xa3, 0xff, 255)
    # cloud: stepped rects = body + two bumps (reads rounded at icon size)
    _rect(cx0 + 2 * n, cy0 + 4 * n, cx0 + 10 * n, cy0 + 7 * n, white)
    _rect(cx0 + 3 * n, cy0 + 3 * n, cx0 + 7 * n, cy0 + 4 * n, white)
    _rect(cx0 + 6 * n, cy0 + 2 * n, cx0 + 9 * n, cy0 + 4 * n, white)
    # rain: three bars below the cloud
    for rx in (cx0 + 3 * n, cx0 + 6 * n, cx0 + 8 * n + n // 2):
        _rect(rx, cy0 + 8 * n, rx + n, cy0 + 11 * n, blue)
    return _png(s, s, rows)


# ------------------------------------------------------------ manifest
def _manifest():
    return {
        "name": "Tennessee Weather Network",
        "short_name": "TN Weather",
        "description": ("Live East Tennessee weather: radar, future cast, "
                        "satellite, forecast models, severe weather - free, "
                        "no keys, always updating."),
        "start_url": "./index.html",
        "scope": "./",
        "display": "standalone",
        "background_color": THEME,
        "theme_color": THEME,
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": "icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": "icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "maskable"},
        ],
    }


# ------------------------------------------------------------ service worker
_SW = """/* Tennessee Weather Network service worker.
   Conservative by design (live-weather site): the shell is precached,
   pages + data.json are network-first with offline fallback, and tiles,
   gifs and CDN assets pass through untouched - stale radar is never
   served while a connection exists. */
const VER = "%s";
const SHELL = ["./index.html", "./offline.html", "./icon-192.png",
               "./icon-512.png", "./manifest.webmanifest"];
const PAGES = %s;
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
"""


def _offline_page():
    """Offline fallback in the site's dark theme (stand-alone file)."""
    return """<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Offline - Tennessee Weather Network</title>
<style>
body{margin:0;background:#0e1117;color:#e8edf4;font-family:"Segoe UI",system-ui,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px}
.box{max-width:420px;text-align:center;border:1px solid rgba(255,255,255,.08);
background:#161b26;border-radius:14px;padding:28px 22px}
h1{font-size:20px;margin:0 0 10px}
p{color:#9aa4b2;font-size:14px;line-height:1.7;margin:8px 0}
button{margin-top:14px;background:#2b80ff;color:#fff;border:none;border-radius:8px;
padding:10px 22px;font-size:16px;cursor:pointer}
</style></head><body><div class="box">
<div style="font-size:44px">&#127783;&#65039;</div>
<h1>You're offline</h1>
<p>Live radar, forecasts and alerts need a connection - this site never
shows you hours-old weather as if it were current. Reconnect and tap
retry; the dashboard will pick right back up.</p>
<button onclick="location.reload()">Retry</button>
</div></body></html>"""


# ------------------------------------------------------------ writer
def build_pwa():
    """Write manifest, SW, icons, offline page into static/site (idempotent)."""
    os.makedirs(SITE_DIR, exist_ok=True)
    out = {}
    with open(os.path.join(SITE_DIR, "manifest.webmanifest"), "w",
              encoding="utf-8") as f:
        json.dump(_manifest(), f, indent=1)
    out["manifest"] = "manifest.webmanifest"
    with open(os.path.join(SITE_DIR, "sw.js"), "w", encoding="utf-8") as f:
        # precache the stable pages present in THIS build (SW stays installable
        # even if a page is later retired - PAGES is filtered to what exists)
        pages = [p for p in STABLE_PAGES
                 if p not in ("index.html", "offline.html")  # already in SHELL
                 and os.path.isfile(os.path.join(SITE_DIR, p))]
        # version derives from the page list: adding/removing a page bumps
        # the cache name, so installed SWs refresh instead of keeping the
        # old precache forever
        import hashlib
        ver = "%s-%s" % (SW_VERSION,
                         hashlib.sha1(json.dumps(pages).encode()).hexdigest()[:8])
        f.write(_SW % (ver, json.dumps(pages)))
    out["sw"] = "sw.js"
    with open(os.path.join(SITE_DIR, "offline.html"), "w",
              encoding="utf-8") as f:
        f.write(_offline_page())
    out["offline"] = "offline.html"
    for size in (192, 512):
        fn = f"icon-{size}.png"
        with open(os.path.join(SITE_DIR, fn), "wb") as f:
            f.write(_icon_png(size))
        out[fn] = f"{os.path.getsize(os.path.join(SITE_DIR, fn))} B"
    return out


if __name__ == "__main__":
    print(build_pwa())

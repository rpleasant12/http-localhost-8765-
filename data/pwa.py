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
    "pricing.html", "member.html", "admin.html",
]
# membership client, shipped verbatim as a generated asset (static/ is a
# build output, so the source of truth lives here, not in static/site)
_MEMBER_JS = r'''/* Tennessee Weather Network - membership client (shared by pricing/member
 * admin pages and the Models-page members strip). Plain JS, no deps. The
 * worker origin comes from window.TWN_WORKER, stamped by website.py. */
(function () {
  "use strict";
  var WORKER = (window.TWN_WORKER || "").replace(/\/$/, "");
  if (!WORKER) { console.warn("TWN_WORKER not set - member features disabled"); }

  function req(path, opts) {
    opts = opts || {};
    return fetch(WORKER + path, {
      method: opts.method || "GET",
      credentials: "include",
      headers: opts.body ? { "content-type": "application/json" } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    }).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, data: d }; });
    });
  }

  var ME = null;

  function refreshMe() {
    return req("/api/me").then(function (r) { ME = r.data; return ME; });
  }
  function settled(r) {
    /* Resolve to a consistent {status, data} shape: after a 200 auth call
       refreshMe() hands back the bare ME object, and the auth-card handlers
       test r.status on it - a SUCCESSFUL signup/login used to show
       "Something went wrong." (found in the 2026-10-02 browser test). */
    if (r && typeof r.status === "number") return r;
    return { status: 200, data: r };
  }
  function signup(email, password) {
    return req("/api/auth/signup", { method: "POST", body: { email: email, password: password } })
      .then(function (r) { if (r.status === 200) return refreshMe().then(settled); return r; });
  }
  function login(email, password) {
    return req("/api/auth/login", { method: "POST", body: { email: email, password: password } })
      .then(function (r) { if (r.status === 200) return refreshMe().then(settled); return r; });
  }
  function logout() {
    return req("/api/auth/logout", { method: "POST" }).then(function () { ME = null; return refreshMe(); });
  }
  function startCheckout() {
    return req("/api/billing/checkout", { method: "POST" }).then(function (r) {
      if (r.data && r.data.url) { location.href = r.data.url; return r; }
      alert((r.data && r.data.error) || "Checkout unavailable - Stripe not configured yet.");
      return r;
    });
  }
  function openPortal() {
    return req("/api/billing/portal", { method: "POST" }).then(function (r) {
      if (r.data && r.data.url) { location.href = r.data.url; return r; }
      alert((r.data && r.data.error) || "Billing portal unavailable.");
      return r;
    });
  }
  function premiumIndex() { return req("/api/premium").then(function (r) { return r.data; }); }
  function premiumUrl(rel) { return WORKER + "/api/premium/" + rel; }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* ---- auth card renderer: swaps between login form and member status ---- */
  var AUTH = null;
  function authCard(el, opts) {
    opts = opts || {};
    AUTH = { el: el, opts: opts };
    refreshMe().then(function (me) { paint(el, me, opts); });
  }
  /* Re-paint the auth card with the current ME (no refetch). Used by the
     ?paid=1 banner when the webhook flips premium on AFTER the page already
     rendered "Free account" - opts are preserved so onChange still fires
     and the premium-content gallery loads without a manual reload. */
  function repaintAuth() {
    if (!AUTH || !ME) return;
    paint(AUTH.el, ME, AUTH.opts);
  }

  /* ---- ?paid=1 checkout success banner (member.html) ---------------------
     Stripe redirects back to member.html?paid=1 (+ its session_id) after a
     successful checkout. The webhook that flips premium_until usually lands
     before the redirect, but can lag a few seconds, so: show "activating"
     immediately, poll /api/me for up to ~30s, then either confirm the
     subscription (and live-flip the auth card to Premium via repaintAuth)
     or tell the user to refresh in a minute. The querystring is stripped
     right away so refresh / back-nav never replays the banner. */
  function paidCard(msgHtml) {
    var card = document.getElementById("memCard");
    if (!card || !card.parentElement || !card.parentElement.parentElement) return null;
    var b = document.createElement("div");
    b.id = "paidBanner";
    b.className = "card";
    b.style.borderColor = "#f59e0b";
    b.style.background = "rgba(245,158,11,.08)";
    b.style.margin = "0 0 14px";
    b.innerHTML = msgHtml;
    card.parentElement.parentElement.insertBefore(b, card.parentElement);
    return b;
  }

  function runPaidBanner() {
    var q;
    try { q = new URLSearchParams(location.search); } catch (e) { return; }
    if (q.get("paid") !== "1") return;
    if (document.getElementById("paidBanner")) return;   // idempotent
    var el = paidCard("\u23f3 <b>Payment received</b> - activating your TNWN Premium subscription\u2026");
    if (!el) return;
    try { history.replaceState(null, "", location.pathname); } catch (e) {}
    var tries = 0, MAX = 12, WAIT = 2500;   // ~30s of polling before we stop
    function confirm() {
      el.style.borderColor = "#22c55e";
      el.style.background = "rgba(34,197,94,.08)";
      el.innerHTML = "\u2705 <b>You're subscribed!</b> TNWN Premium is active"
        + (ME && ME.premiumUntil ? " through <b>" + new Date(ME.premiumUntil).toLocaleDateString() + "</b>" : "")
        + ". The Severe Weather, Storms, Tropical and Winter centers and the AI model maps are unlocked - the \u2b50 links in the menu now open the full pages.";
      repaintAuth();
    }
    function poll() {
      refreshMe().then(function (me) {
        if (me && me.member && me.premiumActive) { confirm(); return; }
        if (me && me.member) {   // logged in, webhook has not landed yet
          if (++tries < MAX) { setTimeout(poll, WAIT); return; }
          el.innerHTML = "\u2705 <b>Payment received.</b> Premium is still activating -"
            + " refresh this page in a minute if \u2b50 Premium hasn't appeared yet.";
          return;
        }
        el.innerHTML = "\u2705 <b>Payment received.</b> Log in with the email you used"
          + " at checkout to see your premium status here.";
      }, function () {         // network hiccup: keep trying until MAX
        if (++tries < MAX) { setTimeout(poll, WAIT); return; }
        el.innerHTML = "\u2705 <b>Payment received.</b> We couldn't verify the subscription"
          + " just now - refresh in a minute and it will show here.";
      });
    }
    poll();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", runPaidBanner);
  } else {
    runPaidBanner();
  }

  function paint(el, me, opts) {
    if (me && me.member) {
      el.innerHTML =
        '<div class="mem-ok">' +
        "<b>\u{1F464} " + esc(me.email) + "</b>" +
        (me.admin ? ' <span style="background:#7b1fa2;color:#fff;border-radius:6px;padding:1px 8px;font-size:12px">ADMIN</span>' : "") +
        '<br/><span class="src">' +
        (me.premiumActive
          ? "\u2b50 Premium active" + (me.premiumUntil ? " through " + new Date(me.premiumUntil).toLocaleDateString() : "")
          : "Free account - premium not active") +
        "</span><br/>" +
        (me.premiumActive
          ? '<button class="bBtn" id="memPortal">Manage billing</button> '
          : '<button class="bBtn" id="memUpgrade">\u2b50 Upgrade</button> ') +
        '<button class="bBtn" id="memLogout">Log out</button>' +
        "</div>";
      var po = document.getElementById("memPortal");
      if (po) po.onclick = openPortal;
      var up = document.getElementById("memUpgrade");
      if (up) up.onclick = startCheckout;
      document.getElementById("memLogout").onclick = function () {
        logout().then(function () {
          paint(el, ME, opts);
          /* the footer strip painted on DOMContentLoaded and knows nothing
             about in-page logouts - flip it to the logged-out nudge now,
             else it shows the member as still logged in until a reload
             (found in the 2026-10-02 browser test) */
          var st = document.getElementById("memStrip");
          if (st) st.innerHTML = "<span class='src'>\u2b50 <a href='pricing.html'>Go premium: weather centers, AI model maps, ad-free - $4.99/mo</a></span><br/>";
        });
      };
      if (opts.onChange) opts.onChange(me);
      return;
    }
    el.innerHTML =
      '<div class="mem-form">' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:end">' +
      '<label>Email<br><input id="memEmail" type="email" autocomplete="email" style="min-width:210px"></label>' +
      '<label>Password<br><input id="memPass" type="password" autocomplete="current-password" style="min-width:160px"></label>' +
      '<button class="bBtn" id="memLogin">Log in</button>' +
      '<button class="bBtn" id="memSignup">Create account</button>' +
      "</div>" +
      '<div class="src" id="memMsg" style="margin-top:6px"></div></div>';
    var msg = document.getElementById("memMsg");
    function showErr(r) { msg.textContent = (r.data && r.data.error) || "Something went wrong."; }
    document.getElementById("memLogin").onclick = function () {
      msg.textContent = "...";
      login(document.getElementById("memEmail").value.trim(), document.getElementById("memPass").value)
        .then(function (r) { if (r.status === 200) { paint(el, ME, opts); } else showErr(r); });
    };
    document.getElementById("memSignup").onclick = function () {
      msg.textContent = "...";
      signup(document.getElementById("memEmail").value.trim(), document.getElementById("memPass").value)
        .then(function (r) { if (r.status === 200) { paint(el, ME, opts); } else showErr(r); });
    };
    if (opts.onChange) opts.onChange(null);
  }

  window.TWN = window.TWN || {};
  window.TWN.member = {
    refreshMe: refreshMe, signup: signup, login: login, logout: logout,
    startCheckout: startCheckout, openPortal: openPortal,
    premiumIndex: premiumIndex, premiumUrl: premiumUrl, authCard: authCard, esc: esc,
    repaintAuth: repaintAuth,
    get me() { return ME; },
  };
})();
'''
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
                        "satellite, forecast models - free, no keys, "
                        "always updating."),
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
    with open(os.path.join(SITE_DIR, "member.js"), "w", encoding="utf-8") as f:
        f.write(_MEMBER_JS)
    out["member_js"] = "member.js"
    for size in (192, 512):
        fn = f"icon-{size}.png"
        with open(os.path.join(SITE_DIR, fn), "wb") as f:
            f.write(_icon_png(size))
        out[fn] = f"{os.path.getsize(os.path.join(SITE_DIR, fn))} B"
    return out


if __name__ == "__main__":
    print(build_pwa())

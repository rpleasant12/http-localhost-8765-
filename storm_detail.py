"""Per-storm detail pages for the TNWN storm archive.

page_storm_detail(d, sid) renders storm_<sid>.html: the full advisory
timeline (oldest first), intensity/pressure sparklines, the cone-evolution
player, and a per-advisory "Share this advisory" link that opens a
public OG landing page (archive/<sid>/<stamp>.html, generated here alongside
the archived images so Facebook shows a rich preview card).
"""
import html
import json
import os
import re
import urllib.parse

import config


def _spark_svg(points, w=560, h=130, color="#4da3ff"):
    """Minimal inline SVG sparkline. points = [(x_label, value), ...] in
    chronological order; skips None values. Returns '' when unusable."""
    vals = [(t, v) for t, v in points if isinstance(v, (int, float))]
    if len(vals) < 2:
        return ""
    lo = min(v for _t, v in vals)
    hi = max(v for _t, v in vals)
    if hi == lo:
        hi = lo + 1
    pad, bw, bh = 26, w - 46, h - 40
    xs = [pad + i * bw / (len(vals) - 1) for i in range(len(vals))]
    ys = [pad * 0.6 + bh - (v - lo) * bh / (hi - lo) for _t, v in vals]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{color}"/>'
        for x, y in zip(xs, ys))
    labels = "".join(
        f'<text x="{x:.1f}" y="{h - 8}" font-size="10" fill="#7d8794" '
        f'text-anchor="middle">{t}</text>'
        for x, (t, _v) in zip(xs, vals))
    lo_l = (f'<text x="2" y="{pad * 0.6 + 10}" font-size="10" '
            f'fill="#7d8794">{lo:.0f}</text>')
    hi_l = (f'<text x="2" y="{pad * 0.6 - 2}" font-size="10" '
            f'fill="#7d8794">{hi:.0f}</text>')
    return (f'<svg width="{w}" height="{h}" role="img" '
            f'style="max-width:100%;background:#10151f;border-radius:8px">'
            f'<polyline points="{poly}" fill="none" stroke="{color}" '
            f'stroke-width="2"/>{dots}{labels}{lo_l}{hi_l}</svg>')


def season_summary_png(d, year=None):
    """Year-in-review graphic (static/share/season_<year>.png).

    One panel per archived storm in the season: an Atlantic/Caribbean map
    (simple equirectangular projection, 15W-100W / 5N-45N) with the storm's
    archived track colored by category and a peak-intensity dot, plus a
    per-storm stat block. Rebuilt only when a track gains points; written
    to a temp file then atomically swapped. Returns the static/ path or
    None. Never raises.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        year = year or _utc_year()
        storms = []
        for b in (d.get("stormArchive") or []):
            pts = [(a.get("lon"), a.get("lat"), a.get("intensity"),
                    a.get("class")) for a in (b.get("advisories") or [])]
            pts = [(lo, la, k, c) for lo, la, k, c in pts
                   if isinstance(lo, (int, float))
                   and isinstance(la, (int, float))]
            if len(pts) >= 2:
                peak = max((k for _lo, _la, k, _c in pts
                            if isinstance(k, (int, float))), default=0)
                storms.append({"b": b, "pts": pts, "peak": peak})
        if not storms:
            return None
        out = os.path.join("static", "share", f"season_{year}.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        npts = sum(len(s["pts"]) for s in storms)
        try:
            if os.path.exists(out + ".n") and \
                    open(out + ".n").read().strip() == str(npts) \
                    and os.path.exists(out) and \
                    os.path.exists(out.replace(".png", ".html")):
                return out.replace("\\", "/")
        except OSError:
            pass

        W, H = 1200, 700
        img = Image.new("RGB", (W, H), (16, 19, 24))
        d2 = ImageDraw.Draw(img)

        def _font(sz, bold=False):
            for p in ("C:/Windows/Fonts/segoeuib.ttf" if bold else
                      "C:/Windows/Fonts/segoeui.ttf",
                      "C:/Windows/Fonts/arialbd.ttf" if bold else
                      "C:/Windows/Fonts/arial.ttf"):
                try:
                    return ImageFont.truetype(p, sz)
                except OSError:
                    continue
            return ImageFont.load_default()

        # map panel (equirectangular, auto-fit to the season's storm points
        # so Atlantic AND East Pacific storms both land on the map)
        MX, MY, MW, MH = 30, 90, 620, 560
        all_pts = [(lo, la) for s in storms for lo, la, _k, _c in s["pts"]]
        lo_min = min(lo for lo, _la in all_pts) - 6
        lo_max = max(lo for lo, _la in all_pts) + 6
        la_min = min(la for _lo, la in all_pts) - 5
        la_max = max(la for _lo, la in all_pts) + 5
        lo_min, lo_max = max(lo_min, -140.0), min(lo_max, -10.0)
        la_min, la_max = max(la_min, 0.0), min(la_max, 52.0)
        LO0, LO1, LA0, LA1 = lo_min, lo_max, la_min, la_max

        def _xy(lo, la):
            x = MX + (lo - LO0) / (LO1 - LO0) * MW
            y = MY + MH - (la - LA0) / (LA1 - LA0) * MH
            return x, y

        d2.rectangle((MX, MY, MX + MW, MY + MH), fill=(20, 30, 42))
        for glo in range(int(LO0), int(LO1) + 1, 10):
            x, _y = _xy(glo, LA0)
            d2.line((x, MY, x, MY + MH), fill=(38, 48, 62), width=1)
        for gla in range(int(LA0), int(LA1) + 1, 10):
            _x, y = _xy(LO0, gla)
            d2.line((MX, y, MX + MW, y), fill=(38, 48, 62), width=1)
        # very coarse coastline polylines covering the Atlantic + East
        # Pacific basins the archive can span (hand-simplified)
        _coast(MX, MY, MW, MH, LO0, LO1, LA0, LA1, _xy, d2)

        def _kt(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        CAT = {"TD": (140, 170, 220), "SD": (140, 170, 220),
               "TS": (90, 200, 140), "SS": (90, 200, 140),
               "HU": (255, 190, 80), "MH": (255, 90, 90),
               "PTC": (150, 150, 160)}
        for s in storms:
            # numeric kt per point (NHC stores strings) - drives the peak dot
            pts_k = [(lo, la, _kt(k), c) for lo, la, k, c in s["pts"]]
            s["kt"] = [k for _lo, _la, k, _c in pts_k]
            col = CAT.get(s["b"].get("class") or "", (220, 220, 230))
            xy = [_xy(lo, la) for lo, la, _k, _c in pts_k]
            d2.line(xy, fill=col, width=4)
            # small dots along the track + big peak-intensity dot
            for lo, la, k, _c in pts_k:
                x, y = _xy(lo, la)
                d2.ellipse((x - 2.5, y - 2.5, x + 2.5, y + 2.5), fill=col)
            pk = max(pts_k, key=lambda t: t[2] or 0)
            x, y = _xy(pk[0], pk[1])
            _pkv = pk[2] or 0
            r = 7 + min(10, int(_pkv) // 15)

        # legend + stat block
        x = MX + MW + 50
        y = MY - 20
        d2.text((x, 34), "TENNESSEE WEATHER NETWORK", font=_font(22, True),
                fill=(255, 170, 60))
        d2.text((x, y), f"{year} Atlantic season", font=_font(32, True),
                fill=(240, 244, 250))
        y += 56
        d2.text((x, y), f"{len(storms)} archived storm"
                f"{'s' if len(storms) != 1 else ''}",
                font=_font(20), fill=(160, 170, 182))
        y += 42
        CLS = {"TD": "Tropical Depression", "SD": "Subtropical Dep.",
               "TS": "Tropical Storm", "SS": "Subtropical Storm",
               "HU": "Hurricane", "MH": "Major Hurricane",
               "PTC": "Post-tropical"}
        for s in sorted(storms, key=lambda s: -(max(s["kt"]) if s["kt"] else 0)):
            b = s["b"]
            col = CAT.get(b.get("class") or "", (220, 220, 230))
            d2.ellipse((x, y + 4, x + 14, y + 18), fill=col)
            peak_kt = max(s["kt"]) if s["kt"] else None
            d2.text((x + 24, y), f"{b.get('name') or '?'}", font=_font(20, True),
                    fill=(225, 232, 240))
            d2.text((x + 24, y + 26),
                    f"{CLS.get(b.get('class') or '', '')} \u00b7 peak "
                    f"{int(peak_kt) if peak_kt else '?'} kt "
                    f"\u00b7 {b.get('count') or '?'} advisories",
                    font=_font(15), fill=(150, 160, 172))
            y += 66
        y = max(y, MY + MH - 40)
        d2.text((24, H - 34),
                f"Tracks from archived NHC advisory positions \u00b7 "
                f"facebook.com/tennesseeweathernetwork",
                font=_font(15), fill=(122, 132, 144))
        d2.rectangle((0, 0, W, 6), fill=(255, 170, 60))
        tmp = out + ".tmp"
        img.save(tmp, "PNG", optimize=True)
        os.replace(tmp, out)
        with open(out + ".n", "w") as fh:
            fh.write(str(npts))
        # public OG landing page for the season graphic (one-click FB share)
        try:
            pub = config.PUBLIC_SITE_URL.rstrip("/")
            page_url = f"{pub}/share/season_{year}.html"
            img_url = f"{pub}/share/season_{year}.png"
            title = f"{year} hurricane season in review"
            desc = (f"{len(storms)} archived storms - tracks and peak "
                    "intensity from official NHC advisories. "
                    "Tennessee Weather Network storm archive.")
            sharer = ("https://www.facebook.com/sharer/sharer.php?u="
                      + urllib.parse.quote(page_url, safe=""))
            page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:type" content="article">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:image" content="{img_url}">
<meta property="og:url" content="{page_url}">
<title>{html.escape(title)}</title>
<style>body{{margin:0;background:#12151a;color:#e8eef5;font:16px/1.5 system-ui,sans-serif;text-align:center}}
img{{max-width:min(94vw,1100px);border-radius:12px;margin:18px auto 6px;display:block}}
a.btn{{display:inline-block;margin:14px 6px 30px;padding:13px 26px;border-radius:10px;
background:#1877f2;color:#fff;font-weight:700;text-decoration:none;font-size:18px}}
a.alt{{background:#2a313b;color:#cdd7e4;font-weight:500;font-size:15px}}
</style></head><body>
<img src="{img_url}" alt="{html.escape(title)}">
<a class="btn" href="{sharer}" target="_blank" rel="noopener">Share on Facebook</a>
<a class="btn alt" href="{img_url}" target="_blank" rel="noopener">Open full image</a>
</body></html>"""
            pg = out.replace(".png", ".html")
            if (not os.path.exists(pg)
                    or open(pg, encoding="utf-8").read() != page):
                ptmp = pg + ".tmp"
                with open(ptmp, "w", encoding="utf-8") as fh:
                    fh.write(page)
                os.replace(ptmp, pg)
        except OSError:
            pass
        return out.replace("\\", "/")
    except Exception:  # noqa: BLE001
        return None


def _utc_year():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).year


def _coast(mx, my, mw, mh, lo0, lo1, la0, la1, xy, dr):
    """Very coarse North-America/Caribbean coastline polylines so the map
    reads as the Atlantic basin (hand-simplified, good enough at this size)."""
    COASTS = [
        # US East Coast + Gulf (west -> east)
        [(-97, 26), (-95, 29), (-90, 29), (-84, 30), (-82, 25), (-80, 25),
         (-81, 31), (-76, 35), (-74, 40), (-70, 42), (-67, 45)],
        # Central America -> Yucatan
        [(-100, 20), (-94, 16), (-88, 16), (-87, 21), (-90, 22), (-94, 18)],
        # Greater Antilles
        [(-85, 22), (-80, 23), (-77, 20), (-74, 20), (-71, 19)],
        # Lesser Antilles arc
        [(-61, 10), (-61, 15), (-63, 17), (-66, 18)],
        # South America north coast
        [(-72, 12), (-66, 10), (-61, 9), (-55, 6), (-50, 0)],
        # Bahamas
        [(-78, 27), (-75, 26), (-73, 24)],
        # Mexico west coast + Central America (East Pacific storms)
        [(-110, 24), (-112, 22), (-115, 20), (-110, 16), (-105, 12),
         (-100, 8), (-96, 6)],
        [(-97, 16), (-94, 12), (-92, 8)],
        # Hawaii reference (far west edge)
        [(-156, 20), (-155, 19)],
    ]
    for line in COASTS:
        pts = [xy(lo, la) for lo, la in line
               if lo0 <= lo <= lo1 and la0 <= la <= la1]
        if len(pts) >= 2:
            dr.line(pts, fill=(120, 132, 148), width=2)


def advisory_share_pages(d):
    """Write public OG landing pages for every archived advisory.

    Files: static/archive/<sid>/<stamp>.html (Facebook sharer target with
    that advisory's summary/cone as og:image). Content-deduped so the
    every-cycle build only touches changed files. Returns count written.
    """
    n = 0
    pub = config.PUBLIC_SITE_URL.rstrip("/")
    for b in (d.get("stormArchive") or []):
        sid = b.get("id") or ""
        adv_dir = os.path.join("static", "archive", sid)
        if not os.path.isdir(adv_dir):
            continue
        cls_lbl = {"HU": "Hurricane", "MH": "Major Hurricane",
                   "TS": "Tropical Storm", "TD": "Tropical Depression",
                   "SS": "Subtropical Storm", "SD": "Subtropical Depression",
                   "PTC": "Post-tropical"}.get(b.get("class") or "", "Cyclone")
        for a in b.get("advisories") or []:
            try:
                stamp = a.get("stamp")
                if not stamp:
                    continue
                page_url = f"{pub}/archive/{sid}/{stamp}.html"
                img = (f"{pub}/archive/{sid}/{stamp}_summary.png"
                       if a.get("summary")
                       else f"{pub}/archive/{sid}/{stamp}_cone.png")
                title = f"{cls_lbl} {b['name']} \u00b7 {a.get('lastUpdate') or stamp}"
                desc = (f"{a.get('intensity') or '?'} kt winds, pressure "
                        f"{a.get('pressure') or '?'} mb. Official NHC cone "
                        "from the TNWN storm archive.")
                if a.get("tnThreat") == "watch":
                    desc += " Watch/warning area included Tennessee."
                elif a.get("tnThreat") == "track":
                    desc += " Forecast track toward Tennessee."
                sharer = ("https://www.facebook.com/sharer/sharer.php?u="
                          + urllib.parse.quote(page_url, safe=""))
                doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:type" content="article">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:image" content="{img}">
<meta property="og:url" content="{page_url}">
<title>{html.escape(title)}</title>
<style>body{{margin:0;background:#12151a;color:#e8eef5;font:16px/1.5 system-ui,sans-serif;text-align:center}}
img{{max-width:min(94vw,900px);border-radius:12px;margin:18px auto 6px;display:block}}
a.btn{{display:inline-block;margin:14px 6px 30px;padding:13px 26px;border-radius:10px;
background:#1877f2;color:#fff;font-weight:700;text-decoration:none;font-size:18px}}
a.alt{{background:#2a313b;color:#cdd7e4;font-weight:500;font-size:15px}}
.src{{color:#7d8794;font-size:13px;margin:8px 0 40px}}</style></head><body>
<img src="{img}" alt="{html.escape(title)}">
<a class="btn" href="{sharer}" target="_blank" rel="noopener">Share on Facebook</a>
<a class="btn alt" href="{img}" target="_blank" rel="noopener">Open full image</a>
<div class="src"><a href="../../storms.html" style="color:#7d8794">Storm history</a>
 &#183; <a href="https://www.facebook.com/tennesseeweathernetwork" style="color:#7d8794">Tennessee Weather Network</a></div>
</body></html>"""
                dst = os.path.join(adv_dir, stamp + ".html")
                if (not os.path.exists(dst)
                        or open(dst, encoding="utf-8").read() != doc):
                    tmp = dst + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        fh.write(doc)
                    os.replace(tmp, dst)
                    n += 1
            except Exception:  # noqa: BLE001
                continue
    return n


def page_storm_detail(d, sid):
    """Detail page for one archived storm; None when the id is unknown."""
    b = next((x for x in (d.get("stormArchive") or []) if x.get("id") == sid),
             None)
    if not b:
        return None
    from website import _page          # late import: site shell helper
    cls_lbl = {"HU": "Hurricane", "MH": "Major Hurricane",
               "TS": "Tropical Storm", "TD": "Tropical Depression",
               "SS": "Subtropical Storm", "SD": "Subtropical Depression",
               "PTC": "Post-tropical"}.get(b.get("class") or "", "")
    adv = list(reversed(b.get("advisories") or []))   # oldest -> newest

    def _short(t):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})", str(t))
        return f"{m.group(2)}/{m.group(3)} {m.group(4)}z" if m else str(t)[:9]

    kt_svg = _spark_svg([(_short(a.get("lastUpdate")), a.get("intensity"))
                         for a in adv], color="#ff8a80")
    mb_svg = _spark_svg([(_short(a.get("lastUpdate")),
                          float(a["pressure"]) if a.get("pressure") else None)
                         for a in adv], color="#4da3ff")
    fr = [{"u": a["cone"], "t": str(a.get("lastUpdate") or ""),
           "k": a.get("intensity"), "p": a.get("pressure")} for a in adv]

    cards = ""
    for a in adv:
        threat = ("<span style=\"color:#ff8a80;font-weight:700\">\u26a0 TN watch</span>"
                  if a.get("tnThreat") == "watch"
                  else ("<span style=\"color:#e0a458\">\u2192 TN track</span>"
                        if a.get("tnThreat") == "track" else ""))
        sum_img = (f'<a href="{a["summary"]}" target="_blank" rel="noopener">'
                   f'<img src="{a["summary"]}" loading="lazy" alt="summary" '
                   'style="width:100%;max-width:260px;border-radius:8px"/></a>'
                   if a.get("summary") else "")
        cards += (
            f'<div class="day" style="text-align:left">'
            f'<div class="dname" style="font-size:13px">'
            f'{html.escape(str(a["lastUpdate"]))} {threat}</div>'
            f'<a href="{a["cone"]}" target="_blank" rel="noopener">'
            f'<img src="{a["cone"]}" loading="lazy" alt="cone" '
            'style="width:100%;max-width:260px;border-radius:8px;'
            'margin-top:4px"/></a>'
            + sum_img +
            f'<div style="color:#7d8794;font-size:12px;margin-top:4px">'
            f'{a.get("intensity") or "?"} kt \u00b7 '
            f'{a.get("pressure") or "?"} mb</div>'
            f'<div style="margin-top:6px">'
            f'<a href="archive/{sid}/{a["stamp"]}.html" target="_blank" '
            'rel="noopener" style="color:#1877f2;font-weight:600">'
            '\U0001f4e3 Share this advisory</a></div></div>')

    body = f"""
<header class="hero"><h1>\U0001f32f <span style="color:var(--acc)">{html.escape(b["name"])}</span></h1>
<div class="sub">{html.escape(cls_lbl)} &#183; {b["count"]} archived advisories &#183; newest {html.escape(str(b["advisories"][0]["lastUpdate"]))} &#183; <a href="storms.html">back to storm history</a></div></header>

<div class="card"><h2>\U0001f4c8 Intensity (kt) &amp; pressure (mb) &#8212; advisory by advisory</h2>
<div style="display:grid;gap:14px">{kt_svg}{mb_svg}</div>
<p class="src">One point per archived NHC advisory, oldest &#8594; newest. Pressure falls as the storm strengthens.</p></div>

<div class="card"><h2>\u25b6\ufe0f Cone evolution</h2>
<img id="danim" src="{fr[0]["u"]}" loading="lazy" alt="cone evolution"
 style="max-width:380px;width:100%;border-radius:10px;border:1px solid #333c46"/>
<div style="display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap">
<button onclick="dAnim.prev()" style="background:#1d2432;color:#eee;border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:14px">\u23ee</button>
<button id="dplay" onclick="dAnim.toggle()" style="background:#1d2432;color:#eee;border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:14px">\u25b6 Play</button>
<button onclick="dAnim.next()" style="background:#1d2432;color:#eee;border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:14px">\u23ed</button>
<span id="dlab" class="src"></span></div></div>

<div class="card"><h2>\U0001f4cb Full advisory timeline</h2>
<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))">{cards}</div></div>

<script>
window.DFRAMES = {json.dumps(fr)};
window.dAnim = {{
  i: 0, timer: null,
  show: function (i) {{
    this.i = ((i % DFRAMES.length) + DFRAMES.length) % DFRAMES.length;
    const f = DFRAMES[this.i];
    document.getElementById("danim").src = f.u;
    document.getElementById("dlab").textContent = (this.i + 1) + "/" + DFRAMES.length
      + " \u00b7 " + f.t + " \u00b7 " + (f.k == null ? "?" : f.k) + " kt \u00b7 "
      + (f.p == null ? "?" : f.p) + " mb";
  }},
  stop: function () {{ if (this.timer) {{ clearInterval(this.timer); this.timer = null;
    document.getElementById("dplay").textContent = "\u25b6 Play"; }} }},
  step: function (dd) {{ this.stop(); this.show(this.i + dd); }},
  next: function () {{ this.step(1); }},
  prev: function () {{ this.step(-1); }},
  toggle: function () {{
    if (this.timer) {{ this.stop(); return; }}
    document.getElementById("dplay").textContent = "\u23f8 Pause";
    this.timer = setInterval(() => this.show(this.i + 1), 1100);
  }}
}};
dAnim.show(0);
</script>
"""
    return _page(b["name"], "storms.html", body)

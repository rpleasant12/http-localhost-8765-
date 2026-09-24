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

"""Climate & long-range outlooks: CPC 6-10/8-14 day, monthly, seasonal + ENSO.

All keyless NOAA/CPC products, downloaded and cached locally (the CPC pages
rotate filenames, so this module re-fetches index pages and mirrors whatever
graphics they currently publish):

- 6-10 day and 8-14 day temperature/precipitation outlook maps (CPC)
- Week 3-4 (8-14) hazards: the same outlooks at longer lead
- Monthly (days 1-31) official temp/precip outlooks
- Seasonal (multi_season 13-lead) temp/precip outlooks
- ENSO: ONI index table + latest ENSO discussion headline/summary

Graphics land in static/climate/ and are referenced by climate.html. Text
(ONI, ENSO discussion) is parsed into compact JSON for cards.
"""
import html as _htmlmod
import os
import re
import threading
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
OUT_DIR = os.path.join("static", "climate")
CACHE = {"t": 0.0, "b": None}
CACHE_LOCK = threading.Lock()

_SESSION = requests.Session()

# index pages -> (out-name prefix, img filename patterns on that page)
SOURCES = [
    ("610", "https://www.cpc.ncep.noaa.gov/products/predictions/610day/index.php",
     {"temp": "610temp", "prcp": "610prcp"}, "6-10 day outlook"),
    ("814", "https://www.cpc.ncep.noaa.gov/products/predictions/814day/index.php",
     {"temp": "814temp", "prcp": "814prcp"}, "8-14 day outlook"),
    ("monthly", "https://www.cpc.ncep.noaa.gov/products/predictions/30day/index.php",
     {"temp": "off15_temp", "prcp": "off15_prcp"}, "Monthly outlook (days 1-31)"),
    ("seasonal", "https://www.cpc.ncep.noaa.gov/products/predictions/long_range/tools.php",
     {"temp": "seasonal_outlooks/color/t", "prcp": "seasonal_outlooks/color/p"}, "Seasonal outlook"),
]
SEASONAL_BASE = "https://www.cpc.ncep.noaa.gov/products/predictions/multi_season/13_seasonal_outlooks/"


def _get(url, timeout=30):
    r = _SESSION.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def _fetch_graphic(url, dst_path):
    """Mirror one CPC graphic into static/climate/ if changed."""
    r = _get(url, timeout=40)
    if os.path.isfile(dst_path) and os.path.getsize(dst_path) == len(r.content):
        return dst_path
    tmp = dst_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, dst_path)
    return dst_path


def _grab_index_imgs(index_url):
    """All gif/png hrefs on a CPC index page (they rotate filenames)."""
    html_txt = _get(index_url).text
    return sorted(set(re.findall(r'(?:src|href)="([^"]+\.(?:gif|png))"', html_txt)))


def _mirror_group(gid, idx_url, keys, base_url=None):
    """Download the current temp/prcp images for one outlook group."""
    try:
        imgs = _grab_index_imgs(idx_url)
    except Exception:
        return {}
    out = {}
    for kind, prefix in keys.items():
        hit = next((u for u in imgs if prefix in u), None)
        if not hit:
            continue
        if hit.startswith("http"):
            url = hit
        elif hit.startswith("/"):
            url = "https://www.cpc.ncep.noaa.gov" + hit
        else:
            url = idx_url.rsplit("/", 1)[0] + "/" + hit.lstrip("./")
        # deterministic flat name - CPC hrefs sometimes nest subdirectories
        out_name = f"{gid}_{kind}.gif"
        dst = os.path.join(OUT_DIR, out_name)
        try:
            _fetch_graphic(url, dst)
            out[kind] = f"../climate/{out_name}"
        except Exception:
            continue
    return out


def oni_table():
    """Oceanic Nino Index - last 12 seasons (3-month SST anomalies, Nino 3.4)."""
    txt = _get("https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt").text
    rows = []
    for line in txt.strip().splitlines():
        parts = line.split()
        # format: SEAS YR TOTAL ANOM  (e.g. 'DJF 1950 25.01 -1.32')
        if len(parts) >= 4 and len(parts[0]) == 3 and parts[0].isalpha() and parts[1].isdigit():
            try:
                rows.append({"season": f"{parts[0]} {parts[1]}", "anom": float(parts[3])})
            except ValueError:
                continue
    return rows[-12:]


def enso_summary():
    """Latest ENSO discussion: title + first paragraphs (plain text)."""
    try:
        raw = _get("https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/"
                   "enso_advisory/ensodisc.shtml").text
    except Exception:
        return {}
    # strip tags crudely, keep paragraphs
    txt = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<style.*?</style>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", "\n", txt)
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    # discussion body usually starts at 'ENSO ALERT SYSTEM' or 'Synopsis:'
    body, collecting = [], False
    for l in lines:
        low = l.lower()
        if not collecting and ("synopsis:" in low or "alert system" in low):
            collecting = True
        if collecting and len(" ".join(body)) < 2200:
            body.append(l)
    paras = []
    cur = []
    for l in body:
        if len(l) < 3 and cur:
            paras.append(" ".join(cur)); cur = []
        elif len(l) >= 3:
            cur.append(l)
    if cur:
        paras.append(" ".join(cur))
    # unescape entities (El Ni&ntilde;o -> El Niño), collapse nbsp runs
    paras = [re.sub(r"\s+", " ", _htmlmod.unescape(p)).replace("\xa0", " ").strip()
             for p in paras]
    paras = [p for p in paras if p]
    m_title = re.search(r"ENSO\s*Alert\s*System\s*Status:.*?<span[^>]*>([^<]+)</span>",
                        raw, re.I | re.S)
    title = ("ENSO Alert System Status: " + _htmlmod.unescape(m_title.group(1)).strip()) \
        if m_title else "ENSO Diagnostic Discussion"
    return {
        "title": re.sub(r"\s+", " ", title).replace("\xa0", " "),
        "paragraphs": paras[:4],
        "source": "CPC ENSO Diagnostic Discussion",
    }


def oni_status(oni):
    """Classify current ENSO phase from the latest ONI anomaly."""
    if not oni:
        return "unknown", "#9e9e9e"
    a = oni[-1]["anom"]
    if a >= 0.5:
        return ("El Nino", "#ef5350") if a >= 1.0 else ("weak El Nino", "#ffb74d")
    if a <= -0.5:
        return ("La Nina", "#42a5f5") if a <= -1.0 else ("weak La Nina", "#90caf9")
    return "ENSO-neutral", "#9e9e9e"


def bundle(max_age=3600):
    """Everything the climate page needs; cached (CPC updates daily-ish).

    The cache is SELF-HEALING: if any image the cached bundle references has
    vanished from disk (a cleanup sweep once deleted half the outlook set,
    2026-09-15), the cache is discarded and everything is re-mirrored now -
    so a broken climate page can never persist for more than one build.
    """
    with CACHE_LOCK:
        now = time.time()
        if CACHE["b"] is not None and now - CACHE["t"] < max_age:
            cached = CACHE["b"]
            refs_ok = True
            for g in cached.get("groups", []):
                for href in (g.get("images") or {}).values():
                    fn = os.path.basename(href)
                    if not os.path.isfile(os.path.join(OUT_DIR, fn)):
                        refs_ok = False
                        break
                if not refs_ok:
                    break
            if refs_ok:
                return cached
            CACHE.update(t=0.0, b=None)   # stale refs -> full re-mirror below

    os.makedirs(OUT_DIR, exist_ok=True)
    groups = []
    for (gid, idx_url, kinds, label) in SOURCES:
        base = SEASONAL_BASE if gid == "seasonal" else None
        imgs = _mirror_group(gid, idx_url, kinds, base_url=base)
        if imgs:
            groups.append({"id": gid, "label": label, "images": imgs})

    oni = []
    try:
        oni = oni_table()
    except Exception:
        pass
    phase, phase_color = oni_status(oni)
    try:
        enso = enso_summary()
    except Exception:
        enso = {}

    b = {
        "generated": _stamp(),
        "groups": groups,
        "oni": oni,
        "ensoPhase": phase, "ensoPhaseColor": phase_color,
        "enso": enso,
        "source": "NOAA CPC long-range outlooks + ENSO diagnostics",
    }
    with CACHE_LOCK:
        CACHE.update(t=time.time(), b=b)
    return b


def _stamp():
    from data import _tz
    return _tz.stamp(__import__("datetime").datetime.now(__import__("datetime").timezone.utc))

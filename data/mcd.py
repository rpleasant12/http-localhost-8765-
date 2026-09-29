"""SPC Mesoscale Discussions (MCD) as map polygons.

IEM's spc_mcd_current.geojson has been in and out of maintenance, so this
fetcher goes straight to SPC's own published text products: lastmd.txt plus
the latest product page from the MD index. Each product's LAT...LON block
decodes to lon/lat pairs - 8-digit tokens split 4+4, hundredths of a degree,
west negative with the >3600 wrap past 100 W (SPC's decades-old convention,
verified against products from Montana to Arkansas).
"""
import datetime as dt
import re
import time

import requests

from data import _tz

LASTMD_URL = "https://www.spc.noaa.gov/products/spcmd/lastmd.txt"
INDEX_URL = "https://www.spc.noaa.gov/products/md/"
MD_URL = "https://www.spc.noaa.gov/products/md/md{n}.html"
UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}
_CACHE = {"t": 0.0, "b": None}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"))}


def _get_text(url, pre=False):
    """GET a URL; with pre=True extract+clean the <pre> product text."""
    try:
        r = requests.get(url, timeout=12, headers=UA)
        if not r.ok:
            return None
        t = r.text
        if pre:
            m = re.search(r"<pre[^>]*>(.*?)</pre>", t, re.S)
            if not m:
                return None
            t = re.sub(r"<[^>]+>", "", m.group(1))
        return t
    except Exception:  # noqa: BLE001 - network flake -> caller skips
        return None


def _decode_coords(txt):
    """LAT...LON block -> [[lon, lat], ...] ring (GeoJSON order).

    Each 8-digit token is one vertex: first 4 digits = lat, last 4 = lon,
    in hundredths of a degree (west negative, wrapped past 100 W).
    """
    m = re.search(r"LAT\.\.\.LON\s+(\d{8}[\s\S]*?)(?:\n\s*\n|\Z)", txt)
    if not m:
        return None
    toks = re.findall(r"\d{8}", m.group(1))
    if len(toks) < 4:
        return None
    ring = []
    for tok in toks:
        lat = int(tok[:4]) / 100.0
        lon4 = int(tok[4:]) / 100.0
        if lon4 < 65:            # wrapped past 100 W (SPC convention)
            lon4 += 100
        if not (15 <= lat <= 72 and 55 <= lon4 <= 130):
            return None          # decode sanity: CONUS-ish only
        ring.append([-lon4, lat])
    if ring[0] != ring[-1]:      # close the ring
        ring.append(ring[0])
    return [ring]


def _issue_utc(txt):
    """Header '0541 PM CDT Thu Sep 10 2026' -> aware UTC datetime (or None)."""
    m = re.search(r"(\d{3,4})\s+(AM|PM)\s+(CDT|CST|EDT|EST)\s+\w{3}\s+"
                  r"(\w{3})\.?\s+(\d{1,2}),?\s+(\d{4})", txt)
    if not m:
        return None
    hhmm, ampm, tzn, mon, day, year = m.groups()
    h = int(hhmm[:2]) % 12 + (12 if ampm == "PM" else 0)
    mi = int(hhmm[2:])
    month = _MONTHS.get(mon.lower()[:3])
    if not month:
        return None
    off = 5 if tzn in ("CDT", "EST") else 6 if tzn == "CST" else 4
    try:
        loc = dt.datetime(int(year), month, int(day), h, mi,
                          tzinfo=dt.timezone(dt.timedelta(hours=-off)))
        return loc.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _valid_window(txt, issued):
    """'Valid 102241Z - 110045Z' -> (start_utc, end_utc)."""
    m = re.search(r"Valid\s+(\d{2})(\d{2})(\d{2})Z\s*-\s*(\d{2})(\d{2})(\d{2})Z", txt)
    if not m or not issued:
        return None, None
    sd, sh, sm, ed, eh, em = (int(g) for g in m.groups())
    base = issued
    try:
        start = base.replace(day=sd, hour=sh, minute=sm)
        end = base.replace(day=ed, hour=eh, minute=em)
        if ed < sd:              # rolled past month end
            nxt = (dt.datetime(base.year, base.month, 28) + dt.timedelta(days=6))
            end = end.replace(year=nxt.year, month=nxt.month)
        return start, end
    except ValueError:
        return None, None


def _para(txt, key, stop_key=None):
    """'KEY...value' paragraph, collapsed to one line (or None)."""
    pat = (rf"{key}\.\.\.([\s\S]*?)(?=\n\s*\n"
           + (rf"|\n\s*{stop_key}\.\.\." if stop_key else "") + r"|\Z)")
    m = re.search(pat, txt)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip()


def parse_product(txt, url=None):
    """One MCD text product -> feature dict (None if not an MCD)."""
    if not txt or "MESOSCALE" not in txt.upper():
        return None
    num_m = re.search(r"MESOSCALE DISCUSSION (\d+)", txt, re.I)
    ring = _decode_coords(txt)
    if not num_m or not ring:
        return None
    issued = _issue_utc(txt)
    start, end = _valid_window(txt, issued)
    prob_m = re.search(r"Probability of Watch Issuance\.\.\.(\d+)\s*percent", txt, re.I)
    attn_m = re.search(r"ATTN\.\.\.WFO\.\.\.([\s\S]{3,120}?)(?:\n\s*\n|\Z)", txt)
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "num": int(num_m.group(1)),
        "url": url or (MD_URL.format(n=num_m.group(1))),
        "areas": _para(txt, "Areas affected") or "",
        "concerning": _para(txt, "Concerning") or "",
        "prob": int(prob_m.group(1)) if prob_m else None,
        "summary": _para(txt, "SUMMARY", "DISCUSSION") or "",
        "discussion": (re.sub(r"\s+", " ",
                              re.search(r"DISCUSSION\.\.\.([\s\S]*?)(?=\n\s*\.\.|Please see|\Z)", txt).group(1)).strip()
                      if re.search(r"DISCUSSION\.\.\.([\s\S]*?)(?=\n\s*\.\.|Please see|\Z)", txt) else ""),
        "wfos": re.sub(r"[\s.]+", " ", attn_m.group(1)).strip() if attn_m else "",
        "validStart": _tz.stamp(start) if start else "-",
        "validEnd": _tz.stamp(end) if end else "-",
        "current": bool(end and end >= now),
        "geometry": {"type": "Polygon", "coordinates": ring},
    }


def bundle(max_age=300):
    """Bundle: active + recent MCDs (newest first, capped at 4)."""
    now = time.time()
    if _CACHE["b"] is not None and now - _CACHE["t"] < max_age:
        return _CACHE["b"]
    texts = []
    raw = _get_text(LASTMD_URL)
    if raw:
        texts.append((raw, None))
    idx = _get_text(INDEX_URL)
    if idx:
        m = re.search(r"md(\d{4})\.html", idx)
        if m:
            page = _get_text(MD_URL.format(n=m.group(1)), pre=True)
            if page:
                texts.append((page, MD_URL.format(n=m.group(1))))
    feats, seen = [], set()
    for txt, url in texts:
        try:
            f = parse_product(txt, url)
        except Exception:  # noqa: BLE001 - one bad product never kills the bundle
            f = None
        if f and f["num"] not in seen:
            seen.add(f["num"])
            feats.append(f)
    feats.sort(key=lambda f: f["num"], reverse=True)
    b = {"features": feats[:4],
         "fetched": _tz.full(dt.datetime.now(dt.timezone.utc))}
    _CACHE.update(t=now, b=b)
    return b

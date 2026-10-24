"""WPC Probabilistic Precipitation Portal - mirrored + Leaflet-ready.

The portal (https://www.wpc.ncep.noaa.gov/Prob_Precip/) is the NWS's
experimental ensemble-odds guidance for the next 72 hours: expected
snow/QPF amounts with low-end and high-end (10th/90th-percentile) bands,
plus "at least N inches" exceedance-probability fields that deliberately
OUTRUN the deterministic maps on the Winter page. That page renders its
PNG tiles inside a locked ArcGIS SDK; we fetch the same tiles ourselves
(four per CONUS cycle, standard Web Mercator bbox) and reassemble one
composite PNG per product into static/winter/ppp/ - identical imagery,
no map-key barrier.

Tile placement comes straight from the portal's script.js ('co' region
branch, geographic bounds per tile). Product slugs follow its own scheme
(pw24ige4p00 = winter probabilistic field, 24-hour window, at least 4.00
inches; ipndfd = expected amount). Empty fields arrive as fully
transparent tiles and are reported...
"""
import datetime as dt
import io
import os
import re
import threading
import time

import numpy as np
import requests

from data._tz import full as _full

OUT_DIR = os.path.join("static", "winter", "ppp")
# gh-pages packaging existence-checks ppp.views refs minutes AFTER the next
# hourly build already pruned the referenced cycle (2026-10-23: views were
# silently emptied on the public copy). Keep this many recent cycles of
# composites: a 4-product set is ~1 MB, so depth 3 is trivially cheap
# and bridges the prune-vs-package race.
_KEEP_CYCLE_DEPTH = 3
UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
HOST = "https://www.wpc.ncep.noaa.gov/Prob_Precip"

# 'co' (CONUS) tile bounds from the portal's script.js - geographic bbox
# per _itr tile in PNG file order (row-major y,x).
_TILE_W = -124.786952, -95.856708, -66.926465      # x edges (west->east)
_TILE_H = 49.389568, 38.002106, 24.530802          # y edges (north->south)
IM_SIZE = (2643, 1445)                             # px per tile (w, h)

# (slug, human label, science line). The exceedance grid stacks like a
# ladder: 0.1" asks where ANY snow falls, 1"/4"/8" climb the impact
# ladder. In the warm season only the bottom rung has honest contours;
# empty rungs are dropped and the page says so (never invent odds).
PRODUCTS = (
    ("pw24ige0p10", "Any snow (\u2265 0.1\") - 24 h odds",
     "Where the ensembles put ANY accumulating snowfall - the foot of the "
     "ladder, and usually the only rung with honest contours in October."),
    ("pw24ige1p00", "At least 1\" of snow - 24 h odds",
     "The measurable-snow cutoff: odds of enough to whiten grass and "
     "bridge decks."),
    ("pw24ige4p00", "At least 4\" of snow - 24 h odds",
     "Contour = chance >= 4\"; deeper blues = lower odds. The linework the "
     "deterministic WWD snowfall maps invite you to ignore."),
    ("pw24ige8p00", "At least 8\" of snow - 24 h odds",
     "The heavy-band odds the 8\"-plus runs hint at before any single "
     "deterministic map commits to them. Empty most of the year."),
)

# Center-of-Valley sample point (Knoxville): trails one tile edge exactly,
# good for a stable odds estimate out of the mostly transparent field.
Knoxville_LNLT = 35.9606, -83.9207

_CACHE = {"t": 0.0, "b": None}
_LK = threading.Lock()
_STAMP_RE = re.compile(r"window\.cycle\s*=\s*['\"](\d{10})['\"]")
_NAME_RE = re.compile(r"^(pw|pp)\d{2}i[a-z0-9]+_\d{10}\.png$")


def _tile_bounds(slot):
    """Leaflet-style [south, west, north, east] for one PNG tile slot."""
    t_r, t_c = slot
    return [
        _TILE_H[2 - t_r],                      # south
        _TILE_W[t_c],                          # west
        _TILE_H[1 - t_r],                      # north
        _TILE_W[t_c + 1],                      # east
    ]


def _latest_cycle():
    try:
        r = requests.get(f"{HOST}/current_cycle_co.js", headers=UA, timeout=20)
        m = _STAMP_RE.search(r.text)
        if m:
            return dt.datetime.strptime(m.group(1), "%Y%m%d%H").replace(
                tzinfo=dt.timezone.utc)
    except requests.RequestException:
        pass
    return None


def _tile_exists(slugs, cycle, fh):
    """True when the portal has posted this product's first tile."""
    try:
        url = (f"{HOST}/hourly-data/co/{cycle:%Y%m%d%H}/png/"
               f"ppp_ndfd_co_{slugs}_{cycle:%Y%m%d%H}f0{fh:02d}_tile_0_0.png")
        r = requests.head(url, headers=UA, timeout=20,
                          allow_redirects=True)
        if r.status_code == 405:            # some servers dislike HEAD
            r = requests.get(url, headers=UA, timeout=20, stream=True)
            r.close()
        return r.status_code == 200
    except requests.RequestException:
        return False


def _window_hours(cycle):
    """The 24-hour-window forecast hours the portal posted this cycle.

    The window grid rotates with the run (f024 on midday cycles, f022/f028
    ... on others - confirmed live 2026-10-20), so the hour is DISCOVERED
    against the base snow product, not assumed. Probed order starts at
    f024 and walks the observed rotation; the first hit wins for every
    pw24* product.
    """
    for fh in (24, 22, 28, 34, 40, 46, 18, 20, 30, 36, 42, 12, 48):
        if _tile_exists(PRODUCTS[0][0], cycle, fh):
            return fh
    return None


def _composite(slugs, cycle, fh=24):
    """Fetch + reassemble the 4 CONUS tiles of one product.

    Returns (dest_path or None, is_empty). Tile sizes vary by a pixel
    between rows (WPC's crop), so columns are stacked dynamically and the
    canvas clips any rollover. is_empty = tiles arrived but the field is
    fully transparent (no feature this run, expected most of the year).
    """
    from PIL import Image
    os.makedirs(OUT_DIR, exist_ok=True)
    pieces, sizes = {}, {}
    for row, col in ((0, 0), (0, 1), (1, 0), (1, 1)):
        url = (f"{HOST}/hourly-data/co/{cycle:%Y%m%d%H}/png/"
               f"ppp_ndfd_co_{slugs}_{cycle:%Y%m%d%H}f0{fh:02d}"
               f"_tile_{row}_{col}.png")
        try:
            r = requests.get(url, headers=UA, timeout=30)
            if not r.ok:
                return None, True                      # cycle not posted yet
            img = Image.open(io.BytesIO(r.content)).convert("RGBA")
            if img.size[0] > IM_SIZE[0] * 2 or img.size[0] < IM_SIZE[0] - 4:
                return None, True
            pieces[(row, col)] = img
            sizes[row] = img.size[1]
        except requests.RequestException:
            return None, True
    canvas = Image.new("RGBA", (IM_SIZE[0] * 2,
                                sum(sizes[r] for r in (0, 1))), (0, 0, 0, 0))
    y = 0
    for r in (0, 1):
        for c in (0, 1):
            canvas.paste(pieces[(r, c)], (c * IM_SIZE[0], y))
        y += sizes[r]
    empty = np.asarray(canvas)[..., 3].max() == 0
    if empty:
        return None, True
    dest = os.path.join(OUT_DIR, f"{slugs}_{cycle:%Y%m%d%H}.png")
    if os.path.isfile(dest) and os.path.getsize(dest) > 5_000:
        return dest, False
    tmp = dest + ".tmp"
    canvas.save(tmp, "PNG", optimize=True)
    os.replace(tmp, dest)
    return dest, False


def _prune(slug, cycle):
    """Keep the newest few cycles on disk per product slug.

    Depth 3: the gh-pages packager existence-checks every ppp.views ref
    when it runs (often minutes AFTER the next hourly build already
    pruned the prior cycle - 2026-10-23: views were silently emptied
    when the referenced composite was gone). A small buffer bridges
    that race; a 4-tile composite is ~300 KB so 3 cycles stay cheap.
    """
    depth = _KEEP_CYCLE_DEPTH      # cycles (~h) whose composites stay on disk
    try:
        for fn in os.listdir(OUT_DIR):
            m = re.match(rf"^{re.escape(slug)}_(\d{{10}})\.png$", fn)
            if not m:
                continue
            try:
                stamp = dt.datetime.strptime(m.group(1), "%Y%m%d%H").replace(
                    tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            if cycle - stamp >= dt.timedelta(hours=depth):
                try:
                    os.remove(os.path.join(OUT_DIR, fn))
                except OSError:
                    pass
    except OSError:
        pass


def _odds_sample(slug, cycle):
    """Odds-band probe at Knoxville from the tile's alpha + value coding.

    Filled fields encode the percent band in the image value channel
    (bigger = higher odds). Nothing drawn = None (no feature worth
    guessing at from one far-off pixel).
    """
    path = os.path.join(OUT_DIR, f"{slug}_{cycle:%Y%m%d%H}.png")
    if not os.path.exists(path):
        return None
    from PIL import Image
    cx = (Knoxville_LNLT[1] - _TILE_W[0]) / (_TILE_W[2] - _TILE_W[0]) * (IM_SIZE[0] * 2 - 1)
    cy = (_TILE_H[0] - Knoxville_LNLT[0]) / (_TILE_H[0] - _TILE_H[2]) * (IM_SIZE[1] * 2 - 1)
    try:
        px = Image.open(path).load()
        win = [px[int(cx) + dx, int(cy) + dy]
               for dx in (-2, 0, 2) for dy in (-2, 0, 2)]
        hits = [p for p in win if p[3] > 32]
        if not hits:
            return None
        med = int(np.median([max(p[0], p[1], p[2]) for p in hits]))
        pct = round(95.0 - med / 255.0 * 90.0)             # palette mapping
        return max(5, min(95, pct))
    except Exception:                                      # noqa: BLE001
        return None


def _views_from_disk(max_hours=4):
    """Rebuild views from composites already saved on disk.

    The portal retires cycle directories within hours of posting them, so
    once a composite exists locally the disk copy is the only archive. Used
    when neither the stamp cycle nor stamp-1h has posted tiles yet (the
    hourly stamp-vs-files lag seen 2026-10-22).
    """
    try:
        files = os.listdir(OUT_DIR)
    except OSError:
        return [], None
    best = {}
    for fn in files:
        m = re.match(r"^(pw24ige\d+p\d+)_(\d{10})\.png$", fn)
        if not m:
            continue
        slug, snap = m.groups()
        if slug not in best or snap > best[slug][0]:
            path = os.path.join(OUT_DIR, fn)
            if os.path.getsize(path) > 5_000:
                best[slug] = (snap, path)
    if not best:
        return [], None
    stamp = max(s for s, _ in best.values())
    cyc = dt.datetime.strptime(stamp, "%Y%m%d%H").replace(
        tzinfo=dt.timezone.utc)
    if dt.datetime.now(dt.timezone.utc) - cyc > dt.timedelta(hours=max_hours):
        return [], None
    views = []
    for slug, label, science in PRODUCTS:
        hit = best.get(slug)
        if not hit or hit[0] != stamp:
            continue
        views.append({
            "id": slug,
            "label": label,
            "science": science,
            "pngUrl": f"/app/static/winter/ppp/{os.path.basename(hit[1])}",
            "bounds": [(_TILE_H[2]), _TILE_W[0], _TILE_H[0], _TILE_W[2]],
            "odds": _odds_sample(slug, cyc),
        })
    return views, cyc


def build(max_age=1_800):
    """Payload: composites + bounds + summary odds (30-min inside-cache)."""
    with _LK:
        now = time.time()
        if _CACHE["b"] is not None and now - _CACHE["t"] < max_age:
            return _CACHE["b"]

    cycle = _latest_cycle()
    views, empty_seen, fh = [], 0, None
    # The stamp is an ADVANCE pointer: after each hourly rotation tiles for
    # the new cycle directory appear with a lag (observed ~1 h, 2026-10-22)
    # while the previous cycle's directory is still live. Probe the stamp
    # first, then fall back an hour; only then trust the poison-free disk
    # archive for the gap window.
    if cycle is not None:
        for cand in (cycle, cycle - dt.timedelta(hours=1)):
            fh = _window_hours(cand)
            if fh is not None:
                cycle = cand
                break
        if fh is None:
            cycle = None
    if cycle is not None and fh is not None:
        for slug, label, science in PRODUCTS:
            dest, empty = _composite(slug, cycle, fh=fh)
            _prune(slug, cycle)
            if dest:
                views.append({
                    "id": slug,
                    "label": label,
                    "science": science,
                    "pngUrl": f"/app/static/winter/ppp/{os.path.basename(dest)}",
                    "bounds": [(_TILE_H[2]), _TILE_W[0], _TILE_H[0], _TILE_W[2]],
                    "odds": _odds_sample(slug, cycle),
                })
            elif empty:
                empty_seen += 1
    disk_cycle = None
    if not views:
        views, disk_cycle = _views_from_disk()
    label_cycle = cycle or disk_cycle
    out = {
        "ok": bool(views),
        "cycle": _full(label_cycle) if label_cycle else None,
        "cycleEpochS": int(label_cycle.timestamp()) if label_cycle else None,
        "views": views,
        "empty": empty_seen == len(PRODUCTS) and not views,
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "source": "noaa wpc prob precip portal (no key, reassembled tiles)",
    }
    with _LK:
        _CACHE.update(t=time.time(), b=out)
    return out

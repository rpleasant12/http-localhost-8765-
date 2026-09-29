"""Severe-weather forecast MAPS from the HRRR: hail, tornado-rotation, combined.

The Severe page's forecast card shows numbers per hour; these are the same
HRRR fields drawn as geolocated PNG overlays so visitors can SEE where the
hail and rotation threats are forecast, hour by hour:

- hail:    HAIL (max expected hail diameter, mm) - SPC-style size classes
- tornado: UPHL (2-5 km updraft helicity, m2/s2) - the standard HRRR
           tornado proxy (130+ mesocyclone-strength, 250+ tornado threat)
- severe:  combined "storm severity chance" index: the max of normalized
           hail (mm/25) and rotation (UPHL/130), 0-1+ - where ANY significant
           severe threat is forecast, one honest picture

Each render decodes the newest HRRR cycle's HAIL+UPHL blobs (byte-range GRIB,
griblock-safe like every other decode here), paints threshold-classified
pixels onto a transparent PNG on the HRRR Lambert grid, and records the
frame's geographic bounds [south, west, north, east] for Leaflet
imageOverlay. One new PNG per forecast hour (F01..F08), cached on disk and
re-rendered only when the cycle advances. Quiet maps render honestly empty
(almost invisible) - no fake swirls.
"""
import datetime as dt
import json
import os
import threading
import time

import numpy as np
import requests

from data._tz import day_hm

OUT_DIR = os.path.join("static", "sevmaps")
REGISTRY = os.path.join(OUT_DIR, "registry.json")

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
HRRR_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"

HOURS = 8                 # F01..F08
MAX_PX = 1200             # downsample cap for the render grid
# any file older than KEEP_SEC and outside the current cycle's ids is pruned
KEEP_SEC = 900            # grace so in-flight data.json refs keep resolving

_cache = {"at": 0.0, "bundle": None}
_lock = threading.Lock()


# ------------------------------------------------------------ colormaps
def _hail_rgba(v):
    """SPC-style hail size classes -> RGBA (transparent below 19 mm/severe-ish)."""
    rgba = np.zeros(v.shape + (4,), dtype=np.uint8)
    bands = (
        (19, 25, (173, 255, 47)),     # small (pea)      greenyellow
        (25, 50, (255, 255, 0)),      # 1"               yellow
        (50, 75, (255, 140, 0)),      # 2" severe        darkorange
        (75, 100, (255, 0, 0)),       # 3"               red
        (100, 10_000, (255, 0, 255)), # 4"+ giant        magenta
    )
    for lo, hi, (r, g, b) in bands:
        m = (v >= lo) & (v < hi)
        if m.any():
            rgba[..., 0][m] = r
            rgba[..., 1][m] = g
            rgba[..., 2][m] = b
            rgba[..., 3][m] = 200
    return rgba


def _uphl_rgba(v):
    """Rotation categories (matching the site's UPHL classes) -> RGBA."""
    rgba = np.zeros(v.shape + (4,), dtype=np.uint8)
    bands = (
        (25, 75, (174, 213, 129)),    # weak rotation    light green
        (75, 130, (255, 213, 79)),    # rotation         amber
        (130, 250, (255, 159, 67)),   # strong / tor poss orange
        (250, 10_000, (224, 64, 251)),  # TORNADO THREAT purple
    )
    for lo, hi, (r, g, b) in bands:
        m = (v >= lo) & (v < hi)
        if m.any():
            rgba[..., 0][m] = r
            rgba[..., 1][m] = g
            rgba[..., 2][m] = b
            rgba[..., 3][m] = 205
    return rgba


def _sev_rgba(idx):
    """Combined severity chance 0..1+ -> warm ramp (transparent below 0.15)."""
    rgba = np.zeros(idx.shape + (4,), dtype=np.uint8)
    bands = (
        (0.15, 0.30, (174, 213, 129)),   # marginal
        (0.30, 0.50, (255, 213, 79)),    # slight
        (0.50, 0.75, (255, 140, 0)),     # enhanced
        (0.75, 1.00, (255, 0, 0)),       # moderate
        (1.00, 99.0, (255, 0, 255)),     # high
    )
    for lo, hi, (r, g, b) in bands:
        m = (idx >= lo) & (idx < hi)
        if m.any():
            rgba[..., 0][m] = r
            rgba[..., 1][m] = g
            rgba[..., 2][m] = b
            rgba[..., 3][m] = 190
    return rgba


KIND_FN = {"hail": _hail_rgba, "tornado": _uphl_rgba, "severe": _sev_rgba}
KIND_LABEL = {
    "hail": "HRRR hail forecast (max diameter)",
    "tornado": "HRRR rotation forecast (UPHL tornado proxy)",
    "severe": "Severe storm chance (hail + rotation combined)",
}


# ------------------------------------------------------------ grid + fetch
_GRID = {}


def _hrrr_grid_lonlat(step):
    """(lon2d, lat2d) for the HRRR CONUS 3-km grid at a downsample step."""
    if step in _GRID:
        return _GRID[step]
    from pyproj import Transformer

    nx, ny = 1799, 1059
    dx = dy = 3000.0
    R = 6371229.0
    proj4 = (
        f"+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 "
        f"+x_0=0 +y_0=0 +R={R} +units=m +no_defs"
    )
    tr = Transformer.from_crs(proj4, "EPSG:4326", always_xy=True)
    x = -2697500.0 + np.arange(0, nx, step) * dx
    y = -1587300.0 + np.arange(0, ny, step) * dy
    xx, yy = np.meshgrid(x, y)
    lon, lat = tr.transform(xx, yy)
    _GRID[step] = (lon, lat)
    return lon, lat


def _find_range(idx_text, short, level_sub=None):
    """Byte range of the named message in a GRIB .idx (same rule as severe.py)."""
    lines = idx_text.splitlines()
    for i, line in enumerate(lines):
        fields = line.split(":")
        if len(fields) > 4 and fields[3] == short:
            if level_sub and level_sub.lower() not in ":".join(fields[4:]).lower():
                continue
            start = int(float(fields[1]))
            end = start + 4_000_000
            if i + 1 < len(lines):
                try:
                    nxt = int(float(lines[i + 1].split(":")[1]))
                    if nxt > start:
                        end = nxt
                except ValueError:
                    pass
            return start, end
    return None


def _latest_cycle():
    """Newest HRRR cycle with a live .idx (walks back up to 6 h)."""
    now = dt.datetime.now(dt.timezone.utc)
    for back in range(0, 6):
        c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        try:
            probe = requests.head(
                f"{HRRR_BUCKET}/hrrr.{c:%Y%m%d}/conus/hrrr.t{c:%H}z.wrfsfcf01.grib2.idx",
                headers=UA, timeout=12,
            )
            if probe.status_code == 200:
                return c
        except requests.RequestException:
            continue
    return None


def _fetch_grid(url, rng):
    """Byte-range fetch + decode one GRIB blob -> (values, lon, lat) on the
    downsampled HRRR grid, or None."""
    from data.models import _decode_blob

    try:
        r = requests.get(url, headers={**UA, "Range": f"bytes={rng[0]}-{rng[1] - 1}"},
                         timeout=60)
        if not r.ok:
            return None
        step = 3                                   # ~9 km render pixels
        values, glat, glon = _decode_blob(r.content, level_sub="entire atmosphere")
        values = values[::step, ::step]
        glat, glon = glat[::step, ::step], glon[::step, ::step]
        if glon.max() > 180:
            glon = np.where(glon > 180, glon - 360, glon)
        return values, glon, glat
    except Exception:                              # noqa: BLE001
        return None


# ------------------------------------------------------------ render
def _render_frame(kind, cycle, fh, url, idx_text, grid):
    """Render one (kind, forecast-hour) PNG. Returns the frame dict or None."""
    hail_rng = _find_range(idx_text, "HAIL", "entire atmosphere") or _find_range(idx_text, "HAIL")
    # HRRR publishes the hourly max as MXUPHL (UPHL never matches as an exact name)
    uphl_rng = (_find_range(idx_text, "MXUPHL") or _find_range(idx_text, "UPHL", "entire atmosphere")
                or _find_range(idx_text, "UPHL"))

    hail, uphl = None, None
    if kind in ("hail", "severe") and hail_rng:
        got = _fetch_grid(url, hail_rng)
        if got:
            hail = got[0]
            grid.update(lon=got[1], lat=got[2])
    if kind in ("tornado", "severe") and uphl_rng:
        got = _fetch_grid(url, uphl_rng)
        if got:
            uphl = got[0]
            grid.update(lon=got[1], lat=got[2])
    if kind == "severe" and hail is None and uphl is None:
        # combined index needs at least ONE field this hour
        return None
    if kind == "hail" and hail is None:
        return None
    if kind == "tornado" and uphl is None:
        return None

    if kind == "hail":
        rgba = KIND_FN[kind](hail)
    elif kind == "tornado":
        rgba = KIND_FN[kind](uphl)
    else:
        h_ok, u_ok = hail is not None, uphl is not None
        if not (h_ok or u_ok):
            return None
        if h_ok and u_ok:
            idx = np.maximum(np.nan_to_num(hail, nan=0.0) / 25.0,
                             np.nan_to_num(uphl, nan=0.0) / 130.0)
        else:
            # one field undecodable this hour: use the available one alone
            idx = (np.nan_to_num(hail, nan=0.0) / 25.0 if h_ok
                   else np.nan_to_num(uphl, nan=0.0) / 130.0)
        rgba = KIND_FN[kind](idx)

    if not rgba[..., 3].any():
        # quiet hour: keep a (near-empty) file anyway so the layer timeline is
        # continuous and the payload has frames - honest, nothing painted
        pass

    os.makedirs(OUT_DIR, exist_ok=True)
    fid = f"sev_{kind}_{cycle:%Y%m%d%H}_f{fh:02d}"
    png = os.path.join(OUT_DIR, fid + ".png")
    from PIL import Image
    Image.fromarray(rgba, "RGBA").save(png, "PNG", optimize=True)

    lon, lat = grid["lon"], grid["lat"]
    valid = cycle + dt.timedelta(hours=fh)
    return {
        "kind": kind, "id": fid, "hour": fh,
        "time": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": f"F{fh:02d} · {day_hm(valid)} ET",
        "pngUrl": f"/app/static/sevmaps/{fid}.png",
        "bounds": [round(float(lat.min()), 3), round(float(lon.min()), 3),
                   round(float(lat.max()), 3), round(float(lon.max()), 3)],
    }


def render_sevmaps(max_age_min=90):
    """Render the severe forecast maps for the newest cycle.

    Returns {'ok', 'cycle', 'frames': {'hail': [...], 'tornado': [...],
    'severe': [...]}} - or {'ok': False, 'reason': ...}. Frames are cached on
    disk per (kind, cycle, hour); only a new cycle triggers re-download.
    """
    with _lock:
        now = time.time()
        c = _cache.get("bundle")
        if c and c.get("ok") and now - _cache["at"] < 600:
            return c
        try:
            return _render_all()
        except Exception as exc:                   # noqa: BLE001
            return {"ok": False, "reason": str(exc)[:120]}


def _render_all():
    cycle = _latest_cycle()
    if cycle is None:
        return {"ok": False, "reason": "HRRR unavailable"}

    # disk cache: skip hours whose PNG for this cycle already exists
    try:
        have = set(os.listdir(OUT_DIR))
    except OSError:
        have = set()

    out = {"hail": [], "tornado": [], "severe": []}
    grid = {}
    base = f"{HRRR_BUCKET}/hrrr.{cycle:%Y%m%d}/conus/hrrr.t{cycle:%H}z.wrfsfcf"
    for fh in range(1, HOURS + 1):
        url = f"{base}{fh:02d}.grib2"
        try:
            idx = requests.get(f"{url}.idx", headers=UA, timeout=15)
            if not idx.ok:
                continue
            idx_text = idx.text
        except requests.RequestException:
            continue
        for kind in out:
            fid = f"sev_{kind}_{cycle:%Y%m%d%H}_f{fh:02d}"
            if fid + ".png" in have:
                valid = cycle + dt.timedelta(hours=fh)
                # reuse cached PNG; bounds are cycle-constant
                out[kind].append({
                    "kind": kind, "id": fid, "hour": fh,
                    "time": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "label": f"F{fh:02d} · {day_hm(valid)} ET",
                    "pngUrl": f"/app/static/sevmaps/{fid}.png",
                    "bounds": None,          # filled after the grid pass
                })
                continue
            fr = _render_frame(kind, cycle, fh, url, idx_text, grid)
            if fr:
                out[kind].append(fr)

    # bounds are identical for every frame (one grid) - patch cached entries
    lon, lat = _hrrr_grid_lonlat(3)
    bounds = [round(float(lat.min()), 3), round(float(lon.min()), 3),
              round(float(lat.max()), 3), round(float(lon.max()), 3)]
    for kind in out:
        for f in out[kind]:
            if f.get("bounds") is None:
                f["bounds"] = bounds
            if "ET" not in f["label"]:
                valid = cycle + dt.timedelta(hours=f["hour"])
                f["label"] = f"F{f['hour']:02d} \u00b7 {day_hm(valid)} ET"

    total = sum(len(v) for v in out.values())
    if not total:
        return {"ok": False, "reason": "no HAIL/UPHL frames decoded"}

    from data._tz import full
    result = {"ok": True, "cycle": full(cycle),
              "generated": time.strftime("%Y-%m-%d %H:%M"), "frames": out}

    # prune stale-cycle PNGs (with the usual in-flight grace window)
    keep = {f"{k}_{cycle:%Y%m%d%H}" for k in out}
    now_e = time.time()
    try:
        for fn in os.listdir(OUT_DIR):
            if not fn.endswith(".png"):
                continue
            parts = fn.split("_")
            if len(parts) >= 4 and "_".join(parts[1:3]) in keep:
                continue
            p = os.path.join(OUT_DIR, fn)
            try:
                if now_e - os.stat(p).st_mtime < KEEP_SEC:
                    continue
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass

    _cache["bundle"] = result
    _cache["at"] = time.time()
    return result


def sevmaps_bundle():
    """Public entry for the site payload: {'ok','cycle','frames'{kind:[...]}}."""
    return render_sevmaps()

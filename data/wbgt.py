"""WBGT (wet-bulb globe temperature) heat map for the site.

Source: NWS gridpoint API `wetBulbGlobeTemperature` - the official NDFD-derived
WBGT forecast (outdoor, sun-exposed estimate), hourly out to ~5 days. Keyless.

Rendering: sample the WBGT forecast at a lattice of grid points around East TN
(one api.weather.gov call each, cached ~30 min), IDW-interpolate onto the shared
HRRR Lambert grid (same geometry/bounds as the winter + sevmaps overlays), and
paint NWS heat-risk-style categories:

    < 82 F      - clear (not painted)
    82-85 F     - caution (yellow)          extreme caution 85-88 (orange)
    88-90 F     - strong caution (red-orange)
    90-93 F     - danger (red)              93 F+ - extreme danger (magenta)

(Flags use F conversions of the standard ^C thresholds: 82/88/90 for green/
yellow/red flags; bands above split the caution span for map readability.)
"""
import datetime as dt
import json
import os
import re
import threading
import time

import numpy as np
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0"}
OUT_DIR = os.path.join("static", "wbgt")
_TTL = 1800          # NWS gridpoint cache: 30 min (forecast refreshes hourly)
_TTL_US = 3600       # national lattice is 4x the calls - refresh hourly
HOURS = (0, 1, 2, 3, 6, 12, 24)   # 0 = right now (markers readable even when unpainted)

_lock = threading.Lock()
_cache = {"at": 0.0, "bundle": None}
_LEGEND = [["#ffe066", "caution 82-85°F"], ["#ffb242", "extreme caution 85-88°F"],
           ["#ff783c", "88-90°F"], ["#eb3c3c", "danger 90-93°F"],
           ["#c828c8", "extreme danger 93°F+"]]

# East TN + surroundings sample lattice (lat, lon): covers the map viewport
# densely where people live, coarser at the edges.
_LATTICE = [
    (36.6, -83.2), (36.6, -83.9), (36.6, -84.6),
    (36.3, -82.4), (36.3, -83.2), (36.3, -83.9), (36.3, -84.6), (36.3, -85.3),
    (36.0, -82.0), (36.0, -82.8), (36.0, -83.6), (36.0, -84.3), (36.0, -85.0), (36.0, -85.7),
    (35.7, -82.4), (35.7, -83.2), (35.7, -83.9), (35.7, -84.6), (35.7, -85.3),
    (35.4, -82.8), (35.4, -83.6), (35.4, -84.3), (35.4, -85.0),
    (35.0, -82.8), (35.0, -83.6), (35.0, -84.3), (35.0, -85.0), (35.0, -85.6),
    (34.6, -84.0), (34.6, -84.8), (34.6, -85.5),
]

# National lattice for the US map: 1 deg x 2 deg over lat 25-49 /
# lon -124 to -66 (~750 points) + chunked IDW gives sharp state-level
# heat-risk detail; the hourly refresh absorbs the ~750 API calls.
_LATTICE_US = [(float(la), float(lo))
               for la in range(25, 50, 1)
               for lo in range(-124, -65, 2)]


def _c_to_f(c):
    return c * 9.0 / 5.0 + 32.0


def _wbgt_series(lat, lon):
    """NWS WBGT forecast for one point -> [(valid_dt, wbgt_F), ...]."""
    try:
        p = requests.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
                         headers=UA, timeout=15)
        p.raise_for_status()
        pr = p.json()["properties"]
        grid = f"{pr['gridId']}/{pr['gridX']},{pr['gridY']}"
        r = requests.get(f"https://api.weather.gov/gridpoints/{grid}", headers=UA, timeout=20)
        r.raise_for_status()
        wb = (r.json().get("properties") or {}).get("wetBulbGlobeTemperature") or {}
        out = []
        for v in wb.get("values") or []:
            raw = v.get("validTime") or ""
            val = v.get("value")
            if val is None:
                continue
            m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", raw)
            if not m:
                continue
            start = dt.datetime.fromisoformat(m.group(1)).replace(tzinfo=dt.timezone.utc)
            out.append((start, _c_to_f(float(val))))
        return out
    except Exception:                                  # noqa: BLE001
        return []


def _rgba(v):
    """WBGT (F) grid -> RGBA overlay; unpainted where below 82 F or no data."""
    rgba = np.zeros(v.shape + (4,), dtype=np.uint8)
    bands = (
        (82.0, 85.0, (255, 224, 102)),   # caution - yellow
        (85.0, 88.0, (255, 178, 66)),    # extreme caution - orange
        (88.0, 90.0, (255, 120, 60)),    # strong caution - red-orange
        (90.0, 93.0, (235, 60, 60)),     # danger - red
        (93.0, 999.0, (200, 40, 200)),   # extreme danger - magenta
    )
    for lo, hi, (r, g, b) in bands:
        m = (v >= lo) & (v < hi)
        if m.any():
            rgba[..., 0][m] = r
            rgba[..., 1][m] = g
            rgba[..., 2][m] = b
            rgba[..., 3][m] = 205
    return rgba


_GRID = {}


def _hrrr_grid_lonlat(step=3):
    if step in _GRID:
        return _GRID[step]
    from pyproj import Transformer
    nx, ny = 1799, 1059
    dx = dy = 3000.0
    R = 6371229.0
    proj4 = (f"+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 "
             f"+x_0=0 +y_0=0 +R={R} +units=m +no_defs")
    tr = Transformer.from_crs(proj4, "EPSG:4326", always_xy=True)
    x = -2697500.0 + np.arange(0, nx, step) * dx
    y = -1587300.0 + np.arange(0, ny, step) * dy
    xx, yy = np.meshgrid(x, y)
    lon, lat = tr.transform(xx, yy)
    _GRID[step] = (lon, lat)
    return lon, lat


def _render_frames(lattice=None, win=(33.8, 37.4, -86.6, -81.4), suffix=""):
    """Fetch all lattice series once, render one PNG per HOURS window.

    lattice=None -> the East TN lattice; win is the (lat_lo, lat_hi, lon_lo,
    lon_hi) crop of the shared HRRR grid; suffix distinguishes output ids
    ("" for East TN, "us" for the national set).
    """
    from concurrent.futures import ThreadPoolExecutor
    lattice = _LATTICE if lattice is None else lattice
    series = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_wbgt_series, la, lo): (la, lo) for la, lo in lattice}
        for fut, (la, lo) in futs.items():
            got = fut.result()
            if got:
                series[(la, lo)] = got
    min_pts = {"us": 250}.get(suffix, 30 if len(lattice) > 50 else 6)
    if len(series) < min_pts:
        return None
    pts = [(la, lo) for (la, lo) in series]
    lon, lat = _hrrr_grid_lonlat(3)

    # crop the full CONUS grid to the target window (fast + small PNGs)
    lat_lo, lat_hi, lon_lo, lon_hi = win
    win = ((lat > lat_lo) & (lat < lat_hi)) & ((lon > lon_lo) & (lon < lon_hi))
    ys, xs = np.where(win)
    if len(ys) < 10:
        return None
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub_lon, sub_lat = lon[y0:y1, x0:x1], lat[y0:y1, x0:x1]

    out = {}
    city_vals = {}                     # fh -> [(lat, lon, wbgt_F), ...] for map markers
    for fh in HOURS:
        vals = []
        for pt in pts:
            d = {t: w for t, w in series[pt]}
            hit = min((t for t in d if t >= _target(fh)), default=None)
            vals.append(d.get(hit) if hit is not None else None)
        good = [(p, v) for p, v in zip(pts, vals) if v is not None]
        if len(good) < 6:
            continue
        city_vals[fh] = [(round(p[0], 3), round(p[1], 3), round(v, 1)) for p, v in good]
        px = np.array([p[1] for p, _ in good])
        py = np.array([p[0] for p, _ in good])
        pv = np.array([v for _, v in good])
        # IDW onto the sub-grid, chunked over cells so big lattices
        # never build a grid_x_points-sized matrix in one go
        gx, gy = sub_lon.ravel(), sub_lat.ravel()
        grid_v = np.empty(gx.shape)
        CH = 20000
        for i in range(0, gx.size, CH):
            sx = gx[i:i + CH, None] - px[None, :]
            sy = gy[i:i + CH, None] - py[None, :]
            w = 1.0 / np.maximum(sx * sx + sy * sy, 1e-6) ** 1.5
            grid_v[i:i + CH] = (w * pv[None, :]).sum(axis=1) / w.sum(axis=1)
        grid_v = grid_v.reshape(sub_lon.shape)
        fid = f"wbgt_f{fh:02d}{suffix}"
        os.makedirs(OUT_DIR, exist_ok=True)
        from PIL import Image
        Image.fromarray(_rgba(grid_v), "RGBA").save(
            os.path.join(OUT_DIR, fid + ".png"), "PNG", optimize=True)
        out[fh] = (fid, grid_v)
    bounds = [round(float(sub_lat.min()), 3), round(float(sub_lon.min()), 3),
              round(float(sub_lat.max()), 3), round(float(sub_lon.max()), 3)]
    return bounds, out, city_vals


_cache_us = {"at": 0.0, "bundle": None, "loading": False}


def wbgt_bundle_us():
    """National WBGT bundle (hourly refresh, non-blocking when cold)."""
    with _lock:
        now = time.time()
        if _cache_us["bundle"] is not None and now - _cache_us["at"] < _TTL_US:
            return _cache_us["bundle"]
        if _cache_us["loading"]:
            return (_cache_us["bundle"]
                    or {"ok": False, "frames": [],
                        "legend": _LEGEND,
                        "note": "Rendering the national map - ready within a few minutes."})
        _cache_us["loading"] = True
    thread = threading.Thread(target=_us_worker, daemon=True, name="wbgt-us")
    thread.start()
    return (_cache_us["bundle"]
            or {"ok": False, "frames": [], "legend": _LEGEND,
                "note": "Rendering the national map - ready within a few minutes."})


def _us_worker():
    try:
        rendered = _render_frames(lattice=_LATTICE_US,
                                  win=(24.0, 50.0, -125.5, -66.0), suffix="us")
        if rendered:
            bounds, out, city_vals = rendered
            from data._tz import day_hm
            frames = []
            for fh in sorted(out):
                fid, _ = out[fh]
                valid = _target(fh)
                frames.append({
                    "hour": fh, "id": fid,
                    "label": ("Right now" if fh == 0 else f"+{fh} h") + f" · {day_hm(valid)} ET",
                    "pngUrl": f"/app/static/wbgt/{fid}.png",
                    "bounds": bounds,
                    "vals": city_vals.get(fh) or [],
                })
            bundle = {"ok": bool(frames), "frames": frames, "legend": _LEGEND}
        else:
            bundle = {"ok": False, "frames": [], "legend": _LEGEND,
                      "note": "National WBGT data unavailable right now."}
    except Exception:                                      # noqa: BLE001
        bundle = {"ok": False, "frames": [], "legend": _LEGEND,
                  "note": "National WBGT data unavailable right now."}
    with _lock:
        _cache_us["bundle"] = bundle
        _cache_us["at"] = time.time()
        _cache_us["loading"] = False


def _target(fh):
    return dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0) \
        + dt.timedelta(hours=fh)


def _prune_old():
    try:
        keep = {"wbgt_f%02d.png" % fh for fh in HOURS}
        keep |= {"wbgt_f%02dus.png" % fh for fh in HOURS}
        for fn in os.listdir(OUT_DIR):
            if fn.endswith(".png") and fn not in keep:
                try:
                    os.remove(os.path.join(OUT_DIR, fn))
                except OSError:
                    pass
    except OSError:
        pass


def _city_risk(series_map):
    """Peak WBGT in the next 24 h per sampled point (for the page table)."""
    rows = []
    now = dt.datetime.now(dt.timezone.utc)
    for (la, lo), ser in series_map.items():
        nxt = [(t, w) for t, w in ser if now <= t <= now + dt.timedelta(hours=24)]
        if not nxt:
            continue
        t_peak, w_peak = max(nxt, key=lambda x: x[1])
        rows.append({"lat": la, "lon": lo, "peakF": round(w_peak),
                     "peakTime": t_peak})
    return rows


def _cat(wf):
    if wf >= 93:
        return "extreme danger", "#c828c8"
    if wf >= 90:
        return "danger", "#eb3c3c"
    if wf >= 88:
        return "strong caution", "#ff783c"
    if wf >= 85:
        return "extreme caution", "#ffb242"
    if wf >= 82:
        return "caution", "#ffe066"
    return "low", "#81c784"


def wbgt_bundle():
    """Payload for the forecast page: frames + city/point risks (30-min cache)."""
    with _lock:
        now = time.time()
        if _cache["bundle"] is not None and now - _cache["at"] < _TTL:
            return _cache["bundle"]
        rendered = None
        try:
            rendered = _render_frames()
        except Exception:                          # noqa: BLE001
            rendered = None
        bundle = {"ok": False, "frames": [], "legend": _LEGEND}
        if rendered:
            _prune_old()
            bounds, out, city_vals = rendered
            from data._tz import day_hm
            frames = []
            for fh in sorted(out):
                fid, _ = out[fh]
                valid = _target(fh)
                frames.append({
                    "hour": fh, "id": fid,
                    "label": ("Right now" if fh == 0 else f"+{fh} h") + f" · {day_hm(valid)} ET",
                    "pngUrl": f"/app/static/wbgt/{fid}.png",
                    "bounds": bounds,
                    "vals": city_vals.get(fh) or [],
                })
            bundle["ok"] = bool(frames)
            bundle["frames"] = frames
        _cache["bundle"] = bundle
        _cache["at"] = time.time()
        return bundle


_city_cache = {"at": 0.0, "data": {}}


def _et_tomorrow_window():
    """Tomorrow's ET calendar day as a (start_utc, end_utc) pair."""
    from data._tz import ET
    d0 = (dt.datetime.now(ET) + dt.timedelta(days=1)).date()
    start = dt.datetime(d0.year, d0.month, d0.day, tzinfo=ET).astimezone(dt.timezone.utc)
    return start, start + dt.timedelta(hours=24)


def city_wbgt_tomorrow(cities=None, max_age=1800):
    """Tomorrow's peak WBGT per East TN city (30-min cache).

    Returns {city: {peakF, peakTime (ET label), cat, color}} over the
    tomorrow ET calendar window - feeds the forecast cards' heat flag.
    """
    from data.observations import EAST_TN_CITIES
    cities = cities or EAST_TN_CITIES
    now = time.time()
    if _city_cache["data"] and now - _city_cache["at"] < max_age:
        return _city_cache["data"]
    start, end = _et_tomorrow_window()

    def work(kv):
        city, (la, lo) = kv
        ser = _wbgt_series(la, lo)
        day = [(t, w) for t, w in ser if start <= t < end]
        if not day:
            return city, None
        pt, peak = max(day, key=lambda x: x[1])
        cat, color = _cat(peak)
        from data._tz import day_hm
        return city, {"peakF": round(peak), "peakTime": day_hm(pt),
                      "cat": cat, "color": color}

    from concurrent.futures import ThreadPoolExecutor
    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for city, res in ex.map(work, cities.items()):
            if res:
                out[city] = res
    if out:
        _city_cache.update(at=now, data=out)
    return out

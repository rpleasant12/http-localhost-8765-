"""Winter-weather service: snow/ice forecast maps + alerts + national outlooks.

Three keyless sources, all verified live (2026-09-10):

1. HRRR winter fields from NOAA's AWS bucket (byte-range GRIB, griblock-safe):
   ASNOW  accumulated snowfall (in, 0-6/0-12/0-18 h windows, F01..F18)
   FROZR  accumulated freezing rain (in, same windows)
   Rendered as threshold-classified transparent PNG overlays on the HRRR
   Lambert grid (same pattern as data/sevmaps.py). September runs are honestly
   empty; the machinery lights up the moment cold air arrives.

2. NWS active winter alerts (api.weather.gov, event-filtered, CONUS-wide
   count + Tennessee rows for the page list).

3. WPC Winter Weather Desk probability graphics (Day 1-3 snow >4/8/12 in,
   ice >0.25 in, day composites) - mirrored to static/winter/wpc/ so the
   public site serves them without hotlinking.
"""
import datetime as dt
import json
import os
import re
import threading
import time

import numpy as np
import requests

from data._tz import full

OUT_DIR = os.path.join("static", "winter")
WPC_DIR = os.path.join(OUT_DIR, "wpc")

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
HRRR_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
WPC_BASE = "https://www.wpc.ncep.noaa.gov/wwd/"

HOURS = (6, 12, 18)       # HRRR accumulation windows available for ASNOW/FROZR
MAX_PX = 1200
KEEP_SEC = 900

_cache = {"at": 0.0, "bundle": None}
_lock = threading.Lock()


# ------------------------------------------------------------ colormaps
def _snow_rgba(v):
    """Snow accumulation (inches) -> RGBA; NWS-style ramp, transparent below 0.1."""
    rgba = np.zeros(v.shape + (4,), dtype=np.uint8)
    bands = (
        (0.1, 1.0, (173, 216, 230)),   # light blue
        (1.0, 2.0, (120, 190, 235)),
        (2.0, 4.0, (80, 150, 230)),
        (4.0, 6.0, (60, 110, 215)),
        (6.0, 12.0, (120, 60, 200)),   # deep purple
        (12.0, 999.0, (230, 60, 230)), # extreme magenta
    )
    for lo, hi, (r, g, b) in bands:
        m = (v >= lo) & (v < hi)
        if m.any():
            rgba[..., 0][m] = r
            rgba[..., 1][m] = g
            rgba[..., 2][m] = b
            rgba[..., 3][m] = 205
    return rgba


def _ice_rgba(v):
    """Freezing-rain accumulation (inches) -> RGBA; ice = pink/red danger ramp."""
    rgba = np.zeros(v.shape + (4,), dtype=np.uint8)
    bands = (
        (0.02, 0.10, (255, 190, 203)),  # glaze beginning
        (0.10, 0.25, (255, 120, 150)),  # disruptive
        (0.25, 0.50, (255, 60, 100)),   # damaging
        (0.50, 999.0, (190, 20, 60)),   # extreme / power outages
    )
    for lo, hi, (r, g, b) in bands:
        m = (v >= lo) & (v < hi)
        if m.any():
            rgba[..., 0][m] = r
            rgba[..., 1][m] = g
            rgba[..., 2][m] = b
            rgba[..., 3][m] = 205
    return rgba


KIND_FN = {"snow": _snow_rgba, "ice": _ice_rgba}
KIND_FIELD = {"snow": "ASNOW", "ice": "FROZR"}


# ------------------------------------------------------------ grid + decode
_GRID = {}


def _hrrr_grid_lonlat(step):
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
    """Newest cycle that has ALL accumulation windows (probe F18, not F01:
    a cycle younger than ~1 h is still publishing its later forecast files)."""
    now = dt.datetime.now(dt.timezone.utc)
    for back in range(0, 6):
        c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        try:
            probe = requests.head(
                f"{HRRR_BUCKET}/hrrr.{c:%Y%m%d}/conus/hrrr.t{c:%H}z.wrfsfcf18.grib2.idx",
                headers=UA, timeout=12,
            )
            if probe.status_code == 200:
                return c
        except requests.RequestException:
            continue
    return None


def _fetch_grid(url, rng):
    from data.models import _decode_blob

    try:
        r = requests.get(url, headers={**UA, "Range": f"bytes={rng[0]}-{rng[1] - 1}"},
                         timeout=60)
        if not r.ok:
            return None
        step = 3
        values, glat, glon = _decode_blob(r.content, level_sub="entire atmosphere",
                                          want_short=None)
        values = values[::step, ::step]
        glat, glon = glat[::step, ::step], glon[::step, ::step]
        if glon.max() > 180:
            glon = np.where(glon > 180, glon - 360, glon)
        return values, glon, glat
    except Exception:                              # noqa: BLE001
        return None


# ------------------------------------------------------------ HRRR maps
def _render_hrrr(cycle):
    """Render snow + ice overlay frames for one cycle -> {kind: [frame dicts]}."""
    out = {"snow": [], "ice": []}
    grid = {}
    base = f"{HRRR_BUCKET}/hrrr.{cycle:%Y%m%d}/conus/hrrr.t{cycle:%H}z.wrfsfcf"
    try:
        have = set(os.listdir(OUT_DIR))
    except OSError:
        have = set()

    for fh in HOURS:
        url = f"{base}{fh:02d}.grib2"
        try:
            idx = requests.get(f"{url}.idx", headers=UA, timeout=15)
            if not idx.ok:
                continue
            idx_text = idx.text
        except requests.RequestException:
            continue
        for kind in out:
            field = KIND_FIELD[kind]
            # HRRR accumulations: prefer the "0-N hour acc" window message
            rng = (_find_range(idx_text, field, f"0-{fh} hour acc")
                   or _find_range(idx_text, field, "acc"))
            if rng is None:
                continue
            fid = f"wnt_{kind}_{cycle:%Y%m%d%H}_f{fh:02d}"
            if fid + ".png" not in have:
                got = _fetch_grid(url, rng)
                if not got:
                    continue
                values, lon, lat = got
                grid.update(lon=lon, lat=lat)
                rgba = KIND_FN[kind](values)
                os.makedirs(OUT_DIR, exist_ok=True)
                from PIL import Image
                Image.fromarray(rgba, "RGBA").save(
                    os.path.join(OUT_DIR, fid + ".png"), "PNG", optimize=True)
            valid = cycle + dt.timedelta(hours=fh)
            from data._tz import day_hm
            out[kind].append({
                "kind": kind, "id": fid, "hour": fh,
                "time": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "label": f"{fh} h accumulation · {day_hm(valid)} ET",
                "pngUrl": f"/app/static/winter/{fid}.png",
                "bounds": None,
            })

    lon, lat = _hrrr_grid_lonlat(3)
    bounds = [round(float(lat.min()), 3), round(float(lon.min()), 3),
              round(float(lat.max()), 3), round(float(lon.max()), 3)]
    for kind in out:
        for f in out[kind]:
            if f.get("bounds") is None:
                f["bounds"] = bounds
    return out


def _prune_old(cycle):
    keep = {f"{cycle:%Y%m%d%H}"}
    now_e = time.time()
    try:
        for fn in os.listdir(OUT_DIR):
            if not fn.endswith(".png"):
                continue
            m = re.match(r"wnt_\w+_(\d{10})_f\d+\.png$", fn)
            if not m or m.group(1) in keep:
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


# ------------------------------------------------------------ alerts
WINTER_EVENTS = (
    "Winter Weather Advisory", "Winter Storm Warning", "Winter Storm Watch",
    "Ice Storm Warning", "Blizzard Warning", "Blizzard Watch",
    "Freeze Warning", "Frost Advisory", "Lake Effect Snow Warning",
    "Lake Effect Snow Advisory", "Snow Squall Warning", "Extreme Cold Warning",
    "Cold Weather Advisory", "Wind Chill Warning", "Wind Chill Advisory",
)


def _iso_to_et(iso):
    """ISO UTC string -> '2026-09-11 02:00 ET' display (safe fallback)."""
    try:
        w = dt.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=dt.timezone.utc)
        from data._tz import iso_local
        return iso_local(w)
    except (ValueError, TypeError):
        return iso or ""


def winter_alerts():
    """Active US winter alerts: CONUS count + Tennessee rows (page list)."""
    ev = "&event=".join(requests.utils.quote(e) for e in WINTER_EVENTS)
    try:
        # NOTE: api.weather.gov rejects `limit` with 400 - never send it
        r = requests.get(
            f"https://api.weather.gov/alerts/active?status=actual&message_type=alert&event={ev}",
            headers={**UA, "Accept": "application/geo+json"}, timeout=20,
        )
        r.raise_for_status()
        feats = r.json().get("features", [])
    except Exception:                              # noqa: BLE001
        return {"ok": False, "usCount": 0, "tn": []}
    tn = []
    for f in feats:
        p = f.get("properties", {}) or {}
        area = p.get("areaDesc") or ""
        if re.search(r"\bTN\b", area):
            tn.append({
                "event": p.get("event"),
                "area": area,
                "headline": p.get("headline"),
                "expires": _iso_to_et(p.get("expires")),
            })
    tn.sort(key=lambda a: a.get("event") or "")
    return {"ok": True, "usCount": len(feats), "tn": tn[:12]}


# ------------------------------------------------------------ WPC mirror
def _mirror_wpc():
    """Mirror the live WPC Winter Weather Desk graphics we link on the page."""
    products = (
        ("day1_psnow_gt_01_conus.gif", "Day 1 snow > 1 in"),
        ("day1_psnow_gt_04.gif", "Day 1 snow > 4 in"),
        ("day1_psnow_gt_08.gif", "Day 1 snow > 8 in"),
        ("day1_psnow_gt_12.gif", "Day 1 snow > 12 in"),
        ("day1_pice_gt_25.gif", "Day 1 ice > 0.25 in"),
        ("day1_composite.gif", "Day 1 winter composite"),
        ("day2_psnow_gt_01_conus.gif", "Day 2 snow > 1 in"),
        ("day2_psnow_gt_04.gif", "Day 2 snow > 4 in"),
        ("day2_psnow_gt_08.gif", "Day 2 snow > 8 in"),
        ("day2_pice_gt_25.gif", "Day 2 ice > 0.25 in"),
        ("day2_composite.gif", "Day 2 winter composite"),
        ("day3_psnow_gt_01_conus.gif", "Day 3 snow > 1 in"),
        ("day3_psnow_gt_04.gif", "Day 3 snow > 4 in"),
        ("day3_psnow_gt_08.gif", "Day 3 snow > 8 in"),
        ("day3_composite.gif", "Day 3 winter composite"),
        ("lowtrack_public.gif", "Winter storm low tracks"),
    )
    os.makedirs(WPC_DIR, exist_ok=True)
    out = []
    for fn, label in products:
        dest = os.path.join(WPC_DIR, fn)
        try:
            if not (os.path.exists(dest) and os.path.getsize(dest) > 5_000
                    and time.time() - os.stat(dest).st_mtime < 3600):
                r = requests.get(WPC_BASE + fn, headers=UA, timeout=25)
                if r.ok and r.content[:3] in (b"GIF", b"\xff\xd8\xff", b"\x89PN"):
                    tmp = dest + ".part"
                    with open(tmp, "wb") as f:
                        f.write(r.content)
                    os.replace(tmp, dest)
            if os.path.exists(dest) and os.path.getsize(dest) > 5_000:
                # app-absolute url: the Pages packager rewrites these and copies
                # the file (relative HTML srcs would never be discovered)
                out.append({"file": fn, "label": label,
                            "url": f"/app/static/winter/wpc/{fn}"})
        except Exception:                          # noqa: BLE001
            continue
    return out


# ------------------------------------------------------------ CPC outlooks
def _mirror_cpc():
    """CPC 6-10 day and 8-14 day temperature/precipitation outlooks.

    The standard winter-weather extended-range guidance (weeks 1-2): below-
    normal temperature + wet signal = the pattern that produces snow in the
    Tennessee Valley. Mirrored to static/winter/cpc/ so the public site
    serves them without hotlinking.
    """
    cpc_dir = os.path.join(OUT_DIR, "cpc")
    os.makedirs(cpc_dir, exist_ok=True)
    products = (
        ("610day", "610temp.new.gif", "6-10 day temperature outlook"),
        ("610day", "610prcp.new.gif", "6-10 day precipitation outlook"),
        ("814day", "814temp.new.gif", "8-14 day temperature outlook"),
        ("814day", "814prcp.new.gif", "8-14 day precipitation outlook"),
    )
    out = []
    for sub, fn, label in products:
        dest = os.path.join(cpc_dir, fn)
        try:
            if not (os.path.exists(dest) and os.path.getsize(dest) > 5_000
                    and time.time() - os.stat(dest).st_mtime < 21_600):
                r = requests.get(
                    f"https://www.cpc.ncep.noaa.gov/products/predictions/{sub}/{fn}",
                    headers=UA, timeout=25)
                if r.ok and r.content[:3] in (b"GIF", b"\xff\xd8\xff", b"\x89PN"):
                    tmp = dest + ".part"
                    with open(tmp, "wb") as f:
                        f.write(r.content)
                    os.replace(tmp, dest)
            if os.path.exists(dest) and os.path.getsize(dest) > 5_000:
                out.append({"file": fn, "label": label,
                            "url": f"/app/static/winter/cpc/{fn}"})
        except Exception:                          # noqa: BLE001
            continue
    return out


# ------------------------------------------------------------ seasonal outlooks
def _mirror_long_range():
    """NOAA winter-2026-27 seasonal outlooks (CPC long-lead + WPC days 4-8).

    CPC long-lead outlooks are probability maps, not values: each 3-month
    window is forecast as a TILT - blues = below normal, reds = above,
    gray = equal chances (no forecastable signal). Lead 0.5 is the newest
    window; lead 1.2 / 2.2 shift it one / two months into winter, which is
    what makes them the winter-2026-27 seasonal forecast through the cold
    season. Updated the third Thursday of each month, so the gifs are
    re-fetched at most every 3 days. (The days 4-8 bridge to these windows
    is SPC's gif, mirrored for the severe page in data/severe.py.)
    """
    lr_dir = os.path.join(OUT_DIR, "lr")
    os.makedirs(lr_dir, exist_ok=True)
    CPC_LR = "https://www.cpc.ncep.noaa.gov/products/predictions/long_range"
    products = (
        ("cpc_lr_t05", f"{CPC_LR}/lead01/m.01.t.gif",
         "Seasonal temperature outlook - newest window (blue below / red above)"),
        ("cpc_lr_p05", f"{CPC_LR}/lead01/m.01.p.gif",
         "Seasonal precipitation outlook - newest window"),
        ("cpc_lr_t12", f"{CPC_LR}/lead02/m.02.t.gif",
         "Seasonal temperature outlook - window +1 month"),
        ("cpc_lr_p12", f"{CPC_LR}/lead02/m.02.p.gif",
         "Seasonal precipitation outlook - window +1 month"),
        ("cpc_lr_t22", f"{CPC_LR}/lead03/m.03.t.gif",
         "Seasonal temperature outlook - window +2 months"),
        ("cpc_lr_p22", f"{CPC_LR}/lead03/m.03.p.gif",
         "Seasonal precipitation outlook - window +2 months"),
    )
    out = []
    for fid, url, label in products:
        fn = fid + ".gif"
        dest = os.path.join(lr_dir, fn)
        try:
            if not (os.path.exists(dest) and os.path.getsize(dest) > 5_000
                    and time.time() - os.stat(dest).st_mtime < 259_200):
                r = requests.get(url, headers=UA, timeout=60)
                if r.ok and r.content[:3] in (b"GIF", b"\xff\xd8\xff", b"\x89PN"):
                    tmp = dest + ".part"
                    with open(tmp, "wb") as f:
                        f.write(r.content)
                    os.replace(tmp, dest)
            if os.path.exists(dest) and os.path.getsize(dest) > 5_000:
                out.append({"id": fid, "file": fn, "label": label,
                            "url": f"/app/static/winter/lr/{fn}"})
        except Exception:                          # noqa: BLE001
            continue
    return out


def _model_snow_frames():
    """Multi-model snowfall guidance as overlay frames (models-page renders).

    Points the winter map at the SAME pre-rendered model_maps PNGs the models
    page explorer serves (2026-09-20 user request: more winter-weather
    forecast models). GFS snowfall reaches day 10, GEFS adds the ensemble
    mean + spread (uncertainty), NBM the 6-h blend accumulation - between
    them: hourly CAM detail, 10-day global reach, and consensus.

    Returns {model: {label, frames: [{id, label, pngUrl, bounds}]}} using
    the latest cycle present on disk (the models-page rotation keeps them
    current; nothing new is downloaded here).
    """
    base = os.path.join("static", "model_maps")
    try:
        names = os.listdir(base)
    except OSError:
        return {}
    rx = re.compile(r"^(\w[\w\-]*?)_(\w*snow\w*)_f(\d{3})_(\d{10})_(\w+)\.png$")
    want = (("GFS", "snow"), ("GEFS", "snow"),
            ("GEFS-Spread", "sp_snow"), ("NBM", "nbm_snow06"))
    MODEL_LABEL = {
        "GFS": "GFS snowfall (day 1-10)",
        "GEFS": "GEFS ensemble mean snowfall",
        "GEFS-Spread": "GEFS spread - where runs disagree",
        "NBM": "NBM 6-hour snow accumulation",
    }
    # region extents must match MAP_REGIONS in data/model_maps.py (the
    # model map is rendered with exactly these bounds)
    REG_BOUNDS = {
        "us": [51, -125, 23, -66],      # [latN, lonW, latS, lonE] for Leaflet
        "etn": [42, -92, 30, -74],
    }
    out = {}
    for model, prod in want:
        rx_m = re.compile(
            rf"^{re.escape(model)}_{re.escape(prod)}_f(\d{{3}})_(\d{{10}})_(\w+)\.png$")
        cyc_files = {}
        for fn in names:
            m = rx_m.match(fn)
            if not m:
                continue
            cyc_files.setdefault(m.group(2), {}).setdefault(
                m.group(3), []).append((int(m.group(1)), fn))
        if not cyc_files:
            continue
        newest = max(cyc_files)
        for region, items in cyc_files[newest].items():
            bounds = REG_BOUNDS.get(region)
            if not bounds:
                continue
            frames = []
            for fh, fn in sorted(items):
                frames.append({
                    "id": fn[:-4],
                    "hour": fh,
                    "label": f"+{fh} h ({fh // 24}d {fh % 24}h) · " + (
                        "US" if region == "us" else "East TN"),
                    "pngUrl": f"/app/static/model_maps/{fn}",
                    "bounds": bounds,
                })
            key = model if region == "us" else f"{model}-ETN"
            lbl = MODEL_LABEL[model] + ("" if region == "us"
                                        else " - East TN zoom")
            out[key] = {"label": lbl, "cycle": newest, "frames": frames}
    return out


# ------------------------------------------------------------ bundle
def winter_bundle():
    """Everything the winter page needs (10-min cache)."""
    with _lock:
        now = time.time()
        c = _cache.get("bundle")
        if c and now - _cache["at"] < 600:
            return c
        cycle = _latest_cycle()
        frames = _render_hrrr(cycle) if cycle else {"snow": [], "ice": []}
        if cycle:
            _prune_old(cycle)
        bundle = {
            "ok": bool(frames["snow"] or frames["ice"]),
            "cycle": (full(cycle) if cycle else None),
            "frames": frames,
            "alerts": winter_alerts(),
            "wpc": _mirror_wpc(),
            "cpc": _mirror_cpc(),
            "longRange": _mirror_long_range(),
            "modelSnow": _model_snow_frames(),
            "generated": time.strftime("%Y-%m-%d %H:%M"),
        }
        _cache["bundle"] = bundle
        _cache["at"] = time.time()
        return bundle

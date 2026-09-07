"""Weather frame service: past radar, satellite, and future radar (HRRR).

Past radar:    RainViewer public tile API (api.rainviewer.com) - global NEXRAD
               composite as native map tiles, no key required.
Satellite:     RainViewer infrared when available, else NASA GIBS keyless WMTS
               (GOES-East ABI GeoColor, 10-minute cadence).
Future radar:  HRRR simulated composite reflectivity (REFC) from NOAA's AWS
               open-data bucket (noaa-hrrr-bdp-pds), fetched via byte-range,
               decoded with cfgrib, reprojected to Web Mercator and colormapped
               to an NWS-style dBZ palette. No key required. Up to 18 forecast
               hours (f01-f18), rendered in a background thread with a disk
               cache and served to the map component via Streamlit's static
               route so frames load lazily and pop in as they finish.

(NCEP's opengeo WMS was previously used for past frames but its mosaic feed
went dark; RainViewer is the reliable keyless replacement.)
"""
import datetime as dt
import json
import os
import re
import threading
import time

import numpy as np
import requests

RAINVIEWER_API = "https://api.rainviewer.com/public/weather-maps.json"
GIBS_BASE = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best"
HRRR_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"

UA = {"User-Agent": "freebuff-weather-app/1.0 (local demo)"}

# RainViewer tile style: color scheme 2 (Universal Blue) + smooth rendering
RV_COLOR = 2
RV_SMOOTH = 1

PAST_COUNT = 12

# ---------------------------------------------------------------- past radar


def get_past_frames(count=PAST_COUNT):
    """Return recent RainViewer past-radar frames for tile-based animation.

    Returns list of {'time': unix_seconds, 'path': '/v2/radar/<hash>',
    'label': '-30m'|'Now', 'kind': 'past'} - oldest first, ending at 'Now'.
    RainViewer keeps ~2 h of 10-minute NEXRAD-composite frames.
    """
    try:
        r = requests.get(RAINVIEWER_API, headers=UA, timeout=10)
        r.raise_for_status()
        frames = (r.json().get("radar") or {}).get("past") or []
    except requests.RequestException:
        return []
    frames = frames[-count:]
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for f in frames:
        mins = int(max(0, (now - dt.datetime.fromtimestamp(f["time"], dt.timezone.utc)).total_seconds() // 60))
        label = "Now" if mins < 6 else f"-{mins}m"
        out.append({"time": f["time"], "path": f["path"], "label": label, "kind": "past"})
    return out


def get_nowcast_frames(count=3):
    """RainViewer radar nowcast - next ~30 min extrapolated NEXRAD frames.

    Comes from the same weather-maps.json as the past frames ('radar.nowcast')
    and renders as plain RainViewer tiles, so it slots into the future-radar
    timeline ahead of the HRRR/NAM model hours.
    """
    try:
        r = requests.get(RAINVIEWER_API, headers=UA, timeout=10)
        r.raise_for_status()
        frames = (r.json().get("radar") or {}).get("nowcast") or []
    except requests.RequestException:
        return []
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for f in frames[:count]:
        mins = int(max(1, (dt.datetime.fromtimestamp(f["time"], dt.timezone.utc) - now).total_seconds() // 60))
        out.append({"time": f["time"], "path": f["path"], "label": f"+{mins}m",
                    "kind": "future", "model": "rvnow"})
    return out


# ---------------------------------------------------------------- satellite


def get_satellite_frames(count=12):
    """Return recent satellite frames for tile-based animation.

    Prefers RainViewer infrared if the feed is populated; otherwise falls back
    to NASA GIBS GOES-East ABI GeoColor (keyless WMTS, 10-minute cadence).
    Frames are {'label': '-30m'|'Now', 'kind': 'satellite'} plus either
    'path' (RainViewer) or 'time' (GIBS REST time) so the component can build
    tile URLs either way.
    """
    now = dt.datetime.now(dt.timezone.utc)
    frames = []
    try:
        r = requests.get(RAINVIEWER_API, headers=UA, timeout=10)
        r.raise_for_status()
        rv = ((r.json().get("satellite") or {}).get("infrared")) or []
    except requests.RequestException:
        rv = []
    if rv:
        now_ts = time.time()
        for f in rv[-count:]:
            mins = int(max(0, (now_ts - f["time"]) // 60))
            label = "Now" if mins < 6 else f"-{mins}m"
            frames.append({"time": f["time"], "path": f["path"], "label": label, "kind": "satellite"})
        return frames

    # GIBS fallback: GOES-East ABI GeoColor, latest frame near :00/:10/:20...
    latest = now.replace(second=0, microsecond=0)
    latest -= dt.timedelta(minutes=latest.minute % 10)
    latest -= dt.timedelta(minutes=5)  # GIBS publishes a few minutes behind
    for m in range(0, count):
        t = latest - dt.timedelta(minutes=10 * m)
        mins = int((now - t).total_seconds() // 60)
        label = "Now" if mins < 6 else f"-{mins}m"
        frames.append({
            "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "label": label,
            "kind": "satellite",
        })
    return frames


def satellite_source(frames):
    """Classify a get_satellite_frames() result: 'rainviewer' or 'gibs'."""
    return "rainviewer" if frames and "path" in frames[0] else "gibs"


# ---------------------------------------------------------------- HRRR future


def get_future_frames(max_hours=48):
    """Future-radar frame descriptors, HRRR (0-18 h) + NAM nest (19-48 h).

    HRRR's newest cycle publishes incrementally (f01 first), so we combine
    its fresh frames with earlier cycles' later frames; NAM CONUS-nest REFC
    (3 km, hourly to f48) fills the hours beyond HRRR's 18 h horizon. Newer
    model wins per valid time. Descriptors carry the GRIB2 byte range for
    the REFC message so rendering is cheap (~300 KB HRRR / ~350 KB NAM).
    """
    now = dt.datetime.now(dt.timezone.utc)
    by_valid_hour = {}
    # --- HRRR (fine detail, nearest hours) ---
    for back in range(0, 6):
        cycle_dt = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        base = f"{HRRR_BUCKET}/hrrr.{cycle_dt:%Y%m%d}/conus/hrrr.t{cycle_dt:%H}z.wrfsfcf"
        for fh in range(1, 19):  # HRRR goes out to f18
            valid = cycle_dt + dt.timedelta(hours=fh)
            ahead = int((valid - now).total_seconds() // 60)
            if ahead < 10:
                continue
            if ahead > 18 * 60:
                break
            key = valid.strftime("%Y%m%d%H")
            if key in by_valid_hour:
                continue  # newer cycle already provided this valid hour
            try:
                r = requests.get(f"{base}{fh:02d}.grib2.idx", headers=UA, timeout=15)
                if r.status_code != 200:
                    continue
            except requests.RequestException:
                continue
            rng = _find_refc_range(r.text)
            if not rng:
                continue
            start, end = rng
            by_valid_hour[key] = {
                "model": "hrrr",
                "time": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "label": f"+{ahead}m",
                "kind": "future",
                "url": f"{base}{fh:02d}.grib2",
                "start": start,
                "end": end,
                "cycle": cycle_dt.strftime("%Y%m%d%H"),
                "fh": fh,
            }
    # --- NAM CONUS nest (fills 19-48 h; 3 km, hourly) ---
    for d in _nam_future_descriptors(now):
        t = d["time"]  # 'YYYY-MM-DDTHH:MM:SSZ'
        key = t[:4] + t[5:7] + t[8:10] + t[11:13]
        if key not in by_valid_hour:
            by_valid_hour[key] = d
    return [by_valid_hour[k] for k in sorted(by_valid_hour)][:max_hours]


_NAM_CYCLE_CACHE = {"key": None, "cycle": None}


def _nam_cycle(now):
    """Newest NAM cycle that has nest hour f19 published (cached 30 min)."""
    ck = now.strftime("%Y%m%d%H")
    if _NAM_CYCLE_CACHE["key"] == ck and _NAM_CYCLE_CACHE["cycle"]:
        return _NAM_CYCLE_CACHE["cycle"]
    for back in range(2, 14):
        c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        base = f"https://noaa-nam-pds.s3.amazonaws.com/nam.{c:%Y%m%d}/nam.t{c:%H}z.conusnest.hiresf"
        try:
            r = requests.head(f"{base}19.tm00.grib2.idx", headers=UA, timeout=12)
        except requests.RequestException:
            continue
        if r.status_code == 200:
            _NAM_CYCLE_CACHE["key"] = ck
            _NAM_CYCLE_CACHE["cycle"] = c
            return c
    return None


def _nam_future_descriptors(now):
    """NAM CONUS-nest REFC descriptors for valid hours 19-48 ahead."""
    cycle_dt = _nam_cycle(now)
    if cycle_dt is None:
        return []
    out = []
    base = f"https://noaa-nam-pds.s3.amazonaws.com/nam.{cycle_dt:%Y%m%d}/nam.t{cycle_dt:%H}z.conusnest.hiresf"
    for fh in range(19, 49):
        valid = cycle_dt + dt.timedelta(hours=fh)
        ahead = int((valid - now).total_seconds() // 60)
        if ahead < 19 * 60 - 30:
            continue
        try:
            r = requests.get(f"{base}{fh:02d}.tm00.grib2.idx", headers=UA, timeout=15)
            if r.status_code != 200:
                continue
        except requests.RequestException:
            continue
        rng = _find_refc_range(r.text)
        if not rng:
            continue
        start, end = rng
        out.append({
            "model": "nam",
            "time": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "label": f"+{ahead // 60}h{(ahead % 60):02d}" if ahead % 60 else f"+{ahead // 60}h",
            "kind": "future",
            "url": f"{base}{fh:02d}.tm00.grib2",
            "start": start,
            "end": end,
            "cycle": cycle_dt.strftime("%Y%m%d%H"),
            "fh": fh,
        })
    return out


_DESCRIPTORS_CACHE = {"key": None, "frames": []}


def get_future_frames_cached(max_hours=48):
    """get_future_frames with a per-cycle cache - .idx checks hit S3 ~90x/call."""
    key = (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H"), max_hours)
    if _DESCRIPTORS_CACHE["key"] == key:
        return _DESCRIPTORS_CACHE["frames"]
    frames = get_future_frames(max_hours=max_hours)
    _DESCRIPTORS_CACHE["key"] = key
    _DESCRIPTORS_CACHE["frames"] = frames
    return frames


def _find_refc_range(idx_text):
    lines = idx_text.splitlines()
    for i, line in enumerate(lines):
        fields = line.split(":")
        if len(fields) > 3 and fields[3] == "REFC":
            start = int(fields[1])
            end = int(lines[i + 1].split(":")[1]) if i + 1 < len(lines) else start + 5_000_000
            return start, end
    return None


# ---------------------------------------------------------------- HRRR grid
_GRID_CACHE = {}


def _hrrr_grid_lonlat(step=1):
    """Return (lon2d, lat2d) for the HRRR CONUS 3km grid at a downsample step.

    Coordinates are computed at the downsampled resolution so they align
    exactly with values[::step, ::step] arrays.
    """
    if step in _GRID_CACHE:
        return _GRID_CACHE[step]
    from pyproj import Transformer

    nx, ny = 1799, 1059
    dx = dy = 3000.0
    R = 6371229.0  # WRF sphere radius used by HRRR
    proj4 = (
        f"+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 "
        f"+x_0=0 +y_0=0 +R={R} +units=m +no_defs"
    )
    transformer = Transformer.from_crs(proj4, "EPSG:4326", always_xy=True)
    x = -2697500.0 + np.arange(0, nx, step) * dx
    y = -1587300.0 + np.arange(0, ny, step) * dy
    xx, yy = np.meshgrid(x, y)
    lon, lat = transformer.transform(xx, yy)
    _GRID_CACHE[step] = (lon, lat)
    return lon, lat


def _to_hrrr_display(values, lat_src, lon_src, max_px=1400):
    """Nearest-resample any CONUS model grid onto the HRRR display grid.

    Used for future-radar extension frames (NAM nest): keeps one image size,
    one bounds box and one AI-cell detector for the whole timeline.
    """
    from scipy.spatial import cKDTree

    step = max(1, int(np.ceil(max(values.shape) / max_px)))
    lon_s = ((np.asarray(lon_src, dtype=float)[::step, ::step] + 180) % 360) - 180
    lat_s = np.asarray(lat_src, dtype=float)[::step, ::step]
    dlon, dlat = _hrrr_grid_lonlat(step)
    tree = cKDTree(np.column_stack([lat_s.reshape(-1), lon_s.reshape(-1)]))
    _, idx = tree.query(np.column_stack([dlat.reshape(-1), dlon.reshape(-1)]), workers=-1)
    out = values[::step, ::step].reshape(-1)[idx].reshape(dlat.shape)
    return out, dlon, dlat


_DBZ_COLORS = [
    (5, 4, 63, 116), (10, 8, 105, 158), (15, 33, 145, 190),
    (20, 30, 190, 218), (25, 90, 220, 220), (30, 105, 200, 105),
    (35, 60, 175, 60), (40, 35, 140, 35), (45, 240, 230, 60),
    (50, 230, 160, 30), (55, 220, 40, 30), (60, 180, 20, 25),
    (65, 130, 15, 20), (70, 80, 10, 15),
]
_DBZ_BOUNDS = np.array([c[0] for c in _DBZ_COLORS])
_DBZ_RGB = np.array([c[1:] for c in _DBZ_COLORS], dtype=np.uint8)

# Registry of on-disk HRRR frame artifacts.
FRAME_DIR = os.path.join("static", "hrrr")
REGISTRY_PATH = os.path.join(FRAME_DIR, "registry.json")
DESCRIPTORS_PATH = os.path.join(FRAME_DIR, "descriptors.json")


def _frame_id(descriptor):
    """Stable id per (model, cycle, forecast-hour) so renders survive restarts."""
    return f"{descriptor.get('model', 'hrrr')}_{descriptor['cycle']}_{descriptor['fh']:02d}"


def _load_registry():
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_registry(reg):
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f)
    os.replace(tmp, REGISTRY_PATH)


def _dbz_to_rgba(dbz):
    """Map dBZ array to RGBA uint8; weak/missing echoes transparent."""
    idx = np.searchsorted(_DBZ_BOUNDS, dbz, side="right") - 1
    valid = (idx >= 0) & np.isfinite(dbz)
    rgba = np.zeros(dbz.shape + (4,), dtype=np.uint8)
    if valid.any():
        rgb = _DBZ_RGB[np.clip(idx, 0, len(_DBZ_RGB) - 1)]
        rgba[..., :3][valid] = rgb[valid]
        rgba[..., 3][valid] = np.clip((dbz - 5) * 5, 25, 235).astype(np.uint8)[valid]
    return rgba


def _render_future_frame(descriptor, max_px=1400):
    """Fetch + decode one HRRR REFC frame and write artifacts to FRAME_DIR.

    Writes <id>.png and <id>.json (bounds + cells payload) and marks the
    registry entry 'done'. Returns the registry entry.
    """
    import xarray as xr

    fid = _frame_id(descriptor)
    os.makedirs(FRAME_DIR, exist_ok=True)
    tmp = os.path.join(FRAME_DIR, fid + ".grib2")
    entry = {"id": fid, "status": "rendering", "label": descriptor["label"],
             "time": descriptor["time"]}

    try:
        r = requests.get(
            descriptor["url"],
            headers={**UA, "Range": f"bytes={descriptor['start']}-{descriptor['end'] - 1}"},
            timeout=60,
        )
        r.raise_for_status()
        with open(tmp, "wb") as f:
            f.write(r.content)
        ds = xr.open_dataset(tmp, engine="cfgrib", backend_kwargs={"indexpath": ""})
        var = "refc" if "refc" in ds else list(ds.data_vars)[0]
        raw = np.asarray(ds[var].values, dtype=float)
        if descriptor.get("model") == "nam":
            lat_src = np.asarray(ds["latitude"].values, dtype=float)
            lon_src = np.asarray(ds["longitude"].values, dtype=float)
            if lat_src.ndim == 1:
                lon_src, lat_src = np.meshgrid(lon_src, lat_src)
        os.remove(tmp)

        if descriptor.get("model") == "nam":
            # NAM nest -> HRRR display grid so the whole timeline shares
            # one image size, bounds box and AI-cell detector
            values, lon_full, lat_full = _to_hrrr_display(raw, lat_src, lon_src, max_px)
            step = 1
        else:
            # Downsample full 1059x1799 grid for speed
            step = max(1, int(np.ceil(max(raw.shape) / max_px)))
            values = raw[::step, ::step]
            lon_full, lat_full = _hrrr_grid_lonlat(step)

        # Sparse point samples (~30 km spacing) for per-location model charts
        sstep = 15
        samples = np.stack([
            lat_full[::sstep, ::sstep], lon_full[::sstep, ::sstep], values[::sstep, ::sstep],
        ], axis=-1).reshape(-1, 3)
        samples = samples[np.isfinite(samples[:, 2])]
        with open(os.path.join(FRAME_DIR, fid + ".sample.json"), "w", encoding="utf-8") as f:
            json.dump([[round(a, 3), round(b, 3), round(c, 1)] for a, b, c in samples], f)

        rgba = _dbz_to_rgba(values)
        from PIL import Image

        img = Image.fromarray(rgba, "RGBA")
        png_path = os.path.join(FRAME_DIR, fid + ".png")
        img.save(png_path, "PNG", optimize=True)

        west, east = float(lon_full.min()), float(lon_full.max())
        south, north = float(lat_full.min()), float(lat_full.max())

        # AI storm-cell detection + motion projection for this frame
        from ai.storm_tracker import detect_cells

        cells = detect_cells(values, lon_full, lat_full, px_area_km2=9.0 * step * step)
        track_out = _track_with_history(descriptor["time"], cells)

        meta = {
            "bounds": [south, west, north, east],
            "cells": cells,
            "tracks": track_out,
        }
        with open(os.path.join(FRAME_DIR, fid + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)

        entry.update({"status": "done", **meta})
    except Exception as exc:  # noqa: BLE001 - record any render failure
        entry["status"] = "error"
        entry["error"] = str(exc)[:200]

    reg = _load_registry()
    reg[fid] = entry
    _save_registry(reg)
    return entry


_TRACK_HISTORY = {}
_TRACK_LOCK = threading.Lock()


def _track_with_history(valid_time, cells):
    """Attach AI motion tracks using cells from previously rendered frames.

    Keyed by ISO valid time (chronological even when render order differs).
    """
    from ai.storm_tracker import track_cells, project_cells

    with _TRACK_LOCK:
        _TRACK_HISTORY[valid_time] = cells
        ordered = [_TRACK_HISTORY[k] for k in sorted(_TRACK_HISTORY)]
        if len(ordered) > 4:
            for k in sorted(_TRACK_HISTORY)[:-4]:
                _TRACK_HISTORY.pop(k, None)
    if len(ordered) < 2:
        return {"30": [], "60": []}
    tracked = track_cells(ordered, interval_minutes=60)
    last = tracked[-1] if tracked else []
    return {"30": project_cells(last, 30), "60": project_cells(last, 60)}


def _prune_registry(reg):
    """Drop registry entries older than 24 h so the dir cannot grow forever."""
    now = dt.datetime.now(dt.timezone.utc)
    keep = {}
    for fid, entry in reg.items():
        m = re.match(r"(?:hrrr|nam)_(\d{10})_\d+", fid)
        if m:
            t = dt.datetime.strptime(m.group(1), "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
            if (now - t).total_seconds() < 24 * 3600:
                keep[fid] = entry
                continue
        # remove orphaned artifacts
        for suffix in (".png", ".json", ".sample.json"):
            try:
                os.remove(os.path.join(FRAME_DIR, fid + suffix))
            except OSError:
                pass
    return keep


def _save_descriptors(descriptors):
    """Persist slim frame descriptors so the UI never blocks on S3."""
    os.makedirs(FRAME_DIR, exist_ok=True)
    slim = [{"id": _frame_id(d), "label": d["label"], "time": d["time"]} for d in descriptors]
    tmp = DESCRIPTORS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(slim, f)
    os.replace(tmp, DESCRIPTORS_PATH)


def render_future_frames(descriptors, max_hours=None):
    """Render pending HRRR frames, nearest valid hour first (chronological)."""
    if max_hours:
        descriptors = descriptors[-max_hours:]
    reg = _prune_registry(_load_registry())
    _save_registry(reg)
    pending = [d for d in descriptors if reg.get(_frame_id(d), {}).get("status") != "done"]
    for d in pending:
        _render_future_frame(d)
    return _load_registry()


_RENDERER_THREAD = None


def start_future_renderer(max_hours=48):
    """Start the background renderer loop (once per process).

    Each pass: discover HRRR descriptors (S3), persist them for the UI, render
    pending frames to disk, then sleep ~5 min before the next cycle.
    """
    global _RENDERER_THREAD
    if _RENDERER_THREAD and _RENDERER_THREAD.is_alive():
        return _RENDERER_THREAD

    def _worker():
        while True:
            try:
                descriptors = get_future_frames_cached(max_hours=max_hours)
                _save_descriptors(descriptors)
                render_future_frames(descriptors)
            except Exception:  # noqa: BLE001 - background worker must not crash app
                pass
            time.sleep(300)

    _RENDERER_THREAD = threading.Thread(target=_worker, daemon=True, name="hrrr-renderer")
    _RENDERER_THREAD.start()
    return _RENDERER_THREAD


def future_bundle(max_hours=48):
    """Fast UI snapshot: reads persisted descriptors + registry (no network).

    Returns {'total': n, 'ready': n, 'labels': [...], 'summary': str|None,
             'frames': [{'id','label','time','pngUrl','bounds','cells','tracks'}]}
    Frames are returned only when rendered (pngUrl points at /static route).
    """
    from ai.storm_tracker import summarize

    try:
        with open(DESCRIPTORS_PATH, encoding="utf-8") as f:
            descriptors = json.load(f)
    except (OSError, ValueError):
        descriptors = []
    descriptors = descriptors[-max_hours:]
    reg = _load_registry()
    frames = []
    labels = []
    for d in descriptors:
        fid = d["id"]
        labels.append(d["label"])
        entry = reg.get(fid)
        if entry and entry.get("status") == "done":
            frames.append({
                "id": fid,
                "label": d["label"],
                "time": d["time"],
                "pngUrl": f"/app/static/hrrr/{fid}.png",
                "bounds": entry.get("bounds"),
                "cells": entry.get("cells") or [],
                "tracks": entry.get("tracks") or {"30": [], "60": []},
            })
    ready = len(frames)
    summary = None
    if frames:
        summary = summarize((frames[-1].get("cells") or []))
    return {
        "total": len(descriptors),
        "ready": ready,
        "labels": labels,
        "frames": frames,
        "summary": summary,
    }


def sample_future_dbz(lat, lon):
    """Peak modeled dBZ within ~15 km of (lat, lon) across ready future frames.

    Returns list of {'label','time','dbz'} sorted by valid time, for frames
    that have cached sampling data.
    """
    out = []
    reg = _load_registry()
    for fid, entry in sorted(reg.items()):
        path = os.path.join(FRAME_DIR, fid + ".sample.json")
        try:
            with open(path, encoding="utf-8") as f:
                samples = json.load(f)
        except (OSError, ValueError):
            continue
        best = 0.0
        for s in samples:
            d = _haversine_km(lat, lon, s[0], s[1])
            if d <= 15 and s[2] > best:
                best = s[2]
        if entry.get("time"):
            out.append({"label": entry.get("label", fid), "time": entry["time"], "dbz": round(best, 1)})
    return sorted(out, key=lambda r: r["time"])


def _haversine_km(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt

    p1, p2 = radians(lat1), radians(lat2)
    dp = p2 - p1
    dl = radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * 6371.0 * asin(a ** 0.5)

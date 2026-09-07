"""MRMS past radar: NOAA's official merged reflectivity mosaic (no key).

Source: noaa-mrms-pds (AWS open data), CONUS/MergedReflectivityQCComposite
- the operational Multi-Radar Multi-Sensor 0.5-degree composite, one GRIB2
per 2-minute scan (~280 KB gzipped, 3500x7000 grid, 20-55 N / 130-60 W).

Frames are rendered to Web-Mercator PNGs served via Streamlit's static
route (same lazy-load pattern as the HRRR future frames) so the map can
swap between RainViewer tiles and the official MRMS mosaic.
"""
import datetime as dt
import gzip
import json
import os
import re
import threading
import time

import numpy as np
import requests

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
BUCKET = "https://noaa-mrms-pds.s3.amazonaws.com"
PRODUCT = "MergedReflectivityQCComposite_00.50"
FRAME_COUNT = 10          # 10 scans x 2 min = 20-minute loop

FRAME_DIR = os.path.join("static", "mrms")
REGISTRY_PATH = os.path.join(FRAME_DIR, "registry.json")
DESCRIPTORS_PATH = os.path.join(FRAME_DIR, "descriptors.json")

_LIST_CACHE = {"key": None, "keys": [], "at": 0.0}


def _recent_keys(count=FRAME_COUNT):
    """Latest CONUS scan object keys, oldest first (listing cached 90 s)."""
    now = dt.datetime.now(dt.timezone.utc)
    ck = now.strftime("%Y%m%d%H")
    if _LIST_CACHE["key"] == ck and time.time() - _LIST_CACHE["at"] < 90:
        keys = _LIST_CACHE["keys"]
    else:
        keys = []
        day_dir = now.strftime("%Y%m%d")
        for back in range(0, 2):
            day = (now - dt.timedelta(days=back)).strftime("%Y%m%d")
            pre = f"CONUS/{PRODUCT}/{day}/"
            try:
                r = requests.get(f"{BUCKET}/?list-type=2&prefix={pre}&max-keys=1000",
                                 headers=UA, timeout=25)
            except requests.RequestException:
                continue
            day_keys = sorted(re.findall(r"<Key>([^<]+)</Key>", r.text))
            keys.extend(day_keys)
            if len(keys) >= count:
                break
        keys.sort()
        _LIST_CACHE.update(key=ck, keys=keys, at=time.time())
        day_dir = day_dir
    return keys[-count:]


def get_mrms_frames():
    """Frame descriptors for the recent MRMS scans, oldest first."""
    out = []
    now = dt.datetime.now(dt.timezone.utc)
    for key in _recent_keys():
        m = re.search(r"_(\d{8})-(\d{6})\.grib2\.gz$", key)
        if not m:
            continue
        scan = dt.datetime.strptime(m.group(1) + m.group(2)[:4], "%Y%m%d%H%M").replace(
            tzinfo=dt.timezone.utc)
        mins = int(max(0, (now - scan).total_seconds() // 60))
        out.append({
            "id": "mrms_" + m.group(1) + m.group(2)[:4],
            "label": "Now" if mins < 4 else f"-{mins}m",
            "time": scan.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "key": key,
        })
    return out


# ---------------------------------------------------------------- rendering
_REGISTRY = {}


def _load_registry():
    if not _REGISTRY.get("loaded"):
        try:
            with open(REGISTRY_PATH, encoding="utf-8") as f:
                _REGISTRY["data"] = json.load(f)
        except (OSError, ValueError):
            _REGISTRY["data"] = {}
        _REGISTRY["loaded"] = True
    return _REGISTRY["data"]


def _save_registry(reg):
    os.makedirs(FRAME_DIR, exist_ok=True)
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f)
    os.replace(tmp, REGISTRY_PATH)
    _REGISTRY["data"] = reg
    _REGISTRY["loaded"] = True


def _save_descriptors(frames):
    os.makedirs(FRAME_DIR, exist_ok=True)
    slim = [{k: d[k] for k in ("id", "label", "time")} for d in frames]
    tmp = DESCRIPTORS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(slim, f)
    os.replace(tmp, DESCRIPTORS_PATH)


def _prune_registry(reg):
    now = dt.datetime.now(dt.timezone.utc)
    keep = {}
    for fid, entry in reg.items():
        m = re.match(r"mrms_(\d{12})", fid)
        if m:
            t = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
            if (now - t).total_seconds() < 12 * 3600:
                keep[fid] = entry
                continue
        for suffix in (".png", ".json"):
            try:
                os.remove(os.path.join(FRAME_DIR, fid + suffix))
            except OSError:
                pass
    return keep


# dBZ colormap identical to the HRRR future-radar palette (shared look)
_DBZ_COLORS = [
    (5, 4, 63, 116), (10, 8, 105, 158), (15, 33, 145, 190),
    (20, 30, 190, 218), (25, 90, 220, 220), (30, 105, 200, 105),
    (35, 60, 175, 60), (40, 35, 140, 35), (45, 240, 230, 60),
    (50, 230, 160, 30), (55, 220, 40, 30), (60, 180, 20, 25),
    (65, 130, 15, 20), (70, 80, 10, 15),
]
_DBZ_BOUNDS = np.array([c[0] for c in _DBZ_COLORS])
_DBZ_RGB = np.array([c[1:] for c in _DBZ_COLORS], dtype=np.uint8)

# MRMS 0.005-degree CONUS fixed grid (from the GRIB2 geo keys)
_M_BBOX = {"lat0": 20.005, "lat1": 54.995, "lon0": -129.995, "lon1": -60.005,
           "ny": 3500, "nx": 7000}


def _mrms_grid(step):
    """(lat2d, lon2d) for the MRMS grid at a downsample step."""
    lats = np.linspace(_M_BBOX["lat0"], _M_BBOX["lat1"], _M_BBOX["ny"])[::step]
    lons = np.linspace(_M_BBOX["lon0"], _M_BBOX["lon1"], _M_BBOX["nx"])[::step]
    return np.meshgrid(lats, lons, indexing="ij")


def _render_mrms_frame(desc, max_px=2400):
    """Download, decode, colorize one scan; write PNG + meta; update registry."""
    fid = desc["id"]
    entry = {"id": fid, "status": "rendering", "label": desc["label"], "time": desc["time"]}
    os.makedirs(FRAME_DIR, exist_ok=True)
    try:
        import xarray as xr

        r = requests.get(f"{BUCKET}/{desc['key']}", headers=UA, timeout=90)
        r.raise_for_status()
        raw = gzip.decompress(r.content)
        tmp = os.path.join(FRAME_DIR, fid + ".grib2")
        with open(tmp, "wb") as f:
            f.write(raw)
        ds = xr.open_dataset(tmp, engine="cfgrib", backend_kwargs={"indexpath": ""})
        var = list(ds.data_vars)[0]
        values = np.asarray(ds[var].values, dtype=float)
        ds.close()
        os.remove(tmp)

        values[values < 0] = np.nan            # -99 = no coverage, -33 etc = clear-air noise floor
        step = max(1, int(np.ceil(max(values.shape) / max_px)))
        values = values[::step, ::step]
        lat_g, lon_g = _mrms_grid(step)

        rgba = np.zeros(values.shape + (4,), dtype=np.uint8)
        idx = np.searchsorted(_DBZ_BOUNDS, values, side="right") - 1
        valid = (idx >= 0) & np.isfinite(values)
        if valid.any():
            rgb = _DBZ_RGB[np.clip(idx, 0, len(_DBZ_RGB) - 1)]
            rgba[..., :3][valid] = rgb[valid]
            rgba[..., 3][valid] = np.clip((values[valid] - 5) * 5, 25, 235).astype(np.uint8)

        from PIL import Image

        Image.fromarray(rgba, "RGBA").save(os.path.join(FRAME_DIR, fid + ".png"),
                                           "PNG", optimize=True)
        meta = {"bounds": [float(lat_g.min()), float(lon_g.min()),
                           float(lat_g.max()), float(lon_g.max())]}
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


_RENDERER_THREAD = None


def start_mrms_renderer():
    """Background loop: discover scans, persist descriptors, render pending."""
    global _RENDERER_THREAD
    if _RENDERER_THREAD and _RENDERER_THREAD.is_alive():
        return _RENDERER_THREAD

    def _worker():
        while True:
            try:
                frames = get_mrms_frames()
                _save_descriptors(frames)
                reg = _prune_registry(_load_registry())
                _save_registry(reg)
                for fr in frames:
                    if reg.get(fr["id"], {}).get("status") != "done":
                        _render_mrms_frame(fr)
            except Exception:  # noqa: BLE001 - worker must not crash the app
                pass
            time.sleep(180)

    _RENDERER_THREAD = threading.Thread(target=_worker, daemon=True, name="mrms-renderer")
    _RENDERER_THREAD.start()
    return _RENDERER_THREAD


def mrms_bundle():
    """UI snapshot (no network): ready frames with static pngUrl + bounds."""
    start_mrms_renderer()
    try:
        with open(DESCRIPTORS_PATH, encoding="utf-8") as f:
            desc = json.load(f)
    except (OSError, ValueError):
        desc = []
    reg = _load_registry()
    frames = []
    for d in desc:
        entry = reg.get(d["id"])
        if entry and entry.get("status") == "done":
            frames.append({
                "kind": "mrms",
                "id": d["id"],
                "label": d["label"],
                "time": d["time"],
                "pngUrl": f"/app/static/mrms/{d['id']}.png",
                "bounds": entry.get("bounds"),
            })
    return {"total": len(desc), "ready": len(frames), "frames": frames}

"""MRMS past radar: NOAA's official NSSL Multi-Radar Multi-Sensor products (no key).

Source: noaa-mrms-pds (AWS open data), CONUS/<PRODUCT> - the operational
MRMS suite catalogued on NSSL's product tables page. The default is the
0.01-degree merged reflectivity composite (one GRIB2 per 2-minute scan,
~280 KB gzipped, 3500x7000 grid, 20-55 N / 130-60 W); the severe-weather
products (rotation tracks, MESH, az shear, echo tops, precip rate, QPE)
ride the same bucket with their own directories and colormaps.

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
DEFAULT_PRODUCT = "MergedReflectivityQCComposite_00.50"
FRAME_COUNT = 10          # 10 scans x 2 min = 20-minute loop

# NSSL MRMS operational catalog (from nssl.noaa.gov product tables).
# key -> {dir, label, unit, cmap, vmin/vmax-style bounds, cadence}
# Grids: most are 3500x7000 (0.01 deg); AzShear/RotationTrack are 7000x14000 (0.005 deg).
CATALOG = {
    "cref":   {"dir": "MergedReflectivityQCComposite_00.50", "label": "Composite Reflectivity",
               "unit": "dBZ", "kind": "dbz", "cadence": 2},
    "lowref": {"dir": "MergedReflectivityAtLowestAltitude_00.50", "label": "Lowest-Altitude Reflectivity",
               "unit": "dBZ", "kind": "dbz", "cadence": 2},
    "rots":   {"dir": "RotationTrack30min_00.50", "label": "Rotation Tracks (30 min)",
               "unit": "10\u207b\u00b2 s\u207b\u00b9", "kind": "rot", "cadence": 2},
    "mesh":   {"dir": "MESH_Max_30min_00.50", "label": "MESH Max 30-min (hail size)",
               "unit": "mm", "kind": "mesh", "cadence": 2},
    "shi":    {"dir": "SHI_00.50", "label": "Severe Hail Index",
               "unit": "J kg\u207b\u00b9", "kind": "shi", "cadence": 2},
    "vil":    {"dir": "VIL_00.50", "label": "Vertically Integrated Liquid",
               "unit": "kg m\u207b\u00b2", "kind": "vil", "cadence": 2},
    "etop":   {"dir": "EchoTop_50_00.50", "label": "Echo Top 50 dBZ",
               "unit": "km", "kind": "etop", "cadence": 2},
    "azshr":  {"dir": "MergedAzShear_0-2kmAGL_00.50", "label": "Azimuthal Shear 0-2 km",
               "unit": "10\u207b\u00b3 s\u207b\u00b9", "kind": "azshr", "cadence": 2},
    "prate":  {"dir": "PrecipRate_00.00", "label": "Precipitation Rate",
               "unit": "in/hr", "kind": "prate", "cadence": 2},
    "qpe1h":  {"dir": "RadarOnly_QPE_01H_00.00", "label": "Radar-Only QPE 1-hour",
               "unit": "in", "kind": "qpe", "cadence": 60},
    # ---- height-sliced reflectivity levels (0.5 - 15 km MSL) ----
    "l0050":  {"dir": "MergedReflectivityQC_00.50", "label": "Reflectivity 0.5 km (low)",
               "unit": "dBZ", "kind": "dbz", "cadence": 2},
    "l0200":  {"dir": "MergedReflectivityQC_02.00", "label": "Reflectivity 2.0 km",
               "unit": "dBZ", "kind": "dbz", "cadence": 2},
    "l0400":  {"dir": "MergedReflectivityQC_04.00", "label": "Reflectivity 4.0 km (mid)",
               "unit": "dBZ", "kind": "dbz", "cadence": 2},
    "l0800":  {"dir": "MergedReflectivityQC_08.00", "label": "Reflectivity 8.0 km (high)",
               "unit": "dBZ", "kind": "dbz", "cadence": 2},
    "l1500":  {"dir": "MergedReflectivityQC_15.00", "label": "Reflectivity 15.0 km",
               "unit": "dBZ", "kind": "dbz", "cadence": 2},
    # ---- dual-pol variables (height slices) + upper az-shear ----
    "zdr050": {"dir": "MergedZdr_00.50", "label": "ZDR 0.5 km (dual-pol)",
               "unit": "dB", "kind": "zdr", "cadence": 2},
    "rho050": {"dir": "MergedRhoHV_00.50", "label": "RhoHV 0.5 km (dual-pol)",
               "unit": "", "kind": "rhohv", "cadence": 2},
    "azshr36": {"dir": "MergedAzShear_3-6kmAGL_00.50", "label": "Azimuthal Shear 3-6 km",
                "unit": "10\u207b\u00b3 s\u207b\u00b9", "kind": "azshr", "cadence": 2},
}

FRAME_DIR = os.path.join("static", "mrms")
REGISTRY_PATH = os.path.join(FRAME_DIR, "registry.json")

_LIST_CACHE = {}   # product key -> {"key": hh, "keys": [...], "at": ts}


def _recent_keys(prod_key, count=FRAME_COUNT):
    """Latest CONUS scan object keys for one catalog product, oldest first."""
    now = dt.datetime.now(dt.timezone.utc)
    ck = now.strftime("%Y%m%d%H")
    hit = _LIST_CACHE.get(prod_key)
    if hit and hit["key"] == ck and time.time() - hit["at"] < 90:
        keys = hit["keys"]
    else:
        keys = []
        directory = CATALOG[prod_key]["dir"]
        for back in range(0, 3):
            day = (now - dt.timedelta(days=back)).strftime("%Y%m%d")
            pre = f"CONUS/{directory}/{day}/"
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
        _LIST_CACHE[prod_key] = {"key": ck, "keys": keys, "at": time.time()}
    return keys[-count:]


def get_mrms_frames(prod_key="cref"):
    """Frame descriptors for the recent scans of one product, oldest first."""
    out = []
    now = dt.datetime.now(dt.timezone.utc)
    for key in _recent_keys(prod_key):
        m = re.search(r"_(\d{8})-(\d{6})\.grib2\.gz$", key)
        if not m:
            continue
        scan = dt.datetime.strptime(m.group(1) + m.group(2)[:4], "%Y%m%d%H%M").replace(
            tzinfo=dt.timezone.utc)
        mins = int(max(0, (now - scan).total_seconds() // 60))
        hhmm = scan.astimezone().strftime("%H:%M")
        out.append({
            "id": f"mrms_{prod_key}_" + m.group(1) + m.group(2)[:4],
            "label": f"Now {hhmm}" if mins < 4 else hhmm,
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


def _save_descriptors(frames, prod_key="cref"):
    os.makedirs(FRAME_DIR, exist_ok=True)
    slim = [{k: d[k] for k in ("id", "label", "time")} for d in frames]
    path = os.path.join(FRAME_DIR, f"descriptors_{prod_key}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(slim, f)
    os.replace(tmp, path)


def _prune_registry(reg):
    now = dt.datetime.now(dt.timezone.utc)
    keep = {}
    for fid, entry in reg.items():
        m = re.match(r"mrms_([a-z0-9]+)_(\d{12})", fid)
        if m:
            t = dt.datetime.strptime(m.group(2), "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
            if (now - t).total_seconds() < 6 * 3600:
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

# Per-product colormaps (NSSL-style). Each: (bounds, colors, alpha_fn, mask_below)
_PROD_PALETTES = {
    "dbz": (_DBZ_BOUNDS, _DBZ_RGB,
            lambda v: np.clip((v - 5) * 5, 25, 235), 5),
    "rot": (np.array([0, 2, 5, 8, 12, 16, 20, 25, 30]),
            np.array([(60, 60, 60), (40, 120, 220), (40, 200, 220), (60, 220, 120),
                      (220, 220, 60), (250, 160, 40), (240, 60, 40), (200, 30, 200)], dtype=np.uint8),
            lambda v: np.clip((v - 2) * 12, 40, 235), 2),
    "mesh": (np.array([0, 6, 12, 19, 25, 38, 51, 76]),
             np.array([(80, 80, 80), (120, 200, 120), (250, 250, 80), (250, 180, 50),
                       (250, 90, 50), (220, 40, 120), (170, 40, 220)], dtype=np.uint8),
             lambda v: np.clip((v - 6) * 4 + 40, 40, 235), 6),
    "shi": (np.array([0, 10, 25, 50, 100, 200, 400]),
            np.array([(80, 80, 80), (150, 220, 150), (250, 250, 80), (250, 170, 50),
                      (250, 80, 50), (200, 40, 180)], dtype=np.uint8),
            lambda v: np.clip((v - 10) * 1.2 + 40, 40, 235), 10),
    "vil": (np.array([0, 5, 10, 20, 35, 50, 70]),
            np.array([(80, 80, 80), (120, 200, 220), (90, 160, 240), (70, 110, 250),
                      (140, 70, 240), (210, 40, 200)], dtype=np.uint8),
            lambda v: np.clip((v - 5) * 4 + 30, 30, 235), 5),
    "etop": (np.array([0, 3, 5, 7, 9, 11, 13, 15]),
             np.array([(80, 80, 80), (80, 140, 220), (80, 210, 210), (90, 220, 120),
                       (230, 220, 60), (250, 150, 40), (240, 60, 40)], dtype=np.uint8),
             lambda v: np.clip((v - 3) * 25 + 30, 30, 235), 3),
    "azshr": (np.array([-20, -10, -5, -2, 2, 5, 10, 20]),
              np.array([(40, 90, 220), (90, 170, 240), (180, 220, 250), (230, 230, 230),
                        (250, 230, 120), (250, 140, 50), (230, 50, 40)], dtype=np.uint8),
              lambda v: np.clip(np.abs(v) * 8 + 30, 30, 235), 2),
    "prate": (np.array([0, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0]),
              np.array([(80, 200, 120), (60, 160, 220), (90, 110, 240), (250, 250, 60),
                        (250, 160, 40), (240, 60, 40), (200, 40, 160)], dtype=np.uint8),
              lambda v: np.clip(v * 90 + 40, 40, 235), 0.05),
    "qpe": (np.array([0, 0.05, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0]),
            np.array([(90, 200, 140), (70, 160, 220), (80, 100, 240), (140, 70, 240),
                      (220, 50, 180), (250, 90, 60), (250, 160, 40)], dtype=np.uint8),
            lambda v: np.clip(v * 45 + 35, 35, 235), 0.05),
    "zdr": (np.array([-1, 0, 0.5, 1, 2, 3, 4, 6]),
            np.array([(120, 120, 120), (200, 200, 200), (120, 220, 120), (250, 250, 80),
                      (250, 160, 50), (230, 60, 40), (170, 40, 220)], dtype=np.uint8),
            lambda v: np.clip((v + 1) * 38, 30, 235), -0.2),
    "rhohv": (np.array([0.2, 0.6, 0.8, 0.9, 0.95, 1.0, 1.05]),
              np.array([(160, 40, 40), (230, 110, 50), (250, 200, 70), (160, 220, 110),
                        (90, 200, 160), (70, 130, 230)], dtype=np.uint8),
              lambda v: np.clip((v - 0.2) * 290, 30, 235), 0.25),
}

# MRMS 0.005/0.01-degree CONUS fixed grid (from the GRIB2 geo keys)
_M_BBOX = {"lat0": 20.005, "lat1": 54.995, "lon0": -129.995, "lon1": -60.005}


def _mrms_grid(step, ny=3500, nx=7000):
    """(lat2d, lon2d) for the MRMS grid at a downsample step."""
    lats = np.linspace(_M_BBOX["lat0"], _M_BBOX["lat1"], ny)[::step]
    lons = np.linspace(_M_BBOX["lon0"], _M_BBOX["lon1"], nx)[::step]
    return np.meshgrid(lats, lons, indexing="ij")


def _render_mrms_frame(desc, prod_key="cref", max_px=2400):
    """Download, decode, colorize one scan; write PNG + meta; update registry."""
    fid = desc["id"]
    spec = CATALOG.get(prod_key, CATALOG["cref"])
    bounds, colors, alpha_fn, mask_below = _PROD_PALETTES[spec["kind"]]
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
        ny, nx = values.shape
        ds.close()
        os.remove(tmp)

        values[values < 0] = np.nan            # -3/-99 = no coverage
        step = max(1, int(np.ceil(max(values.shape) / max_px)))
        values = values[::step, ::step]
        lat_g, lon_g = _mrms_grid(step, ny, nx)

        rgba = np.zeros(values.shape + (4,), dtype=np.uint8)
        idx = np.searchsorted(bounds, values, side="right") - 1
        valid = (idx >= 0) & np.isfinite(values) & (values >= mask_below)
        if valid.any():
            rgb = colors[np.clip(idx, 0, len(colors) - 1)]
            rgba[..., :3][valid] = rgb[valid]
            rgba[..., 3][valid] = alpha_fn(values[valid]).astype(np.uint8)

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


def start_mrms_renderer(prod_key="cref"):
    """Background loop: discover scans, persist descriptors, render pending."""
    global _RENDERER_THREAD
    if _RENDERER_THREAD and _RENDERER_THREAD.is_alive():
        return _RENDERER_THREAD

    def _worker():
        while True:
            try:
                for pk in CATALOG:
                    frames = get_mrms_frames(pk)
                    if not frames:
                        continue
                    _save_descriptors(frames, pk)
                    reg = _prune_registry(_load_registry())
                    _save_registry(reg)
                    for fr in frames:
                        if reg.get(fr["id"], {}).get("status") != "done":
                            _render_mrms_frame(fr, pk)
            except Exception:  # noqa: BLE001 - worker must not crash the app
                pass
            time.sleep(180)

    _RENDERER_THREAD = threading.Thread(target=_worker, daemon=True, name="mrms-renderer")
    _RENDERER_THREAD.start()
    return _RENDERER_THREAD


def mrms_bundle(prod_key="cref"):
    """UI snapshot (no network): ready frames with static pngUrl + bounds."""
    start_mrms_renderer()
    spec = CATALOG.get(prod_key, CATALOG["cref"])
    try:
        with open(os.path.join(FRAME_DIR, f"descriptors_{prod_key}.json"), encoding="utf-8") as f:
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
    return {"total": len(desc), "ready": len(frames), "frames": frames,
            "product": spec["label"], "unit": spec["unit"], "key": prod_key}

"""NOAA STAR sectorized GOES-19 imagery - the data behind
star.nesdis.noaa.gov/goes (the official NESDIS viewer), no API key.

STAR's open image CDN publishes every ABI band plus derived products
(GeoColor, AirMass, Sandwich, Fire Temperature, Dust) as pre-rendered,
pre-enhanced CONUS JPGs every 5 minutes:

    {CDN}/GOES19/ABI/CONUS/{product}/{YYYYDDDHHMM}_GOES19-ABI-CONUS-{product}-{W}x{H}.jpg

with a browsable directory listing per product and a KML super-overlay
(*-GeoTIFF.kmz) documenting the exact geographic footprint. Verified from
the overlay tiles: the images are linear in latitude/longitude (plate
carree) across the CONUS box, so each frame is row-resampled to Web
Mercator on ingest to align pixel-perfectly with the Leaflet maps.
"""
import datetime as dt
import io
import json
import math
import os
import re
import threading
import time

import numpy as np
import requests

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
CDN = "https://cdn.star.nesdis.noaa.gov"
SAT = "GOES19"
SECTOR = "CONUS"
SIZE = "1250x750"
FRAME_COUNT = 12  # 1 h at the 5-minute cadence

# exact footprint of the CONUS sector (from the KML super-overlay's level-0
# tile): S, W, N, E. Images are linear in lat/lon across this box.
BOUNDS = [9.107992, -136.564392, 50.854072, -37.813320]

# key -> (CDN product directory, UI label). Keys carry the 'star_' prefix so
# they never collide with the ABI band keys in data.satellite_bands.
PRODUCTS = {
    "star_geo":      ("GEOCOLOR", "GeoColor - NOAA STAR"),
    "star_wv":       ("08", "Water Vapor - NOAA STAR"),
    "star_wvm":      ("09", "WV Mid Layer - NOAA STAR"),
    "star_wvl":      ("10", "WV Low Layer - NOAA STAR"),
    "star_ir":       ("13", "Infrared - NOAA STAR"),
    "star_vis":      ("02", "Red Visible - NOAA STAR (day)"),
    "star_airmass":  ("AirMass", "Air Mass - NOAA STAR"),
    "star_sandwich": ("Sandwich", "Sandwich - NOAA STAR"),
    "star_fire":     ("FireTemperature", "Fire Temperature - NOAA STAR"),
    "star_dust":     ("Dust", "Dust - NOAA STAR"),
}

FRAME_DIR = os.path.join("static", "star")
REGISTRY_PATH = os.path.join(FRAME_DIR, "registry.json")
LISTINGS_PATH = os.path.join(FRAME_DIR, "listings.json")

_LISTINGS = {}          # prod -> (stamps, fetched_at)
_LIST_LOCK = threading.Lock()
_RENDER_THREADS = {}
_REG_LOCK = threading.Lock()


def _prod_dir(key):
    return PRODUCTS[key][0]


def _stamp_to_dt(stamp):
    """YYYYDDDHHMM (UTC) -> aware datetime."""
    return dt.datetime.strptime(stamp, "%Y%j%H%M").replace(tzinfo=dt.timezone.utc)


def _frame_url(key, stamp, size=SIZE):
    return (f"{CDN}/{SAT}/ABI/{SECTOR}/{_prod_dir(key)}/"
            f"{stamp}_GOES19-ABI-{SECTOR}-{_prod_dir(key)}-{size}.jpg")


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _list_stamps(key):
    """Timestamped frames for a product, oldest first (90 s TTL)."""
    prod = _prod_dir(key)
    with _LIST_LOCK:
        hit = _LISTINGS.get(prod)
        if hit and time.time() - hit[1] < 90:
            return hit[0]
    stamps = []
    try:
        r = requests.get(f"{CDN}/{SAT}/ABI/{SECTOR}/{prod}/", headers=UA, timeout=30)
        if r.status_code == 200:
            stamps = sorted({m for m in re.findall(
                rf"(\d{{11}})_GOES19-ABI-{SECTOR}-{prod}-{SIZE}\.jpg", r.text)})
    except requests.RequestException:
        pass
    if not stamps:  # fall back to the last known listing on disk
        stamps = _load_json(LISTINGS_PATH, {}).get(prod, [])
    else:
        with _LIST_LOCK:
            all_lists = _load_json(LISTINGS_PATH, {})
            all_lists[prod] = stamps
            _save_json(LISTINGS_PATH, all_lists)
        _LISTINGS[prod] = (stamps, time.time())
    return stamps


# ---------------------------------------------------------------- rendering
def _merc_row_positions(lat_n, lat_s, h):
    """Output row -> source row so rows become linear in Web Mercator y."""
    def merc(lat):
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))

    yn, ys = merc(lat_n), merc(lat_s)
    y_grid = yn - np.linspace(0.0, 1.0, h) * (yn - ys)
    lats = np.degrees(2.0 * np.arctan(np.exp(y_grid)) - math.pi / 2)
    pos = (lat_n - lats) / (lat_n - lat_s) * (h - 1)
    return np.clip(pos, 0, h - 1)


def _render_frame(key, stamp):
    """Download one STAR frame, Mercator-align rows, save + register it."""
    fid = f"{key}_{stamp}"
    png_path = os.path.join(FRAME_DIR, fid + ".jpg")
    entry = {"id": fid, "status": "rendering", "time": _stamp_to_dt(stamp).strftime("%Y-%m-%dT%H:%M:%SZ")}
    os.makedirs(FRAME_DIR, exist_ok=True)
    try:
        from PIL import Image

        if not os.path.exists(png_path):
            r = requests.get(_frame_url(key, stamp), headers=UA, timeout=90)
            r.raise_for_status()
            arr = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))
            pos = _merc_row_positions(BOUNDS[2], BOUNDS[0], arr.shape[0])
            i0 = np.floor(pos).astype(int)
            i1 = np.minimum(i0 + 1, arr.shape[0] - 1)
            w = (pos - i0)[:, None, None]
            out = arr[i0] * (1 - w) + arr[i1] * w
            Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(
                png_path, "JPEG", quality=88)
        entry.update({"status": "done", "bounds": BOUNDS})
    except Exception as exc:  # noqa: BLE001 - record any render failure
        entry["status"] = "error"
        entry["error"] = str(exc)[:200]
    with _REG_LOCK:
        reg = _load_json(REGISTRY_PATH, {})
        reg[fid] = entry
        _save_json(REGISTRY_PATH, reg)
    return entry


def _prune_registry(reg):
    now = dt.datetime.now(dt.timezone.utc)
    keep = {}
    for fid, entry in reg.items():
        m = re.match(rf"(\w+)_(\d{{11}})", fid)
        if m:
            t = _stamp_to_dt(m.group(2))
            if (now - t).total_seconds() < 24 * 3600:
                keep[fid] = entry
                continue
        try:
            os.remove(os.path.join(FRAME_DIR, fid + ".jpg"))
        except OSError:
            pass
    return keep


def _ensure_threads(key=None):
    """One lazy background worker per product (refresh + render frames)."""
    for k in ([key] if key else list(PRODUCTS)):
        t = _RENDER_THREADS.get(k)
        if t and t.is_alive():
            continue

        def _worker(kk=k):
            while True:
                try:
                    stamps = _list_stamps(kk)[-FRAME_COUNT:]
                    with _REG_LOCK:
                        reg = _prune_registry(_load_json(REGISTRY_PATH, {}))
                    for stamp in stamps:
                        fid = f"{kk}_{stamp}"
                        if reg.get(fid, {}).get("status") != "done":
                            _render_frame(kk, stamp)
                    with _REG_LOCK:
                        _save_json(REGISTRY_PATH, _prune_registry(
                            _load_json(REGISTRY_PATH, {})))
                except Exception:  # noqa: BLE001 - worker must not crash the app
                    pass
                time.sleep(300)

        _RENDER_THREADS[k] = threading.Thread(
            target=_worker, daemon=True, name=f"star-{k}")
        _RENDER_THREADS[k].start()


def star_bundle(key):
    """UI snapshot for one STAR product: {'label','total','ready','frames'}.

    On first use the product listing is fetched synchronously once (~1 s)
    so frames are available immediately; afterwards it is served from the
    90 s cache and the background worker keeps it fresh.
    """
    _ensure_threads(key)
    if not _load_json(LISTINGS_PATH, {}).get(_prod_dir(key)):
        _list_stamps(key)
    listing = _load_json(LISTINGS_PATH, {}).get(_prod_dir(key), [])[-FRAME_COUNT:]
    reg = _load_json(REGISTRY_PATH, {})
    now = dt.datetime.now(dt.timezone.utc)
    frames = []
    for stamp in listing:
        entry = reg.get(f"{key}_{stamp}")
        if entry and entry.get("status") == "done":
            mins = int(max(0, (now - _stamp_to_dt(stamp)).total_seconds() // 60))
            frames.append({
                "kind": "sat",
                "label": "Now" if mins < 6 else f"-{mins}m",
                "time": entry.get("time"),
                "pngUrl": f"/app/static/star/{key}_{stamp}.jpg",
                "bounds": entry.get("bounds"),
            })
    return {
        "key": key,
        "label": PRODUCTS[key][1],
        "total": len(listing),
        "ready": len(frames),
        "frames": frames,
    }

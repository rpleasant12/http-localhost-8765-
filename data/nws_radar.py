"""NWS radar frames from NOAA's official GeoServer WMS (no key).

opengeo.ncep.noaa.gov serves the QC'd CONUS NEXRAD mosaic (layer
conus_bref_qcd) as EPSG:3857 PNGs with a TIME dimension listing every
~2-minute mosaic scan. Frames are straight downloads at a fixed bbox -
no GRIB decode needed - rendered into the same image-overlay pipeline
as MRMS. A background worker mirrors the MRMS renderer; the UI bundle
carries static pngUrl + geographic bounds per frame.
"""
import datetime as dt
import json
import math
import os
import re
import threading
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0"}

WMS_URL = ("https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows"
           "?service=WMS&version=1.3.0&request=GetMap&layers=conus_bref_qcd"
           "&crs=EPSG:3857&format=image/png&width=1024&height=1024")
CAP_URL = ("https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows"
           "?service=WMS&version=1.3.0&request=GetCapabilities")

# Fixed CONUS request box (EPSG:3857 meters) - covers the lower 48 with margin
BBOX = (-12960000, 2680000, -7100000, 6600000)

FRAME_DIR = os.path.join("static", "nws_radar")
REGISTRY_PATH = os.path.join(FRAME_DIR, "registry.json")
DESCRIPTORS_PATH = os.path.join(FRAME_DIR, "descriptors.json")

FRAME_COUNT = 15

_TIME_CACHE = {"at": 0.0, "times": []}


def _bbox_bounds():
    """Fixed bbox -> [latS, lonW, latN, lonE] for Leaflet imageOverlay."""
    x0, y0, x1, y1 = BBOX
    lat = lambda y: math.degrees(2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2)  # noqa: E731
    return [lat(y0), x0 / 6378137.0 / math.pi * 180, lat(y1), x1 / 6378137.0 / math.pi * 180]


def _wms_times():
    """Mosaic scan times from the WMS capabilities (cached 5 min)."""
    if time.time() - _TIME_CACHE["at"] < 300:
        return _TIME_CACHE["times"]
    try:
        cap = requests.get(CAP_URL, headers=UA, timeout=20).text
        m = re.search(r'<Dimension[^>]*name="time"[^>]*>(.*?)</Dimension>', cap, re.S | re.I)
        times = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", m.group(1)) if m else []
        _TIME_CACHE["at"] = time.time()
        _TIME_CACHE["times"] = times
    except requests.RequestException:
        pass
    return _TIME_CACHE["times"]


def get_nws_frames():
    """Frame descriptors for the recent mosaic scans, oldest first."""
    times = _wms_times()
    out = []
    now = dt.datetime.now(dt.timezone.utc)
    for t in times[-FRAME_COUNT:]:
        try:
            scan = dt.datetime.strptime(t.split(".")[0], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        mins = int(max(0, (now - scan).total_seconds() // 60))
        out.append({
            "id": "nws_" + scan.strftime("%Y%m%d%H%M"),
            "label": "Now" if mins < 4 else f"-{mins}m",
            "time": scan.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "iso": t,
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
        m = re.match(r"nws_(\d{12})", fid)
        if m:
            t = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
            if (now - t).total_seconds() < 6 * 3600:
                keep[fid] = entry
                continue
        for suffix in (".png",):
            try:
                os.remove(os.path.join(FRAME_DIR, fid + suffix))
            except OSError:
                pass
    return keep


def _render_nws_frame(desc):
    """Download one TIME-specific mosaic PNG; write file + registry entry."""
    fid = desc["id"]
    entry = {"id": fid, "status": "rendering", "label": desc["label"], "time": desc["time"]}
    os.makedirs(FRAME_DIR, exist_ok=True)
    try:
        x0, y0, x1, y1 = BBOX
        url = (f"{WMS_URL}&bbox={x0},{y0},{x1},{y1}&time={desc['iso']}")
        r = requests.get(url, headers=UA, timeout=60)
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("image") and len(r.content) > 2000:
            with open(os.path.join(FRAME_DIR, fid + ".png"), "wb") as f:
                f.write(r.content)
            entry.update({"status": "done", "bounds": _bbox_bounds()})
        else:
            entry.update({"status": "error", "error": "non-image WMS response"})
    except Exception as exc:  # noqa: BLE001 - record any render failure
        entry["status"] = "error"
        entry["error"] = str(exc)[:200]
    reg = _load_registry()
    reg[fid] = entry
    _save_registry(reg)
    return entry


_RENDERER_THREAD = None


def start_nws_renderer():
    """Background loop: discover scans, persist descriptors, render pending."""
    global _RENDERER_THREAD
    if _RENDERER_THREAD and _RENDERER_THREAD.is_alive():
        return _RENDERER_THREAD

    def _worker():
        while True:
            try:
                frames = get_nws_frames()
                _save_descriptors(frames)
                reg = _prune_registry(_load_registry())
                _save_registry(reg)
                for fr in frames:
                    if reg.get(fr["id"], {}).get("status") != "done":
                        _render_nws_frame(fr)
            except Exception:  # noqa: BLE001 - worker must not crash the app
                pass
            time.sleep(120)

    _RENDERER_THREAD = threading.Thread(target=_worker, daemon=True, name="nws-radar-renderer")
    _RENDERER_THREAD.start()
    return _RENDERER_THREAD


def nws_bundle():
    """UI snapshot (no network): ready frames with static pngUrl + bounds."""
    start_nws_renderer()
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
                "kind": "nws",
                "id": d["id"],
                "label": d["label"],
                "time": d["time"],
                "pngUrl": f"/app/static/nws_radar/{d['id']}.png",
                "bounds": entry.get("bounds"),
            })
    return frames

"""GOES-19 GLM lightning (FED3 flash density over GeoColor) - NOAA STAR, no key.

STAR publishes 1250x750 CONUS JPGs every ~5 minutes:
  https://cdn.star.nesdis.noaa.gov/GOES19/GLM/CONUS/EXTENT3/{stamp}_GOES19-GLM-CONUS-EXTENT3-1250x750.jpg
stamp = YYYYDDDHHMM (UTC). Frames are map-composited (flashes drawn over the
GeoColor basemap), so serving = overlay the image on its sector bounds.

Rendering notes:
  - For the radar overlay we want flashes only, so the frame is projected to
    Web Mercator (STAR CONUS is linear in lat/lon across the sector box) and
    the near-black GeoColor background is dropped (alpha from darkness).
  - An East Tennessee crop is produced the same way for the mesoanalysis page.
"""
import datetime as dt
import io
import json
import os
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

CDN = "https://cdn.star.nesdis.noaa.gov/GOES19/GLM/CONUS/EXTENT3"
DIR_PAGE = "https://www.star.nesdis.noaa.gov/GOES/conus_band.php?sat=G19&band=EXTENT3&length=24"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0"}

# exact CONUS sector footprint (same as data.star_sat.BOUNDS): S, W, N, E
BOUNDS = [9.107992, -136.564392, 50.854072, -37.813320]
SIZE = (1250, 750)

# East TN box for the meso crop (matches data.meso.EAST_TN_BOX): S, N, W, E
ET_BOX = (34.3, 37.3, -85.8, -80.2)

FRAME_DIR = os.path.join("static", "glm")
REGISTRY_PATH = os.path.join(FRAME_DIR, "registry.json")
FRAME_COUNT = 12          # ~1 h of frames
KEEP_STAMPS = 20

_META = {"t": 0.0, "stamps": []}
_LOCK = threading.Lock()


def _stamp_now():
    return dt.datetime.now(dt.timezone.utc)


def _parse_stamp(s):
    return dt.datetime.strptime(s, "%Y%j%H%M").replace(tzinfo=dt.timezone.utc)


def _frame_url(stamp):
    return f"{CDN}/{stamp}_GOES19-GLM-CONUS-EXTENT3-1250x750.jpg"


def _load_registry():
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_registry(reg):
    os.makedirs(FRAME_DIR, exist_ok=True)
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f)
    os.replace(tmp, REGISTRY_PATH)


def list_stamps(max_n=24):
    """Recent GLM frame stamps, oldest first (2 min TTL)."""
    with _LOCK:
        if _META["stamps"] and time.time() - _META["t"] < 120:
            return _META["stamps"]
    stamps = []
    try:
        r = requests.get(DIR_PAGE, headers=UA, timeout=25)
        if r.ok:
            stamps = sorted(set(re.findall(
                r"(20\d{9})_GOES19-GLM-CONUS-EXTENT3-1250x750\.jpg", r.text)))
        if not stamps:
            r2 = requests.get(f"{CDN}/", headers=UA, timeout=25)
            if r2.ok:
                stamps = sorted(set(re.findall(
                    r"(20\d{9})_GOES19-GLM-CONUS-EXTENT3-1250x750\.jpg", r2.text)))[-60:]
    except requests.RequestException:
        pass
    if not stamps:
        stamps = list(_load_registry().keys())
    stamps = stamps[-max_n:]
    with _LOCK:
        _META["t"] = time.time()
        _META["stamps"] = stamps
    return stamps


def _fetch_frame(stamp, path):
    if os.path.exists(path) and os.path.getsize(path) > 20_000:
        return True
    try:
        r = requests.get(_frame_url(stamp), headers=UA, timeout=40)
        if r.ok and len(r.content) > 20_000:
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(r.content)
            os.replace(tmp, path)
            return True
    except requests.RequestException:
        pass
    return False


def _merc_y(lat):
    import math
    lat = max(min(lat, 85.0), -85.0)
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def render_frame(stamp, out_dir=FRAME_DIR):
    """Render one GLM frame to a transparent flashes-only PNG keyed to BOUNDS.

    STAR composites flashes (yellow-green -> violet density palette) over the
    GeoColor basemap. We isolate the flash palette (validated against real
    storms: blob centroids sit over Mexico/Gulf/SE-US convection), drop
    isolated JPEG-noise pixels, dilate for visibility, then project rows to
    Web Mercator y so an unprojected Leaflet imageOverlay aligns.
    """
    from PIL import Image
    import numpy as np
    from scipy.ndimage import binary_opening, binary_dilation
    os.makedirs(out_dir, exist_ok=True)
    fid = f"glm_{stamp}"
    png = os.path.join(out_dir, fid + ".png")
    reg = _load_registry()
    if os.path.exists(png) and reg.get(stamp, {}).get("status") == "done":
        return png
    raw = os.path.join(out_dir, f"_raw_{stamp}.jpg")
    if not _fetch_frame(stamp, raw):
        return None
    try:
        im = Image.open(raw).convert("RGB").resize(SIZE, Image.BILINEAR)
        arr = np.asarray(im).astype(int)
        R, G, B = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        sat = arr.max(axis=2) - arr.min(axis=2)
        # flash density palette: yellow-green low-blue core + violet tail
        flash = ((G >= 150) & (B <= G * 0.55) & (R <= G + 15))
        flash |= ((B > G + 40) & (R > 90) & (sat > 50))
        # kill isolated JPEG speckle; then grow the blobs back for visibility
        flash = binary_opening(flash, structure=np.ones((3, 3)))
        flash = binary_dilation(flash, structure=np.ones((3, 3)))
        # STAR CONUS images are linear in lat/lon across BOUNDS; make rows
        # linear in Mercator y so an unprojected Leaflet imageOverlay aligns.
        s, w, n, e = BOUNDS
        ys = [_merc_y(s + (n - s) * (i + 0.5) / SIZE[1]) for i in range(SIZE[1])]
        y_lo, y_hi = _merc_y(s), _merc_y(n)
        rows = [int((y - y_lo) / (y_hi - y_lo) * (SIZE[1] - 1)) for y in ys]
        rgb = np.asarray(im)[rows, :, :]
        alpha = (flash[rows, :][:, :] * 235).astype("uint8")
        alpha = alpha[::-1, :]                    # image rows run N->S in Leaflet space
        rgb = rgb[::-1, :, :]
        out = np.dstack([rgb, alpha]).astype("uint8")
        Image.fromarray(out).save(png)
        reg[stamp] = {"status": "done", "file": fid + ".png",
                      "url": f"../glm/{fid}.png", "bounds": BOUNDS,
                      "time": _parse_stamp(stamp).strftime("%Y-%m-%dT%H:%M:%SZ")}
        # keep the registry tight
        extra = sorted(reg)[:-KEEP_STAMPS]
        for old in extra:
            reg.pop(old, None)
            for p in (os.path.join(out_dir, f"glm_{old}.png"),
                      os.path.join(out_dir, f"_raw_{old}.jpg")):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        _save_registry(reg)
        return png
    except Exception:
        return None
    finally:
        if os.path.exists(raw):
            try:
                os.remove(raw)
            except OSError:
                pass


def refresh(max_frames=FRAME_COUNT):
    """Render the newest frames (a few per call). Returns stamps rendered."""
    stamps = list_stamps()[-max_frames:]
    newest = stamps[-6:]
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(render_frame, s): s for s in newest}
        for f in as_completed(futs):
            f.result()
    # older frames of the window fill in lazily, one per refresh
    for s in reversed(stamps[:-6]):
        if render_frame(s):
            break
    return stamps


def _row_to_lat(row):
    """PNG row (Leaflet order, N top, linear Mercator y) -> latitude."""
    import math
    s, w, n, e = BOUNDS
    y_hi, y_lo = _merc_y(n), _merc_y(s)
    y = y_hi - (row + 0.5) / SIZE[1] * (y_hi - y_lo)
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


def _col_to_lon(col):
    s, w, n, e = BOUNDS
    return w + (col + 0.5) / SIZE[0] * (e - w)


def storm_history(max_frames=FRAME_COUNT, min_cell_px=12):
    """Last hour of GLM flash activity, per storm, from the rendered frames.

    Counts flash pixels per 5-minute frame (whole CONUS), then groups the
    hour's flashes into individual storm cells via connected components on
    the union mask - so a cell that flickers between scans still gets one
    continuous history. Returns {asOf, frames, cells, totals} where each
    cell carries lat/lon, per-frame flash counts, peak, and a trend
    (Building / Steady / Fading from the last 3 frames vs the first 3).
    """
    import numpy as np
    from PIL import Image
    from scipy.ndimage import label

    reg = _load_registry()
    stamps = sorted(s for s, v in reg.items()
                    if v.get("status") == "done")[-max_frames:]
    masks, counts = {}, {}
    for s in stamps:
        p = os.path.join(FRAME_DIR, f"glm_{s}.png")
        if not os.path.exists(p):
            continue
        try:
            with Image.open(p) as im:
                a = np.asarray(im.split()[-1])          # alpha = flash mask
        except OSError:
            continue
        m = a > 64
        if m.any():
            masks[s] = m
            counts[s] = int(m.sum())
    if not counts:
        return {"asOf": None, "frames": [], "cells": [], "totals": {}}

    stamps = sorted(counts)
    labels, n_cells = None, 0
    if stamps:
        union = np.zeros_like(next(iter(masks.values())))
        for m in masks.values():
            union |= m
        labels, n_cells = label(union, structure=np.ones((3, 3)))

    cells = []
    if n_cells:
        ids, cnts = np.unique(labels[labels > 0], return_counts=True)
        for cid, area in zip(ids, cnts):
            if area < min_cell_px:
                continue
            rows, cols = np.nonzero(labels == cid)
            r0, c0 = int(rows.mean()), int(cols.mean())
            lat, lon = _row_to_lat(r0), _col_to_lon(c0)
            # per-frame flash count inside this cell's footprint
            per_frame = []
            cell_mask = labels == cid
            for s in stamps:
                per_frame.append(int((masks[s] & cell_mask).sum()))
            km_lat = 111.32 * (BOUNDS[2] - BOUNDS[0]) / SIZE[1]
            km_lon = 111.32 * (BOUNDS[3] - BOUNDS[1]) / SIZE[0] * max(
                0.2, __import__("math").cos(__import__("math").radians(lat)))
            radius = (area * km_lat * km_lon / 3.14159) ** 0.5
            recent = sum(per_frame[-3:]) / 3.0
            older = sum(per_frame[:3]) / 3.0
            trend = (recent / older) if older > 0 else (4.0 if recent > 0 else 1.0)
            cells.append({
                "id": int(cid), "lat": round(lat, 2), "lon": round(lon, 2),
                "radiusKm": round(min(radius, 90.0), 1),
                "now": per_frame[-1], "peak": max(per_frame),
                "trend": round(trend, 2), "history": per_frame,
                "active": per_frame[-1] > 0,
            })
    cells.sort(key=lambda c: -c["peak"])

    frames = [{"label": _parse_stamp(s).strftime("%H:%M"), "count": counts[s]}
              for s in stamps]
    latest = counts[stamps[-1]]
    return {
        "asOf": _parse_stamp(stamps[-1]).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frames": frames,
        "cells": cells[:10],
        "totals": {"latest": latest, "peak": max(counts.values()),
                   "storms": len(cells)},
    }


def bundle():
    """Payload for the maps: {frames: [{id,label,time,pngUrl,bounds}], bounds}."""
    try:
        refresh()
    except Exception:  # noqa: BLE001 - lightning is best-effort
        pass
    reg = _load_registry()
    frames = []
    for stamp in sorted(reg):
        info = reg[stamp]
        if info.get("status") != "done":
            continue
        t = _parse_stamp(stamp)
        frames.append({
            "id": f"glm_{stamp}",
            "label": t.strftime("%H:%M") + " LTG",
            "time": info.get("time"),
            "pngUrl": info.get("url"),
            "bounds": info.get("bounds") or BOUNDS,
        })
    return {"bounds": BOUNDS, "frames": frames[-FRAME_COUNT:]}


if __name__ == "__main__":
    import sys
    sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    b = bundle()
    print("frames:", len(b["frames"]), "| latest:", b["frames"][-1]["label"] if b["frames"] else "-")

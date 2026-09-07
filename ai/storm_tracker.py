"""AI-style storm-cell tracking from HRRR reflectivity grids.

Pipeline per frame: threshold dBZ -> connected components (scipy.ndimage.label)
-> centroid/intensity/area extraction -> cross-frame nearest-neighbour matching
with gating -> velocity estimation -> linear extrapolation of predicted positions.
"""
import datetime as dt
import math

import numpy as np
from scipy import ndimage

DBZ_THRESHOLD = 40.0   # storm-strength echoes
MIN_CELL_PX = 8        # ignore blobs smaller than this (downsampled grid px)
MIN_AREA_KM2 = 72.0    # min storm-cell footprint (km2)
MAX_MATCH_KM = 80.0    # max centroid jump between consecutive frames
PX_AREA_KM2 = 9.0      # 3km x 3km per downsampled grid px


def detect_cells(dbz, lons, lats, px_area_km2=PX_AREA_KM2):
    """Return list of cell dicts for one frame.

    Each: {'lon','lat','dbz_max','dbz_mean','area_km2','track_id'}
    px_area_km2: real-world area of one grid pixel (grows with downsampling).
    """
    mask = dbz >= DBZ_THRESHOLD
    if not mask.any():
        return []
    # Morphological closing to merge neighboring cores into single cells
    mask = ndimage.binary_closing(mask, structure=np.ones((3, 3)))
    labels, n = ndimage.label(mask)
    cells = []
    for i in range(1, n + 1):
        ys, xs = np.where(labels == i)
        if xs.size < MIN_CELL_PX:
            continue
        area_km2 = xs.size * px_area_km2
        if area_km2 < MIN_AREA_KM2:
            continue
        cells.append({
            "lon": float(np.mean(lons[ys, xs])),
            "lat": float(np.mean(lats[ys, xs])),
            "dbz_max": float(dbz[ys, xs].max()),
            "dbz_mean": float(dbz[ys, xs].mean()),
            "area_km2": float(area_km2),
        })
    return cells


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def track_cells(frame_cells, interval_minutes=60):
    """Match cells across frames; attach {'velocity': (dvlat, dvlondt)} in deg/h.

    frame_cells: list of frames, each the output of detect_cells() (chronological)
    Returns the input structure with velocity filled on matched cells.
    """
    for f_prev, f_cur in zip(frame_cells[:-1], frame_cells[1:]):
        used = set()
        for cell in f_cur:
            best, best_d = None, MAX_MATCH_KM
            for j, prev in enumerate(f_prev):
                if j in used or prev.get("velocity") is None and False:
                    continue
                d = _haversine_km(prev["lat"], prev["lon"], cell["lat"], cell["lon"])
                if d < best_d:
                    best, best_d = j, d
            if best is not None:
                used.add(best)
                prev = f_prev[best]
                dlat = cell["lat"] - prev["lat"]
                dlon = cell["lon"] - prev["lon"]
                cell["velocity"] = (dlat / interval_minutes * 60.0,
                                    dlon / interval_minutes * 60.0)
                cell["track_id"] = prev.get("track_id", f"t{abs(hash((prev['lat'], prev['lon']))) % 1000}")
            else:
                cell["track_id"] = f"t{abs(hash((cell['lat'], cell['lon']))) % 1000}"
    # first frame cells get fresh ids
    for cell in frame_cells[0] if frame_cells else []:
        cell.setdefault("track_id", f"t{abs(hash((cell['lat'], cell['lon']))) % 1000}")
    return frame_cells


def project_cells(cells, minutes_ahead):
    """Extrapolate cell centroids linearly by their velocity.

    Returns list of {'lon','lat','dbz_max','minutes'} for cells with velocity.
    """
    out = []
    for cell in cells:
        v = cell.get("velocity")
        if not v:
            continue
        dlat, dlon = v
        f = minutes_ahead / 60.0
        out.append({
            "lon": cell["lon"] + dlon * f,
            "lat": cell["lat"] + dlat * f,
            "dbz_max": cell["dbz_max"],
            "minutes": minutes_ahead,
        })
    return out


def summarize(cells):
    """Human-readable AI summary of the tracked field."""
    if not cells:
        return "No storm-scale echoes detected above 35 dBZ in the latest HRRR frame."
    strongest = max(cells, key=lambda c: c["dbz_max"])
    moving = [c for c in cells if c.get("velocity")]
    if moving:
        # mean motion vector (deg/h), convert to compass
        dlat = np.mean([c["velocity"][0] for c in moving])
        dlon = np.mean([c["velocity"][1] for c in moving])
        speed_kmh = math.hypot(dlat * 111.0, dlon * 111.0 * math.cos(math.radians(strongest["lat"])))
        bearing = (math.degrees(math.atan2(dlon, dlat)) + 360) % 360
        compass = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((bearing + 22.5) // 45) % 8]
        return (
            f"{len(cells)} storm cell(s) tracked; strongest {strongest['dbz_max']:.0f} dBZ. "
            f"Cells moving {compass} at ~{speed_kmh:.0f} km/h."
        )
    return f"{len(cells)} storm cell(s) tracked; strongest {strongest['dbz_max']:.0f} dBZ. Motion indeterminate (single frame)."

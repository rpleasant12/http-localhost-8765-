"""Severe composite (SCP / STP / EHI) sampled at East TN towns - HRRR.

The models page highlights which towns sit in severe-parameter air right
now and through the next ~12 h: every composite the severe walls render is
evaluated at each town's nearest HRRR gridpoint, and towns crossing an SPC
threshold (>= 1) are ranked for the "severe composite threat" card.

One HRRR wrfprsf file per hour carries every input (verified in the live
idx 2026-09-22): CAPE 90-0 mb, HLCY 3000-0 m, TMP/DPT 2 m, UGRD/VGRD
10 m + 500 mb - the same fields the scp/stp/ehi wall renders decode.
The same wall math is applied at a single point:

- SCP = (MUCAPE/1000) x (ESRH/50) x (EBWD/20), each capped at 1.5
- STP = (MUCAPE/1500) x (ESRH/150) x (EBWD/22.5) x lcl_term,
        lcl_term = max((2000 - LCLm)/1000, 0), LCL from the 2 m T/Td
        spread via the Espy 125 m/C lift
- EHI = MUCAPE x ESRH / 160000

EBWD is approximated with the |V500 - V10m| bulk shear exactly like the
walls. Results cache per (cycle, fh) in static/sev_towns/ so the payload
rebuild never re-downloads, and each cache entry expires after 12 h.
Best-effort throughout: any fetch failure returns an empty bundle and the
card says so rather than breaking the build.
"""
import datetime as dt
import json
import math
import os
import re
import time

import numpy as np

from data.model_maps import (_CANON, _decode_grib_bytes, _fetch_range,
                             _find_range, _get_idx_text, _idx_url,
                             _level_type, _target_level, find_cycle)

CACHE_DIR = "static/sev_towns"
CACHE_TTL = 12 * 3600.0
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# East TN towns (name -> lat, lon), same list the obs/city pages use.
TOWNS = {
    "Knoxville": (35.9606, -83.9207),
    "Chattanooga": (35.0456, -85.3097),
    "Tri-Cities": (36.3134, -82.3573),
    "Morristown": (36.0454, -83.2934),
    "Oak Ridge": (35.9903, -84.2853),
    "Maryville": (35.7570, -83.9743),
    "Cleveland": (35.1595, -84.8766),
    "Athens": (35.4429, -84.5988),
    "Cookeville": (36.1628, -85.5016),
    "Crossville": (35.9479, -85.0269),
    "Sevierville": (35.8720, -83.5757),
    "Greeneville": (36.1668, -82.8301),
    "Newport": (35.9645, -83.1115),
    "LaFollette": (36.3807, -84.1332),
    "Dayton": (35.5042, -85.0288),
    "Jellico": (36.5901, -84.1332),
    # Northeast Tennessee additions
    "Kingsport": (36.5484, -82.5618),
    "Johnson City": (36.3134, -82.1981),
    "Elizabethton": (36.3487, -82.2290),
    "Mountain City": (36.1851, -81.8050),
    "Butler": (36.2512, -81.9256),
    # Southwest Virginia
    "Bristol VA": (36.5951, -82.1887),
    "Abingdon": (36.7098, -81.9773),
    "Lebanon VA": (36.9034, -82.0762),
    "Grundy": (37.2068, -82.0979),
    "Marion VA": (36.7582, -81.5143),
    "Tazewell VA": (37.1218, -81.5287),
    "Wytheville": (36.9460, -81.0827),
    "Galax": (36.6618, -80.9254),
    "Norton": (36.9682, -82.6254),
    # Western North Carolina
    "Asheville": (35.5951, -82.5515),
    "Boone": (36.2118, -81.6878),
    "Hendersonville": (35.3187, -82.4609),
    "Waynesville": (35.4860, -82.9990),
    "Murphy": (35.0257, -84.0323),
    "Burnsville": (35.9157, -82.2988),
    "Sylva": (35.3735, -83.2243),
    "Franklin NC": (35.1823, -83.3815),
    "Robbinsville": (35.3226, -83.8065),
}

# (product, threshold, plain-English meaning)
THRESHOLDS = (
    ("stp", 1.0, "tornado-favorable"),
    ("scp", 1.0, "supercell-favorable"),
    ("ehi", 1.0, "supercell-favorable"),
)

_VARS = [("CAPE", "90-0 mb above ground"), ("HLCY", "3000-0 m"),
         ("TMP", "2 m above ground"), ("DPT", "2 m above ground"),
         ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"),
         ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb")]


def _fetch_point_fields(cycle, fh):
    """All _VARS fields + lat/lon for one HRRR hour, or None.

    The idx text comes through _get_idx_text (the walls' own helper - it
    retries and caches, dodging the S3 bucket's flaky truncated-index
    responses a bare GET hits).
    """
    idx_text = _get_idx_text("HRRR", cycle, fh, "scp")
    if not idx_text or idx_text.count(":") < 50:
        return None
    # the GRIB URL derives from the walls' own idx builder (strip .idx) -
    # HRRR file names have NO dot before the f-hour (wrfprsf06.grib2), so a
    # hand-built f{fh:02d} URL 404s every range
    base_url = _idx_url("HRRR", cycle, fh, "scp")[:-4]
    alias = {"CAPE": "CAPE", "HLCY": "HLCY", "TMP": "TMP", "DPT": "DPT",
             "UGRD": "UGRD", "VGRD": "VGRD"}
    groups = {}
    for short, level in _VARS:
        base = short.split("@")[0]
        rng = _find_range(idx_text, alias.get(base, base), level)
        if rng is None:
            continue
        start, end = rng
        g = groups.setdefault(start, {"end": end, "level": level, "shorts": []})
        g["end"] = max(g["end"], end)
        g["shorts"].append(short)
    if not groups:
        return None
    fields, lat, lon = {}, None, None
    for g_start, g in groups.items():
        try:
            blob = _fetch_range(base_url, g_start, g["end"])
            if blob is None:
                continue
            decoded, lat2, lon2 = _decode_grib_bytes(
                blob, type_of_level=_level_type(g["level"]),
                target_level=_target_level(g["level"]))
        except Exception:  # noqa: BLE001 - one bad message skips
            continue
        for sn, values in decoded.items():
            # cfgrib's native names (2T/10U/...) canonicalize exactly like
            # the walls' fetch (2T -> TMP etc.) - raw 'TMP' idx lookups
            # decode as '2T' and would never land under the renderer key
            canonical = _CANON.get(sn, sn)
            for s in g["shorts"]:
                if s.split("@")[0] == canonical:
                    fields[s] = values
        lat, lon = lat2, lon2
    if lat is None or "CAPE" not in fields or "HLCY" not in fields:
        return None
    return {"fields": fields, "lat": lat, "lon": lon}


def _nearest(data, la, lo):
    """(row, col) of the nearest gridpoint to (lat, lon).

    The decoder returns coordinates in TWO layouts depending on the file:
    per-point FLAT arrays (1.9M elements, one per grid cell) or 1-D AXES
    (rows / cols). A naive (lat-la)**2 + (lon-lo)**2 on the axes case
    broadcasts them into a bogus distance matrix whose argmin lands in
    Canada (lat 47.8, lon 225.9) for every town; a meshgrid on the flat
    case tries to allocate 26 TiB. Handle both: match the field's own
    (rows, cols) shape and choose flat-argmin vs meshgrid accordingly.
    """
    f0 = np.asarray(next(iter(data["fields"].values())))
    if f0.ndim > 2:      # degenerate leading dims -> first 2-D slice
        f0 = f0.reshape(-1, *f0.shape[-2:])[0]
    rows, cols = f0.shape
    lat = np.asarray(data["lat"], dtype=float).ravel()
    lon = np.asarray(data["lon"], dtype=float).ravel()
    if lon.max() > 180:  # 0-360 longitudes -> -180..180
        lon = np.where(lon > 180, lon - 360, lon)
    if lat.size == rows * cols:
        # per-point coordinate grids (flat or flattened 2-D)
        d = (lat - la) ** 2 + (lon - lo) ** 2
        flat = int(np.argmin(d))
        return flat // cols, flat % cols
    lon2d, lat2d = np.meshgrid(lon, lat)   # 1-D axes -> 2-D grid
    d = (lat2d - la) ** 2 + (lon2d - lo) ** 2
    return tuple(int(x) for x in np.unravel_index(np.argmin(d), d.shape))


def _composites(f, rc):
    """SCP/STP/EHI at gridpoint rc=(row, col), mirroring the wall formulas."""
    r, c = rc

    def val(k):
        v = np.asarray(f[k])
        if v.ndim > 2:      # degenerate leading dims -> first 2-D slice
            v = v.reshape(-1, *v.shape[-2:])[0]
        return float(v[r, c])

    cape = val("CAPE")
    srh = val("HLCY")
    u10, v10 = val("UGRD"), val("VGRD")
    u5, v5 = val("UGRD@500 mb"), val("VGRD@500 mb")
    shear = math.hypot(u5 - u10, v5 - v10) * 1.94384
    scp = (min(cape / 1000.0, 1.5) * min(srh / 50.0, 1.5)
           * min(shear / 20.0, 1.5))
    t2, td2 = val("TMP"), val("DPT")
    lcl_m = min(max(125.0 * (t2 - td2), 0.0), 4000.0)
    lcl_term = min(max((2000.0 - lcl_m) / 1000.0, 0.0), 1.5)
    stp = (min(cape / 1500.0, 1.5) * min(srh / 150.0, 1.5)
           * min(shear / 22.5, 1.5) * lcl_term)
    ehi = cape * srh / 160000.0
    return {"scp": round(scp, 2), "stp": round(stp, 2), "ehi": round(ehi, 2)}


def _cache_path(cycle, fh):
    return os.path.join(CACHE_DIR, f"hrrr_{cycle:%Y%m%d%H}_f{fh:02d}.json")


def _load_cache(cycle, fh):
    p = _cache_path(cycle, fh)
    try:
        if time.time() - os.path.getmtime(p) < CACHE_TTL:
            with open(p, encoding="utf-8") as fhj:
                return json.load(fhj)
    except (OSError, ValueError):
        pass
    return None


def _save_cache(cycle, fh, rows):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(cycle, fh), "w", encoding="utf-8") as fhj:
            json.dump(rows, fhj)
    except OSError:
        pass


def hour_rows(cycle, fh, data=None):
    """Rows for one HRRR hour: [{town, scp, stp, ehi}] - cached.

    Cycle fallback mirrors the walls' render_product_map: the newest cycle
    is often mid-publish (its f06 not out yet), so the same fh from the
    previous 1-3 cycles renders instead of nothing.
    """
    cached = _load_cache(cycle, fh)
    if cached is not None:
        return cached
    if data is None:
        for back in range(0, 4):
            data = _fetch_point_fields(cycle - dt.timedelta(hours=back), fh)
            if data is not None:
                break
    if data is None:
        return None
    rows = []
    for name, (la, lo) in sorted(TOWNS.items()):
        try:
            vals = _composites(data["fields"], _nearest(data, la, lo))
            vals["town"] = name
            rows.append(vals)
        except Exception:  # noqa: BLE001 - one bad point skips
            continue
    if rows:
        _save_cache(cycle, fh, rows)
    return rows or None


def severe_towns_bundle(max_hours=13, min_lead=1):
    """Payload block: ranked threshold-crossing towns over the next ~12 h.

    Returns {'generated', 'cycle', 'windows': [...], 'ranked': [...],
    'note'} - `windows` is a plain-English summary per crossing hour,
    `ranked` is the town ranking (peak value + which composites fired).
    """
    try:
        cycle = find_cycle("HRRR")
    except Exception:  # noqa: BLE001
        return {"ok": False, "ranked": [], "windows": [],
                "note": "HRRR cycle unavailable"}
    now = time.time()
    windows, seen = [], {}
    fh_scan = []
    for fh in range(min_lead, min_lead + max_hours):
        # the newest cycle's fh=1 valid time is already past by the time the
        # payload builds (the 01Z run publishes ~02:30Z); start at the first
        # hour still in the future so the card never says "in ~-1 h"
        if (cycle + dt.timedelta(hours=fh)).timestamp() <= now + 900:
            continue
        fh_scan.append(fh)
    for fh in fh_scan:
        try:
            rows = hour_rows(cycle, fh)
        except Exception:  # noqa: BLE001
            rows = None
        if not rows:
            continue
        valid = (cycle + dt.timedelta(hours=fh)).timestamp()  # VALID time, not init
        for r in rows:
            hits = [(p, t, m) for p, t, m in THRESHOLDS if r.get(p, 0) >= t]
            if not hits:
                continue
            w = seen.setdefault(r["town"], {"town": r["town"], "first": valid,
                                            "last": valid, "peaks": {},
                                            "hits": set()})
            w["last"] = max(w["last"], valid)
            w["hits"].update(p for p, _, _ in hits)
            for p, _, _ in hits:
                w["peaks"][p] = max(w["peaks"].get(p, 0.0), r[p])
    ranked = []
    for w in seen.values():
        peak = max(w["peaks"].items(), key=lambda kv: (kv[1], kv[0] == "stp"))
        ranked.append({
            "town": w["town"],
            "peakProd": peak[0], "peakVal": round(peak[1], 2),
            "hits": sorted(w["hits"]),
            "peaks": {k: round(v, 2) for k, v in w["peaks"].items()},
            "firstHour": int((w["first"] - now) // 3600) + 1,
            "lastHour": int((w["last"] - now) // 3600) + 1,
        })
    ranked.sort(key=lambda r: (-r["peakVal"], r["town"]))
    windows_txt = []
    for r in ranked[:6]:
        f0 = max(1, r["firstHour"])
        hrs = (f"in ~{f0} h" if f0 <= 1
               else f"in ~{f0}-{max(r['lastHour'], f0)} h")
        parts = {"scp": "supercell", "stp": "tornado", "ehi": "supercell"}
        kinds = sorted({parts.get(p, p) for p in r["hits"]})
        windows_txt.append(f"{r['town']}: {r['peakProd'].upper()} {r['peakVal']:.1f} ({'/'.join(kinds)}) {hrs}")
    return {
        "ok": True,
        "cycle": f"{cycle:%Y%m%d%H}",
        "generated": time.strftime("%Y-%m-%d %H:%M ET", time.localtime()),
        "ranked": ranked,
        "windows": windows_txt,
        "note": (f"{len(ranked)} town(s) in severe-parameter air"
                 if ranked else
                 "no towns cross SCP/STP/EHI >= 1 in the next ~12 h"),
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    import pprint
    pprint.pprint(severe_towns_bundle())

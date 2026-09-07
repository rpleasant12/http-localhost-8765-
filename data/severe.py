"""Severe-weather service: SPC outlooks, HRRR hail/rotation, statewide alerts.

Sources (all keyless):
- SPC Day 1/2 convective outlooks + hail/tornado probability GeoJSON
  (spc.noaa.gov), with point-in-polygon risk lookup for the selected location.
- HRRR hail diameter (HAIL, mm) and updraft helicity (UPHL, m2/s2) maxima from
  NOAA's AWS open-data bucket - decoded for the current cycle's first hours and
  clustered into hail / rotation signature hotspots.
- NWS statewide active-alert feed (api.weather.gov/alerts/active?area=TN) for
  the ticker and official watch/warning polygons.
"""
import datetime as dt

import numpy as np
import requests

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
SPC_BASE = "https://www.spc.noaa.gov/products/outlook"
HRRR_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"

# Official SPC categorical + probability colors
SPC_COLORS = {
    "TSTM": "#c1e9c1", "MRGL": "#66cdaa", "SLGT": "#ffff00",
    "ENH": "#ff8c00", "MDT": "#ff0000", "HIGH": "#ff00ff",
    "0.05": "#adff2f", "0.15": "#ffff00", "0.30": "#ff8c00",
    "0.45": "#ff0000", "0.60": "#ff00ff",
}
CAT_ORDER = ["TSTM", "MRGL", "SLGT", "ENH", "MDT", "HIGH"]


def _fetch_spc(product):
    try:
        r = requests.get(f"{SPC_BASE}/{product}.nolyr.geojson", headers=UA, timeout=15)
        if not r.ok:
            return None
        d = r.json()
    except (requests.RequestException, ValueError):
        return None
    feats = []
    for f in d.get("features", []):
        p = f.get("properties", {})
        feats.append({
            "label": p.get("LABEL"),
            "label2": p.get("LABEL2") or p.get("LABEL"),
            "fill": SPC_COLORS.get(p.get("LABEL"), "#888888"),
            "geometry": f.get("geometry"),
            "expire": p.get("EXPIRE"),
        })
    meta = {
        "product": product,
        "issue": (d.get("features") or [{}])[0].get("properties", {}).get("ISSUE"),
        "valid": (d.get("features") or [{}])[0].get("properties", {}).get("VALID"),
        "expire": (d.get("features") or [{}])[0].get("properties", {}).get("EXPIRE"),
    }
    return {"features": feats, "meta": meta}


def spc_outlooks():
    """Day1-3 categorical and Day1 hail/tornado outlook GeoJSON."""
    out = {}
    for key, product in (
        ("day1", "day1otlk_cat"), ("day2", "day2otlk_cat"), ("day3", "day3otlk_cat"),
        ("day1_hail", "day1otlk_hail"), ("day1_torn", "day1otlk_torn"),
    ):
        out[key] = _fetch_spc(product)
    return out


def _pip(lat, lon, geom):
    """Point-in-polygon (ray casting) for GeoJSON Polygon/MultiPolygon."""

    def in_ring(x, y, ring):
        inside = False
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
                inside = not inside
            j = i
        return inside

    try:
        if not geom:
            return False
        if geom["type"] == "Polygon":
            return in_ring(lon, lat, geom["coordinates"][0])
        if geom["type"] == "MultiPolygon":
            return any(in_ring(lon, lat, poly[0]) for poly in geom["coordinates"])
    except (KeyError, IndexError, TypeError):
        return False
    return False


def spc_risk_at(lat, lon, outlooks=None):
    """Highest SPC risk category at a point: {'cat', 'hail', 'torn', 'summary'}."""
    outlooks = outlooks or spc_outlooks()
    result = {"cat": None, "hail": None, "torn": None}
    d1 = outlooks.get("day1")
    if d1:
        best = -1
        for f in d1["features"]:
            if _pip(lat, lon, f["geometry"]):
                rank = CAT_ORDER.index(f["label"]) if f["label"] in CAT_ORDER else -1
                if rank >= best:
                    best = rank
                    result["cat"] = f
    for key, field in (("day1_hail", "hail"), ("day1_torn", "torn")):
        src = outlooks.get(key)
        if not src:
            continue
        prob = 0.0
        for f in src["features"]:
            try:
                val = float(str(f["label"]).replace("%", ""))
            except ValueError:
                continue
            if val >= prob and _pip(lat, lon, f["geometry"]):
                prob = val
                result[field] = f
    bits = []
    if result["cat"]:
        bits.append(result["cat"]["label2"] or result["cat"]["label"])
    if result["hail"]:
        bits.append(f"{result['hail']['label']}% hail")
    if result["torn"]:
        bits.append(f"{result['torn']['label']}% tornado")
    result["summary"] = " \u00b7 ".join(bits) if bits else "No severe risk area drawn for this spot"
    return result


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


def _hotspots(values, lon, lat, threshold, min_px=2, limit=10):
    """Cluster grid cells above threshold into signature points."""
    from scipy import ndimage

    mask = np.isfinite(values) & (values >= threshold)
    if not mask.any():
        return []
    lab, n = ndimage.label(mask)
    out = []
    for i in range(1, n + 1):
        ys, xs = np.where(lab == i)
        if len(ys) < min_px:
            continue
        k = int(np.argmax(values[ys, xs]))
        out.append({
            "lat": round(float(lat[ys[k], xs[k]]), 3),
            "lon": round(float(lon[ys[k], xs[k]]), 3),
            "peak": round(float(values[ys[k], xs[k]]), 1),
            "cells": int(len(ys)),
        })
    out.sort(key=lambda d: -d["peak"])
    return out[:limit]


def hrrr_severe(lat=None, lon=None, hours=4, max_px=1000):
    """HRRR hail/rotation maxima for the first `hours` of the latest cycle.

    Returns {'cycle', 'hail_max_mm', 'hail_time', 'uphl_max', 'uphl_time',
             'hail_points', 'rot_points', 'frames': [{'time','hail','uphl'}]}
    """
    from data.models import _decode_blob, _sample_point  # reuse decode/sample

    now = dt.datetime.now(dt.timezone.utc)
    cycle = None
    for back in range(0, 6):
        c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        try:
            probe = requests.head(
                f"{HRRR_BUCKET}/hrrr.{c:%Y%m%d}/conus/hrrr.t{c:%H}z.wrfsfcf01.grib2.idx",
                headers=UA, timeout=12,
            )
            if probe.status_code == 200:
                cycle = c
                break
        except requests.RequestException:
            continue
    if cycle is None:
        return {"error": "HRRR unavailable"}

    base = f"{HRRR_BUCKET}/hrrr.{cycle:%Y%m%d}/conus/hrrr.t{cycle:%H}z.wrfsfcf"
    frames, hail_best, uphl_best = [], (0.0, None), (0.0, None)
    hail_all, rot_all = [], []
    step = 3
    from pyproj import Transformer  # grid coords at downsample, matching values
    import io

    for fh in range(1, hours + 1):
        try:
            idx = requests.get(f"{base}{fh:02d}.grib2.idx", headers=UA, timeout=15)
            if not idx.ok:
                continue
            hail_rng = _find_range(idx.text, "HAIL", "entire atmosphere") or _find_range(idx.text, "HAIL")
            uphl_rng = _find_range(idx.text, "UPHL", "entire atmosphere") or _find_range(idx.text, "UPHL")
            url = f"{base}{fh:02d}.grib2"
            valid = cycle + dt.timedelta(hours=fh)
            row = {"time": valid.strftime("%Y-%m-%dT%H:%M:%SZ"), "hail": None, "uphl": None}
            for tag, rng, thr in (("hail", hail_rng, 19.0), ("uphl", uphl_rng, 50.0)):
                if rng is None:
                    continue
                r = requests.get(url, headers={**UA, "Range": f"bytes={rng[0]}-{rng[1] - 1}"}, timeout=60)
                if not r.ok:
                    continue
                values, glat, glon = _decode_blob(r.content, level_sub="entire atmosphere")
                values = values[::step, ::step]
                glat, glon = glat[::step, ::step], glon[::step, ::step]
                if glon.max() > 180:
                    glon = np.where(glon > 180, glon - 360, glon)
                peak = float(np.nanmax(values))
                row[tag] = round(peak, 1)
                if tag == "hail" and peak > hail_best[0]:
                    hail_best = (peak, row["time"])
                if tag == "uphl" and peak > uphl_best[0]:
                    uphl_best = (peak, row["time"])
                # statewide signature hotspots (TN box, throttled thresholds)
                box = (glat > 34.8) & (glat < 36.9) & (glon > -90.4) & (glon < -81.6)
                if box.any():
                    v = np.where(box, values, np.nan)
                    thr_eff = 25.0 if tag == "hail" else 130.0
                    pts = _hotspots(v, glon, glat, thr_eff)
                    for p in pts:
                        p["kind"] = "hail" if tag == "hail" else "rotation"
                        p["time"] = row["time"]
                    (hail_all if tag == "hail" else rot_all).extend(pts)
                if lat is not None and lon is not None:
                    pv = _sample_point(glat, glon, values, lat, lon)
                    if pv is not None:
                        row[f"{tag}_here"] = round(pv, 1)
            frames.append(row)
        except Exception:  # noqa: BLE001 - one bad hour must not kill the summary
            continue

    hail_all.sort(key=lambda p: -p["peak"])
    rot_all.sort(key=lambda p: -p["peak"])
    return {
        "cycle": cycle.strftime("%Y-%m-%d %H:%M UTC"),
        "hail_max_mm": hail_best[0],
        "hail_time": hail_best[1],
        "uphl_max": uphl_best[0],
        "uphl_time": uphl_best[1],
        "hail_points": hail_all[:10],
        "rot_points": rot_all[:10],
        "frames": frames,
    }


def tn_alerts():
    """All active NWS alerts for Tennessee, slimmed for ticker + map polygons."""
    try:
        r = requests.get(
            "https://api.weather.gov/alerts/active?area=TN",
            headers={**UA, "Accept": "application/geo+json"}, timeout=20,
        )
        if not r.ok:
            return []
        feats = r.json().get("features", [])
    except (requests.RequestException, ValueError):
        return []
    out = []
    for f in feats:
        p = f.get("properties", {})
        out.append({
            "event": p.get("event") or "Alert",
            "severity": p.get("severity") or "Unknown",
            "areaDesc": p.get("areaDesc") or "",
            "headline": p.get("headline") or "",
            "expires": (p.get("expires") or "")[:16].replace("T", " ") + "Z"
            if p.get("expires") else "",
            "geometry": f.get("geometry"),
        })
    # worst first for the ticker
    rank = {"Tornado Warning": 0, "Severe Thunderstorm Warning": 1, "Flash Flood Warning": 2}
    out.sort(key=lambda a: (rank.get(a["event"], 5 if "Warning" not in a["event"] else 3), a["event"]))
    return out

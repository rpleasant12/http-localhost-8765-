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
import os
import time

import numpy as np
import requests

from data import _tz
from data.national import us_warnings

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
SPC_BASE = "https://www.spc.noaa.gov/products/outlook"
HRRR_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"

# Official SPC categorical + probability colors
SPC_COLORS = {
    "TSTM": "#c1e9c1", "MRGL": "#66cdaa", "SLGT": "#ffff00",
    "ENH": "#ff8c00", "MDT": "#ff0000", "HIGH": "#ff00ff",
}
# probability palettes are per-hazard (SPC uses different ramps)
SPC_TORN_COLORS = {
    "0.02": "#adff2f", "0.05": "#32cd32", "0.10": "#ffff00",
    "0.15": "#ff8c00", "0.30": "#ff0000", "0.45": "#ff00ff", "0.60": "#c71585",
}
SPC_PROB_COLORS = {  # hail + wind share one ramp
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
    palette = (SPC_TORN_COLORS if "torn" in product
               else SPC_PROB_COLORS if "hail" in product or "wind" in product
               else SPC_COLORS)
    feats = []
    for f in d.get("features", []):
        p = f.get("properties", {})
        feats.append({
            "label": p.get("LABEL"),
            "label2": p.get("LABEL2") or p.get("LABEL"),
            "fill": palette.get(p.get("LABEL"), "#888888"),
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
    """SPC outlook GeoJSON: Day1-3 categorical + Day1-2 tornado/hail/wind probs."""
    out = {}
    for key, product in (
        ("day1", "day1otlk_cat"), ("day2", "day2otlk_cat"), ("day3", "day3otlk_cat"),
        ("day1_hail", "day1otlk_hail"), ("day1_torn", "day1otlk_torn"),
        ("day1_wind", "day1otlk_wind"), ("day2_hail", "day2otlk_hail"),
        ("day2_torn", "day2otlk_torn"), ("day2_wind", "day2otlk_wind"),
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
    """Highest SPC risk at a point: {'cat','hail','torn','wind','summary'}.

    Probability labels are fractions (0.02 = 2%), so they are converted to
    whole percents for display.
    """
    outlooks = outlooks or spc_outlooks()
    result = {"cat": None, "hail": None, "torn": None, "wind": None}
    d1 = outlooks.get("day1")
    if d1:
        best = -1
        for f in d1["features"]:
            if _pip(lat, lon, f["geometry"]):
                rank = CAT_ORDER.index(f["label"]) if f["label"] in CAT_ORDER else -1
                if rank >= best:
                    best = rank
                    result["cat"] = f
    for key, field in (("day1_hail", "hail"), ("day1_torn", "torn"), ("day1_wind", "wind")):
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
        bits.append(f"{float(result['hail']['label']) * 100:g}% hail")
    if result["torn"]:
        bits.append(f"{float(result['torn']['label']) * 100:g}% tornado")
    if result["wind"]:
        bits.append(f"{float(result['wind']['label']) * 100:g}% wind")
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


def hrrr_severe(lat=None, lon=None, hours=4, max_px=1000, cities=None):
    """HRRR hail/rotation maxima for the first `hours` of the latest cycle.

    Returns {'cycle', 'hail_max_mm', 'hail_time', 'uphl_max', 'uphl_time',
             'hail_points', 'rot_points', 'frames': [{'time','hail','uphl'}]}

    With cities={name: (lat, lon)}, each HAIL frame also samples every city
    (row['cities']) and records the East-TN-box max (row['tn_max']) - the
    inputs for the site's hail forecast card.
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
            # HRRR publishes the hourly max as MXUPHL (exact-name "UPHL" never matches)
            uphl_rng = (_find_range(idx.text, "MXUPHL") or _find_range(idx.text, "UPHL", "entire atmosphere")
                        or _find_range(idx.text, "UPHL"))
            url = f"{base}{fh:02d}.grib2"
            valid = cycle + dt.timedelta(hours=fh)
            row = {"time": valid.strftime("%Y-%m-%dT%H:%M:%SZ"), "hail": None,
               "uphl": None, "cities": {}, "tn_max": None}
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
                if tag == "hail":
                    box_all = (glat > 34.8) & (glat < 36.9) & (glon > -90.4) & (glon < -81.6)
                    if box_all.any():
                        row["tn_max"] = round(
                            float(np.nanmax(np.where(box_all, values, np.nan))), 1)
                else:
                    box_all = (glat > 34.8) & (glat < 36.9) & (glon > -90.4) & (glon < -81.6)
                    if box_all.any():
                        row["tn_max_uphl"] = round(
                            float(np.nanmax(np.where(box_all, values, np.nan))), 1)
                if cities:
                    for cname, (clat, clon) in cities.items():
                        pv = _sample_point(glat, glon, values, clat, clon)
                        if pv is not None:
                            row["cities"].setdefault(cname, {})[tag] = round(pv, 1)
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
        "cycle": __import__('data._tz', fromlist=['full']).full(cycle),
        "hail_max_mm": hail_best[0],
        "hail_time": hail_best[1],
        "uphl_max": uphl_best[0],
        "uphl_time": uphl_best[1],
        "hail_points": hail_all[:10],
        "rot_points": rot_all[:10],
        "frames": frames,
    }


_HAIL_CACHE = {"at": 0.0, "data": None}
_HAIL_TTL = 900          # 15 min: forecast is cycle-based, no need to hammer
_HAIL_CITIES = {
    "Greeneville": (36.1627, -82.8332),
    "Knoxville": (35.9606, -83.9207),
    "Tri-Cities": (36.3134, -82.3573),
    "Morristown": (36.0454, -83.2934),
    "Oak Ridge": (35.9903, -84.2853),
    "Chattanooga": (35.0456, -85.3097),
    "Cookeville": (36.1629, -85.5016),
    "Crossville": (35.9479, -85.0269),
}


def _hail_cat(mm):
    """SPC-style severe-hail size categories from HRRR HAIL (mm)."""
    if mm is None or mm < 6.4:
        return None
    if mm < 19:
        return ("small", "pea to penny", "#aed581")
    if mm < 25:
        return ("3/4-1 in", "severe threshold", "#ffd54f")
    if mm < 45:
        return ("1-1.75 in", "quarter to golf ball", "#ff9f43")
    if mm < 70:
        return ("1.75-2.75 in", "golf ball to tennis ball", "#ff5252")
    return ("2.75+ in", "tennis ball and larger", "#e040fb")


def _uphl_cat(v):
    """Rotation/tornado categories from HRRR UPHL (updraft helicity, m2/s2).

    UPHL is the standard HRRR tornado proxy: 2-5 km rotation strength of
    the strongest updraft. 130+ marks mesocyclone-strength rotation (the
    same threshold the hotspot tracker uses); 250+ is a genuine tornado
    threat signal.
    """
    if v is None or v < 25:
        return None
    if v < 75:
        return ("weak rotation", "#aed581")
    if v < 130:
        return ("rotation", "#ffd54f")
    if v < 250:
        return ("strong rotation - tornado possible", "#ff9f43")
    return ("TORNADO THREAT", "#e040fb")


def severe_forecast(hours=8):
    """East-TN hail + tornado-proxy (UPHL) forecast for the site (15-min cache).

    Returns {'ok', 'cycle', 'hours': [{'time','tn_max','cat','catColor',
             'tn_uphl','ucat','ucatColor','cities'}...],
             'peak': {...hail...}, 'peakRot': {...rotation...},
             'cities': {name: {'hail','hcat','uphl','ucat','time'}}}
    or {'ok': False, 'reason': ...}.
    """
    now = time.time()
    c = _HAIL_CACHE.get("data")
    if c and c.get("ok") and now - _HAIL_CACHE["at"] < _HAIL_TTL:
        return c
    try:
        from data.observations import EAST_TN_CITIES
        sev = hrrr_severe(hours=hours, cities=dict(EAST_TN_CITIES))
    except Exception as exc:      # noqa: BLE001 - degrade, never break the site
        return {"ok": False, "reason": str(exc)[:120]}
    if sev.get("error") or not sev.get("frames"):
        return {"ok": False, "reason": sev.get("error") or "no HRRR frames"}

    hours_out = []
    city_peak = {}               # name -> {'hail':(mm,time), 'uphl':(v,time)}
    pk_hail, pk_uphl = (0.0, None), (0.0, None)
    for row in sev["frames"]:
        h_tn, u_tn = row.get("tn_max"), row.get("tn_max_uphl")
        if row.get("hail") is None and h_tn is None and u_tn is None \
                and row.get("uphl") is None:
            continue
        hval = h_tn if h_tn is not None else (row.get("hail") or 0)
        uval = u_tn if u_tn is not None else (row.get("uphl") or 0)
        hcat, ucat = _hail_cat(hval), _uphl_cat(uval)
        hours_out.append({
            "time": row["time"],
            "tn_max": h_tn, "cat": hcat[0] if hcat else None,
            "catColor": hcat[2] if hcat else "#81c784",
            "tn_uphl": u_tn, "ucat": ucat[0] if ucat else None,
            "ucatColor": ucat[1] if ucat else "#81c784",
            "cities": row.get("cities") or {},
        })
        if hval > pk_hail[0]:
            pk_hail = (hval, row["time"])
        if uval > pk_uphl[0]:
            pk_uphl = (uval, row["time"])
        for cname, cv in (row.get("cities") or {}).items():
            e = city_peak.setdefault(cname, {"hail": (0, ""), "uphl": (0, "")})
            hm = cv.get("hail")
            if hm is not None and hm > e["hail"][0]:
                e["hail"] = (hm, row["time"])
            um = cv.get("uphl")
            if um is not None and um > e["uphl"][0]:
                e["uphl"] = (um, row["time"])

    _pc, _pu = _hail_cat(pk_hail[0]), _uphl_cat(pk_uphl[0])
    result = {
        "ok": bool(hours_out),
        "cycle": sev.get("cycle"),
        "hours": hours_out,
        "peak": {"mm": pk_hail[0], "time": pk_hail[1],
                 "cat": _pc[0] if _pc else None,
                 "catColor": _pc[2] if _pc else "#81c784"},
        "peakRot": {"val": pk_uphl[0], "time": pk_uphl[1],
                    "cat": _pu[0] if _pu else None,
                    "catColor": _pu[1] if _pu else "#81c784"},
        "cities": {name: {"hail": v["hail"][0],
                          "hcat": (_hail_cat(v["hail"][0]) or (None,))[0],
                          "uphl": v["uphl"][0],
                          "ucat": (_uphl_cat(v["uphl"][0]) or (None, None))[0],
                          "time": v["hail"][1] or v["uphl"][1]}
                   for name, v in city_peak.items()},
    }
    result["spc48"] = _mirror_spc_extended()
    result["nationwide"] = national_alerts()
    if result["ok"]:
        _HAIL_CACHE["at"], _HAIL_CACHE["data"] = now, result
    return result


def _mirror_spc_extended():
    """SPC Days 4-8 severe probability graphic, mirrored for the public site.

    The day 1-3 outlooks on this page are polygons; days 4-8 SPC publishes
    as a single experimental probability gif (severe = any of tornado/wind/
    hail). It bridges the gap between the 8-hour HRRR forecast card and the
    week-2 CPC outlooks, so a quiet-looking week 1 with a building day 6-8
    signal is visible on the site. Re-fetched hourly.
    """
    out_dir = os.path.join("static", "severe")
    os.makedirs(out_dir, exist_ok=True)
    fn = "spc_day48prob.gif"
    dest = os.path.join(out_dir, fn)
    try:
        if not (os.path.exists(dest) and os.path.getsize(dest) > 5_000
                and time.time() - os.stat(dest).st_mtime < 3_600):
            r = requests.get(
                "https://www.spc.noaa.gov/products/exper/day4-8/day48prob.gif",
                headers=UA, timeout=30)
            if r.ok and r.content[:3] in (b"GIF", b"\xff\xd8\xff", b"\x89PN"):
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, dest)
        if os.path.exists(dest) and os.path.getsize(dest) > 5_000:
            return {"ok": True, "file": fn, "url": f"/app/static/severe/{fn}",
                    "source": "https://www.spc.noaa.gov/products/exper/day4-8/"}
    except Exception:                              # noqa: BLE001
        pass
    return {"ok": False}


def national_alerts(limit=30):
    """Nationwide active NWS alerts for the severe page's US board.

    Reuses data.national.us_warnings() (official NWS ArcGIS watch/warn layer
    + api.weather.gov enrichment, 90 s server-side-tolerant cache) and groups
    it: severity counts plus the most alarming rows for the list. (The map
    polygons already ship nationwide via the severe payload's warnings.)
    """
    try:
        feats = us_warnings() or []
    except Exception:                              # noqa: BLE001
        return {"ok": False, "total": 0, "bySeverity": {}, "rows": []}
    sev_count = {}
    for f in feats:
        s = (f.get("severity") or "Unknown").upper()
        sev_count[s] = sev_count.get(s, 0) + 1
    rank = {"Tornado Warning": 0, "Severe Thunderstorm Warning": 1,
            "Flash Flood Warning": 2, "Special Marine Warning": 3}
    feats.sort(key=lambda a: (rank.get(a.get("event"), 4),
                              a.get("severity") != "Extreme",
                              a.get("severity") != "Severe"))
    rows = [{"event": f.get("event"), "severity": f.get("severity"),
             "areaDesc": f.get("areaDesc"), "headline": f.get("headline"),
             "expires": _tz.iso_z(f.get("expires")), "url": f.get("url")}
            for f in feats[:limit]]
    return {"ok": True, "total": len(feats), "bySeverity": sev_count,
            "rows": rows}


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
            "expires": _tz.iso_z(p.get("expires")),
            "geometry": f.get("geometry"),
        })
    # worst first for the ticker
    rank = {"Tornado Warning": 0, "Severe Thunderstorm Warning": 1, "Flash Flood Warning": 2}
    out.sort(key=lambda a: (rank.get(a["event"], 5 if "Warning" not in a["event"] else 3), a["event"]))
    return out

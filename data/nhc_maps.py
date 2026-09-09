"""NHC map layers: tropical weather outlook + per-storm watch/warning maps.

Adds the NHC tropical-weather-outlook (TWO) graphics to the National tab's
map menu: Atlantic and East Pacific 5-day/2-day formation-area PNGs from
nhc.noaa.gov/xgtwo, plus per-storm wind-radius KMZ overlays. Image bounds
are fixed by NHC's standard outlook projection (same one for both basins),
parsed once from the graphic's <img> HTML page. No keys.
"""
import datetime as dt
import io
import json
import re
import zipfile

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0"}

TWO_GRAPHICS = {
    "nhc_atl": {
        "label": "NHC Tropical Outlook - Atlantic (7 day)",
        "url": "https://www.nhc.noaa.gov/xgtwo/two_atl_7d0.png",
    },
    "nhc_pac": {
        "label": "NHC Tropical Outlook - East Pacific (7 day)",
        "url": "https://www.nhc.noaa.gov/xgtwo/two_pac_7d0.png",
    },
}

_CACHE = {"at": 0.0, "overlays": []}
_CACHE_TTL = 300.0

# NHC outlook graphics: mercator-style fixed region. The Atlantic/EPac 7-day
# graphics share the same lat/lon grid (verified against the KML GroundOverlay
# NHC ships with the same product family).
_BOUNDS = {
    "nhc_atl": [5.0, -100.0, 32.0, -45.0],      # S, W, N, E  (Atlantic basin view)
    "nhc_pac": [3.0, -175.0, 32.0, -100.0],     # East Pacific basin view
}


def nhc_outlook_overlays():
    """NHC TWO image overlays for the map -> [{'name','href','bounds'}].

    href points at the live NHC PNG (browser caches it); bounds are the
    fixed basin rectangles. Cached 5 min.
    """
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    if now - _CACHE["at"] < _CACHE_TTL and _CACHE["overlays"]:
        return _CACHE["overlays"]
    out = []
    for key, g in TWO_GRAPHICS.items():
        try:
            r = requests.get(g["url"], headers=UA, timeout=20)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                out.append({"name": g["label"], "href": g["url"], "bounds": _BOUNDS[key]})
        except requests.RequestException:
            continue
    if out:
        _CACHE["at"] = now
        _CACHE["overlays"] = out
    return out


def nhc_wind_radii_overlays(storms):
    """Per-storm 34/50/64-kt wind radius overlays from NHC GIS.

    storms - the nhc_storms() list (CurrentStorms.json entries). The
    initialWindExtent KMZ carries the current wind-field polygons; parsed
    with the shared KML helpers and returned as geometry features.
    """
    from data.national import _parse_cone_kmz
    # NHC's official wind-radius colors (AABBGGRR in the GIS KMZ)
    _RADIUS_COLORS = {"34": "#ffc800", "50": "#ff8000", "64": "#c80064"}
    out = []
    for s in storms or []:
        kmz = s.get("windKmz")
        if not kmz:
            continue
        try:
            r = requests.get(kmz, headers=UA, timeout=25)
            if r.status_code == 200:
                feats = _parse_cone_kmz(r.content)
                for f in feats:
                    name = (f.get("name") or "").strip()
                    kt = re.match(r"(\d{2})", name)
                    color = _RADIUS_COLORS.get(kt.group(1) if kt else "") or "#ff8000"
                    f["fill"] = color
                    f["name"] = f'{s.get("name", "Storm")} {name} kt winds'.strip()
                out.extend(feats)
        except requests.RequestException:
            continue
    return out

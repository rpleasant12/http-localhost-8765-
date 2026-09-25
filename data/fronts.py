"""WPC surface fronts + pressure centers (free, no key).

Source: NOAA/NWS Weather Prediction Center "National Forecast Chart" vector
service on mapservices.weather.noaa.gov (the old nowCOAST arcgis host stopped
taking REST requests in 2023; this is its official successor):

    outlooks/natl_fcst_wx_chart/MapServer
      layer 1 / 13 / 25  -> Day 1 / 2 / 3 Highs and Lows  (H/L center points)
      layer 2 / 14 / 26  -> Day 1 / 2 / 3 Fronts          (polylines)

Day 1 shows WPC's analyzed frontal positions; Days 2-3 are the forecast
positions from the National Forecast Chart. Geometry arrives as plain WGS84
lon/lat (wkid 4326), so it drops straight onto the site's Leaflet maps.

The WPC chart is rebuilt roughly every 6 hours, so results are cached in
memory for 20 minutes. Every query is guarded - a service outage returns an
empty payload and the site keeps building.
"""
import datetime as dt
import threading
import time
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")

BASE = ("https://mapservices.weather.noaa.gov/vector/rest/services/"
        "outlooks/natl_fcst_wx_chart/MapServer")
LAYERS = {1: (1, 2), 2: (13, 14), 3: (25, 26)}   # day -> (H/L layer, fronts layer)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0"}
MAX_PTS = 120          # decimate each front path to at most this many points

_CACHE = {}
_LOCK = threading.Lock()
_TTL = 1200            # 20 min; WPC rebuilds the chart ~every 6 h

EMPTY = {"issued": "", "fileEpochMs": 0, "days": []}


def _get_json(url, timeout=25):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _query(layer_id):
    """All features of one layer, geometry included."""
    url = (f"{BASE}/{layer_id}/query?where=1%3D1"
           f"&outFields=feat%2Cpopupconte%2Cidp_filedate"
           f"&returnGeometry=true&resultRecordCount=250&f=json")
    return _get_json(url).get("features", [])


def _ftype(feat):
    """'Cold Front Valid' -> 'cold'; None for anything we don't render."""
    f = (feat or "").lower()
    for key in ("cold", "warm", "stationary", "occluded", "trough"):
        if f.startswith(key):
            return key
    return None


def _path(paths):
    """ESRI polyline paths -> decimated [lon, lat] pairs, rounded."""
    out = []
    for path in (paths or []):
        pts = [[round(p[0], 3), round(p[1], 3)] for p in path if len(p) >= 2]
        if not pts:
            continue
        step = max(1, (len(pts) - 1) // (MAX_PTS - 1))
        tail = [pts[-1]] if step > 1 and (len(pts) - 1) % step else []
        out.append(pts[::step] + tail)
    return out


def _file_ms(*feature_lists):
    """Newest idp_filedate (epoch ms) across the given feature lists."""
    best = 0
    for feats in feature_lists:
        for ft in feats or []:
            best = max(best, int(ft.get("attributes", {}).get("idp_filedate")
                                 or 0))
    return best


def _valid_label(fronts_feats, fallback_feats):
    """'Thu Sep 24 2026' from popupconte ('Cold Front Valid: <date>')."""
    for feats in (fronts_feats, fallback_feats):
        for ft in feats or []:
            pop = str(ft.get("attributes", {}).get("popupconte") or "")
            if ":" in pop:
                return pop.split(":", 1)[1].strip()
    return ""


def _issued_label(ms):
    """Chart build time as 'Thu 2:19 PM ET' (Eastern, per site convention)."""
    if not ms:
        return ""
    w = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).astimezone(ET)
    h = w.hour % 12 or 12
    ampm = "AM" if w.hour < 12 else "PM"
    return f"{w:%a} {h}:{w.minute:02d} {ampm} ET"


def _build():
    """Fetch all three days (two queries each); keep any day that succeeded."""
    days = []
    file_ms = 0
    for day in (1, 2, 3):
        hl_layer, fr_layer = LAYERS[day]
        try:
            fr_feats = _query(fr_layer)
        except Exception:  # noqa: BLE001
            fr_feats = []
        try:
            hl_feats = _query(hl_layer)
        except Exception:  # noqa: BLE001
            hl_feats = []

        fronts = []
        for ft in fr_feats:
            t = _ftype(ft.get("attributes", {}).get("feat"))
            if t:
                fronts.append({"t": t,
                               "pts": _path(ft.get("geometry", {}).get("paths"))})
        centers = []
        for ft in hl_feats:
            feat = ft.get("attributes", {}).get("feat") or ""
            g = ft.get("geometry") or {}
            if "x" in g and "y" in g:
                centers.append({"k": "H" if feat.lower().startswith("h")
                                else "L",
                                "x": round(g["x"], 3), "y": round(g["y"], 3)})
        if not fronts and not centers:
            continue          # empty layer (or both queries failed): skip day

        file_ms = max(file_ms, _file_ms(fr_feats, hl_feats))
        days.append({
            "day": day,
            "fronts": fronts,
            "centers": centers,
            "valid": _valid_label(fr_feats, hl_feats),
        })
    return {"issued": _issued_label(file_ms),
            "fileEpochMs": file_ms,
            "days": days}


def payload():
    """Cached payload for data.json / the Fronts page."""
    with _LOCK:
        hit = _CACHE.get("p")
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    try:
        val = _build()
    except Exception:  # noqa: BLE001
        val = dict(EMPTY)
    with _LOCK:
        _CACHE["p"] = (time.time(), val)
    return val


if __name__ == "__main__":
    import json
    p = payload()
    print("issued:", p["issued"], "| days:", [d["day"] for d in p["days"]])
    for d in p["days"]:
        kinds = sorted({f["t"] for f in d["fronts"]})
        print(f"  day {d['day']}: {len(d['fronts'])} fronts {kinds}, "
              f"{len(d['centers'])} H/L, valid {d['valid']}")

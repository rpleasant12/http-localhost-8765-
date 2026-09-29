"""Marine-style weather dashboard: station panels + river gauge panels.

Modeled on LakeErieWX's marine dashboards: every panel shows the current
readings big, with a small trend/history chart, all on one grid page.

Sources (keyless, already used elsewhere on the site):
- api.weather.gov /stations/{id}/observations?limit=30 -> ~24 h of hourly
  obs per station: current temp/dew/wind/gust/RH + the 24 h history that
  powers the inline temperature sparkline.
- The site's own river bundle (data.rivers, NWPS) for gauge stage/status,
  with the OFFICIAL NWPS hydrograph PNG embedded per card
  (water.noaa.gov/resources/hydrographs/{lid}_hg.png - verified 2026-09-21).

Panels are curated for East Tennessee first. Cache: 10 minutes (the updater
cycles every 2 min; NWS obs update ~hourly, gauges ~hourly too).
"""
import datetime as dt
import threading
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0",
      "Accept": "application/geo+json"}

_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()
_TTL_S = 600
_OBS_MAX_AGE_H = 30          # history window (~24 h + slack)

# Dashboard stations: (station id, display name, group) - East TN first.
# All are ASOS/AWOS sites that report hourly on api.weather.gov.
STATIONS = [
    ("KTRI", "Tri-Cities Airport", "Airports"),
    ("KMRN", "Morristown", "Airports"),
    ("KGKT", "Gatlinburg - Pigeon Forge", "Airports"),
    ("KOQT", "Oak Ridge", "Airports"),
    ("KCHT", "McMinn County (Athens)", "Airports"),
    ("KCHA", "Chattanooga", "Airports"),
    ("KTYS", "Knoxville (McGhee Tyson)", "Airports"),
    ("K1A5", "Monroe County (Madisonville)", "Airports"),
    ("KAVL", "Asheville NC", "Regional"),
    ("KETN", "Elizabethton", "Airports"),
    ("KGEV", "Jefferson County (Greeneville area)", "Airports"),
    ("KVLF", "Lafollette / Caryville", "Airports"),
]

# Dashboard gauges: (NWPS lid, display name, river) - East TN first; the
# current value comes from the site's river bundle (already fetched each
# cycle), and the official hydrograph PNG is mirrored for each card.
GAUGES = [
    ("NOLT1", "Nolichucky Dam", "Nolichucky"),
    ("EMBT1", "Embreeville", "Nolichucky"),
    ("LLDT1", "Limestone", "Nolichucky"),
    ("NWPT1", "Newport", "French Broad"),
    ("DUGT1", "Dandridge", "French Broad"),
    ("CRKT1", "Rogersville", "Holston"),
    ("BOOT1", "Boone Dam", "Holston"),
    ("DOET1", "Doe River at Elizabethton", "Doe"),
    ("WTGT1", "Watauga at Wilbur", "Watauga"),
    ("HRFT1", "Hartford", "Pigeon"),
    ("SEVT1", "Sevierville", "Little Pigeon"),
    ("NRST1", "Norris", "Clinch"),
    ("CHLT1", "Charleston", "Hiwassee"),
    ("OCAT1", "Ocoee at Caney Creek", "Ocoee"),
]

HYDRO_URL = "https://water.noaa.gov/resources/hydrographs/{lid}_hg.png"


def _hydro_lid(lid):
    """NWPS hydrograph PNG is keyed by lower-case LID."""
    return HYDRO_URL.format(lid=lid.lower())


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _c_to_f(c):
    return None if c is None else round(c * 9.0 / 5.0 + 32.0)


def _ms_to_mph(v):
    return None if v is None else round(v * 2.23694)


def _cardinal(deg):
    if deg is None:
        return None
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[int((deg % 360) / 22.5) % 16]


def _station_panel(sid, name, group):
    """One station's current obs + 24 h history (NWS API)."""
    try:
        r = requests.get(f"https://api.weather.gov/stations/{sid}/observations",
                         headers=UA, timeout=25, params={"limit": 30})
        feats = (r.json().get("features") or []) if r.ok else []
    except Exception:                                   # noqa: BLE001
        return None
    props = [f.get("properties") or {} for f in feats]
    if not props:
        return None
    cut = time.time() - _OBS_MAX_AGE_H * 3600
    hist = []          # (epoch, tempF) oldest first, for the sparkline
    for p in props:
        try:
            t = dt.datetime.fromisoformat(p["timestamp"]).timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        if t < cut:
            continue
        tf = _c_to_f(_f((p.get("temperature") or {}).get("value")))
        if tf is not None:
            hist.append((t, tf))
    if not hist:
        return None
    p0 = props[0]      # latest first in the API response
    vis_m = _f((p0.get("visibility") or {}).get("value"))
    return {
        "id": sid, "name": name, "group": group,
        "tempF": hist[0][1],
        "dewF": _c_to_f(_f((p0.get("dewpoint") or {}).get("value"))),
        "rh": round(_f((p0.get("relativeHumidity") or {}).get("value")) or 0),
        "windDir": _cardinal(_f((p0.get("windDirection") or {}).get("value"))),
        "windMph": _ms_to_mph(_f((p0.get("windSpeed") or {}).get("value"))),
        "gustMph": _ms_to_mph(_f((p0.get("windGust") or {}).get("value"))),
        "pressureHg": (round(_f((p0.get("barometricPressure") or {}).get("value")) / 3386.39, 2)
                       if _f((p0.get("barometricPressure") or {}).get("value")) else None),
        "visMi": (round(vis_m / 1609.34, 1) if vis_m else None),
        "desc": ((p0.get("textDescription") or "") or "-"),
        "time": p0.get("timestamp", ""),
        # 24 h temperature history for the inline sparkline (downsampled)
        "history": [[t, v] for t, v in hist][::-1][-48:],
    }


def _gauge_panel(lid, name, river, river_bundle):
    """One gauge panel: live stage from the site bundle + official chart URL."""
    for g in (river_bundle or {}).get("gauges") or []:
        if g.get("lid") == lid:
            return {
                "lid": lid, "name": name, "group": "Rivers",
                "river": river, "stage": g.get("stage"),
                "stageUnit": g.get("stageUnit") or "ft",
                "flow": g.get("flow"), "flowUnit": g.get("flowUnit"),
                "catColor": g.get("catColor"), "catWord": g.get("catWord"),
                "category": g.get("category"),
                "thresholds": g.get("thresholds") or {},
                "obsTime": g.get("obsTime"), "url": g.get("url"),
                "chart": _hydro_lid(lid),
            }
    return None


def dashboard_bundle(max_age=_TTL_S):
    """Panels for the dashboard page (cached)."""
    with _lock:
        now = time.time()
        if _cache["data"] is not None and now - _cache["at"] < max_age:
            return _cache["data"]

    from concurrent.futures import ThreadPoolExecutor
    stations = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for panel in ex.map(lambda s: _station_panel(*s), STATIONS):
            if panel:
                stations.append(panel)

    try:
        from data.rivers import rivers_bundle
        rb = rivers_bundle()
    except Exception:                                   # noqa: BLE001
        rb = {}
    gauges = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for panel in ex.map(lambda g: _gauge_panel(g[0], g[1], g[2], rb), GAUGES):
            if panel:
                gauges.append(panel)

    # headline numbers for the KPI strip
    temps = [s["tempF"] for s in stations if s.get("tempF") is not None]
    gusts = [s["gustMph"] for s in stations if s.get("gustMph")]
    rivers_in_flood = sum(1 for g in gauges
                          if g.get("category") in ("major", "moderate", "minor"))
    data = {
        "ok": bool(stations or gauges),
        "stations": stations,
        "gauges": gauges,
        "kpi": {
            "stations": len(stations),
            "gauges": len(gauges),
            "tempMax": (max(temps) if temps else None),
            "tempMin": (min(temps) if temps else None),
            "gustMax": (max(gusts) if gusts else None),
            "riversInFlood": rivers_in_flood,
        },
        "generated": time.strftime("%Y-%m-%d %H:%M"),
    }
    with _lock:
        _cache.update(at=now, data=data)
    return data


if __name__ == "__main__":
    b = dashboard_bundle(max_age=0)
    print("stations:", len(b["stations"]), "gauges:", len(b["gauges"]),
          "| KPI:", b["kpi"])
    for s in b["stations"][:3]:
        print(f"  {s['id']} {s['name'][:30]}: {s['tempF']}F {s['desc'][:20]} "
              f"wind {s['windMph']}mph, hist {len(s['history'])} pts")

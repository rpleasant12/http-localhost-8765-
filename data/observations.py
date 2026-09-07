"""Nearby surface observations from the NWS API (no key).

/points/{lat},{lon}/stations lists the closest observing stations (METAR /
AWOS / ASOS), then each station's /observations/latest is fetched in
parallel. Cached by the caller (observations refresh roughly hourly).
"""
import math
from concurrent.futures import ThreadPoolExecutor

import requests

# East Tennessee cities covered by the Current tab's city board
# (name -> (lat, lon)); stations are matched per city from api.weather.gov
EAST_TN_CITIES = {
    "Knoxville": (35.9606, -83.9207),
    "Chattanooga": (35.0456, -85.3097),
    "Tri-Cities (JC/KPT/Bristol)": (36.3134, -82.3573),
    "Morristown": (36.0454, -83.2934),
    "Oak Ridge": (35.9903, -84.2853),
    "Maryville": (35.7570, -83.9743),
    "Cleveland": (35.1595, -84.8766),
    "Athens": (35.4429, -84.5988),
    "Cookeville": (36.1628, -85.5016),
    "Crossville": (35.9479, -85.0269),
    "Sevierville": (35.8720, -83.5757),
    "Greeneville": (36.1668, -82.8301),
    "Newport": (35.9651, -83.1204),
    "LaFollette": (36.3759, -84.1316),
    "Clinton": (36.0612, -84.1310),
    "Dayton": (35.4956, -85.0244),
}


def _city_obs(city, lat, lon):
    """Nearest reporting station for one city -> city-tagged obs dict (or None)."""
    near = nearby_observations(lat, lon, limit=2)
    for o in near:
        return {
            "city": city,
            "cityLat": lat,
            "cityLon": lon,
            "station": o["id"],
            "stationName": o["name"],
            "miles": round(math.hypot((o["lat"] - lat) * 111.32,
                                      (o["lon"] - lon) * 111.32 * math.cos(math.radians(lat)))),
            "tempF": o["tempF"], "dewF": o["dewF"],
            "windDir": o["windDir"], "windMph": o["windMph"], "gustMph": o["gustMph"],
            "rh": o["rh"], "desc": o["desc"], "time": o["time"],
        }
    return None


def city_observations(cities=None):
    """Latest observation for every East TN city (nearest station per city).

    Returns city-tagged dicts sorted alphabetically; cities whose nearest
    station is stale or missing come back with tempF=None (rendered as '-').
    """
    cities = cities or EAST_TN_CITIES
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(lambda kv: _city_obs(kv[0], kv[1][0], kv[1][1]), cities.items()))
    out = [r for r in rows if r]
    # cities with no live station still get a row so the board is complete
    seen = {r["city"] for r in out}
    for city, (lat, lon) in cities.items():
        if city not in seen:
            out.append({"city": city, "cityLat": lat, "cityLon": lon,
                        "station": "-", "stationName": "no live station", "miles": None,
                        "tempF": None, "dewF": None, "windDir": "", "windMph": None,
                        "gustMph": None, "rh": None, "desc": "", "time": ""})
    out.sort(key=lambda r: r["city"])
    return out


UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0",
    "Accept": "application/geo+json",
}
API = "https://api.weather.gov"

# stale after 3 h: dead stations shouldn't render as "current weather"
MAX_AGE_S = 3 * 3600

_CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _compass(deg):
    if deg is None:
        return ""
    return _CARDINALS[int(((deg % 360) + 11.25) // 22.5) % 16]


def _f(c_val):
    return None if c_val is None else c_val * 9.0 / 5.0 + 32.0


def _mph(kmh_val):
    return None if kmh_val is None else kmh_val * 0.621371


def _latest(st_id):
    """Latest observation dict for one station id (or None)."""
    try:
        r = requests.get(f"{API}/stations/{st_id}/observations/latest",
                         headers=UA, timeout=15)
        if r.status_code != 200:
            return None
        return r.json().get("properties") or {}
    except requests.RequestException:
        return None


def nearby_observations(lat, lon, limit=20):
    """Closest `limit` stations with their latest observations.

    Returns [{'id','name','lat','lon','tempF','dewF','windDir','windMph',
              'gustMph','rh','desc','time'}] sorted by distance.
    """
    try:
        r = requests.get(f"{API}/points/{lat:.4f},{lon:.4f}/stations",
                         headers=UA, timeout=20)
        r.raise_for_status()
        feats = r.json().get("features", [])
    except (requests.RequestException, ValueError):
        return []

    stations = []
    for f in feats[: max(limit, 12)]:
        try:
            gx, gy = f["geometry"]["coordinates"]
            p = f["properties"]
            sid = p.get("stationIdentifier")
            if not sid:
                continue
            stations.append((sid, p.get("name") or sid, float(gy), float(gx)))
        except (KeyError, TypeError, ValueError):
            continue

    def distance(s):
        _, _, slat, slon = s
        dy = (slat - lat) * 111.32
        dx = (slon - lon) * 111.32 * math.cos(math.radians(lat))
        return math.hypot(dx, dy)

    stations.sort(key=distance)
    stations = [s for s in stations if s[0]][:limit]
    if not stations:
        return []

    with ThreadPoolExecutor(max_workers=8) as ex:
        obs = dict(zip([s[0] for s in stations], ex.map(_latest, [s[0] for s in stations])))

    out = []
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    for sid, name, slat, slon in stations:
        pr = obs.get(sid)
        if not pr:
            continue
        ts = pr.get("timestamp")
        try:
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
            if t is None or (now - t).total_seconds() > MAX_AGE_S:
                continue
        except (ValueError, TypeError, AttributeError):
            pass
        temp_c = (pr.get("temperature") or {}).get("value")
        if temp_c is None:
            continue  # no temperature -> not useful on a temp map
        out.append({
            "id": sid,
            "name": (name or sid).split(",")[0].strip(),
            "lat": slat,
            "lon": slon,
            "tempF": round(_f(temp_c)),
            "dewF": (lambda v: round(_f(v)) if v is not None else None)(
                (pr.get("dewpoint") or {}).get("value")),
            "windDir": _compass((pr.get("windDirection") or {}).get("value")),
            "windMph": (lambda v: round(_mph(v)) if v is not None else None)(
                (pr.get("windSpeed") or {}).get("value")),
            "gustMph": (lambda v: round(_mph(v)) if v is not None else None)(
                (pr.get("windGust") or {}).get("value")),
            "rh": (lambda v: round(v) if v is not None else None)(
                (pr.get("relativeHumidity") or {}).get("value")),
            "desc": pr.get("textDescription") or "",
            "time": (ts[11:16] + "Z") if ts else "",
        })
    return out

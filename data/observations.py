"""Nearby surface observations from the NWS API (no key).

/points/{lat},{lon}/stations lists the closest observing stations (METAR /
AWOS / ASOS), then each station's /observations/latest is fetched in
parallel. Cached by the caller (observations refresh roughly hourly).
"""
import math
import time
from concurrent.futures import ThreadPoolExecutor

import requests


def _tz_hm(t):
    """aware/naive-UTC datetime -> '7:55 PM' Eastern display ('-' if t is None)."""
    if t is None:
        return "-"
    try:
        from data._tz import hm
        return hm(t)
    except Exception:  # noqa: BLE001 - display must never break the feed
        return "-"

# Cities covered by the Current tab's city board: East Tennessee plus the
# neighboring Southwest Virginia and Western North Carolina areas the site
# serves (name -> (lat, lon)); stations are matched per city from
# api.weather.gov. The board, city selector, MOS charts, WBGT heat flags and
# severe-composite town ranking all read this one dict.
EAST_TN_CITIES = {
    # --- East Tennessee ---
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
    "Kingsport": (36.5484, -82.5618),
    "Johnson City": (36.3134, -82.1981),
    "Elizabethton": (36.3487, -82.2290),
    "Mountain City": (36.1851, -81.8050),
    "Butler": (36.2512, -81.9256),
}

# --- Southwest Virginia (three-state coverage) ---
EAST_TN_CITIES.update({
    "Bristol VA": (36.5951, -82.1887),
    "Abingdon": (36.7098, -81.9773),
    "Lebanon VA": (36.9034, -82.0762),
    "Grundy": (37.2068, -82.0979),
    "Marion VA": (36.7582, -81.5143),
    "Tazewell VA": (37.1218, -81.5287),
    "Wytheville": (36.9460, -81.0827),
    "Galax": (36.6618, -80.9254),
    "Norton": (36.9682, -82.6254),
})

# --- Western North Carolina (three-state coverage) ---
EAST_TN_CITIES.update({
    "Asheville": (35.5951, -82.5515),
    "Boone": (36.2118, -81.6878),
    "Hendersonville": (35.3187, -82.4609),
    "Waynesville": (35.4860, -82.9990),
    "Murphy": (35.0257, -84.0323),
    "Burnsville": (35.9157, -82.2988),
    "Sylva": (35.3735, -83.2243),
    "Franklin NC": (35.1823, -83.3815),
    "Robbinsville": (35.3226, -83.8065),
})


def _city_obs(city, lat, lon):
    """Best reporting station for one city -> city-tagged obs dict (or None).

    Stations report on their own cadence (AWOS sites often hourly), so the
    strictly-nearest station can sit 45-60 min past its cycle while a
    slightly farther one just reported. When the nearest is older than 45
    min, prefer a neighbor at least 15 min fresher - the board then reads
    as current instead of mysteriously stuck. Age in minutes rides along
    for display.
    """
    near = [o for o in nearby_observations(lat, lon, limit=3)
            if o.get("tempF") is not None]
    if not near:
        return None
    pick = near[0]
    age = pick.get("ageMin")
    if age is not None and age > 45:
        fresher = [o for o in near[1:]
                   if o.get("ageMin") is not None and o["ageMin"] <= age - 15]
        if fresher:
            pick = fresher[0]
    return {
        "city": city,
        "cityLat": lat,
        "cityLon": lon,
        "station": pick["id"],
        "stationName": pick["name"],
        "miles": round(math.hypot((pick["lat"] - lat) * 111.32,
                                  (pick["lon"] - lon) * 111.32 * math.cos(math.radians(lat)))),
        "tempF": pick["tempF"], "dewF": pick["dewF"],
        "windDir": pick["windDir"], "windMph": pick["windMph"], "gustMph": pick["gustMph"],
        "rh": pick["rh"], "desc": pick["desc"], "time": pick["time"],
        "ageMin": pick.get("ageMin"),
    }


_CITY_OBS_TTL_S = 600           # obs refresh ~hourly at stations; 10 min is plenty
_CITY_OBS_CACHE = {"t": 0.0, "rows": []}


def city_observations(cities=None):
    """Latest observation for every city on the board (nearest station each).

    Returns city-tagged dicts sorted alphabetically; cities whose nearest
    station is stale or missing come back with tempF=None (rendered as '-').
    Cached 10 min in-process: the updater builds every 2 minutes and the NWS
    stations+latest calls cost ~5 requests per city, so an uncached 40-city
    board would spend ~200 requests per build on api.weather.gov.
    """
    now = time.time()
    if now - _CITY_OBS_CACHE["t"] < _CITY_OBS_TTL_S and _CITY_OBS_CACHE["rows"]:
        return _CITY_OBS_CACHE["rows"]
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
    # previous-observation temps so the board can show rise/fall arrows
    prev = previous_temps(r["station"] for r in out)
    for r in out:
        if r["tempF"] is not None:
            r["prevTempF"] = prev.get(r["station"])
    out.sort(key=lambda r: r["city"])
    if out:
        _CITY_OBS_CACHE.update(t=now, rows=out)
    return out


_PREV_URL = "https://aviationweather.gov/api/data/metar?ids={ids}&format=json&hours=3"
_PREV_CACHE = {"t": 0.0, "temps": {}}
_PREV_TTL_S = 600


def _station_id(raw_ob):
    """ICAO id from a raw METAR text (the JSON feed's id field is null)."""
    if not raw_ob:
        return None
    tok = raw_ob.split()
    if len(tok) > 1 and tok[0] in ("METAR", "SPECI"):
        return tok[1]
    if tok and len(tok[0]) == 4 and tok[0].isalpha():
        return tok[0]
    return None


def previous_temps(station_ids):
    """Previous-observation temps (F) for the given stations.

    aviationweather.gov metar?ids=...&hours=3 returns the last few reports
    per station; the second-newest is the 'previous observation' the city
    board's trend arrows compare against. Cached 10 min in-process so the
    every-2-minute updater build stays polite to the API.
    """
    ids = sorted({s for s in station_ids if s and s != "-"})
    if not ids:
        return {}
    now = time.time()
    if now - _PREV_CACHE["t"] > _PREV_TTL_S or not _PREV_CACHE["temps"]:
        temps = {}
        try:
            req = requests.get(_PREV_URL.format(ids=",".join(ids)), headers=UA, timeout=10)
            req.raise_for_status()
            by_station = {}
            for rep in req.json():
                sid = _station_id(rep.get("rawOb") or "")
                t = rep.get("temp")
                tm = rep.get("reportTime") or ""
                if sid and t is not None and tm:
                    by_station.setdefault(sid, []).append((tm, t))
            for sid, reps in by_station.items():
                reps.sort(reverse=True)          # newest first
                if len(reps) >= 2:
                    temps[sid] = round(reps[1][1] * 9.0 / 5.0 + 32.0)
        except Exception:
            temps = {}
        if temps:
            _PREV_CACHE["t"] = now
            _PREV_CACHE["temps"] = temps
    cache = _PREV_CACHE["temps"]
    return {sid: cache[sid] for sid in ids if sid in cache}


UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0",
    "Accept": "application/geo+json",
}

# ---- US-wide observations (aviationweather.gov official cache file, keyless) ----
# The /api/data query endpoints cap at 400 entries, so full-US coverage comes
# from their recommended cache file: metars.cache.csv.gz (all current METARs,
# updated once per minute). Cache in-process for 5 minutes to stay polite.
_METAR_URL = "https://aviationweather.gov/data/cache/metars.cache.csv.gz"
_METAR_CACHE = {"t": 0.0, "rows": []}
_METAR_TTL_S = 300


def us_observations():
    """Current CONUS METARs -> obs dicts for the site's US observations layer.

    K-prefix stations with coordinates in the CONUS bounds and a temperature;
    obs dicts match nearby_observations fields so the pages render both the
    same way (id, name, lat, lon, tempF, dewF, windDir, windMph, gustMph,
    rh, desc, time).
    """
    import time as _time
    import datetime as _dt
    now = _time.time()
    if now - _METAR_CACHE["t"] < _METAR_TTL_S and _METAR_CACHE["rows"]:
        return _METAR_CACHE["rows"]
    try:
        import csv
        import gzip
        r = requests.get(_METAR_URL, headers={"User-Agent": UA["User-Agent"]}, timeout=60)
        r.raise_for_status()
        text = gzip.decompress(r.content).decode("utf-8", "replace")
        rows = list(csv.DictReader(text.splitlines()))
    except Exception:  # noqa: BLE001
        return _METAR_CACHE["rows"]

    out = []
    for m in rows:
        sid = (m.get("station_id") or "").strip()
        if not sid.startswith("K"):
            continue
        try:
            lat, lon = float(m["latitude"]), float(m["longitude"])
            temp_c = float(m["temp_c"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (23.0 < lat < 51.0 and -126.0 < lon < -65.0):
            continue
        try:
            t = _dt.datetime.fromisoformat(m["observation_time"].replace("Z", "+00:00"))
            if (now - t.timestamp()) > MAX_AGE_S:
                continue
        except (KeyError, ValueError):
            continue

        def _fnum(key, mult=1.0):
            try:
                return round(float(m[key]) * mult)
            except (KeyError, TypeError, ValueError):
                return None

        wdir = m.get("wind_dir_degrees") or ""
        out.append({
            "id": sid,
            "name": sid,  # cache file carries no station name column
            "lat": round(lat, 4), "lon": round(lon, 4),
            "tempF": round(temp_c * 9.0 / 5.0 + 32.0),
            "dewF": (lambda v: round(v * 9.0 / 5.0 + 32.0) if v is not None else None)(
                _try_float(m.get("dewpoint_c"))),
            "windDir": _compass(_try_float(wdir)),
            "windMph": _fnum("wind_speed_kt", 1.15078),
            "gustMph": _fnum("wind_gust_kt", 1.15078),
            "rh": None,
            "desc": (m.get("wx_string") or "").replace("/", " ").strip() or "METAR",
            "time": _tz_hm(t),
        })
    _METAR_CACHE["t"] = now
    _METAR_CACHE["rows"] = out
    return out


def _try_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
        age_min = None
        try:
            age_min = max(0, round((now - t).total_seconds() / 60)) if t else None
        except (TypeError, OSError):
            pass
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
            "time": _tz_hm(t) if t else "",
            "ageMin": age_min,
        })
    return out

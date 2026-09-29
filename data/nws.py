import re
from concurrent.futures import ThreadPoolExecutor

import requests

from data.observations import EAST_TN_CITIES

NWS_API = "https://api.weather.gov"
NWS_HEADERS = {
    "User-Agent": "freebuff-weather-app/1.0 (contact: local-demo)",
    "Accept": "application/geo+json",
}


def parse_wind_mph(value):
    """NWS windSpeed strings like '5 to 10 mph' -> 10 (range max); '5 mph' -> 5."""
    numbers = [int(n) for n in re.findall(r"\d+", str(value or ""))]
    return max(numbers) if numbers else 0


def _get(url, timeout=10):
    resp = requests.get(url, headers=NWS_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def get_forecast(lat, lon):
    """12-period day/night forecast (covers ~5 days); [] if unavailable.

    NWS only covers US points - returns [] for anything outside its area.
    """
    try:
        points = _get(f"{NWS_API}/points/{lat:.4f},{lon:.4f}")
        return _get(points["properties"]["forecast"])["properties"]["periods"]
    except (requests.RequestException, KeyError):
        return []


def get_hourly(lat, lon):
    """Next ~156 hours of hourly forecast periods; [] if unavailable."""
    try:
        points = _get(f"{NWS_API}/points/{lat:.4f},{lon:.4f}")
        return _get(points["properties"]["forecastHourly"])["properties"]["periods"]
    except (requests.RequestException, KeyError):
        return []


def get_current_conditions(lat, lon):
    """Nearest current-conditions observation with actual data, or None.

    Some stations publish 'latest' observations without temperature/
    humidity values, which would render the home tile as --/n/a/0%.
    Try up to 5 nearby stations and return the first whose observation
    carries a temperature; fall back to the nearest otherwise.
    """
    try:
        stations = _get(f"{NWS_API}/points/{lat:.4f},{lon:.4f}/stations")
        candidates = [f["properties"]["stationIdentifier"]
                      for f in stations.get("features", [])][:5]
    except (requests.RequestException, KeyError, IndexError):
        return None
    first = None
    for sid in candidates:
        try:
            obs = _get(f"{NWS_API}/stations/{sid}/observations/latest")["properties"]
        except (requests.RequestException, KeyError, IndexError):
            continue
        if first is None:
            first = obs
        if (obs.get("temperature") or {}).get("value") is not None:
            return obs
    return first


def get_active_alerts(lat, lon):
    """Active NWS alerts for a point, reduced to the fields we display."""
    url = f"{NWS_API}/alerts/active?point={lat:.4f},{lon:.4f}"
    try:
        data = _get(url)
    except requests.RequestException:
        return []
    alerts = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        alerts.append({
            "title": props.get("headline", ""),
            "event": props.get("event", ""),
            "severity": props.get("severity", ""),
            "urgency": props.get("urgency", ""),
            "description": props.get("description", ""),
            "instruction": props.get("instruction", ""),
            "areaDesc": props.get("areaDesc", ""),
            "sent": props.get("sent", ""),
            "expires": props.get("expires", ""),
            "geometry": feature.get("geometry"),  # GeoJSON polygon for map display
        })
    return alerts


def city_forecasts(cities=None, max_periods=14):
    """NWS point forecast for every city on the board, fetched in parallel.

    Returns [{'city','lat','lon','periods': [NWS day/night periods]}] -
    max_periods=14 covers the full 7-day day/night sequence. Periods carry
    name/temp(F)/wind/shortForecast/detailedForecast/pop. Cities whose
    forecast fails come back with periods=[] (rendered as '-').

    Cached 30 min in-process: the NWS point forecast updates ~hourly, and
    each city costs 2 api.weather.gov requests (point lookup + forecast).
    With the 40+-city board and the updater's ~2-minute build cycle that is
    the difference between ~280 and ~40 requests per build.
    """
    now = _FC_CLOCK()
    key = tuple(sorted(cities)) if cities else None
    hit = _FC_CACHE.get(key)
    if hit and now - hit[0] < _FC_TTL_S:
        return hit[1]
    cities = cities or EAST_TN_CITIES

    def work(kv):
        city, (lat, lon) = kv
        periods_src = get_forecast(lat, lon) or []
        periods = [{
            "name": p.get("name", ""),
            "tempF": p.get("temperature"),
            "wind": f"{p.get('windSpeed', '')} {p.get('windDirection', '')}".strip(),
            "short": p.get("shortForecast", ""),
            "detailed": p.get("detailedForecast", ""),
            "pop": p.get("probabilityOfPrecipitation", {}).get("value")
                   if isinstance(p.get("probabilityOfPrecipitation"), dict)
                   else p.get("probabilityOfPrecipitation"),
        } for p in periods_src[:max_periods]]
        return {"city": city, "lat": lat, "lon": lon, "periods": periods}

    with ThreadPoolExecutor(max_workers=8) as ex:
        out = list(ex.map(work, cities.items()))
    if out:
        _FC_CACHE[key] = (now, out)
    return out


def _FC_CLOCK():
    import time
    return time.time()


_FC_TTL_S = 1800
_FC_CACHE = {}
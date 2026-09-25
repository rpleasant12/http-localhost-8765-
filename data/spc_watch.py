"""Active SPC severe/tornado watch polygons for the HRRR+RRFS mini-map.

Iowa Environmental Mesonet's watch archive CGI
(/cgi-bin/request/gis/spc_watch.py) serves the Storm Prediction Center's
official watch polygons with per-watch metadata: watch number (ETN), type
(SVR/TOR), issue/expire stamps, watch probabilities and the PDS flag. The
free/keyless alternative api.weather.gov only lists SPC watches as
county-zone lists with no geometry (verified 2026-09-25), and SPC's own
ActiveWW.kmz carries no attributes - so IEM is the only free source that
pairs the polygon with the numbers a visitor wants on a tooltip.

Window design: the CGI filters watches by issue time, and a watch can be
active up to ~10 h after issue, so the query reaches back 16 h and then
filters "still active" (EXPIRE >= now) here. Cached 5 min like data.mcd;
any network flake returns an empty bundle - the map just shows no watch
layer, it never breaks a build.
"""
import datetime as dt
import time

import requests

from data import _tz

WATCH_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/gis/spc_watch.py"
UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}
_CACHE = {"t": 0.0, "b": None}

# Official-ish colors: tornado watches red, svr watches amber (NWS practice)
COLORS = {"TOR": "#ff1744", "SVR": "#ffb300"}


def _utc(s):
    """'202609142045' -> aware UTC datetime (None on garbage)."""
    try:
        return dt.datetime.strptime(str(s)[:12], "%Y%m%d%H%M").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _fetch_window(start, end):
    """Raw IEM CGI GeoJSON for watches issued between start and end."""
    params = {
        "year1": start.year, "month1": start.month, "day1": start.day,
        "hour1": start.hour, "minute1": start.minute,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "hour2": end.hour, "minute2": end.minute,
        "format": "geojson",
    }
    try:
        r = requests.get(WATCH_URL, params=params, headers=UA, timeout=30)
        if not r.ok:
            return []
        return r.json().get("features") or []
    except Exception:  # noqa: BLE001 - network flake -> caller shows none
        return []


def _feature(f, now):
    """One IEM watch feature -> payload dict (None if unusable)."""
    try:
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        num = int(p.get("NUM"))
        wtype = "TOR" if str(p.get("TYPE", "")).upper().startswith("TOR") else "SVR"
        expire = _utc(p.get("EXPIRE"))
        if expire is None or expire < now:
            return None                      # only still-active watches
        if g.get("type") == "Polygon":
            rings = [g.get("coordinates", [])]
        elif g.get("type") == "MultiPolygon":
            rings = [ring for poly in g.get("coordinates", []) for ring in poly]
        else:
            rings = []
        rings = [r for r in rings if len(r) >= 4]
        if not rings:
            return None

        def pct(v):
            try:
                n = int(v)
                return f"{n}%" if n > 0 else ""
            except (TypeError, ValueError):
                return ""

        probs = " \u00b7 ".join(x for x in (
            f"torn {pct(p.get('P_TORTWO'))}".strip(),
            f"wind {pct(p.get('P_WIND10'))}".strip(),
            f"hail {pct(p.get('P_HAIL10'))}".strip(),
        ) if x.split()[-1])
        extras = []
        try:
            if float(p.get("MAX_HAIL") or 0) > 0:
                extras.append(f'{p["MAX_HAIL"]:g}" hail')
        except (TypeError, ValueError):
            pass
        try:
            if float(p.get("MAX_GUST") or 0) > 0:
                extras.append(f'{p["MAX_GUST"]:g} kt gusts')
        except (TypeError, ValueError):
            pass
        pds = str(p.get("IS_PDS", "")).lower() in ("true", "1", "t")
        return {
            "num": num,
            "type": wtype,
            "label": ("Tornado Watch" if wtype == "TOR"
                      else "Severe Thunderstorm Watch") + f" {num}",
            "expire": _tz.stamp(expire),
            "pds": pds,
            "probs": probs,
            "extras": " \u00b7 ".join(extras),
            "color": COLORS[wtype],
            "rings": rings,
        }
    except (KeyError, TypeError, ValueError):
        return None


def bundle(max_age=300):
    """{"features": [...active watches...], "fetched": et-stamp} (cached)."""
    now = time.time()
    if _CACHE["b"] is not None and now - _CACHE["t"] < max_age:
        return _CACHE["b"]
    now_dt = dt.datetime.now(dt.timezone.utc)
    feats = []
    seen = set()
    for f in _fetch_window(now_dt - dt.timedelta(hours=16),
                           now_dt + dt.timedelta(hours=1)):
        d = _feature(f, now_dt)
        if d and d["num"] not in seen:
            seen.add(d["num"])
            feats.append(d)
    feats.sort(key=lambda x: (x["type"] != "TOR", -x["num"]))
    b = {"features": feats,
         "fetched": _tz.full(dt.datetime.now(dt.timezone.utc))}
    _CACHE.update(t=now, b=b)
    return b

"""Forecast charts + MOS guidance for the site (all keyless).

MOS:   primary  - GFS-LAMP/MAV text bulletins via the Iowa Environmental
                   Mesonet JSON API (mesonet.agron.iastate.edu/api/1/mos.json);
         fallback - NWS NDFD hourly digital forecast (MapClick DWML), the
                   official statistically-downscaled guidance MOS feeds.
       NOAA's own text servers (tgftp/nomads) reject this network and IEM is
       periodically down, so when IEM fails the NDFD source takes over and
       the page keeps showing guidance rows.

Charts: NWS hourly gridpoint forecast (api.weather.gov via data.nws) for
       Greeneville + East TN cities -> matplotlib PNGs (temp/dew, PoP,
       wind), cached to static/forecast_charts/ and refreshed by the site
       updater's normal cycles.
"""
import datetime as dt
import math
import os
import re
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0"}
IEM_MOS_URL = "https://mesonet.agron.iastate.edu/api/1/mos.json"

CHART_DIR = os.path.join("static", "forecast_charts")

# station -> MOS bulletin id (IEM wants the 3-letter id for CONUS METARs)
MOS_STATIONS = {
    "Greeneville": "KGCY",
    "Knoxville": "KTYS",
    "Tri-Cities": "KTRI",
    "Chattanooga": "KCHA",
    "Nashville": "KBNA",
    "Crossville": "KCSV",
    "Oak Ridge": "KOQV",
}

# METAR station -> (lat, lon) for the NDFD/MapClick fallback (2.5 km grid,
# so city-center precision is plenty)
STATION_COORDS = {
    "KGCY": (36.17, -82.83),
    "KTYS": (35.81, -83.99),
    "KTRI": (36.47, -82.41),
    "KCHA": (35.03, -85.20),
    "KBNA": (36.12, -86.68),
    "KCSV": (35.95, -84.87),
    "KOQV": (36.01, -84.24),
}

# ------------------------------------------------------------------ MOS


def fetch_mos(station, model="GFS LAV"):
    """One MOS bulletin -> {'station','init','rows':[...]} or None.

    Rows are hourly LAMP entries: {'fhr','tmp','dpt','wsp','wdr','pop','sky'}.
    Returns None (never raises) when IEM is down / has no data yet.
    """
    try:
        r = requests.get(IEM_MOS_URL, params={"station": station, "model": model,
                                              "runtime": "latest"},
                         headers=UA, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:  # noqa: BLE001 - IEM down / offline: skip MOS quietly
        return None
    rows = data.get("data") or []
    out = []
    for rec in rows:
        try:
            fhr = int(rec.get("fhr") or 0)
        except (TypeError, ValueError):
            continue
        row = {"fhr": fhr}
        ok = False
        for key in ("tmp", "dpt", "wsp", "pop", "sky"):
            v = rec.get(key)
            try:
                if v is not None and str(v).strip() not in ("", "M"):
                    row[key] = int(v)
                    ok = True
                else:
                    row[key] = None
            except (TypeError, ValueError):
                row[key] = None
        wdr = rec.get("wdr")
        try:
            row["wdr"] = int(wdr) if wdr is not None else None
        except (TypeError, ValueError):
            row["wdr"] = None
        if ok:
            out.append(row)
    if not out:
        return None
    out.sort(key=lambda x: x["fhr"])
    return {
        "station": station,
        "model": model,
        "init": str(data.get("runtime") or ""),
        "rows": out[:60],
    }


def _fetch_ndfd_mos(station, hours=60):
    """NDFD hourly digital forecast (MapClick DWML) -> MOS-style rows.

    The National Digital Forecast Database is statistically downscaled from
    the model guidance (including MOS), so this is the natural stand-in when
    the IEM bulletin service is down. Rows use the same keys as fetch_mos:
    {'fhr', 'tmp', 'dpt', 'wsp', 'wdr', 'pop', 'sky'}.
    """
    import xml.etree.ElementTree as ET
    coord = STATION_COORDS.get(station)
    if not coord:
        return None
    try:
        r = requests.get("https://forecast.weather.gov/MapClick.php",
                         params={"lat": coord[0], "lon": coord[1],
                                 "FcstType": "digitalDWML"},
                         headers=UA, timeout=25)
        if r.status_code != 200 or not r.text.lstrip().startswith("<?xml"):
            return None
        root = ET.fromstring(r.content)
    except Exception:  # noqa: BLE001 - NWS hiccup: skip quietly
        return None
    times = []
    for tl in root.iter("time-layout"):
        if tl.findtext("layout-key", "").startswith("k-p1h"):
            times = [e.text for e in tl.findall("start-valid-time")]
            break
    if not times:
        return None

    params = root.find(".//parameters")
    if params is None:
        return None

    def series(tag, type_):
        for el in params:
            if (el.tag.split("}")[-1] == tag and (el.get("type") or "") == type_):
                vals = []
                for v in el.findall("value"):
                    try:
                        vals.append(int(v.text))
                    except (TypeError, ValueError):
                        vals.append(None)
                return vals
        return []

    temp = series("temperature", "hourly")
    dew = series("temperature", "dew point")
    wsp = series("wind-speed", "sustained")
    wdr = series("direction", "wind")
    pop = series("probability-of-precipitation", "floating")
    sky = series("cloud-amount", "total")

    from data._tz import day_hm
    base = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = []
    for i, ts in enumerate(times[:hours]):
        tmp, dpt = temp[i] if i < len(temp) else None, dew[i] if i < len(dew) else None
        if tmp is None:
            continue
        try:
            valid = dt.datetime.fromisoformat(ts)
            fhr = max(1, int(round((valid - base).total_seconds() / 3600.0)))
            label = day_hm(valid)
        except (ValueError, TypeError):
            fhr, label = i + 1, ""
        out.append({"fhr": fhr, "tmp": tmp, "dpt": dpt,
                    "wsp": wsp[i] if i < len(wsp) else None,
                    "wdr": wdr[i] if i < len(wdr) else None,
                    "pop": pop[i] if i < len(pop) else None,
                    "sky": sky[i] if i < len(sky) else None,
                    "valid": label})
    if len(out) < 6:
        return None
    init = root.findtext(".//creation-date") or ""
    return {"station": station, "model": "NDFD (MOS-blend)",
            "init": (init[:16].replace("T", " ") + " ET") if init else "",
            "rows": out}


def mos_bundle():
    """MOS for all stations -> {'stations': {...}, 'available': bool}.

    Tries the IEM GFS-LAMP/MAV bulletins first; stations IEM cannot serve
    fall back to the NDFD hourly digital forecast so the page stays live.
    """
    stations = {}
    for name, sid in MOS_STATIONS.items():
        got = fetch_mos(sid)
        if got:
            stations[name] = got
            continue
        got = _fetch_ndfd_mos(sid)
        if got:
            stations[name] = got
    return {"stations": stations, "available": bool(stations),
            "source": ("Iowa Environmental Mesonet (GFS LAMP/MAV bulletins); "
                       "NWS NDFD hourly where bulletins are unavailable")}


# ---------------------------------------------------------------- charts


def _fetch_hourly(lat, lon):
    """NWS hourly gridpoint forecast -> list of periods (raw json).

    Cached 1 h per (lat, lon): the hourly gridpoint forecast updates about
    that often, and charts_bundle runs on every ~2-minute updater build -
    with 40+ chart locations an uncached fetch would cost ~80 api.weather.gov
    requests per build.
    """
    key = (round(lat, 3), round(lon, 3))
    now = time.time()
    hit = _HOURLY_CACHE.get(key)
    if hit and now - hit[0] < _HOURLY_TTL_S:
        return hit[1]
    periods = []
    for attempt in (1, 2):            # one retry: api.weather.gov hiccups
        try:
            p = requests.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
                             headers=UA, timeout=20)
            p.raise_for_status()
            hourly_url = p.json()["properties"]["forecastHourly"]
            r = requests.get(hourly_url, headers=UA, timeout=20)
            r.raise_for_status()
            periods = r.json()["properties"]["periods"]
            break
        except Exception:  # noqa: BLE001
            if attempt == 1:
                time.sleep(2)
    if not periods:
        return hit[1] if hit else []      # serve the stale copy on API trouble
    _HOURLY_CACHE[key] = (now, periods)
    return periods


_HOURLY_TTL_S = 3600
_HOURLY_CACHE = {}

_CHARTS_TTL_S = 6 * 3600       # rendered-chart bundle reuse window
_CHARTS_CACHE = {"t": 0.0, "bundle": None, "n": 0}


def _series(periods, hours=48):
    """Extract aligned time/temp/dew/pop/wind series (last `hours` hours)."""
    t, temp, dew, pop, wind, gust = [], [], [], [], [], []
    for pr in periods[:hours]:
        try:
            t.append(dt.datetime.fromisoformat(pr["startTime"]))
        except (KeyError, ValueError):
            continue
        temp.append(pr.get("temperature"))
        dew.append(pr.get("dewpoint", {}).get("value") if pr.get("dewpoint") else None)
        pop.append((pr.get("probabilityOfPrecipitation") or {}).get("value") or 0)
        ws = pr.get("windSpeed") or ""
        try:
            wind.append(int(ws.split()[0]))
        except (ValueError, IndexError):
            wind.append(None)
        gust.append(None)
    return t, temp, dew, pop, wind, gust


def _style_ax(ax, title):
    ax.set_facecolor("#10151f")
    ax.figure.set_facecolor("#0b0f16")
    ax.set_title(title, color="#e6edf3", fontsize=12, pad=8)
    ax.tick_params(colors="#7d8794", labelsize=9)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#2a3444")
    ax.grid(True, color="#1d2432", linewidth=.8)
    ax.title.set_fontweight("bold")


def _render_charts(name, t, temp, dew, pop, wind, _gust):
    """Three PNGs for one location -> list of static paths."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import numpy as np

    out = []
    x = mdates.date2num(t)
    days = mdates.DayLocator()
    fmt = mdates.DateFormatter("%a %H%M", tz=dt.timezone.utc)

    def save(fig, tag):
        os.makedirs(CHART_DIR, exist_ok=True)
        # URL-safe filename: letters/digits/_/- only (commas+spaces in
        # "Greeneville, TN" broke the Pages asset rewriter and curl)
        slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
        path = os.path.join(CHART_DIR, f"{slug}_{tag}.png")
        fig.savefig(path, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        out.append(path)

    # temp + dew point
    fig, ax = plt.subplots(figsize=(9, 3.2), dpi=110)
    if any(v is not None for v in temp):
        ax.plot(x, temp, color="#ff8c42", lw=2.2, label="Temperature (F)")
        ax.fill_between(x, temp, min((v for v in temp if v is not None), default=0) - 4,
                        color="#ff8c42", alpha=.12)
    if any(v is not None for v in dew):
        ax.plot(x, dew, color="#4da3ff", lw=1.8, label="Dew point (F)")
    _style_ax(ax, f"{name} - Temperature & Dew Point (NWS hourly, 48 h)")
    ax.xaxis.set_major_locator(days)
    ax.xaxis.set_major_formatter(fmt)
    ax.legend(facecolor="#10151f", edgecolor="#2a3444", labelcolor="#cdd7e4",
              loc="upper left", fontsize=9)
    save(fig, "temp")

    # PoP
    fig, ax = plt.subplots(figsize=(9, 2.6), dpi=110)
    ax.bar(x, [p or 0 for p in pop], width=.035, color="#42a5f5", alpha=.85)
    _style_ax(ax, f"{name} - Chance of Precipitation (%)")
    ax.set_ylim(0, 100)
    ax.xaxis.set_major_locator(days)
    ax.xaxis.set_major_formatter(fmt)
    save(fig, "pop")

    # wind
    fig, ax = plt.subplots(figsize=(9, 2.6), dpi=110)
    if any(v is not None for v in wind):
        ax.plot(x, wind, color="#aed581", lw=2, label="Wind (mph)")
        ax.fill_between(x, wind, 0, color="#aed581", alpha=.15)
    _style_ax(ax, f"{name} - Wind Speed (mph)")
    ax.xaxis.set_major_locator(days)
    ax.xaxis.set_major_formatter(fmt)
    ax.legend(facecolor="#10151f", edgecolor="#2a3444", labelcolor="#cdd7e4",
              loc="upper right", fontsize=9)
    save(fig, "wind")
    return out


def charts_bundle(locations):
    """Render charts for [(name, lat, lon)] -> {'locations': {...}} manifest.

    Result cached 6 h in-process (the matplotlib renders, not the data, are
    the cost - re-rendering 40+ cities x 3 charts every ~2-minute build
    would dominate the updater cycle; the hourly data itself refreshes
    hourly in _fetch_hourly).
    """
    now = time.time()
    if (_CHARTS_CACHE["bundle"] is not None
            and now - _CHARTS_CACHE["t"] < _CHARTS_TTL_S
            and len(locations) == _CHARTS_CACHE.get("n")):
        return _CHARTS_CACHE["bundle"]
    out = {}
    for name, lat, lon in locations:
        try:
            periods = _fetch_hourly(lat, lon)
            if not periods:
                continue
            t, temp, dew, pop, wind, gust = _series(periods)
            pngs = _render_charts(name, t, temp, dew, pop, wind, gust)
            out[name] = {"lat": lat, "lon": lon,
                         "charts": [{"tag": os.path.basename(p).rsplit("_", 1)[-1].split(".")[0],
                                     "url": "../forecast_charts/" + os.path.basename(p)}
                                    for p in pngs]}
        except Exception:  # noqa: BLE001 - one bad station must not break the page
            continue
    bundle = {"locations": out}
    # complete bundles cache the full window; a partial one (a city that
    # failed its fetch this pass) only 30 min so it repairs quickly
    ttl = _CHARTS_TTL_S if len(out) >= len(locations) else 1800
    if out:
        _CHARTS_CACHE.update(t=now - (_CHARTS_TTL_S - ttl), bundle=bundle,
                             n=len(locations))
    return bundle


def forecast_charts_bundle():
    """Bundle for the site: Greeneville + every East TN city."""
    import config
    from data.observations import EAST_TN_CITIES
    locs = [(config.DEFAULT_LOCATION_NAME, config.LATITUDE, config.LONGITUDE)]
    locs += [(name, la, lo) for name, (la, lo) in sorted(EAST_TN_CITIES.items())]
    return charts_bundle(locs)

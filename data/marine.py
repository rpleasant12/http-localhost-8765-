"""Marine bundle for the Tropical page's SST + wave-tracking maps.

Two feed families, all free/keyless, fetched once per ~30 min:

1. NDBC buoy observations (www.ndbc.noaa.gov/data/realtime2/<id>.txt):
   significant wave height (WVHT, m), dominant period (DPD, s), mean wave
   direction (MWD, degT) and water temperature (WTMP, C) for a curated
   list of Atlantic / Gulf / Caribbean stations that matter to Tennessee
   visitors - the Gulf Stream, the Florida straits and the hurricane
   Main Development Region.

2. GIBS SST tiles are rendered CLIENT-SIDE (GHRSST L4 MUR, keyless WMTS)
   so this module never touches raster data.

MM values are missing (NDBC's sentinel) - dropped, never invented.
"""
import datetime as dt
import json
import math
import os
import re
import time
import threading

import requests

OUT_DIR = "static/marine"

# Gulf Stream axis: the warm "river" on the SST map is the Gulf Stream, so
# we draw its core. Source: GOFS 3.1 (HYCOM) surface currents, thredds
# OpenDAP slice at tds.hycom.org - free, keyless, and it actually answers
# (probed 2026-09-29: the ERDDAP noaacwBLENDEDNRTcurrentsDaily mirror was
# all-NaN in every era, pfeg's jplOscar frozen at 2014, NOMADS OpenDAP
# retired by SCN 25-81, RTOFS .nc a 155 MB download, and "GFSOI"/CO-OPS
# have no such product - CO-OPS is tide gauges). The axis is where
# cross-section speed peaks, walked north with continuity so Sargasso
# warm-core rings cannot steal the line.
GS_DIR = os.path.join(OUT_DIR, "gs")
GS_DAP = "https://tds.hycom.org/thredds/dodsC/GLBy0.08/latest"
# Corridor: Cape Canaveral FL -> beyond Cape Hatteras NC, 0-360 lon.
GS_LAT0, GS_LAT1 = 27.0, 39.0
GS_LON0, GS_LON1 = 276.0, 288.0                   # 84W -> 72W
GS_TTL = 26 * 3600                                # daily analysis; ~1 refresh/day
GS_WINDOW_DEG = 1.25                              # axis-track lon window
GS_MIN_SPD = 0.35                                 # m/s floor: still the jet?

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}

# (id, name, lat, lon) -Atlantic/Gulf/Caribbean NDBC stations. Coords are
# static station positions from the NDBC station table.
STATIONS = (
    ("41049", "Frying Pan Shoals NC", 33.436, -77.184),
    ("41025", "Diamond Shoals NC (Cape Hatteras)", 35.010, -75.310),
    ("41010", "East of Cape Canaveral FL", 28.880, -78.480),
    ("41009", "Canaveral Approach FL", 28.500, -80.190),
    ("41008", "Grays Reef GA", 31.400, -80.870),
    ("41004", "Edisto SC", 32.500, -79.090),
    ("41002", "South Hatteras - Gulf Stream", 32.310, -75.320),
    ("41001", "East of Cape Hatteras", 34.730, -72.660),
    ("41048", "SE of Bermuda (MDC south)", 31.904, -69.624),
    ("41040", "North of Puerto Rico", 21.700, -57.600),
    ("41044", "East of the Leeward Islands (MDC)", 15.600, -56.400),
    ("42056", "Bay of Campeche SW Gulf", 19.360, -94.400),
    ("42055", "NW Campeche Bay of Campeche", 21.160, -93.900),
    ("42019", "Freeport TX (Gulf)", 27.840, -95.350),
    ("42035", "Galveston TX", 29.230, -94.410),
    ("42002", "Central Gulf", 26.090, -93.780),
    ("42001", "Mid Gulf - hurricane alley", 27.290, -90.130),
    ("42003", "East Gulf", 26.010, -85.640),
    ("42020", "Corpus Christi TX", 26.970, -96.430),
    ("42036", "West Tampa FL", 28.500, -84.520),
    ("42022", "Yucatan Channel", 22.100, -86.140),
    ("42013", "Dry Tortugas FL", 24.600, -82.930),
    ("41046", "NW Bahamas", 27.500, -78.500),
    ("41047", "NE Bahamas (SE of Charleston)", 27.800, -73.000),
    ("41041", "Central tropical Atlantic", 18.900, -49.970),
    ("41043", "E of the Leewards", 15.700, -63.500),
    ("41045", "Tropical N Atlantic", 21.200, -58.500),
    ("41006", "SE of Bermuda", 27.900, -63.000),
    ("41048x", "", 0.0, 0.0),          # placeholder removed below
)
STATIONS = tuple(s for s in STATIONS if s[1])

# Southeast beaches people actually swim at, each with its NDBC wave buoy
# (nearest offshore station; the wave field at the coast is dominated by
# that buoy's height/period). Coordinates only used for a possible map pin.
BEACHES = (
    ("Cape Hatteras, OBX NC", "41025"),
    ("Wilmington NC", "41049"),
    ("Myrtle Beach SC", "41004"),
    ("Charleston SC", "41004"),
    ("Savannah - Jekyll Island GA", "41008"),
    ("Jacksonville FL", "41009"),
    ("Daytona Beach FL", "41009"),
    ("Cocoa Beach FL", "41009"),
)


def _gs_path():
    return os.path.join(GS_DIR, "axis.json")


def _gs_load_cache():
    try:
        with open(_gs_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                              # noqa: BLE001
        return None


def _gs_save_cache(payload):
    try:
        os.makedirs(GS_DIR, exist_ok=True)
        with open(_gs_path(), "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:                              # noqa: BLE001
        pass


def _gs_fetch_axis():
    """Slice the corridor's surface u/v from GOFS/HYCOM over OpenDAP, then
    walk the per-latitude speed maximum north with a continuity window
    (the Gulf Stream axis). Returns the payload to cache; raises on junk."""
    import numpy as np
    import netCDF4
    if hasattr(netCDF4, "set_default_timeout"):
        netCDF4.set_default_timeout(45)
    ds = netCDF4.Dataset(GS_DAP)
    try:
        lat = np.asarray(ds.variables["lat"][:], dtype=float)
        lon = np.asarray(ds.variables["lon"][:], dtype=float)
        tv = ds.variables["time1"]
        times = netCDF4.num2date(np.asarray(tv[:]), tv.units,
                                 only_use_cftime_datetimes=False)
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        ti = max((i for i, t in enumerate(times) if t <= now), default=0)
        i0 = int(np.searchsorted(lat, GS_LAT0))
        i1 = int(np.searchsorted(lat, GS_LAT1))
        j0 = int(np.searchsorted(lon, GS_LON0))
        j1 = int(np.searchsorted(lon, GS_LON1))
        u = np.asarray(ds.variables["ssu"][ti, i0:i1, j0:j1], dtype=float)
        v = np.asarray(ds.variables["ssv"][ti, i0:i1, j0:j1], dtype=float)
        date_s = times[ti].strftime("%Y-%m-%d %H:%M")
    finally:
        ds.close()
    spd = np.hypot(u, v)
    spd[spd > 9] = np.nan                       # sub-cell junk guard
    # Anchor on the southernmost latitude with data (Florida Straits core),
    # then walk north keeping the speed max within +/- GS_WINDOW_DEG of the
    # previous point - a warm-core ring offshore is no longer reachable.
    jw = max(1, int(GS_WINDOW_DEG / abs(float(lon[1] - lon[0]))))
    start = next((k for k in range(spd.shape[0])
                  if np.isfinite(spd[k]).any()), None)
    if start is None:
        raise ValueError("no valid corridor data")
    jc = int(np.nanargmax(spd[start]))
    pts = []
    for k in range(start, spd.shape[0]):
        row = spd[k]
        lo, hi = max(0, jc - jw), min(row.size, jc + jw + 1)
        seg = row[lo:hi]
        if not np.isfinite(seg).any():
            break
        jj = lo + int(np.nanargmax(seg))
        s = float(row[jj])
        if s < GS_MIN_SPD:
            break                               # jet left the corridor/window
        # jj indexes the SLICED columns; lon needs the j0 offset (first run
        # drew the jet over Greenwich!)
        pts.append((round(float(lat[i0 + k]), 2),
                    round(float(lon[j0 + jj]), 2), round(s, 2)))
        jc = jj
    if len(pts) < 8:                            # stream not resolvable
        raise ValueError(f"axis under-resolved ({len(pts)} pts)")
    # 3-pt median on lon kills single-cell spikes without bending the jet
    lons = [p[1] for p in pts]
    med = [sorted(lons[max(0, i - 1):i + 2])[1] for i in range(len(lons))]
    points = [{"lat": p[0], "lon": round(m, 2), "spd": p[2]}
              for p, m in zip(pts, med)]
    return {"ok": True, "date": date_s, "points": points,
            "n": len(points),
            "maxSpd": max(p["spd"] for p in points), "ts": time.time(),
            "src": "GOFS 3.1 (HYCOM) surface currents, tds.hycom.org"}


def gs_bundle():
    """Gulf Stream axis for the SST map, disk-cached ~1 day; never raises."""
    cached = _gs_load_cache()
    if cached and time.time() - cached.get("ts", 0) < GS_TTL and cached.get("ok"):
        return cached
    try:
        data = _gs_fetch_axis()
    except Exception as exc:                       # noqa: BLE001
        data = {"ok": False, "reason": str(exc)[:120], "points": []}
        if cached and cached.get("ok"):
            data["fallback"] = True                # stale tiles beat no tiles
            data["points"] = cached["points"]
            data["date"] = cached.get("date", "")
            data["n"] = cached.get("n", 0)
            data["maxSpd"] = cached.get("maxSpd", 0)
    _gs_save_cache(data)
    return data


def _gs_with_timeout(seconds=90):
    """gs_bundle() with a hard wall-clock cap - a hung OpenDAP socket must
    not wedge the updater thread that calls collect_data."""
    box = {}
    def run():
        try:
            box["res"] = gs_bundle()
        except Exception as exc:                   # noqa: BLE001
            box["res"] = {"ok": False, "reason": str(exc)[:120], "points": []}
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(seconds)
    if "res" not in box:
        return {"ok": False, "reason": f"timed out after {seconds}s",
                "points": []}
    return box["res"]


def _rip_tier(wvht, dpd):
    """Qualitative rip-current risk from NWS practice: wave HEIGHT drives
    the feeder currents (ramping up from ~0.8 m / 2-3 ft surf), long-period
    swell (DPD 10 s+) packs proportionally more energy and boosts risk even
    at moderate heights. Buoy-to-beach is a big simplification - the one-
    liner says so, and always points at a lifeguard/flag check."""
    if wvht is None:
        return ("unknown", "#8b97a5",
                "nearest buoy not reporting waves - check beach flags")
    long_swell = (dpd or 0) >= 10
    mid_swell = (dpd or 0) >= 8
    if wvht >= 1.8 or (wvht >= 1.0 and long_swell):
        return ("high", "#d32f2f",
                "life-threatening rip currents likely - swim near a lifeguard or not at all")
    if wvht >= 0.8 or mid_swell:
        return ("moderate", "#ff9800",
                "rip currents possible - stronger near piers, jetties and sandbars")
    return ("low", "#43a047", "calm surf - usual beach caution applies")


def _beach_risks(buoys):
    """Per-beach one-liner from the nearest buoy that has wave data."""
    by_id = {b["id"]: b for b in buoys}
    out = []
    for name, sid in BEACHES:
        b = by_id.get(sid) or {}
        if not (b.get("ok") and b.get("wvht") is not None):
            tier, col, text = _rip_tier(None, None)
            src = f"buoy {sid} not reporting"
        else:
            tier, col, text = _rip_tier(b["wvht"], b["dpd"])
            bits = [f"{b['wvht']:.1f} m at {b['dpd']:.0f} s" if b.get("dpd") is not None
                    else f"{b['wvht']:.1f} m"
                    ]
            if b.get("mwd") is not None:
                bits.append(f"from {b['mwd']:.0f}°")
            src = " ".join(bits) + f" via buoy {sid}"
        out.append({"beach": name, "risk": tier, "color": col,
                    "text": text, "src": src})
    return out

_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()
_TTL = 30 * 60                       # 30 min; buoys report hourly

# MUR SST analysis date: GIBS publishes T-1 (today's 404s while it builds).
# Probed 2026-09-29: T-2 was the newest served day (T-1 still 404), so walk
# back from T-1 to the newest date GIBS actually answers for - the "latest
# analysis" KPI must never point at tiles that will not load.
def _sst_dates():
    today = dt.datetime.now(dt.timezone.utc).date()
    probe = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/" \
            "GHRSST_L4_MUR_Sea_Surface_Temperature/default/{}/" \
            "GoogleMapsCompatible_Level7/4/9/13.png"
    for back in range(1, 6):
        d = today - dt.timedelta(days=back)
        try:
            r = requests.get(probe.format(d.isoformat()), headers=UA, timeout=10)
            if r.ok:
                latest = d.isoformat()
                break
        except requests.RequestException:
            continue
    else:
        latest = (today - dt.timedelta(days=2)).isoformat()   # honest fallback
    week = (dt.date.fromisoformat(latest) - dt.timedelta(days=7)).isoformat()
    return {"latest": latest, "weekAgo": week}


def _parse_realtime2(text):
    """Newest row with usable wave data -> dict; rows are newest-first."""
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split()
        if len(parts) < 19:
            continue
        def f(i):
            return None if parts[i] == "MM" else float(parts[i])
        yy, mo, dd, hh, mn = (int(x) for x in parts[:5])
        # columns: #YY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD PRES ATMP
        #          WTMP DEWP VIS PTDY TIDE  -> WVHT is index 8 (NOT 7, which
        # is gust speed - the first draft read gusts as 8-m waves!)
        wvht, dpd, apd = f(8), f(9), f(10)
        mwd = f(11)
        wtmp = f(14)
        # physical sanity: sub-2 s "dominant periods" are sensor junk, and
        # 25 m+ significant heights are outside buoy design range
        if dpd is not None and dpd < 2:
            dpd = None
        if apd is not None and apd < 2:
            apd = None
        if wvht is not None and wvht > 25:
            wvht = None
        if wvht is None and dpd is None and wtmp is None:
            continue
        try:
            ts = dt.datetime(yy, mo, dd, hh, mn, tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        return {"ts": ts.isoformat(), "wvht": wvht, "dpd": dpd,
                "apd": apd, "mwd": mwd, "wtmp": wtmp}
    return {}


def _wave_color(wvht):
    """NDBC sea-state ramp for markers (green calm -> purple violent)."""
    if wvht is None:
        return "#8b97a5"
    if wvht >= 6:
        return "#7b1fa2"
    if wvht >= 4:
        return "#d32f2f"
    if wvht >= 3:
        return "#ff9800"
    if wvht >= 2:
        return "#ffd54f"
    if wvht >= 1:
        return "#8bc34a"
    return "#43a047"


def _sea_state(wvht):
    if wvht is None:
        return ("unknown", "report missing waves - check other stations")
    if wvht >= 6:
        return ("violent", "hurricane-force seas - coasts: storm surge risk")
    if wvht >= 4:
        return ("heavy", "very rough seas - shipping reroutes; rip current risk ashore")
    if wvht >= 3:
        return ("rough", "rough seas - small craft stay in port near this buoy")
    if wvht >= 2:
        return ("moderate", "moderate seas - boaters: watch shoaling near shore")
    if wvht >= 1:
        return ("slight", "slight seas - typical chop")
    return ("calm", "calm seas - beach weather on the water")


def _swell_band(dpd):
    """Surf-convention period band. Period = swell quality: wind chop
    (<7 s) is disorganized and closes out fast; windswell (7-10 s) is
    rideable but bumpy; 10 s+ is real groundswell that wraps into beach
    breaks; 13 s+ is the long-period stuff that outruns storms - the early
    head-up that a hurricane swell train is arriving before the storm
    does. Cool colors = longer period, deliberately unlike the warm
    height ramp on the markers."""
    if dpd is None:
        return ("unknown", "#8b97a5")
    if dpd >= 13:
        return ("groundswell 13 s+", "#7c4dff")
    if dpd >= 10:
        return ("long swell 10-13 s", "#5c6bc0")
    if dpd >= 7:
        return ("windswell 7-10 s", "#26c6da")
    return ("chop <7 s", "#90a4ae")


def _fetch_station(sid):
    try:
        r = requests.get(f"https://www.ndbc.noaa.gov/data/realtime2/{sid}.txt",
                         headers=UA, timeout=20)
        if not r.ok:
            return {}
        return _parse_realtime2(r.text)
    except requests.RequestException:
        return {}


def _build():
    buoys = []
    for sid, name, lat, lon in STATIONS:
        obs = _fetch_station(sid)
        wvht = obs.get("wvht")
        col = _wave_color(wvht)
        state, word = _sea_state(wvht)
        band, band_col = _swell_band(obs.get("dpd"))
        buoys.append({
            "id": sid, "name": name, "lat": lat, "lon": lon,
            "wvht": wvht, "dpd": obs.get("dpd"), "apd": obs.get("apd"),
            "mwd": obs.get("mwd"), "wtmp": obs.get("wtmp"),
            "color": col, "state": state, "word": word,
            "band": band, "bandColor": band_col,
            "ts": obs.get("ts"), "ok": bool(obs),
        })
    ok = [b for b in buoys if b["ok"]]
    worst = max((b for b in ok if b["wvht"] is not None),
                key=lambda b: b["wvht"], default=None)
    # Gulf of Mexico box: the loose first draft let the central tropical
    # Atlantic (41041, 19N -50W) count as "Gulf"
    gulf = [b for b in ok if 18.5 <= b["lat"] <= 30.5
            and -98.0 <= b["lon"] <= -80.5 and b["wvht"] is not None]
    gulf_worst = max(gulf, key=lambda b: b["wvht"], default=None)
    # Surf-relevant long-period energy: how many reporting buoys see 10 s+
    # dominant period, and the longest-period report on the board (height
    # says how big, period says whether it is organized swell or chop).
    surf = [b for b in ok if b["wvht"] is not None and b["dpd"] is not None]
    best = max(surf, key=lambda b: b["dpd"], default=None)
    return {
        "swell": {"nSurf": len(surf),
                  "long": sum(1 for b in surf if b["dpd"] >= 10),
                  "best": ({"id": best["id"], "name": best["name"],
                            "dpd": best["dpd"], "wvht": best["wvht"],
                            "band": best["band"]} if best else None)},
        "ok": bool(ok),
        "buoys": buoys,
        "nOk": len(ok),
        "worst": ({"name": worst["name"], "wvht": worst["wvht"],
                   "dpd": worst["dpd"], "mwd": worst["mwd"],
                   "state": worst["state"]} if worst else None),
        "gulfWorst": ({"name": gulf_worst["name"], "wvht": gulf_worst["wvht"],
                       "dpd": gulf_worst["dpd"]} if gulf_worst else None),
        "sst": _sst_dates(),
        "beachRisk": _beach_risks(buoys),
        "generated": time.strftime("%Y-%m-%d %H:%M"),
    }


def marine_bundle():
    """Buoys + SST dates, cached 30 min; never raises."""
    now = time.time()
    if _cache["data"] and now - _cache["at"] < _TTL:
        return _cache["data"]
    with _lock:
        if _cache["data"] and time.time() - _cache["at"] < _TTL:
            return _cache["data"]
        try:
            data = _build()
            data["gulfStream"] = _gs_with_timeout()
        except Exception as exc:                     # noqa: BLE001
            data = {"ok": False, "reason": str(exc)[:120], "buoys": [],
                    "sst": _sst_dates(),
                    "gulfStream": {"ok": False, "points": []}}
        _cache["at"], _cache["data"] = time.time(), data
        return data


if __name__ == "__main__":
    import json
    b = marine_bundle()
    print("ok:", b["ok"], "| buoys reporting:", b["nOk"], "/", len(b["buoys"]))
    if b.get("worst"):
        print("worst:", b["worst"])
    if b.get("gulfWorst"):
        print("gulf:", b["gulfWorst"])
    print("sst dates:", b["sst"])
    g = gs_bundle()
    print("gs axis:", g.get("ok"), g.get("date"), g.get("n"), "pts, max",
          g.get("maxSpd"), "m/s")
    print("swell:", b.get("swell"))
    for x in b["buoys"][:6]:
        print("  ", x["id"], x["name"][:28].ljust(28),
              f"wvht={x['wvht']} dpd={x['dpd']} mwd={x['mwd']} wtmp={x['wtmp']}",
              x["state"])

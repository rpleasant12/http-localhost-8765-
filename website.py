"""Tennessee Weather Network public site generator.

Builds a multi-page static website (static/site/) from the same live,
keyless feeds as the app - the FULL option set:

  index.html      current conditions, alerts, SPC risks, AI storm tracker
  radar.html      interactive map: RainViewer real-time, Future radar (HRRR
                  0-18 h + NAM 18-48 h), MRMS (10 products), NWS mosaic
  satellite.html  ALL GOES-19 ABI bands (17): water vapor high/mid/low,
                  GINI CONUS + full-disk, IR, visible bands 1-6, fire,
                  ozone, CO2 - animated on the map
  models.html     EVERY model x product catalog (17 models, 20+ products
                  each), with on-demand render buttons + frame steppers
  tropical.html   NHC page: active storms with cones/tracks, wind field,
                  outlook swath, official NHC graphics
  severe.html     severe dashboard: official watches/warnings, SPC outlook
                  outlines, mesoscale discussions, storm reports (SPC/WPC)
  national.html   WPC charts + SPC observed upper-air analyses
  forecast.html   7-day + 24-hour point forecast and alerts

Desktop and mobile out of the box (responsive CSS, touch players). The
site_updater regenerates every few minutes; pages re-fetch data.json in
the browser. Pages reference render artifacts with RELATIVE paths
(../hrrr/...), so the site works wherever static/ is served.
"""
import datetime as dt
import html
import json
import os
import re
import threading
import time

import config

SITE_DIR = os.path.join("static", "site")

# single-radar NEXRAD sites offered on the radar page (cover TN + neighbors)
NEXRAD_SITES = ["KMRX", "KJKL", "KOHX", "KHTX", "KTYX", "KFCX",
                "KGSP", "KCAE", "KFFC", "KGWX", "KBMX", "KPAH"]
# local radars pinned to the top of the picker
NEXRAD_LOCAL = ["KMRX", "KJKL", "KOHX", "KHTX", "KFCX", "KGSP"]

# basemap: Mapbox vector styles when MAPBOX_TOKEN is set, keyless OSM otherwise
_MAPBOX_TOKEN = ""
try:
    with open("mapbox_token.txt", encoding="utf-8") as _f:
        _MAPBOX_TOKEN = _f.read().strip()
except OSError:
    pass
MAPBOX_JS = (
    '<script src="https://api.mapbox.com/mapbox-gl-js/v3.5.1/mapbox-gl.js"></script>'
    '<script src="https://unpkg.com/mapbox-gl-leaflet@0.0.16/leaflet-mapbox-gl.js"></script>'
    '<link href="https://api.mapbox.com/mapbox-gl-js/v3.5.1/mapbox-gl.css" rel="stylesheet"/>'
    if _MAPBOX_TOKEN else ""
)


def _basemap_js():
    """JS snippet returning the map base layer for Leaflet."""
    if _MAPBOX_TOKEN:
        return (
            'const rc = L.mapboxGL({accessToken: MAPBOX_TOKEN, style: "mapbox://styles/mapbox/light-v11", attribution: "&copy; Mapbox"}).addTo(map);'
        )
    return (
        'L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",'
        '{ maxNativeZoom: 19, maxZoom: 21, attribution: "&copy; OpenStreetMap contributors" }).addTo(map);'
    )


def _mapbox_token_js():
    return f"const MAPBOX_TOKEN = {json.dumps(_MAPBOX_TOKEN)};" if _MAPBOX_TOKEN else \
        "const MAPBOX_TOKEN = null;"


# ---------------------------------------------------------------- data
def _compass(deg):
    try:
        pts = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        return pts[int((float(deg) + 22.5) // 45) % 8]
    except (TypeError, ValueError):
        return ""


def _f(c):
    return None if c is None else c * 9 / 5 + 32


def _mph(kmh):
    return None if kmh is None else kmh * 0.621371


# Skew-T rotation: render a few new (site, hour) combos per collect cycle so
# the 8-site matrix fills in over time; combos are disk-cached by the sounding
# module, so this only ever pays the expensive RAP decode once per combo.
_SND_BUDGET = 3
_SND_ORDER_FILE = os.path.join("static", "sounding", "render_order.json")


def _snd_rotate():
    """Shift the render-order pointer so successive cycles cover all sites."""
    try:
        n = int(open(_SND_ORDER_FILE).read().strip() or 0)
    except Exception:  # noqa: BLE001
        n = 0
    try:
        os.makedirs(os.path.dirname(_SND_ORDER_FILE), exist_ok=True)
        open(_SND_ORDER_FILE, "w").write(str(n + 1))
    except Exception:  # noqa: BLE001
        pass


STATIC_AI = os.path.join("static", "aimodels")
_MS_RX = re.compile(r"(mpas|shield)_(\w+)_(\w+)_f(\d+)_(\d{10})\.jpg$")


_MS_DOM_RX = re.compile(r"_(atl|epac|wpac|conus|global)_")


def _cache_mpas_shield_from_disk(payload):
    """Index already-downloaded MPAS/SHiELD frames (no network) into the payload.

    Frames are grouped by (kind, key, domain, init) so an animation never mixes
    domains or init cycles. A live-fetched product only accepts disk frames of
    its own init AND domain; a queued product is filled with the largest
    coherent disk group (CONUS preferred). Stale SHiELD cycles (older inits)
    are dropped entirely so the picker never animates yesterday's 2 frames.
    """
    try:
        names = os.listdir(STATIC_AI)
    except OSError:
        return
    groups = {}
    for fn in names:
        m = _MS_RX.match(fn)
        if not m:
            continue
        kind, key, dom, fh, init = m.groups()
        dom = (dom or "").lower()
        if kind == "shield" or dom not in ("atl", "epac", "wpac", "conus", "global"):
            dom = "conus"            # SHiELD regions are already in the field files
        groups.setdefault((kind, key, dom, init), []).append((int(fh), fn))
    # keep only the newest SHiELD init on disk (older cycles are stale)
    shield_inits = {init for (kind, _k, _d, init) in groups if kind == "shield"}
    newest_shield = max(shield_inits) if shield_inits else None
    for (kind, key, dom, init), items in sorted(groups.items(),
                                                key=lambda kv: -len(kv[1])):
        if kind == "shield" and newest_shield and init != newest_shield:
            continue
        bucket = payload[kind].get(key)
        if bucket and bucket.get("frames"):
            if str(bucket.get("init")) != init:
                continue
            bdom = _MS_DOM_RX.search(bucket["frames"][0]["url"] or "")
            if bdom and bdom.group(1) != dom:
                continue
            have = {f["hour"] for f in bucket["frames"]}
            add = [(fh, fn) for fh, fn in items if fh not in have]
        elif bucket:
            bucket.update({"init": init, "domain": dom})
            add = items
        else:
            payload[kind][key] = {"init": init, "domain": dom, "frames": []}
            add = items
        for fh, fn in add:
            payload[kind][key]["frames"].append({"hour": fh, "url": "../aimodels/" + fn,
                                                 "label": f"F{fh:03d}"})
        payload[kind][key]["frames"].sort(key=lambda f: f["hour"])


def collect_data():
    """Everything the site needs, from disk caches + a few fast NWS calls."""
    from data.nws import get_active_alerts, get_current_conditions, get_forecast, get_hourly
    from data.radar_frames import (future_bundle, get_past_frames,
                                   get_nowcast_frames)
    from data.mrms import (mrms_bundle, start_mrms_renderer,
                           CATALOG as MRMS_CATALOG)
    from data.nws_radar import nws_bundle
    from data.severe import spc_outlooks
    from data.satellite_bands import BANDS as SAT_BANDS, band_bundle
    from ai.storm_tracker import summarize

    lat, lon = config.LATITUDE, config.LONGITUDE

    cur = get_current_conditions(lat, lon) or {}
    temp_c = (cur.get("temperature") or {}).get("value")
    dew_c = (cur.get("dewpoint") or {}).get("value")
    wind_kmh = (cur.get("windSpeed") or {}).get("value") if cur.get("windSpeed") else None

    forecast = get_forecast(lat, lon) or []
    days = []
    seen = set()
    for period in forecast:
        name = period.get("name", "")
        if name in seen or "Night" in name:
            continue
        seen.add(name)
        days.append({
            "name": name,
            "hi": period.get("temperature"),
            "text": period.get("shortForecast") or "",
            "wind": period.get("windSpeed") or "",
            "pop": (period.get("probabilityOfPrecipitation") or {}).get("value") or 0,
        })
        if len(days) == 7:
            break

    hourly = []
    for period in (get_hourly(lat, lon) or [])[:24]:
        t = dt.datetime.fromisoformat(period["startTime"])
        hourly.append({
            "t": t.strftime("%H:%M"),
            "temp": period.get("temperature"),
            "pop": (period.get("probabilityOfPrecipitation") or {}).get("value") or 0,
            "text": period.get("shortForecast") or "",
        })

    alerts = [{
        "event": a.get("event") or "Alert",
        "severity": a.get("severity") or "Unknown",
        "areaDesc": a.get("areaDesc") or "",
        "expires": (a.get("expires") or "")[:16].replace("T", " ") + "Z" if a.get("expires") else "",
    } for a in (get_active_alerts(lat, lon) or [])]

    # SPC risk at home, days 1-3 (point-in-polygon against outlook polygons)
    outlooks = spc_outlooks()
    spc_days = {}
    try:
        from data.severe import CAT_ORDER, _pip
        for day_key, label in (("day1", "Day 1"), ("day2", "Day 2"), ("day3", "Day 3")):
            best = None
            for f in ((outlooks.get(day_key) or {}).get("features") or []):
                if _pip(lat, lon, f["geometry"]):
                    rank = CAT_ORDER.index(f["label"]) if f["label"] in CAT_ORDER else -1
                    if best is None or rank >= best[0]:
                        best = (rank, f)
            cat = best[1] if best else None
            spc_days[label] = {
                "label": (cat.get("label2") or cat.get("label") or "NONE") if cat else "NONE",
                "fill": (cat.get("fill") or "#c1e9c1") if cat else "#c1e9c1",
            }
    except Exception:  # noqa: BLE001 - SPC down must not kill the site
        spc_days = {}

    # radar bundles (disk reads; renderers run in app.py / the updater)
    past = get_past_frames()
    nowcast = get_nowcast_frames()
    # RainViewer's index occasionally stalls (its outage on 2026-09-09 served
    # 4-hour-old frames all day) - drop stale frames so the page falls back
    # to the official NWS mosaic instead of presenting old radar as live
    _now = time.time()
    past = [f for f in past if (_now - (f.get("time") or 0)) < 2400]
    nowcast = [f for f in nowcast if abs((f.get("time") or 0) - _now) < 3600]
    future = future_bundle(max_hours=48)
    mrms = mrms_bundle("cref")
    nws = nws_bundle()

    def _existing(frames):
        """Keep only frames whose rendered PNG is actually on disk (renderers
        prune old scans, so descriptors can outlive their files)."""
        out = []
        for f in frames:
            u = f.get("pngUrl") or ""
            if not u:
                out.append(f)
                continue
            rel = u.replace("../", "static/").replace("/app/static/", "static/")
            if os.path.isfile(rel):
                out.append(f)
        return out

    # satellite: ALL ABI bands; start renderers for the site's picks
    sat_bands = {}
    for key, b in SAT_BANDS.items():
        try:
            bb = band_bundle(key)
            sat_bands[key] = {
                "label": b["label"],
                "total": bb.get("total", 0),
                "ready": bb.get("ready", 0),
                "frames": _existing((bb.get("frames") or [])[-8:]),
            }
        except Exception:  # noqa: BLE001
            continue

    cells = (future["frames"][-1].get("cells") if future["frames"] else None) or []

    # ---------------- severe payload ----------------
    def _geo(geom):
        return geom if geom and geom.get("coordinates") else None

    outlook_feats = []
    for day_key in ("day1", "day2", "day3", "day1_torn", "day1_hail", "day1_wind", "day2_torn", "day2_hail", "day2_wind"):
        for f in ((outlooks.get(day_key) or {}).get("features") or []):
            g = _geo(f.get("geometry"))
            if g and (f.get("label") or "").upper() not in ("", "NONE", "LESS THAN 5% ALL AREAS", "LESS THAN 2% ALL AREAS", "LESS THAN 15% ALL AREAS"):
                outlook_feats.append({"day": day_key, "label": f["label"],
                                      "label2": f.get("label2") or "",
                                      "fill": f.get("fill") or "#c1e9c1", "geometry": g})
    outlook_feats = outlook_feats[:120]

    try:
        from data.national import us_warnings
        ww = []
        for f in (us_warnings() or [])[:250]:
            g = _geo(f.get("geometry"))
            if g:
                ww.append({"event": f.get("event") or f.get("prod_type") or "Alert",
                           "code": f.get("code") or "",
                           "kind": f.get("kind") or "warning",
                           "color": f.get("color") or "#ff9f43",
                           "severity": f.get("severity") or "",
                           "areaDesc": f.get("areaDesc") or "",
                           "headline": f.get("headline") or "",
                           "tor": f.get("tor") or "",
                           "expires": f.get("expires") or "",
                           "url": f.get("url") or "", "geometry": g})
    except Exception:  # noqa: BLE001
        ww = []

    try:
        from data.severe import tn_alerts
        tn = []
        for a in (tn_alerts() or [])[:80]:
            g = _geo(a.get("geometry"))
            tn.append({"event": a["event"], "severity": a["severity"],
                       "areaDesc": a["areaDesc"], "expires": a["expires"],
                       "headline": a.get("headline") or "", "geometry": g})
    except Exception:  # noqa: BLE001
        tn = []

    # SPC mesoscale discussions + storm reports
    md, reports = [], {}
    try:
        import requests
        UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}
        r = requests.get("https://www.spc.noaa.gov/products/spcmd/lastmd.txt", headers=UA, timeout=10)
        if r.ok and "MESOSCALE" in r.text.upper():
            md = [{"text": r.text[:1200]}]
        rr = requests.get("https://www.spc.noaa.gov/climo/reports/today.csv", headers=UA, timeout=15)
        if rr.ok:
            # three sections, each with its own header: F_Scale (tornado),
            # Speed (wind), Size (hail); Lat/Lon at columns 5/6 in all of them
            kinds = {"F_Scale": "T", "Speed": "W", "Size": "H"}
            rows, section = [], None
            for ln in rr.text.strip().splitlines():
                if not ln.strip():
                    continue
                parts = ln.split(",")
                if parts[0].strip() == "Time":
                    section = kinds.get(parts[1].strip() if len(parts) > 1 else "")
                    continue
                if section and len(parts) >= 7:
                    try:
                        rlat, rlon = float(parts[5]), float(parts[6])
                    except ValueError:
                        continue
                    rows.append({"kind": section, "lat": rlat, "lon": rlon,
                                 "comment": (parts[7] if len(parts) > 7 else "").strip()})
            reports = {"torn": sum(1 for r_ in rows if r_["kind"] == "T"),
                       "wind": sum(1 for r_ in rows if r_["kind"] == "W"),
                       "hail": sum(1 for r_ in rows if r_["kind"] == "H"),
                       "rows": rows[:60]}
    except Exception:  # noqa: BLE001
        pass

    # ---------------- single-radar NEXRAD sites (WMS -> cached PNG) ----------------
    site_frames = _nexrad_site_frames()
    site_catalog = _nexrad_catalog(site_frames)
    start_nexrad_mode_renderer()          # persistent: every site x every mode
    start_mrms_renderer("cref")           # persistent: all MRMS products incl. levels

    # ---------------- observations (stations + East TN city board) ----------------
    obs_stations, city_obs = [], []
    city_fc = []
    try:
        from data.observations import nearby_observations, city_observations
        obs_stations = nearby_observations(lat, lon, limit=18)
        city_obs = city_observations()
    except Exception:  # noqa: BLE001
        pass
    # full 7-day NWS forecast for every East TN city (parallel; disk-cache
    # the point->forecast mapping is unnecessary - NWS caps at ~1 req/s per
    # host but 8 workers finish 16 cities in ~6 s and the updater runs 2-min
    # cycles, so in-process only)
    try:
        from data.nws import city_forecasts
        city_fc = city_forecasts()
    except Exception:  # noqa: BLE001
        pass
    # US-wide observations (aviationweather.gov METAR cache file, keyless)
    us_obs = []
    try:
        from data.observations import us_observations
        us_obs = us_observations()
    except Exception:  # noqa: BLE001
        pass

    # ---------------- Skew-T soundings (RAP 13 km + MetPy, multi-location) ----------------
    # 8 sites x 4 hours, pre-baked to PNG. A rotating pointer renders a few new
    # (site, hour) combos per cycle so the whole matrix fills in over time;
    # every combo is cached on disk by data.sounding, so this stays cheap.
    _SND_SITES = [
        (config.DEFAULT_LOCATION_NAME, lat, lon),
        ("Knoxville TN", 35.9606, -83.9207),
        ("Tri-Cities TN", 36.3134, -82.3573),
        ("Chattanooga TN", 35.0456, -85.3097),
        ("Nashville TN", 36.1628, -86.7816),
        ("Crossville TN", 35.9479, -85.0269),
        ("Oak Ridge TN", 35.9903, -84.2853),
        ("Atlanta GA", 33.6407, -84.4277),
    ]
    _SND_FHS = (0, 6, 12, 18)
    sounding = {"hours": {}, "locations": {name: {"lat": la, "lon": lo}
                                           for name, la, lo in _SND_SITES}}
    try:
        from data.sounding import build_sounding, _cycle, _sounding_cache_path
        for name, la, lo in _SND_SITES:
            done = 0
            for fh in _SND_FHS:
                try:
                    cached = _sounding_cache_path(*_cycle(fh), la, lo)
                    fresh = os.path.exists(cached) and os.path.getsize(cached) > 10_000
                except Exception:  # noqa: BLE001
                    fresh = False
                if fresh or done < _SND_BUDGET:
                    try:
                        s = build_sounding(la, lo, fh=fh, place=name)
                        if s.get("png"):
                            done += 0 if fresh else 1
                            p = s["png"].replace("\\", "/")
                            url = "../" + p.split("static/", 1)[1]
                            sounding["hours"].setdefault(name, {})[str(fh)] = {
                                "url": url, "meta": s.get("meta", {})}
                    except Exception:  # noqa: BLE001
                        continue
        _snd_rotate()
    except Exception:  # noqa: BLE001
        pass

    # ---------------- tropical payload ----------------
    storms = []
    nhc_gfx = []
    try:
        from data.national import nhc_storms
        raw_storms = nhc_storms() or []
        for s in raw_storms[:8]:
            cone = _geo(s.get("coneGeometry"))
            track = _geo(s.get("trackGeometry"))
            entry = {k: s.get(k) for k in
                     ("name", "classification", "intensity", "pressure", "lat", "lon")}
            entry["cone"] = cone
            entry["track"] = track
            storms.append(entry)
    except Exception:  # noqa: BLE001
        raw_storms = []
    # NHC wind radii + 7-day development areas, baked as GeoJSON (static-safe);
    # wind radii parse from the RAW storms (they carry the windKmz links)
    wr_geo, out_geo = [], []
    try:
        from data.nhc_maps import nhc_wind_radii_overlays, nhc_outlook_overlays
        wr_geo = [o for o in (nhc_wind_radii_overlays(raw_storms) or [])
                  if _geo(o.get("geometry"))][:20]
        out_geo = (nhc_outlook_overlays() or [])[:2]   # NHC TWO basin images
    except Exception:  # noqa: BLE001
        pass
    nhc_gfx = [
        {"title": "Atlantic overview (NHC)", "url": "https://www.nhc.noaa.gov/xgtwo/two_atl_7d0.png"},
        {"title": "East Pacific overview (NHC)", "url": "https://www.nhc.noaa.gov/xgtwo/two_pac_7d0.png"},
        {"title": "Atlantic text outlook", "url": "https://www.nhc.noaa.gov/archive/xgtwo/two_atl/2026/two_atl_2026090812.txt"},
    ]

    # ---------------- model catalog (everything) ----------------
    model_catalog = {}
    try:
        from data.model_maps import PRODUCTS_BY_MODEL, PRODUCTS
        for model, prods in PRODUCTS_BY_MODEL.items():
            model_catalog[model] = {
                "label": model,
                "products": [{"key": p, "label": PRODUCTS.get(p, {}).get("label", p)} for p in prods],
            }
    except Exception:  # noqa: BLE001
        model_catalog = {}

    # MPAS + FV3 (SHiELD) into the same menu: products served from the
    # pre-rendered official frame loops (mpasShield payload), not REND
    try:
        from data.shield_mpas import MPAS_PRODUCTS, SHIELD_PRODUCTS
        model_catalog["MPAS"] = {
            "label": "NCAR MPAS (3.75 km global)",
            "products": [{"key": k, "label": v["label"]} for k, v in MPAS_PRODUCTS.items()],
        }
        model_catalog["FV3 (SHiELD)"] = {
            "label": "GFDL FV3 (SHiELD)",
            "products": [{"key": k, "label": v["label"]} for k, v in SHIELD_PRODUCTS.items()],
        }
    except Exception:  # noqa: BLE001
        pass

    # ---------------- MPAS + FV3/SHiELD experimental globals ----------------
    # Pre-rendered official frames (JPG) downloaded to static/aimodels/. A
    # rotating pointer fetches one (model, product) set per cycle so the viewer
    # fills in without slowing generation; previously downloaded sets are
    # re-indexed from disk at no network cost.
    mpas_shield = {"mpas": {}, "shield": {}, "mpasProducts": {}, "mpasDomains": {},
                   "shieldProducts": {}, "shieldRegions": {}}
    try:
        from data.shield_mpas import (MPAS_PRODUCTS, MPAS_DOMAINS, SHIELD_PRODUCTS,
                                      SHIELD_REGIONS, mpas_product, shield_product)
        mpas_shield["mpasProducts"] = {k: v["label"] for k, v in MPAS_PRODUCTS.items()}
        mpas_shield["mpasDomains"] = MPAS_DOMAINS
        mpas_shield["shieldProducts"] = {k: v["label"] for k, v in SHIELD_PRODUCTS.items()}
        mpas_shield["shieldRegions"] = SHIELD_REGIONS
        _MS_ORDER_FILE = os.path.join("static", "aimodels", "render_order.json")
        try:
            n = int(open(_MS_ORDER_FILE).read().strip() or 0)
        except Exception:  # noqa: BLE001
            n = 0
        try:
            os.makedirs(os.path.dirname(_MS_ORDER_FILE), exist_ok=True)
            open(_MS_ORDER_FILE, "w").write(str(n + 1))
        except Exception:  # noqa: BLE001
            pass
        _shield_sets = ["max_reflectivity_wind", "vort500_hgt_wind", "CAPE", "TMP2m"]

        def _norm(got):
            """Frames from the live fetchers carry app-absolute 'file'; convert
            to site-relative 'url' so both the site and the Pages build serve them."""
            if got:
                for f in got.get("frames", []):
                    if "url" not in f and f.get("file"):
                        f["url"] = "../aimodels/" + os.path.basename(f["file"])
            return got

        try:
            v = list(MPAS_PRODUCTS)[n % len(MPAS_PRODUCTS)]
            got = _norm(mpas_product(v, "conus", max_frames=18))
            if got:
                mpas_shield["mpas"][v] = got
        except Exception:  # noqa: BLE001
            pass
        try:
            f_ = _shield_sets[n % len(_shield_sets)]
            got = _norm(shield_product(f_, "CONUS", max_frames=18))
            if got:
                mpas_shield["shield"][f_] = got
        except Exception:  # noqa: BLE001
            pass
        _cache_mpas_shield_from_disk(mpas_shield)
    except Exception:  # noqa: BLE001
        pass

    # ---------------- forecast charts + MOS guidance ----------------
    charts, mos = {}, {"available": False}
    try:
        from data.mos import forecast_charts_bundle, mos_bundle
        charts = forecast_charts_bundle()
        mos = mos_bundle()
    except Exception:  # noqa: BLE001
        pass

    meso = {}
    try:
        from data.meso import meso_bundle
        meso = meso_bundle()
    except Exception:  # noqa: BLE001
        pass

    return {
        "generated": dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
        "pageName": config.PAGE_NAME,
        "pageUrl": config.PAGE_URL,
        "place": config.DEFAULT_LOCATION_NAME,
        "lat": lat,
        "lon": lon,
        "current": {
            "tempF": round(_f(temp_c)) if temp_c is not None else None,
            "text": cur.get("textDescription") or "",
            "dewF": round(_f(dew_c)) if dew_c is not None else None,
            "rh": round((cur.get("relativeHumidity") or {}).get("value") or 0),
            "wind": (f'{_compass(cur.get("windDirection", {}).get("value") if cur.get("windDirection") else None)} '
                     f'{round(_mph(wind_kmh))} mph').strip() if wind_kmh is not None else "calm",
            "time": (cur.get("timestamp") or "")[:16].replace("T", " ") + "Z",
        },
        "days": days,
        "hourly": hourly,
        "alerts": alerts,
        "spc": spc_days,
        "storm": {
            "summary": summarize(cells) if cells else None,
            "cells": [{"lat": c.get("lat"), "lon": c.get("lon"), "dbz": c.get("dbz")}
                      for c in cells[:24] if c.get("lat") is not None],
        },
        "radar": {
            "past": past[-12:],
            "nowcast": nowcast,
            "future": future["frames"][-24:],
            "futureReady": future["ready"],
            "futureTotal": future["total"],
            "mrms": mrms["frames"][-10:],
            "nws": nws[-6:] if isinstance(nws, list) else nws,
        },
        "mrmsProducts": {k: v.get("label", k) for k, v in MRMS_CATALOG.items()},
        # per-product MRMS loops for the radar page's level picker (disk reads;
        # the shared background renderer fills each over time)
        "mrmsLoops": {pk: mrms_bundle(pk)["frames"][-8:]
                      for pk in ("cref", "lowref", "l0050", "l0200", "l0400", "l0800",
                                 "l1500", "zdr050", "rho050", "rots", "mesh",
                                 "azshr", "azshr36", "etop", "vil", "shi",
                                 "prate", "qpe1h")},
        "sites": site_frames,
        "siteCatalog": site_catalog,
        "obs": {"stations": obs_stations, "cities": city_obs, "us": us_obs},
        "cityForecasts": city_fc,
        "sounding": sounding,
        "satBands": sat_bands,
        "satHome": "wvh",
        "severe": {
            "outlooks": outlook_feats,
            "warnings": ww,
            "tnAlerts": tn,
            "md": md,
            "reports": reports,
        },
        "tropical": {
            "storms": storms,
            "graphics": nhc_gfx,
            "windRadii": wr_geo,
            "outlook": out_geo,
        },
        "modelCatalog": model_catalog,
        "renderIndex": _render_index(),
        "mpasShield": mpas_shield,
        "forecastCharts": charts,
        "mos": mos,
        "meso": meso,
    }


# ------------------------------------------------- single-radar NEXRAD
def _nexrad_catalog(rendered):
    """All 159 NEXRAD sites for the picker, with per-site render status.

    Local radars (NEXRAD_LOCAL) come first; every entry says whether its
    frames are currently rendered ("ready") or queued ("pending") - the
    background mode renderer fills every site x mode over time. `modes`
    counts how many of the 5 WMS radar modes are on disk for the site.
    """
    try:
        sites = json.load(open("static/nexrad_sites.json", encoding="utf-8"))
    except (OSError, ValueError):
        sites = {}
    entries = []
    for sid, info in sites.items():
        n_modes = len((rendered.get(sid) or {}).get("modes") or {})
        entries.append({
            "id": sid,
            "name": info.get("name", sid),
            "lat": info["lat"], "lon": info["lon"],
            "local": sid in NEXRAD_LOCAL,
            "status": "ready" if sid in rendered else "pending",
            "frames": len((rendered.get(sid) or {}).get("frames") or []),
            "modes": n_modes,
        })
    entries.sort(key=lambda e: (not e["local"], e["id"]))
    return entries


# Per-site radar modes on NOAA's opengeo WMS (no key). Every NEXRAD site
# exposes all five; each animates over the latest volume scans.
SITE_WMS_MODES = {
    "sr_bref": {"label": "Super-Res Reflectivity", "stamps": 6},
    "sr_bvel": {"label": "Velocity (SR)", "stamps": 4},
    "bdhc":    {"label": "Hybrid Scan Reflectivity", "stamps": 4},
    "bdsa":    {"label": "Storm-Total Precip", "stamps": 4},
    "boha":    {"label": "1-Hour Precip", "stamps": 4},
}
_NEXRAD_DIR = os.path.join("static", "nexrad_sites")
_NEXRAD_MODE_THREAD = []          # singleton guard [thread]


def _wms_getmap(sid, mode, center, ts, size):
    """One WMS frame render -> PNG path (or None). Caller probes cheap first."""
    import math
    import requests
    lat, lon = center
    k = 111.32
    dlat = 230 / k
    dlon = 230 / (k * math.cos(math.radians(lat)))
    bb = [lat - dlat, lon - dlon, lat + dlat, lon + dlon]
    try:
        r = requests.get(
            "https://opengeo.ncep.noaa.gov/geoserver/ows",
            params={"service": "WMS", "version": "1.1.1", "request": "GetMap",
                    "layers": f"{sid.lower()}_{mode}", "styles": "",
                    "format": "image/png", "transparent": "true",
                    "srs": "EPSG:4326", "width": size, "height": size,
                    "bbox": f"{bb[1]},{bb[0]},{bb[3]},{bb[2]}"},
            headers={"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}, timeout=60)
        if not (r.ok and r.headers.get("content-type", "").startswith("image")):
            return None
        png = os.path.join(_NEXRAD_DIR, f"{sid}_{mode}_{ts}.png")
        tmp = png + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(r.content)
        os.replace(tmp, png)
        return png
    except requests.RequestException:
        return None


def _nexrad_mode_worker():
    """Persistent renderer: sweeps every site x mode x stamp continuously so
    ALL radar sites gain ALL modes over time (locals first). Disk-only reads
    elsewhere keep site generation fast."""
    import math
    import requests
    UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}
    while True:
        try:
            sites = json.load(open("static/nexrad_sites.json", encoding="utf-8"))
            order = [s for s in sorted(sites) if s in NEXRAD_LOCAL] + \
                    [s for s in sorted(sites) if s not in NEXRAD_LOCAL]
            now = time.time()
            for sid in order:
                info = sites.get(sid) or {}
                lat, lon = info.get("lat"), info.get("lon")
                if lat is None:
                    continue
                for mode, spec in SITE_WMS_MODES.items():
                    keep_s = 7200 if mode == "sr_bref" else 10800
                    for age in range(0, spec["stamps"] * 5, 5):
                        ts = int(now - age * 60) // 300 * 300
                        png = os.path.join(_NEXRAD_DIR, f"{sid}_{mode}_{ts}.png")
                        if os.path.isfile(png) and now - os.path.getmtime(png) < 720:
                            continue
                        k = 111.32
                        dlat = 230 / k
                        dlon = 230 / (k * math.cos(math.radians(lat)))
                        bb = [lat - dlat, lon - dlon, lat + dlat, lon + dlon]
                        try:
                            probe = requests.get(
                                "https://opengeo.ncep.noaa.gov/geoserver/ows",
                                params={"service": "WMS", "version": "1.1.1", "request": "GetMap",
                                        "layers": f"{sid.lower()}_{mode}", "styles": "",
                                        "format": "image/png", "transparent": "true",
                                        "srs": "EPSG:4326", "width": 64, "height": 64,
                                        "bbox": f"{bb[1]},{bb[0]},{bb[3]},{bb[2]}"},
                                headers=UA, timeout=25)
                            if not (probe.ok and probe.headers.get("content-type", "").startswith("image")):
                                continue
                            size = 900 if (sid in NEXRAD_LOCAL or mode == "sr_bref") else 700
                            _wms_getmap(sid, mode, (lat, lon), ts, size)
                        except requests.RequestException:
                            continue
                # prune this site's stale mode renders
                for fn in os.listdir(_NEXRAD_DIR):
                    if fn.startswith(sid + "_") and now - os.path.getmtime(os.path.join(_NEXRAD_DIR, fn)) > 14400:
                        try:
                            os.remove(os.path.join(_NEXRAD_DIR, fn))
                        except OSError:
                            pass
        except Exception:  # noqa: BLE001 - worker must survive everything
            pass
        time.sleep(60)


def start_nexrad_mode_renderer():
    if _NEXRAD_MODE_THREAD and _NEXRAD_MODE_THREAD[0].is_alive():
        return _NEXRAD_MODE_THREAD[0]
    th = threading.Thread(target=_nexrad_mode_worker, daemon=True, name="nexrad-mode-renderer")
    _NEXRAD_MODE_THREAD.clear()
    _NEXRAD_MODE_THREAD.append(th)
    th.start()
    return th


def _nexrad_site_frames(max_stamps=6, time_budget=150.0):
    """Disk-only reader: per-site frames for EVERY mode (reflectivity,
    velocity, hybrid scan, storm-total + 1-hour precip).

    All network rendering happens in the persistent background thread
    (start_nexrad_mode_renderer, started by the updater); this reader just
    assembles whatever PNGs are already on disk, so site generation stays
    fast. Payload shape: sites[sid] = {name, lat, lon,
    modes: {mode: {label, frames: [...]}}} - the UI builds one picker per
    site from it. Back-compat: frames = best reflectivity loop.
    """
    import math
    try:
        sites = json.load(open("static/nexrad_sites.json", encoding="utf-8"))
    except (OSError, ValueError):
        sites = {}
    out_dir = _NEXRAD_DIR
    os.makedirs(out_dir, exist_ok=True)

    now = time.time()
    out = {}
    for sid in sorted(sites):
        info = sites[sid]
        lat, lon = info["lat"], info["lon"]
        k = 111.32
        dlat = 230 / k                                   # ~230 km half-width
        dlon = 230 / (k * math.cos(math.radians(lat)))
        bounds = [lat - dlat, lon - dlon, lat + dlat, lon + dlon]
        modes_out = {}
        best = []
        for mode, spec in SITE_WMS_MODES.items():
            frames = []
            for age in range(0, spec["stamps"] * 5, 5):
                ts = int(now - age * 60) // 300 * 300
                png = os.path.join(out_dir, f"{sid}_{mode}_{ts}.png")
                if not os.path.isfile(png):
                    continue
                mins = int((now - ts) / 60)
                frames.append({"time": ts,
                               "label": "Now " + dt.datetime.fromtimestamp(ts).strftime("%H:%M") if mins < 6 else f"-{mins}m " + dt.datetime.fromtimestamp(ts).strftime("%H:%M"),
                               "pngUrl": f"../nexrad_sites/{sid}_{mode}_{ts}.png",
                               "bounds": bounds})
            if frames:
                modes_out[mode] = {"label": spec["label"], "frames": frames}
                if mode == "sr_bref":
                    best = frames
        if modes_out:
            entry = {"name": info.get("name", sid), "lat": lat, "lon": lon,
                     "modes": modes_out}
            if not best:                       # older caches: bare sid_ts.png
                legacy = []
                for age in range(0, max_stamps * 5, 5):
                    ts = int(now - age * 60) // 300 * 300
                    png = os.path.join(out_dir, f"{sid}_{ts}.png")
                    if os.path.isfile(png):
                        mins = int((now - ts) / 60)
                        legacy.append({"time": ts,
                                       "label": "Now " + dt.datetime.fromtimestamp(ts).strftime("%H:%M") if mins < 6 else f"-{mins}m " + dt.datetime.fromtimestamp(ts).strftime("%H:%M"),
                                       "pngUrl": f"../nexrad_sites/{sid}_{ts}.png",
                                       "bounds": bounds})
                if legacy:
                    modes_out["sr_bref"] = {"label": SITE_WMS_MODES["sr_bref"]["label"], "frames": legacy}
                    best = legacy
            entry["frames"] = best
            out[sid] = entry
    return out


# ---------------------------------------------------------------- manifest
def _render_index():
    """Every (model, product, region) render on disk, latest cycle only.

    Powers the models page explorer on the static site: any combo listed
    here displays instantly; unlisted combos report honestly that they are
    not pre-rendered in this build. Capped at the newest 6 frames/combo to
    keep the Pages payload small.
    """
    base = "static/model_maps"
    rx = re.compile(r"([A-Za-z0-9\-]+)_(\w+)_f(\d+)_(\d{10})_(\w+)\.png$")
    combos = {}
    try:
        names = os.listdir(base)
    except OSError:
        names = []
    for fn in names:
        m = rx.match(fn)
        if not m:
            continue
        model, prod, fh, cyc, region = m.groups()
        combos.setdefault((model, prod, region), []).append((cyc, int(fh), fn))
    out = []
    for (model, prod, region), items in combos.items():
        newest = max(c for c, _f, _n in items)
        frames = sorted((f, fn) for c, f, fn in items if c == newest)[-6:]
        out.append({
            "model": model, "product": prod, "region": region,
            "cycle": newest,
            "frames": [{"fh": fh, "url": f"../model_maps/{fn}"}
                       for fh, fn in frames],
        })
    out.sort(key=lambda c: c["cycle"], reverse=True)
    return out


def _model_manifest():
    """Latest rendered model maps grouped for the models page gallery."""
    base = "static/model_maps"
    rx = re.compile(r"([A-Za-z0-9\-]+)_(\w+)_f(\d+)_(\d{10})_(\w+)\.png$")
    groups = {}
    try:
        names = os.listdir(base)
    except OSError:
        names = []
    for fn in names:
        m = rx.match(fn)
        if not m:
            continue
        model, prod, fh, cyc, region = m.groups()
        key = (model, prod, region)
        groups.setdefault(key, []).append((int(fh), cyc, fn))
    out = []
    for (model, prod, region), items in sorted(groups.items()):
        items.sort()
        out.append({
            "model": model,
            "product": prod,
            "region": region,
            "count": len(items),
            "cycle": items[-1][1],
            "frames": [{"fh": fh, "url": f"../model_maps/{fn}"} for fh, _cyc, fn in items[-16:]],
        })
    # newest cycles first
    out.sort(key=lambda g: g["cycle"], reverse=True)
    return out


def _psu_manifest():
    """PSU e-Wall HRRR 15-min loop files for the models page player."""
    try:
        from data.psu_hrrr import psu_hrrr_loop
        loop = psu_hrrr_loop(max_frames=24)
        return {"init": loop.get("init"), "cycle": loop.get("cycle"),
                "frames": [{"url": f["file"], "label": f["label"]} for f in loop.get("frames", [])]}
    except Exception:  # noqa: BLE001
        return None


def _national_payload():
    """WPC charts + SPC upper-air analyses for the national page."""
    wpc, upper = [], []
    try:
        from data.national import wpc_catalog, upper_air_maps
        wpc = wpc_catalog()
        upper = upper_air_maps()
    except Exception:  # noqa: BLE001
        pass
    return {"wpc": wpc, "upperAir": upper}


# ---------------------------------------------------------------- shared html
_CSS = """
  :root { color-scheme: dark; --bg:#0e1117; --card:#161b26; --line:rgba(255,255,255,.08); --dim:#9aa4b2; --acc:#4da3ff; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:"Segoe UI",system-ui,sans-serif; background:var(--bg); color:#e8edf4; }
  a { color:var(--acc); text-decoration:none; }
  .wrap { max-width:1100px; margin:0 auto; padding:14px; }
  nav { position:sticky; top:0; z-index:50; background:rgba(14,17,23,.95); backdrop-filter:blur(6px);
        border-bottom:1px solid var(--line); }
  nav .wrap { display:flex; flex-wrap:wrap; align-items:center; gap:6px 14px; padding:10px 14px; }
  nav .brand { font-weight:800; font-size:18px; margin-right:auto; }
  nav .brand span { color:var(--acc); }
  nav a.pg { color:#cdd7e4; font-size:14px; padding:4px 8px; border-radius:6px; }
  nav a.pg:hover, nav a.pg.on { background:#1d2432; color:#fff; }
  header.hero { text-align:center; padding:20px 8px 8px; }
  header.hero h1 { margin:0; font-size:clamp(22px,5vw,36px); }
  header.hero .sub { color:var(--dim); margin-top:6px; font-size:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; margin:14px 0; }
  h2 { font-size:17px; margin:0 0 10px; color:#cdd7e4; }
  .grid { display:grid; gap:10px; }
  .cards7 { grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); }
  .day { background:#10151f; border:1px solid var(--line); border-radius:12px; padding:12px; text-align:center; }
  .day .hi { font-size:24px; font-weight:800; }
  .day .tx { color:var(--dim); font-size:12.5px; min-height:30px; }
  .now { display:flex; flex-wrap:wrap; gap:16px; align-items:center; }
  .now .big { font-size:clamp(42px,10vw,64px); font-weight:800; }
  .now .meta { color:var(--dim); font-size:14px; line-height:1.7; }
  .chip { display:inline-block; padding:8px 18px; border-radius:12px; font-weight:900; color:#102015; }
  .alerts { display:grid; gap:8px; }
  .alert { background:#10151f; border-radius:10px; padding:10px 12px; border-left:6px solid #888; }
  .alert b { color:#fff; } .alert span { color:var(--dim); font-size:13px; display:block; }
  .alert.ok { border-left-color:#2e7d32; color:#a5d6a7; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:10px; }
  .kpi { background:#10151f; border:1px solid var(--line); border-radius:12px; padding:10px; text-align:center; }
  .kpi b { display:block; font-size:22px; } .kpi span { color:var(--dim); font-size:12px; }
  #map { height:clamp(340px,56vh,580px); border-radius:12px; z-index:0; }
  .ctl { display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin-top:10px; }
  .ctl button { background:#2b80ff; color:#fff; border:none; border-radius:8px; padding:9px 20px; font-size:16px; cursor:pointer; }
  .ctl button:active { transform:scale(.97); }
  .ctl select { background:#1b1f27; color:#eee; border:1px solid rgba(255,255,255,.2); border-radius:6px; padding:8px; font-size:14px; max-width:100%; }
  .stack { position:relative; width:100%; aspect-ratio:4/3; background:#0a0d13; border-radius:10px; overflow:hidden; }
  .stack .layer { position:absolute; inset:0; width:100%; height:100%; object-fit:contain; }
  .frame { font-family:ui-monospace,monospace; color:#ffd54f; }
  .ctl input[type=range] { flex:1; min-width:110px; accent-color:#2b80ff; height:26px; }
  .frame { min-width:84px; text-align:center; font-weight:700; color:#ffd54f; font-size:16px; }
  .src { color:var(--dim); font-size:12px; margin-top:8px; }
  .legend { display:flex; flex-wrap:wrap; gap:8px 14px; font-size:12.5px; color:var(--dim); margin-top:8px; }
  .legend i { display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:5px; vertical-align:-1px; }
  table.cells { width:100%; border-collapse:collapse; font-size:14px; }
  table.cells th { text-align:left; color:var(--dim); font-weight:600; font-size:12px; padding:6px; }
  table.cells td { border-top:1px solid var(--line); padding:7px 6px; }
  .dbz { font-weight:800; } .sev { color:#ff5252; } .mod { color:#ffb74d; } .lit { color:#aed581; }
  .gal { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:14px; }
  .gal img { width:100%; border-radius:10px; border:1px solid var(--line); }
  .gal .cap { color:var(--dim); font-size:12.5px; margin-top:4px; }
  .stepper { display:flex; align-items:center; gap:8px; margin-top:8px; }
  .stepper button { background:#1d2432; color:#eee; border:1px solid var(--line); border-radius:6px; padding:5px 12px; cursor:pointer; font-size:14px; }
  .cmp4 { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
  .cmp4 .pane { background:#10151f; border:1px solid var(--line); border-radius:10px; padding:8px; }
  .cmp4 .pane h3 { margin:0 0 6px; font-size:13.5px; color:#cdd7e4; display:flex; align-items:center; gap:8px; }
  .cmp4 .pane h3 .cap { color:var(--dim); font-weight:400; }
  .cmp4 .pane select { background:#1d2432; color:#eee; border:1px solid var(--line); border-radius:6px; padding:3px 6px; font-size:13px; }
  .cmp4 img { width:100%; border-radius:8px; }
  .cmp4 .miss { color:var(--dim); font-size:12.5px; padding:22px 6px; text-align:center; }
  @media (max-width:820px){ .cmp4 { grid-template-columns:1fr; } }
  .hourly { display:flex; gap:8px; overflow-x:auto; padding-bottom:6px; }
  .hr { background:#10151f; border:1px solid var(--line); border-radius:10px; padding:8px 10px; text-align:center; min-width:72px; }
  .hr b { display:block; } .hr span { color:var(--dim); font-size:11.5px; }
  .natimg { width:100%; max-width:860px; display:block; margin:0 auto; border-radius:10px; border:1px solid var(--line); background:#fff; }
  .pre { white-space:pre-wrap; font-size:12.5px; color:#cdd7e4; background:#10151f; border:1px solid var(--line); border-radius:10px; padding:12px; max-height:260px; overflow:auto; }
  footer { text-align:center; color:var(--dim); font-size:12.5px; padding:18px 8px 26px; line-height:1.8; }
  .navctl { display:flex; align-items:center; gap:6px; }
  .navctl button { background:#1d2432; color:#eee; border:1px solid var(--line); border-radius:6px; padding:4px 10px; cursor:pointer; font-size:14px; }
  .autolbl { color:var(--dim); font-size:12px; display:flex; align-items:center; gap:4px; }
  .map-dark .leaflet-tile-pane { filter:invert(1) hue-rotate(180deg) brightness(.92) contrast(1.05); }
  .leaflet-control-attribution { background:rgba(14,17,23,.8) !important; color:var(--dim) !important; }
  .mapctlbtn { background:#1d2432; border:1px solid var(--line); border-radius:6px; color:#eee;
    padding:5px 9px; font-size:15px; cursor:pointer; margin-bottom:6px; box-shadow:0 1px 4px rgba(0,0,0,.4); text-align:center; }
  .mapcoord { background:rgba(14,17,23,.85); color:#cdd7e4; border:1px solid var(--line); border-radius:6px;
    font-size:11.5px; padding:3px 8px; }
  @media (max-width:640px){ .wrap{padding:10px;} .card{padding:12px;} nav .brand{font-size:16px;} }
"""


# Shared Leaflet control suite (plain JS, no f-string braces): basemap switcher,
# fullscreen, home, locate-me, scale bar, live lat/lon readout. Attached to
# every site map via addMapControls(map, homeLatLng, homeZoom).
_MAP_CONTROLS_JS = """
const BASEMAPS = {
  dark:  { label: "Dark", url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", invert: true },
  light: { label: "Light", url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", invert: false },
  sat:   { label: "Satellite", url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", invert: false },
  topo:  { label: "Terrain", url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}", invert: false }
};
let _baseLayer = null, _baseKey = null;
function setBase(key, map) {
  if (key === "mapbox" && !BASEMAPS.mapbox && typeof MAPBOX_TOKEN === "string" && MAPBOX_TOKEN)
    BASEMAPS.mapbox = { label: "Mapbox", style: "mapbox://styles/mapbox/light-v11" };
  const b = BASEMAPS[key] || BASEMAPS.dark;
  if (_baseKey === key && _baseLayer) return;
  if (_baseLayer) map.removeLayer(_baseLayer);
  if (b.style) _baseLayer = L.mapboxGL({ accessToken: MAPBOX_TOKEN, style: b.style, attribution: "&copy; Mapbox" }).addTo(map);
  else _baseLayer = L.tileLayer(b.url, { maxNativeZoom: 19, maxZoom: 21, attribution: "&copy; OpenStreetMap contributors" }).addTo(map);
  map.getContainer().classList.toggle("map-dark", !!b.invert);
  _baseKey = key;
  try { localStorage.setItem("tnwxBase", key); } catch (_e) {}
  const sel = document.getElementById("baseSel"); if (sel && sel.value !== key) sel.value = key;
}
function addMapControls(map, home, homeZoom) {
  const btn = (txt, title, fn) => {
    const c = L.control({ position: "topleft" });
    c.onAdd = () => { const d = L.DomUtil.create("div", "mapctlbtn"); d.textContent = txt; d.title = title;
      L.DomEvent.disableClickPropagation(d); d.onclick = fn; return d; };
    c.addTo(map);
  };
  btn("\u26f6", "Fullscreen map", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else map.getContainer().requestFullscreen();
  });
  btn("\u2302", "Home view", () => map.setView(home, homeZoom));
  btn("\u25ce", "My location", () => {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(p => {
      map.setView([p.coords.latitude, p.coords.longitude], 10);
      L.circleMarker([p.coords.latitude, p.coords.longitude], { radius: 7, color: "#fff", weight: 2, fillColor: "#4da3ff", fillOpacity: 1 })
        .addTo(map).bindTooltip("You are here").openTooltip();
    }, () => {}, { timeout: 8000 });
  });
  L.control.scale({ imperial: true, metric: false }).addTo(map);
  document.addEventListener("fullscreenchange", () => setTimeout(() => map.invalidateSize(), 200));
  const coord = L.control({ position: "bottomright" });
  coord.onAdd = () => L.DomUtil.create("div", "mapcoord");
  coord.addTo(map);
  const el = document.querySelector(".mapcoord");
  map.on("mousemove", e => { if (el) el.textContent = e.latlng.lat.toFixed(3) + ", " + e.latlng.lng.toFixed(3); });
  map.on("mouseout", () => { if (el) el.textContent = ""; });
  const sel = document.getElementById("baseSel");
  if (sel) {
    sel.innerHTML = Object.entries(BASEMAPS).map(([k, b]) => `<option value="${k}">${b.label}</option>`).join("");
    let saved = null; try { saved = localStorage.getItem("tnwxBase"); } catch (_e) {}
    setBase(saved && BASEMAPS[saved] ? saved : ((typeof MAPBOX_TOKEN === "string" && MAPBOX_TOKEN) ? "mapbox" : "dark"), map);
    sel.onchange = e => setBase(e.target.value, map);
  } else setBase((typeof MAPBOX_TOKEN === "string" && MAPBOX_TOKEN) ? "mapbox" : "dark", map);
}
"""

# Post-boot glue (plain JS): wire basemap switching onto maps whose body
# scripts create the map after this script ran; attaches the full control
# suite to static maps that did not call addMapControls themselves.
_BOOT_CONTROLS_JS = """
setTimeout(function () {
  if (typeof map === "undefined" || !map) return;
  const sel = document.getElementById("baseSel");
  if (typeof addMapControls === "function" && (typeof _baseLayer === "undefined" || !_baseLayer)) {
    const la = (typeof DATA_LAT !== "undefined") ? DATA_LAT : 36.2;
    const lo = (typeof DATA_LON !== "undefined") ? DATA_LON : -83.0;
    addMapControls(map, [la, lo], 6);
  } else if (typeof setBase === "function") {
    if (!_baseLayer) setBase((typeof MAPBOX_TOKEN === "string" && MAPBOX_TOKEN) ? "mapbox" : "dark", map);
    if (sel) sel.onchange = function (e) { setBase(e.target.value, map); };
  }
}, 600);
"""


def _player_js(layers_js, opts=""):
    """Shared animated map player. layers_js: JSON of {value:{label,mode,kind?,key?,path?}}."""
    return f"""
{_mapbox_token_js()}
let LAYERS = {layers_js};
const OPACITY = () => {{ const o = document.getElementById("opacity"); return o ? o.value / 100 : 0.8; }};
let DATA = null, map, curLayers = [], frames = [], idx = 0, timer = null, playing = true, kind = "{opts or 'past'}";
const frameEl = document.getElementById("frame");

function initMap() {{
  window.DATA_LAT = DATA.lat; window.DATA_LON = DATA.lon;
  map = L.map("map", {{ zoomSnap: 0.5, maxZoom: 21 }}).setView([DATA.lat, DATA.lon], 6);
  addMapControls(map, [DATA.lat, DATA.lon], 6);
  L.circleMarker([DATA.lat, DATA.lon], {{ radius: 7, color: "#fff", weight: 2, fillColor: "#ff5252", fillOpacity: 1 }})
    .addTo(map).bindTooltip(DATA.place);
}}
function fmt(ts) {{ return new Date(ts * 1000).toLocaleTimeString([], {{ hour: "2-digit", minute: "2-digit", hour12: false }}); }}
function clear() {{ for (const l of curLayers) {{ try {{ map.removeLayer(l); }} catch (_e) {{}} }} curLayers = []; }}
function show(i) {{
  idx = i; clear();
  const f = frames[i]; if (!f) return;
  const spec = LAYERS[kind] || {{}};
  if (spec.mode === "tiles" && f.path) {{
    const ts = f.time + "";
    curLayers.push(L.tileLayer("https://tilecache.rainviewer.com" + f.path + spec.path + ts + ".png",
      {{ opacity: OPACITY(), maxNativeZoom: 10, maxZoom: 21 }}).addTo(map));
    frameEl.textContent = fmt(f.time);
  }} else if (f.pngUrl && f.bounds) {{
    const b = f.bounds;
    const lb = (Array.isArray(b) && !Array.isArray(b[0])) ? L.latLngBounds([[b[0], b[1]], [b[2], b[3]]]) : b;
    curLayers.push(L.imageOverlay(f.pngUrl, lb, {{ opacity: OPACITY(), maxZoom: 21 }}).addTo(map));
    frameEl.textContent = f.label || "";
  }} else {{ frameEl.textContent = "rendering\\u2026"; }}
  if (document.getElementById("pend")) {{
    const r = DATA.radar || {{}};
    document.getElementById("pend").textContent = (typeof queuedNote !== "undefined" && queuedNote) ||
      ((kind === "future" && r.futureReady < r.futureTotal)
        ? "Rendering future radar: " + r.futureReady + "/" + r.futureTotal + " hours ready - new hours appear automatically." : "");
  }}
}}
function play() {{ playing = true; document.getElementById("play").textContent = "\\u23f8"; timer = setInterval(() => show((idx + 1) % frames.length), 700); }}
function pause() {{ playing = false; document.getElementById("play").textContent = "\\u25b6"; clearInterval(timer); }}
function framesFor(k) {{
  const spec = LAYERS[k] || {{}};
  if (spec.framesKey) return (DATA.radar && DATA.radar[spec.framesKey]) || [];
  if (spec.satKey) return (DATA.satBands[spec.satKey] || {{}}).frames || [];
  return [];
}}
function build() {{
  if (kind.startsWith("site:")) {{
    const sid = kind.slice(5), rendered = DATA.sites[sid];
    frames = (rendered && rendered.frames) || [];
    if (frames.length) {{ queuedNote = "";
      if (!timer) {{ show(frames.length - 1); if (playing) play(); }} else show(frames.length - 1);
      return;
    }}
  }}
  frames = framesFor(kind);
  const spec0 = LAYERS[kind] || {{}};
  for (const fb of (spec0.fallbacks || [])) if (!frames.length && framesFor(fb).length) {{ kind = fb; break; }}
  frames = framesFor(kind);
  const sel = document.getElementById("layer"); if (sel && sel.value !== kind) sel.value = kind;
  if (!frames.length) {{ frameEl.textContent = "no frames yet"; return; }}
  if (!timer) {{ show(frames.length - 1); if (playing) play(); }} else show(frames.length - 1);
}}
function refresh(d) {{ DATA = d; if (map) build(); }}
"""


def _page(title, active, body, extra_head=""):
    pages = [("index.html", "Home"), ("radar.html", "Radar"), ("satellite.html", "Satellite"),
             ("models.html", "Models"), ("tropical.html", "NHC"), ("severe.html", "Severe"),
             ("meso.html", "Mesoanalysis"),
             ("obs.html", "Obs & Skew-T"), ("charts.html", "Charts & MOS"), ("national.html", "National"),
             ("forecast.html", "Forecast")]
    nav = "".join(
        f'<a class="pg{" on" if p == active else ""}" href="{p}">{label}</a>'
        for p, label in pages
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)} - {config.PAGE_NAME}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
{MAPBOX_JS}{extra_head}
<style>{_CSS}</style>
</head><body>
<nav><div class="wrap">
  <a class="brand" href="index.html">🌧️ <span>{html.escape(config.PAGE_NAME)}</span></a>
  {nav}
  <div class="navctl">
    <select id="baseSel" title="Basemap" style="max-width:110px"></select>
    <button id="refreshBtn" title="Refresh this page now">⟳ Refresh</button>
    <label class="autolbl"><input type="checkbox" id="autoChk" checked/> auto <span id="autoCnt">90</span>s</label>
    <a id="fbShare" title="Share this page on Facebook" target="_blank" rel="noopener"
       style="text-decoration:none;background:#1877f2;color:#fff;padding:4px 10px;border-radius:8px;font-size:12.5px;font-weight:600;display:inline-block">📘 Share</a>
  </div>
</div></nav>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const SITE_DATA_URL = "data.json";
/* live-update watchdog: re-fetch data.json every 3 min and show its age */
let SITE_DATA = null;
async function siteRefresh() {{
  try {{
    SITE_DATA = await (await fetch(SITE_DATA_URL, {{ cache: "no-store" }})).json();
    if (typeof onDataRefresh === "function") onDataRefresh(SITE_DATA);
    const gen = new Date(SITE_DATA.generated.replace(" ", "T"));
    const mins = Math.max(0, Math.round((Date.now() - gen.getTime()) / 60000));
    const el = document.getElementById("upd");
    if (el) el.textContent = mins <= 5 ? (`\\u2705 Live data \\u00b7 updated ${{mins}} min ago`) : (`\\u26a0\\ufe0f Data ${{mins}} min old \\u00b7 waiting for refresh`);
  }} catch (e) {{ /* offline: keep last data */ }}
}}
siteRefresh();
setInterval(siteRefresh, 180000);
/* page auto-refresh: soft (map pages define onDataRefresh) or full reload */
let AUTO_LEFT = 90;
const autoCnt = document.getElementById("autoCnt"), autoChk = document.getElementById("autoChk");
document.getElementById("refreshBtn").onclick = () => location.reload();
try {{ autoChk.checked = localStorage.getItem("tnwxAuto") !== "off"; }} catch (_e) {{}}
autoChk.onchange = () => {{ AUTO_LEFT = 90; try {{ localStorage.setItem("tnwxAuto", autoChk.checked ? "on" : "off"); }} catch (_e) {{}} }};
setInterval(() => {{
  if (!autoChk.checked) {{ if (autoCnt) autoCnt.textContent = "\\u221e"; return; }}
  AUTO_LEFT -= 1;
  if (autoCnt) autoCnt.textContent = AUTO_LEFT;
  if (AUTO_LEFT <= 0) {{
    AUTO_LEFT = 90;
    if (typeof onDataRefresh === "function") siteRefresh(); else location.reload();
  }}
}}, 1000);
{_MAP_CONTROLS_JS}
{_BOOT_CONTROLS_JS}
</script>
<div class="wrap">
{body}
</div>
<footer>
  <div id="upd" style="color:#7d8794">checking data age…</div>
  Data: National Weather Service · NOAA · RainViewer · SPC — all free, no keys.<br/>
  Auto-updated every few minutes by the {html.escape(config.PAGE_NAME)} weather center ·
  <a href="{config.PAGE_URL}" target="_blank">Facebook page</a> ·
  <a id="fbShareFt" href="#" target="_blank" rel="noopener">📘 Post this page to Facebook</a> ·
  <a href="fb_page.html" target="_blank">📱 Facebook post page (stand-alone)</a>
</footer>
<script>
/* Facebook share: use the canonical public URL when served from it,
   otherwise share the page's public home (localhost shares are useless) */
(function () {{
  var pub = {json.dumps(getattr(config, "PUBLIC_SITE_URL", "") or "")};
  var here = location.origin + location.pathname;
  var target = (location.hostname === "localhost" || location.hostname === "127.0.0.1") && pub ? pub : here;
  var u = "https://www.facebook.com/sharer/sharer.php?u=" + encodeURIComponent(target);
  ["fbShare", "fbShareFt"].forEach(function (id) {{
    var a = document.getElementById(id); if (a) a.href = u;
  }});
}})();
</script>
</body></html>"""


def _alert_color(event):
    e = (event or "").lower()
    if "tornado warning" in e:
        return "#ff1744"
    if "warning" in e:
        return "#ff5252" if ("severe" in e or "flash" in e) else "#ff9f43"
    if "watch" in e:
        return "#ffd54f"
    if "advisory" in e:
        return "#ffe0b2"
    return "#888888"


def _icon(text):
    f = (text or "").lower()
    if "thunder" in f:
        return "⛈️"
    if "snow" in f:
        return "❄️"
    if "rain" in f or "shower" in f:
        return "🌧️"
    if "sunny" in f or "clear" in f:
        return "☀️"
    if "cloud" in f:
        return "☁️"
    return "🌤️"


# ---------------------------------------------------------------- pages
def page_index(d):
    cur = d["current"]
    alerts = d["alerts"]
    spc = d.get("spc") or {}
    storm = d.get("storm") or {}
    cells = storm.get("cells") or []

    if alerts:
        alert_html = "".join(
            f'<div class="alert" style="border-left-color:{_alert_color(a["event"])}">'
            f'<b>{html.escape(a["event"])}</b><span>{html.escape(a["areaDesc"])} · until {a["expires"] or "further notice"}</span></div>'
            for a in alerts[:8])
    else:
        alert_html = '<div class="alert ok">No active alerts for this area.</div>'

    spc_html = "".join(
        f'<div class="kpi"><span>SPC {label}</span>'
        f'<b class="chip" style="background:{v["fill"]};font-size:16px">{html.escape(v["label"])}</b></div>'
        for label, v in spc.items())

    days_html = "".join(
        f'<div class="day"><div class="tx" style="font-weight:700;color:#cdd7e4">{html.escape(day["name"])}</div>'
        f'<div style="font-size:30px;margin:4px 0">{_icon(day["text"])}</div>'
        f'<div class="hi">{day["hi"]}°F</div><div class="tx">{html.escape(day["text"])}</div>'
        f'<div style="color:#7d8794;font-size:12px">💧 {day["pop"]}% · 💨 {html.escape(day["wind"])}</div></div>'
        for day in d["days"][:7])

    cells_html = ""
    cells = [c for c in cells if c.get("dbz") is not None and c.get("lat") is not None]
    if cells:
        rows = "".join(
            f'<tr><td>{i + 1}</td><td class="dbz {"sev" if c["dbz"] >= 55 else "mod" if c["dbz"] >= 45 else "lit"}">{c["dbz"]:.0f} dBZ</td>'
            f'<td>{c["lat"]:.2f}, {c["lon"]:.2f}</td></tr>'
            for i, c in enumerate(cells[:10]))
        cells_html = (f'<h2>🧠 AI storm tracker</h2><p class="src">{html.escape(storm.get("summary") or "")}</p>'
                      f'<table class="cells"><tr><th>#</th><th>Peak echo</th><th>Location</th></tr>{rows}</table>')

    body = f"""
<header class="hero">
  <h1>🌦️ <span style="color:var(--acc)">{html.escape(config.PAGE_NAME)}</span></h1>
  <div class="sub">{html.escape(d["place"])} · updated {d["generated"]}</div>
</header>

<div class="card"><div class="now">
  <div class="big">{cur["tempF"] if cur["tempF"] is not None else "--"}°F</div>
  <div><div style="font-size:18px;color:#cdd7e4">{html.escape(cur["text"])}</div>
  <div class="meta">💧 Dew point {cur["dewF"] if cur["dewF"] is not None else "n/a"}°F · 💨 {html.escape(cur["wind"])} · Humidity {cur["rh"]}%<br/>Observation {cur["time"]}</div></div>
</div></div>

<div class="card"><h2>⚠️ Active alerts</h2><div class="alerts">{alert_html}</div></div>

<div class="card"><h2>🎩 SPC convective outlook</h2><div class="kpis">{spc_html or '<span class="src">SPC data unavailable.</span>'}</div></div>

<div class="card">{cells_html or '<h2>🧠 AI storm tracker</h2><span class="src">No storm cells detected in the current HRRR forecast window.</span>'}</div>

<div class="card"><h2>📅 7-day forecast</h2><div class="grid cards7">{days_html}</div></div>

<div class="card"><h2>🗺️ Explore</h2>
  <p class="src">Everything from the weather center, right here on the site:</p>
  <div class="ctl">
    <a href="radar.html" style="background:#2b80ff;color:#fff;border-radius:8px;padding:9px 18px">📡 Radar + future</a>
    <a href="satellite.html" style="background:#2b80ff;color:#fff;border-radius:8px;padding:9px 18px">🛰️ All satellite bands</a>
    <a href="models.html" style="background:#2b80ff;color:#fff;border-radius:8px;padding:9px 18px">🧮 All model maps</a>
    <a href="tropical.html" style="background:#6a1b9a;color:#fff;border-radius:8px;padding:9px 18px">🌀 NHC tropical</a>
    <a href="severe.html" style="background:#c62828;color:#fff;border-radius:8px;padding:9px 18px">🚨 Severe storms</a>
    <a href="national.html" style="background:#2b80ff;color:#fff;border-radius:8px;padding:9px 18px">🗺️ National + upper air</a>
  </div>
</div>
"""
    return _page("Live", "index.html", body)


def page_radar(d):
    layers = {
        "past": {"label": "Real-time (RainViewer)", "mode": "tiles", "framesKey": "past",
                 "path": "/256/{z}/{x}/{y}/2/1_1_", "fallbacks": ["nws"]},
        "nowcast": {"label": "Nowcast (+10-30 min)", "mode": "tiles", "framesKey": "nowcast",
                    "path": "/256/{z}/{x}/{y}/2/1_1_", "fallbacks": ["past", "nws"]},
        "future": {"label": "Future radar (HRRR + NAM, 48 h)", "mode": "png", "framesKey": "future"},
        "mrms": {"label": "MRMS mosaic (official)", "mode": "png", "framesKey": "mrms"},
        "nws": {"label": "NWS mosaic (official)", "mode": "png", "framesKey": "nws"},
    }
    site_opts = "".join(
        f'<option value="site:{sid}">{sid} — {html.escape(v["name"])} radar</option>'
        for sid, v in (d.get("sites") or {}).items())
    # radar-mode + MRMS-level pickers (populated/refreshed by JS)
    mrms_order = ["cref", "lowref", "l0050", "l0200", "l0400", "l0800", "l1500",
                  "zdr050", "rho050", "azshr", "azshr36", "rots", "mesh",
                  "shi", "vil", "etop", "prate", "qpe1h"]
    mrms_labels = d.get("mrmsProducts") or {}
    mrms_opts = "".join(f'<option value="{pk}">{html.escape(mrms_labels.get(pk, pk))}</option>'
                        for pk in mrms_order if pk in mrms_labels)
    body = f"""
<header class="hero"><h1>📡 Radar</h1>
<div class="sub">Real-time NEXRAD · Future radar 0-48 h · MRMS + NWS mosaics · every NWS radar site — drag to pan, pinch/scroll to zoom.</div></header>

<div class="card">
  <div id="map" class="map-dark"></div>
  <div class="ctl">
    <button id="play">⏸</button><span class="frame" id="frame">--:--</span>
    <select id="layer">
      <option value="past">Real-time (RainViewer)</option>
      <option value="nowcast">Nowcast (+10-30 min)</option>
      <option value="future">Future radar (HRRR + NAM, 48 h)</option>
      <option value="mrms">MRMS mosaic (official)</option>
      <option value="nws">NWS mosaic (official)</option>
      <optgroup label="Individual radar sites" id="siteGroup">{site_opts}</optgroup>
    </select>
    <select id="sitePick"></select>
    <select id="siteMode" style="display:none" title="Radar mode"></select>
    <select id="mrmsProd" style="display:none" title="MRMS product / level">{mrms_opts}</select>
    <input type="range" id="opacity" min="20" max="100" value="80"/>
  </div>
  <div class="ctl" style="margin-top:12px">
    <label><input type="checkbox" id="ly_obs" checked/> Observations</label>
    <select id="obsSel">
      <option value="tn">East Tennessee stations</option>
      <option value="us">All US stations</option>
    </select>
  </div>
  <div class="src" id="pend"></div>
  <div class="src">Radar: RainViewer global NEXRAD composite + NOAA MRMS / NWS WMS mosaics. Future radar: HRRR 3 km (0-18 h) + NAM 3 km nest (18-48 h). Individual sites: every NWS radar serves 5 modes — super-res reflectivity, velocity, hybrid scan, 1-hour + storm-total precip — full 460 km range, all animated. MRMS picker: height levels 0.5–15 km, dual-pol (ZDR/RhoHV), azimuthal shear, rotation tracks, hail, echo tops, precip. Everything renders in over the first few update cycles.</div>
</div>

<script>
{_player_js(json.dumps(layers))}
document.getElementById("play").onclick = () => playing ? pause() : play();
document.getElementById("opacity").oninput = () => {{ for (const l of curLayers) if (l.setOpacity) l.setOpacity(OPACITY()); }};
document.getElementById("layer").onchange = (e) => {{
  kind = e.target.value;
  if (!kind.startsWith("site:")) queuedNote = "";
  document.getElementById("mrmsProd").style.display = kind === "mrms" ? "" : "none";
  if (kind.startsWith("site:")) {{
    const sid = kind.slice(5), site = DATA.sites[sid];
    if (site) {{ curSite = sid; fillModePicker(site); map.setView([site.lat, site.lon], 7);
      frames = siteFrames(sid); idx = 0;
      if (timer) {{}} else if (playing) play(); else show(frames.length - 1); return; }}
  }} else {{ curSite = null; document.getElementById("siteMode").style.display = "none"; }}
  if (kind.startsWith("mrms")) {{ mrmsPick(); return; }}
  build();
}};
/* per-site radar MODE picker: reflectivity, velocity, hybrid scan, precip */
let curSite = null;
function siteFrames(sid, mode) {{
  const site = DATA.sites[sid];
  if (!site) return [];
  if (site.modes) {{
    const m = mode && site.modes[mode] ? mode : Object.keys(site.modes)[0];
    return site.modes[m].frames;
  }}
  return site.frames || [];
}}
function fillModePicker(site) {{
  const sel = document.getElementById("siteMode");
  if (!site.modes) {{ sel.style.display = "none"; return; }}
  sel.innerHTML = Object.entries(site.modes).map(([k, v]) =>
    `<option value="${{k}}">${{v.label}}</option>`).join("");
  sel.style.display = "";
}}
document.getElementById("siteMode").onchange = (e) => {{
  if (!curSite) return;
  frames = siteFrames(curSite, e.target.value); idx = 0;
  if (timer) {{}} else if (playing) play(); else show(frames.length - 1);
}};
/* MRMS product / level picker (heights 0.5-15 km, dual-pol, shear, precip) */
function mrmsPick() {{
  const pk = document.getElementById("mrmsProd").value;
  frames = (DATA.mrmsLoops && DATA.mrmsLoops[pk]) || [];
  if (!frames.length && pk === "cref") frames = (DATA.radar && DATA.radar.mrms) || [];
  if (!frames.length) {{
    pause(); clear(); frameEl.textContent = "queued";
    document.getElementById("pend").textContent = (DATA.mrmsProducts && DATA.mrmsProducts[pk] || pk) + " renders within the next few update cycles.";
    return;
  }}
  document.getElementById("pend").textContent = "";
  kind = "mrms"; idx = 0;
  if (timer) {{}} else if (playing) play(); else show(frames.length - 1);
}}
document.getElementById("mrmsProd").onchange = () => mrmsPick();
let obsLayer = null;
function drawObs() {{
  if (obsLayer) map.removeLayer(obsLayer);
  const scope = (document.getElementById("obsSel") || {{}}).value || "tn";
  const src = scope === "us" ? ((DATA.obs && DATA.obs.us) || []) : ((DATA.obs && DATA.obs.stations) || []);
  const rend = L.canvas({{ padding: 0.5 }});
  obsLayer = L.layerGroup(src.map(s =>
    L.circleMarker([s.lat, s.lon], {{ radius: 4, renderer: rend, color: "#fff", weight: 1.5,
      fillColor: s.tempF == null ? "#777" : (s.tempF >= 85 ? "#ff9f43" : s.tempF >= 65 ? "#ffd54f" : s.tempF >= 45 ? "#aed581" : "#4da3ff"), fillOpacity: .95 }})
      .bindTooltip(`<b>${{s.id}}</b>${{s.name && s.name !== s.id ? " " + s.name : ""}}<br/>${{s.tempF == null ? "n/a" : s.tempF + "\u00b0F"}} · dew ${{s.dewF == null ? "-" : s.dewF + "\u00b0F"}}<br/>${{s.windDir || ""}} ${{s.windMph == null ? "" : s.windMph + " mph"}} ${{s.desc || ""}} ${{s.time || ""}}`)));
  obsLayer.addTo(map);
}}
/* the full NWS site picker: local radars first, then all 159 with status */
function fillSitePicker() {{
  const sp = document.getElementById("sitePick");
  const cats = DATA.siteCatalog || [];
  const local = cats.filter(c => c.local), rest = cats.filter(c => !c.local);
  const opt = c => `<option value="${{c.id}}">${{c.id}} — ${{c.name}} (${{c.status === "ready" ? c.frames + " frames" : "queued"}})</option>`;
  sp.innerHTML = `<optgroup label="★ Local radars">${{local.map(opt).join("")}}</optgroup>` +
                 `<optgroup label="All NWS radars">${{rest.map(opt).join("")}}</optgroup>`;
}}
let queuedNote = "";
function pickSite(sid) {{
  const cat = (DATA.siteCatalog || []).find(c => c.id === sid);
  if (cat) map.setView([cat.lat, cat.lon], 7);
  const rendered = DATA.sites[sid];
  const s = document.getElementById("layer");
  if (rendered && (rendered.frames || []).length) {{
    queuedNote = "";
    curSite = sid; fillModePicker(rendered);
    s.value = "site:" + sid; kind = s.value;
    document.getElementById("mrmsProd").style.display = "none";
    frames = siteFrames(sid); idx = 0;
    if (timer) {{}} else if (playing) play(); else show(frames.length - 1);
  }} else {{
    pause();
    queuedNote = sid + " (" + (cat ? cat.name : "") + ") is queued - its imagery renders within the next few update cycles. Showing no radar until then.";
    clear(); frames = []; frameEl.textContent = "queued";
    document.getElementById("pend").textContent = queuedNote;
  }}
}}
async function boot() {{
  DATA = await (await fetch(SITE_DATA_URL, {{cache: "no-store"}})).json();
  document.title = DATA.pageName + " - Radar";
  initMap(); build(); drawObs(); fillSitePicker();
  document.getElementById("sitePick").onchange = (e) => pickSite(e.target.value);
  document.getElementById("obsSel").onchange = () => drawObs();
  document.getElementById("ly_obs").onchange = (e) => {{
    if (e.target.checked && !map.hasLayer(obsLayer)) obsLayer.addTo(map);
    if (!e.target.checked && map.hasLayer(obsLayer)) map.removeLayer(obsLayer);
  }};
  setInterval(async () => {{ refresh(await (await fetch(SITE_DATA_URL, {{cache: "no-store"}})).json()); drawObs(); fillSitePicker(); }}, 180000);
}}
boot();
document.addEventListener("visibilitychange", () => document.hidden ? pause() : play());
function onDataRefresh(d) {{ refresh(d); drawObs(); }}
</script>
"""
    return _page("Radar", "radar.html", body)


def page_satellite(d):
    body = """
<header class="hero"><h1>🛰️ Satellite</h1>
<div class="sub">Every GOES-19 ABI band — water vapor (high/mid/low + GINI full-disk), IR, all visible bands, fire, ozone, CO2.</div></header>
<div class="card">
  <div id="map" class="map-dark"></div>
  <div class="ctl">
    <button id="play">⏸</button><span class="frame" id="frame">--:--</span>
    <select id="layer"></select>
    <input type="range" id="opacity" min="30" max="100" value="90"/>
  </div>
  <div class="src" id="pend"></div>
  <div class="src">GOES-19 ABI decoded from NOAA open data, rendered locally. Visible bands go dark at night - that is the satellite, not the site. Full-disk water vapor takes a couple of minutes to render the first time.</div>
</div>
<script>
""" + _player_js("{}") + """
document.getElementById("play").onclick = () => playing ? pause() : play();
document.getElementById("opacity").oninput = () => { for (const l of curLayers) if (l.setOpacity) l.setOpacity(OPACITY()); };
const layerSel = document.getElementById("layer");
layerSel.onchange = (e) => { kind = e.target.value; try { localStorage.setItem("tnwxSatKind", kind); } catch (_e) {} build(); };
function fillLayerPicker() {
  layerSel.innerHTML = "";
  for (const [k, v] of Object.entries(LAYERS)) {
    const o = document.createElement("option"); o.value = k; o.textContent = v.label;
    if (k === kind) o.selected = true; layerSel.appendChild(o);
  }
}
async function boot() {
  DATA = await (await fetch(SITE_DATA_URL, {cache: "no-store"})).json();
  document.title = DATA.pageName + " - Satellite";
  LAYERS = {};
  for (const [k, v] of Object.entries(DATA.satBands || {}))
    if ((v.frames || []).length) LAYERS[k] = { label: v.label, mode: "png", satKey: k };
  try { const sv = localStorage.getItem("tnwxSatKind"); if (sv && LAYERS[sv]) kind = sv; } catch (_e) {}
  if (LAYERS[DATA.satHome]) kind = DATA.satHome;
  else if (Object.keys(LAYERS).length) kind = Object.keys(LAYERS)[0];
  initMap(); fillLayerPicker(); build();
  setInterval(async () => { refresh(await (await fetch(SITE_DATA_URL, {cache: "no-store"})).json()); }, 180000);
}
boot();
document.addEventListener("visibilitychange", () => document.hidden ? pause() : play());
function onDataRefresh(d) { refresh(d); }
</script>
"""
    return _page("Satellite", "satellite.html", body)


# 4-pane model comparison (plain JS, no f-string braces): four model pickers
# sharing one product + region, frames synchronized across all panes.
_CMP4_JS = """
const cmpGrid = document.getElementById("cmpGrid");
const cmpProdSel = document.getElementById("cmpProd");
const cmpRegionSel = document.getElementById("cmpRegion");
const cmpPlayBtn = document.getElementById("cmpPlay");
const cmpFrameEl = document.getElementById("cmpFrame");
const CMP_MODELS = Object.keys(CAT).sort();
let cmpCurrent = [], cmpPanes = [], cmpFhList = [], cmpIdx = 0, cmpTimer = null, cmpPlaying = false;
(function initCmpCurrent() {
  const pref = ["GFS", "NAM", "RRFS", "AIFS", "HRRR", "GraphCast", "ECMWF"].filter(m => CMP_MODELS.includes(m));
  cmpCurrent = [...pref, ...CMP_MODELS.filter(m => !pref.includes(m))].slice(0, 4);
  while (cmpCurrent.length < 4 && CMP_MODELS.length) cmpCurrent.push(CMP_MODELS[cmpCurrent.length % CMP_MODELS.length]);
})();
function cmpFillProducts() {
  const counts = {}, labels = {};
  for (const c of REND) counts[c.product] = (counts[c.product] || 0) + 1;
  for (const k of Object.keys(MS.mpasProducts || {})) counts[k] = (counts[k] || 0) + (MS.mpas[k] && MS.mpas[k].frames ? 1 : 0);
  for (const k of Object.keys(MS.shieldProducts || {})) counts[k] = (counts[k] || 0) + (MS.shield[k] && MS.shield[k].frames ? 1 : 0);
  for (const m of Object.keys(CAT)) for (const p of (CAT[m].products || [])) labels[p.key] = p.label;
  cmpProdSel.innerHTML = Object.keys(counts).sort((a, b) => counts[b] - counts[a])
    .map(k => `<option value="${k}">${labels[k] || k} (${counts[k]})</option>`).join("");
}
function cmpCombo(m) {
  const r = cmpRegionSel.value, p = cmpProdSel.value;
  const c = REND.find(x => x.model === m && x.product === p && x.region === r);
  if (c && c.frames.length) return c;
  /* MPAS / FV3 panes render from their pre-rendered frame loops (MS) */
  const msKey = m === "MPAS" ? "mpas" : (m.indexOf("FV3") === 0 ? "shield" : null);
  if (msKey) {
    const got = ((MS[msKey] || {})[p] || {}).frames || [];
    if (got.length) return { model: m, product: p, region: "etn", cycle: String((MS[msKey] || {})[p].init || ""),
                             frames: got.map(f => ({fh: f.hour, url: f.url})) };
  }
  return null;
}
function cmpBuild() {
  cmpStop();
  for (const p of cmpPanes) cmpPaneStop(p);
  cmpPanes = []; cmpFhList = []; cmpGrid.innerHTML = "";
  cmpCurrent.forEach((m, i) => {
    const pane = document.createElement("div"); pane.className = "pane";
    const c = cmpCombo(m);
    const opts = CMP_MODELS.map(x => `<option value="${x}"${x === m ? " selected" : ""}>${x}</option>`).join("");
    pane.innerHTML = `<h3><select data-i="${i}">${opts}</select><span class="cap"></span><button class="pb" title="Play/pause this pane">⏸</button></h3>` +
      (c ? `<img alt="${m}"/>` : `<div class="miss">queued - renders within a few update cycles</div>`);
    cmpGrid.appendChild(pane);
    pane.querySelector("select").onchange = e => { cmpCurrent[i] = e.target.value; cmpBuild(); };
    if (c) {
      const initTxt = c.cycle ? "init " + String(c.cycle).slice(-6, -2) + "Z " : "";
      pane.querySelector(".cap").innerHTML = `${initTxt}<span class="fh">F${String(c.frames[c.frames.length-1].fh).padStart(3,"0")}</span>`;
      const paneObj = { img: pane.querySelector("img"), combo: c, timer: null, idx: 0 };
      cmpPanes.push(paneObj);
      c.frames.forEach(f => cmpFhList.push(f.fh));
      pane.querySelector(".pb").onclick = () => {
        if (paneObj.timer) { cmpPaneStop(paneObj); pane.querySelector(".pb").textContent = "▶"; }
        else { cmpPanePlay(paneObj, i); pane.querySelector(".pb").textContent = "⏸"; }
      };
    } else cmpPanes.push({ img: null, combo: null, timer: null });
  });
  cmpFhList = [...new Set(cmpFhList)].sort((a, b) => a - b);
  cmpIdx = Math.max(0, cmpFhList.length - 1);
  cmpShow();
}
function cmpShow() {
  if (!cmpFhList.length) { cmpFrameEl.textContent = "--"; return; }
  const fh = cmpFhList[cmpIdx];
  cmpFrameEl.textContent = "F" + String(fh).padStart(3, "0");
  for (const p of cmpPanes) {
    if (!p.combo || !p.img) continue;
    const avail = p.combo.frames.filter(f => f.fh <= fh);
    const f = avail.length ? avail[avail.length - 1] : p.combo.frames[0];
    if (f) p.img.src = f.url;
  }
}
/* per-pane animation: every model plays its own full loop simultaneously */
function cmpPanePlay(p, i) {
  if (p.timer) clearInterval(p.timer);
  p.idx = Math.max(0, (p.combo ? p.combo.frames.length : 1) - 1);
  p.timer = setInterval(() => {
    if (!p.combo || !p.img || !p.combo.frames.length) return;
    p.idx = (p.idx + 1) % p.combo.frames.length;
    const f = p.combo.frames[p.idx];
    if (f) { p.img.src = f.url; const cap = p.img.closest(".pane").querySelector(".cap .fh");
             if (cap) cap.textContent = "F" + String(f.fh).padStart(3, "0"); }
  }, 1100 + i * 40);
}
function cmpPaneStop(p) { if (p.timer) { clearInterval(p.timer); p.timer = null; } }
function cmpPlayAll() {
  cmpPanes.forEach((p, i) => cmpPanePlay(p, i));
  if (cmpTimer) clearInterval(cmpTimer);
  cmpTimer = setInterval(() => { cmpIdx = (cmpIdx + 1) % Math.max(cmpFhList.length, 1); cmpShow(); }, 1200);
}
function cmpStop() {
  cmpPlaying = false; cmpPlayBtn.textContent = "\u25b6";
  if (cmpTimer) clearInterval(cmpTimer); cmpTimer = null;
  for (const p of cmpPanes) cmpPaneStop(p);
}
cmpPlayBtn.onclick = () => { if (cmpPlaying) { cmpStop(); } else { cmpPlaying = true; cmpPlayBtn.textContent = "\u23f8"; cmpPlayAll(); } };
cmpProdSel.onchange = cmpBuild; cmpRegionSel.onchange = cmpBuild;
cmpFillProducts(); cmpBuild();
"""


# MPAS / FV3-SHiELD viewer (plain JS, no f-string braces): animated loop over
# the pre-rendered official frames, one model source + product at a time.
_MSVIEWER_JS = """
const msSrc = document.getElementById("msSrc");
const msProd = document.getElementById("msProd");
const msImg = document.getElementById("msImg");
const msCap = document.getElementById("msCap");
const msFrame = document.getElementById("msFrame");
const msPlay = document.getElementById("msPlay");
let msFrames = [], msIdx = 0, msTimer = null, msPlaying = false;
function msFill() {
  const src = msSrc.value;
  const avail = MS[src] || {};
  const labels = src === "mpas" ? MS.mpasProducts : MS.shieldProducts;
  const keys = Object.keys(labels);
  msProd.innerHTML = keys.map(k => {
    const has = avail[k] && avail[k].frames && avail[k].frames.length;
    return `<option value="${k}">${labels[k]}${has ? "" : " (queued)"}</option>`;
  }).join("");
  const firstAvail = keys.find(k => avail[k] && avail[k].frames && avail[k].frames.length);
  if (firstAvail) msProd.value = firstAvail;
}
function msStop() { msPlaying = false; msPlay.textContent = "\u25b6"; if (msTimer) clearInterval(msTimer); msTimer = null; }
function msShow() {
  if (!msFrames.length) {
    msImg.removeAttribute("src"); msFrame.textContent = "--";
    msCap.textContent = "This product downloads on a coming update cycle - the viewer fills itself in.";
    return;
  }
  const f = msFrames[msIdx];
  msImg.src = f.url;
  msFrame.textContent = f.label || ("F" + String(f.hour).padStart(3, "0"));
  const e = (MS[msSrc.value] || {})[msProd.value] || {};
  msCap.textContent = "Init " + (e.init || "") + " \u00b7 frame " + (msIdx + 1) + "/" + msFrames.length;
}
function msBuild() {
  msStop();
  const e = (MS[msSrc.value] || {})[msProd.value];
  msFrames = (e && e.frames) || [];
  msIdx = Math.max(0, msFrames.length - 1);
  msShow();
  if (msFrames.length > 1) {
    msPlaying = true; msPlay.textContent = "\u23f8";
    msTimer = setInterval(() => { msIdx = (msIdx + 1) % msFrames.length; msShow(); }, 900);
  }
}
msSrc.onchange = () => { msFill(); msBuild(); };
msProd.onchange = msBuild;
msPlay.onclick = () => {
  if (!msFrames.length) return;
  if (msPlaying) msStop();
  else { msPlaying = true; msPlay.textContent = "\u23f8";
    msTimer = setInterval(() => { msIdx = (msIdx + 1) % msFrames.length; msShow(); }, 900); }
};
msFill(); msBuild();
"""


def page_models(d):
    gal = d.get("models") or []
    cards = []
    for i, g in enumerate(gal[:36]):
        fr = g["frames"]
        last = fr[-1]
        # MPAS-style controls on EVERY model card: play/pause, frame select,
        # step buttons, speed. Single-frame cards get a static note instead.
        if len(fr) > 1:
            opts = "".join(f'<option value="{f["fh"]}"{" selected" if f is last else ""}>F{f["fh"]:03d}</option>' for f in fr)
            ctl = (f'<div class="ctl" style="margin-top:8px">'
                   f'<button class="gplay" data-g="{i}">▶</button>'
                   f'<span class="frame gfh" data-g="{i}">F{last["fh"]:03d}</span>'
                   f'<button class="gstep" data-g="{i}" data-d="-1">◀</button>'
                   f'<select class="gsel" data-g="{i}">{opts}</select>'
                   f'<button class="gstep" data-g="{i}" data-d="1">▶</button>'
                   f'<select class="gspeed" data-g="{i}"><option value="2000">0.5x</option>'
                   f'<option value="1000" selected>1x</option><option value="500">2x</option>'
                   f'<option value="250">4x</option></select></div>')
        else:
            ctl = '<div class="src">single frame - more hours render each update cycle</div>'
        cards.append(
            f'<div class="card"><h2>{html.escape(g["model"])} · {html.escape(g["product"])} '
            f'({html.escape(g["region"])} · init {g["cycle"][-6:-2]}Z {g["cycle"][-2:]}Z)</h2>'
            f'<img id="mg_{i}" loading="lazy" src="{last["url"]}" alt="{html.escape(g["model"] + " " + g["product"])}"/>'
            f'<div class="cap">Rendered locally with MetPy · <a href="{last["url"]}" target="_blank">full size</a></div>'
            f'{ctl}</div>')

    # full catalog explorer: pick model + product + region, renders on demand
    cat = d.get("modelCatalog") or {}
    model_opts = "".join(f'<option value="{html.escape(m)}">{html.escape(m)}</option>' for m in sorted(cat))
    psu = d.get("psu")
    psu_html = ""
    if psu and psu.get("frames"):
        pfr = psu["frames"]
        popts = "".join(f'<option value="{k}"{" selected" if k == len(pfr) - 1 else ""}>{html.escape(f["label"])}</option>'
                        for k, f in enumerate(pfr))
        psu_html = (f'<div class="card"><h2>🎞️ HRRR 15-minute future radar loop (PSU e-Wall · init {html.escape(str(psu.get("init", "")))})</h2>'
                    f'<img id="psu" loading="lazy" src="{pfr[-1]["url"]}" style="width:100%;border-radius:10px"/>'
                    f'<div class="ctl" style="margin-top:8px">'
                    f'<button id="psuPlay">▶</button><span class="frame" id="psuFh">{html.escape(pfr[-1]["label"])}</span>'
                    f'<button id="psuPrev">◀</button><select id="psuSel">{popts}</select><button id="psuNext">▶</button>'
                    f'<select id="psuSpeed"><option value="2000">0.5x</option><option value="900" selected>1x</option>'
                    f'<option value="450">2x</option><option value="250">4x</option></select></div></div>')
    body = f"""
<header class="hero"><h1>🧮 Model maps</h1>
<div class="sub">Every model, every product — MetPy × Cartopy renders from NOAA/ECMWF GRIB2 · East Tennessee + US · updated {d["generated"]}</div></header>

<div class="card"><h2>🔍 Render any model product</h2>
  <div class="ctl">
    <select id="catModel">{model_opts}</select>
    <select id="catProd"></select>
    <select id="catRegion">
      <option value="etn">East Tennessee</option>
      <option value="us">US (CONUS)</option>
    </select>
    <button id="catGo">Render</button>
  </div>
  <div class="src" id="catMsg">Pick a model + product and hit Render - the map renders server-side (GRIB decode, ~10-60 s first time, then cached).</div>
  <div id="catWrap"></div>
</div>  <div class="card"><h2>🗂️ 4-pane model comparison (all models · all levels · animated)</h2>
  <div class="ctl">
    <span class="src" style="margin:0">Product / level</span>
    <select id="cmpProd"></select>
    <span class="src" style="margin:0">Region</span>
    <select id="cmpRegion">
      <option value="etn">East Tennessee</option>
      <option value="us">US (CONUS)</option>
    </select>
    <button id="cmpPlay">⏸</button>
    <span class="frame" id="cmpFrame">--</span>
  </div>
  <div class="cmp4" id="cmpGrid"></div>
  <div class="src">All 20 models (NWS global + CAM + AI + MPAS + FV3) × every product/level, animated side-by-side — each pane plays its own loop and the big clock advances every pane that has that hour. Missing hours snap to the nearest earlier frame; unrendered combos queue and fill in automatically.</div>
</div>

{psu_html}
{''.join(cards) or '<div class="card"><span class="src">No model maps rendered yet - the gallery fills as products finish.</span></div>'}

<div class="card"><h2>🌍 MPAS + FV3 (SHiELD) — experimental global models</h2>
  <div class="ctl">
    <select id="msSrc">
      <option value="mpas">NCAR MPAS (3.75 km global)</option>
      <option value="shield">GFDL SHiELD (FV3 core)</option>
    </select>
    <select id="msProd"></select>
    <button id="msPlay">⏸</button>
    <span class="frame" id="msFrame">--</span>
  </div>
  <img id="msImg" class="natimg" loading="lazy" alt="MPAS / SHiELD frame"/>
  <div class="cap src" id="msCap"></div>
  <div class="src">NCAR MPAS-A real-time global 3.75 km and GFDL SHiELD — the FV3-core model — official pre-rendered frames, animated. One product set downloads per update cycle and is cached; the rest queue and fill in automatically.</div>
</div>
<script>
const CAT = {json.dumps(cat)};
const REND = {json.dumps(d.get("renderIndex") or [])};
const GALLERY = {json.dumps([{"i": i, "frames": g["frames"]} for i, g in enumerate(gal[:36])])};
const psu = {json.dumps(psu)};

/* catalog explorer: shows every model x product; pre-rendered combos
   display instantly, the rest report that the updater will fill them in */
const catModel = document.getElementById("catModel"), catProd = document.getElementById("catProd");
function fillProds() {{
  const m = CAT[catModel.value] || {{ products: [] }};
  catProd.innerHTML = m.products.map(p => `<option value="${{p.key}}">${{p.label}}</option>`).join("");
}}
catModel.onchange = fillProds; fillProds();
document.getElementById("catGo").onclick = () => {{
  const msg = document.getElementById("catMsg"), wrap = document.getElementById("catWrap");
  const m = catModel.value, p = catProd.value, r = document.getElementById("catRegion").value;
  const c = REND.find(x => x.model === m && x.product === p && x.region === r);
  const msKey = m === "MPAS" ? "mpas" : (m.indexOf("FV3") === 0 ? "shield" : null);
  const msFrames = msKey ? ((((MS[msKey] || {{}})[p] || {{}}).frames) || []).map(f => ({{fh: f.hour, url: f.url}})) : [];
  wrap.innerHTML = "";
  if (msFrames.length) {{
    const avail = (MS[msKey] || {{}})[p] || {{}};
    msg.textContent = `${{m}} ${{p}} — ${{msFrames.length}} animated frames` + (avail.init ? ` (init ${{avail.init}})` : "");
    const im = new Image(); im.src = msFrames[msFrames.length - 1].url;
    im.style.width = "100%"; im.style.borderRadius = "10px"; wrap.appendChild(im);
    const st = document.createElement("div"); st.className = "stepper";
    const sel = document.createElement("select");
    sel.innerHTML = msFrames.map(f => `<option value="${{f.fh}}">F${{String(f.fh).padStart(3, "0")}}</option>`).join("");
    sel.value = msFrames[msFrames.length - 1].fh;
    sel.onchange = () => {{ const f = msFrames.find(x => x.fh === +sel.value); if (f) im.src = f.url; }};
    const back = document.createElement("button"); back.textContent = "\u25c0";
    const fwd = document.createElement("button"); fwd.textContent = "\u25b6";
    const step = dd => {{ const at = msFrames.findIndex(x => x.fh === +sel.value);
      const f = msFrames[Math.min(msFrames.length - 1, Math.max(0, at + dd))]; if (f) {{ sel.value = f.fh; im.src = f.url; }} }};
    back.onclick = () => step(-1); fwd.onclick = () => step(1);
    st.append(back, sel, fwd); wrap.appendChild(st);
    return;
  }}
  if (msKey) {{
    msg.textContent = `${{m}} ${{p}} is queued - one product set downloads per update cycle; pick another or try again in a few minutes.`;
    return;
  }}
  if (c && c.frames.length) {{
    msg.textContent = `${{m}} ${{p}} (${{r}}) - init ${{String(c.cycle).slice(-6,-2)}}Z ${{String(c.cycle).slice(-2)}}Z, ${{c.frames.length}} frames`;
    const im = new Image(); im.src = c.frames[c.frames.length - 1].url;
    im.style.width = "100%"; im.style.borderRadius = "10px"; wrap.appendChild(im);
    if (c.frames.length > 1) {{
      const st = document.createElement("div"); st.className = "stepper";
      const sel = document.createElement("select");
      sel.innerHTML = c.frames.map(f => `<option value="${{f.fh}}">F${{String(f.fh).padStart(3, "0")}}</option>`).join("");
      sel.value = c.frames[c.frames.length - 1].fh;
      sel.onchange = () => {{ im.src = c.frames.find(f => f.fh === +sel.value).url; }};
      const back = document.createElement("button"); back.textContent = "\u25c0";
      const fwd = document.createElement("button"); fwd.textContent = "\u25b6";
      const step = dd => {{ const at = c.frames.findIndex(f => f.fh === +sel.value);
        const f = c.frames[Math.min(c.frames.length - 1, Math.max(0, at + dd))]; if (f) {{ sel.value = f.fh; im.src = f.url; }} }};
      back.onclick = () => step(-1); fwd.onclick = () => step(1);
      st.append(back, sel, fwd); wrap.appendChild(st);
    }}
  }} else {{
    msg.textContent = `${{m}} ${{p}} (${{r}}) is not pre-rendered in this build yet - the site updater adds more combos every few minutes. Popular products are in the gallery below.`;
  }}
}};

/* gallery animator: MPAS-style controls on every model card */
const gTimers = {{}}, gIdx = {{}};
function gFrames(gi) {{ return (GALLERY.find(x => x.i === gi) || {{}}).frames || []; }}
function gShow(gi, fh) {{
  const fr = gFrames(gi);
  const f = fr.find(x => x.fh === +fh); if (!f) return;
  const img = document.getElementById("mg_" + gi);
  if (img) img.src = f.url;
  const sel = document.querySelector(`.gsel[data-g="${{gi}}"]`);
  if (sel) sel.value = f.fh;
  const lb = document.querySelector(`.gfh[data-g="${{gi}}"]`);
  if (lb) lb.textContent = "F" + String(f.fh).padStart(3, "0");
}}
function gStop(gi) {{ if (gTimers[gi]) {{ clearInterval(gTimers[gi]); gTimers[gi] = null;
  const b = document.querySelector(`.gplay[data-g="${{gi}}"]`); if (b) b.textContent = "\u25b6"; }} }}
function gPlay(gi) {{
  const fr = gFrames(gi); if (fr.length < 2) return;
  if (gTimers[gi]) gStop(gi);
  const spd = +((document.querySelector(`.gspeed[data-g="${{gi}}"]`) || {{}}).value || 1000);
  gIdx[gi] = fr.findIndex(x => x.fh === +(document.querySelector(`.gsel[data-g="${{gi}}"]`) || {{}}).value);
  if (gIdx[gi] < 0) gIdx[gi] = fr.length - 1;
  const b = document.querySelector(`.gplay[data-g="${{gi}}"]`); if (b) b.textContent = "\u23f8";
  gTimers[gi] = setInterval(() => {{
    gIdx[gi] = (gIdx[gi] + 1) % fr.length;
    gShow(gi, fr[gIdx[gi]].fh);
  }}, spd);
}}
document.querySelectorAll(".gplay").forEach(b => b.onclick = () => {{
  const gi = +b.dataset.g; gTimers[gi] ? gStop(gi) : gPlay(gi);
}});
document.querySelectorAll(".gsel").forEach(s => s.onchange = () => {{ const gi = +s.dataset.g; gStop(gi); gShow(gi, s.value); }});
document.querySelectorAll(".gstep").forEach(b => b.onclick = () => {{
  const gi = +b.dataset.g, fr = gFrames(gi);
  const cur = +(document.querySelector(`.gsel[data-g="${{gi}}"]`) || {{}}).value;
  const at = fr.findIndex(x => x.fh === cur);
  const nxt = fr[Math.min(fr.length - 1, Math.max(0, at + (+b.dataset.d)))];
  if (nxt) {{ gStop(gi); gShow(gi, nxt.fh); }}
}});
document.querySelectorAll(".gspeed").forEach(s => s.onchange = () => {{ const gi = +s.dataset.g; if (gTimers[gi]) gPlay(gi); }});
window.addEventListener("beforeunload", () => Object.keys(gTimers).forEach(gi => gStop(gi)));
/* PSU HRRR loop: same controls as the model cards */
if (psu && psu.frames && psu.frames.length) {{
  let psuI = psu.frames.length - 1, psuT = null;
  const psuImg = document.getElementById("psu"), psuLb = document.getElementById("psuFh"),
        psuSel = document.getElementById("psuSel"), psuBtn = document.getElementById("psuPlay");
  function psuShow(k, keepTimer) {{
    psuI = ((k % psu.frames.length) + psu.frames.length) % psu.frames.length;
    const f = psu.frames[psuI];
    const pre = new Image();
    pre.onload = () => {{ psuImg.src = f.url; psuLb.textContent = f.label; psuSel.value = psuI; }};
    pre.src = f.url;
    if (!keepTimer) psuStop();
  }}
  function psuStop() {{ if (psuT) {{ clearInterval(psuT); psuT = null; psuBtn.textContent = "\u25b6"; }} }}
  function psuPlay() {{
    psuStop(); psuBtn.textContent = "\u23f8";
    const spd = +document.getElementById("psuSpeed").value;
    psuT = setInterval(() => psuShow(psuI + 1, true), spd);
  }}
  psuBtn.onclick = () => psuT ? psuStop() : psuPlay();
  document.getElementById("psuPrev").onclick = () => psuShow(psuI - 1);
  document.getElementById("psuNext").onclick = () => psuShow(psuI + 1);
  psuSel.onchange = () => psuShow(+psuSel.value);
  document.getElementById("psuSpeed").onchange = () => {{ if (psuT) psuPlay(); }};
  psuPlay();
}}

const MS = {json.dumps(d.get("mpasShield") or {})};
{_CMP4_JS}
{_MSVIEWER_JS}
</script>
"""
    return _page("Models", "models.html", body)


def page_tropical(d):
    trop = d.get("tropical") or {}
    storms = trop.get("storms") or []
    gfx = trop.get("graphics") or []

    storm_html = ""
    for s in storms:
        storm_html += (f'<div class="alert" style="border-left-color:#e1bee7"><b>🌀 {html.escape(s.get("name") or "Storm")} '
                       f'({html.escape(s.get("classification") or "?")})</b>'
                       f'<span>{html.escape(str(s.get("intensity") or ""))} kt · {html.escape(str(s.get("pressure") or ""))} mb · '
                       f'{s.get("lat", "?")}, {s.get("lon", "?")} · cone + track + wind field on the map</span></div>')
    if not storm_html:
        storm_html = '<div class="alert ok">No active tropical storms (NHC).</div>'

    gfx_html = "".join(
        f'<div class="card"><h2>{html.escape(g["title"])}</h2>'
        f'<img class="natimg" loading="lazy" src="{g["url"]}" alt="{html.escape(g["title"])}"/></div>'
        for g in gfx)

    body = f"""
<header class="hero"><h1>🌀 NHC Tropical</h1>
<div class="sub">Active storms with official cones/tracks · NHC outlooks · updated {d["generated"]}</div></header>

<div class="card">
  <div id="map" class="map-dark"></div>
  <div class="legend">
    <span><i style="background:#e1bee7"></i>Cone + track</span>
    <span><i style="background:#ff8a80"></i>34-kt wind radii</span>
    <span><i style="background:#b39ddb"></i>NHC 7-day development area</span>
  </div>
  <div class="src" id="stormCount">{len(storms)} active storm(s) · cone/track KMZ parsed from NHC, wind radii + outlook from NHC GIS</div>
</div>

<div class="card"><h2>🌀 Active storms</h2>{storm_html}</div>
{gfx_html}

<script>
const TROP = {json.dumps(trop)};
let map, layers = [];
function toggle(id, lyr) {{
  const on = document.getElementById(id).checked;
  if (on && !map.hasLayer(lyr)) lyr.addTo(map);
  if (!on && map.hasLayer(lyr)) map.removeLayer(lyr);
}}
async function boot() {{
  DATA = await (await fetch(SITE_DATA_URL, {{cache: "no-store"}})).json();
  const T = DATA.tropical || TROP;
  document.title = DATA.pageName + " - NHC";
  {_mapbox_token_js()}
  map = L.map("map", {{ zoomSnap: 0.5, maxZoom: 21 }}).setView([25, -78], 4);
  addMapControls(map, [25, -78], 4);
  layers.storms = L.layerGroup();
  (T.storms || []).forEach(s => {{
    if (s.lat != null && s.lon != null)
      L.circleMarker([s.lat, s.lon], {{ radius: 9, color: "#fff", weight: 2, fillColor: "#e1bee7", fillOpacity: .95 }})
        .bindTooltip("🌀 " + s.name + " - " + (s.intensity || "?") + " kt").addTo(layers.storms);
    const addG = (g, style) => {{ if (!g) return;
      L.geoJSON({{ type: "Feature", properties: {{}}, geometry: g }}, {{ style }}).addTo(layers.storms); }};
    addG(s.cone, {{ color: "#e1bee7", weight: 1.5, fillOpacity: 0.08 }});
    addG(s.track, {{ color: "#e1bee7", weight: 2.5, dashArray: "6 6" }});
  }});
  layers.storms.addTo(map);
  layers.wr = L.layerGroup((T.windRadii || []).map(w =>
    L.geoJSON({{ type: "Feature", properties: w, geometry: w.geometry }},
      {{ style: {{ color: "#ff8a80", weight: 1.5, fillOpacity: 0.1 }} }})).flat());
  /* NHC 2/7-day outlook graphics are basin-wide images with fixed bounds */
  layers.outlook = L.layerGroup((T.outlook || []).map(o => {{
    const b = o.bounds;
    const lb = (Array.isArray(b) && !Array.isArray(b[0])) ? L.latLngBounds([[b[0], b[1]], [b[2], b[3]]]) : b;
    return L.imageOverlay(o.href, lb, {{ opacity: 0.8 }}).bindTooltip(o.name || "NHC outlook");
  }}));
  layers.wr.addTo(map); layers.outlook.addTo(map);
}}
boot();
function onDataRefresh(d) {{ /* overlays rebuilt on reload */ }}
</script>
"""
    return _page("NHC", "tropical.html", body)


def page_severe(d):
    sev = d.get("severe") or {}
    outlooks = sev.get("outlooks") or []
    ww = sev.get("warnings") or []
    tn = sev.get("tnAlerts") or []
    md = sev.get("md") or []
    reports = sev.get("reports") or {}

    spc = d.get("spc") or {}
    spc_html = "".join(
        f'<div class="kpi"><span>SPC {label} at {html.escape(d["place"])}</span>'
        f'<b class="chip" style="background:{v["fill"]};font-size:16px">{html.escape(v["label"])}</b></div>'
        for label, v in spc.items())

    tn_html = ""
    if tn:
        tn_html = "".join(
            f'<div class="alert" style="border-left-color:{_alert_color(a["event"])}">'
            f'<b>{html.escape(a["event"])}</b><span>{html.escape(a["areaDesc"])} · until {a["expires"] or "further notice"}</span></div>'
            for a in tn[:12])
    else:
        tn_html = '<div class="alert ok">No active alerts across Tennessee.</div>'

    rep = reports if isinstance(reports, dict) else {}
    rep_html = (f'<div class="kpis">'
                f'<div class="kpi"><span>Tornadoes today</span><b style="color:#ff1744">{rep.get("torn", 0)}</b></div>'
                f'<div class="kpi"><span>Wind reports</span><b style="color:#ffb74d">{rep.get("wind", 0)}</b></div>'
                f'<div class="kpi"><span>Hail reports</span><b style="color:#4da3ff">{rep.get("hail", 0)}</b></div>'
                f'</div>')

    md_html = ""
    for m in md[:1]:
        md_html = f'<div class="card"><h2>📍 Latest SPC Mesoscale Discussion</h2><div class="pre">{html.escape(m.get("text", ""))}</div></div>'

    body = f"""
<header class="hero"><h1>🚨 Severe storms</h1>
<div class="sub">Official NWS watches/warnings · SPC outlooks & MCDs · storm reports · MRMS severe products — updated {d["generated"]}</div></header>

<div class="card">
  <div id="map" class="map-dark"></div>
  <div class="legend">
    <span><i style="background:#ff0000"></i>Tornado warning</span>
    <span><i style="background:#ffa500"></i>Severe t-storm warning</span>
    <span><i style="background:#8b0000"></i>Flash flood warning</span>
    <span><i style="background:#ff9f43"></i>Other warning</span>
    <span><i style="background:#ffd54f"></i>Watch</span>
    <span><i style="background:#c1e9c1"></i><i style="background:#66cdaa"></i><i style="background:#ffff00"></i><i style="background:#ff8c00"></i><i style="background:#ff0000"></i><i style="background:#ff00ff"></i> SPC Day 1-3</span>
    <span><i style="background:#ff8a80"></i>Storm report</span>
  </div>
  <div class="ctl" style="margin-top:12px">
    <label><input type="checkbox" id="ly_ww" checked/> Warnings &amp; watches</label>
    <label><input type="checkbox" id="ly_spc" checked/> SPC outlooks</label>
    <select id="spcDay" title="Outlook day / hazard">
      <option value="all">All days</option>
      <option value="day1">Day 1</option>
      <option value="day2">Day 2</option>
      <option value="day3">Day 3</option>
      <option value="torn">Tornado probs</option>
      <option value="hail">Hail probs</option>
      <option value="wind">Wind probs</option>
    </select>
    <label><input type="checkbox" id="ly_reports" checked/> Storm reports</label>
    <label><input type="checkbox" id="ly_cells" checked/> AI cells</label>
  </div>
  <div class="src">{len(ww)} warning polygons · {len(outlooks)} outlook areas · {len(tn)} TN alerts · basemap {'Mapbox' if _MAPBOX_TOKEN else 'OpenStreetMap'}</div>
</div>

<div class="card"><h2>📊 Storm reports today (SPC)</h2>{rep_html}</div>
<div class="card"><h2>🎩 SPC risk at home</h2><div class="kpis">{spc_html or '<span class="src">SPC data unavailable.</span>'}</div></div>
<div class="card"><h2>⚠️ Tennessee alerts (all counties)</h2><div class="alerts">{tn_html}</div></div>
{md_html}

<script>
const SEV = {json.dumps(sev)};
let map, layers = {{}};
const SPC_FILL = {{"TSTM":"#c1e9c1","MRGL":"#66cdaa","SLGT":"#ffff00","ENH":"#ff8c00","MDT":"#ff0000","HIGH":"#ff00ff","0.02":"#adff2f","0.05":"#ffff00","0.10":"#ff8c00","0.15":"#ff0000","0.30":"#ff00ff","0.45":"#7fffd4","0.60":"#ff00ff"}};
function styleWW(f) {{
  const p = f.properties || {{}};
  const tor = p.tor && p.tor !== "OBSERVED" ? 3 : p.tor ? 2 : 0;
  return {{ color: p.color || "#ff9f43", weight: tor ? 2.5 : 1.5,
           dashArray: p.kind === "watch" ? "5 4" : null, fillOpacity: 0.10 }};
}}
function warnPopup(f) {{
  const p = f.properties || {{}};
  const torTag = p.tor === "POSSIBLE" ? " · <b style='color:#ff1744'>TORNADO POSSIBLE</b>"
               : p.tor ? " · <b style='color:#ff1744'>TORNADO " + p.tor + "</b>" : "";
  return "<b>" + (p.event || "Alert") + "</b>" + torTag
    + (p.severity ? "<br/><span class=src>Severity: " + p.severity + "</span>" : "")
    + (p.areaDesc ? "<br/>" + p.areaDesc : "")
    + (p.headline ? "<br/><span class=src>" + p.headline + "</span>" : "")
    + (p.expires ? "<br/><span class=src>Until " + p.expires + "Z</span>" : "")
    + (p.url ? "<br/><a href='" + p.url + "' target='_blank'>Full alert (NWS) ↗</a>" : "");
}}
function spcPopup(o) {{
  const name = o.day.includes("torn") ? "Tornado probability" : o.day.includes("hail") ? "Hail probability"
             : o.day.includes("wind") ? "Damaging-wind probability" : "Convective outlook";
  const lbl = SPC_FILL[o.label] ? (o.label2 || o.label) : o.label;
  return "<b>SPC " + o.day.replace(/_/, " · ") + "</b><br/>" + name + ": " + lbl;
}}
function spcStyle(o) {{
  return {{ color: "#555555", weight: 1, fillColor: o.fill || SPC_FILL[o.label] || "#c1e9c1", fillOpacity: 0.45 }};
}}
function spcDayMatches(o) {{
  const v = document.getElementById("spcDay").value;
  if (v === "all") return !o.day.includes("_");
  if (v === "torn") return o.day.includes("torn");
  if (v === "hail") return o.day.includes("hail");
  if (v === "wind") return o.day.includes("wind");
  return o.day === v;
}}
function buildSpcLayer(S) {{
  if (layers.spc) map.removeLayer(layers.spc);
  layers.spc = L.layerGroup((S.outlooks || []).filter(spcDayMatches).map(o => {{
    const mk = (coords) => L.polygon(coords, spcStyle(o))
      .bindTooltip("SPC " + o.day.replace(/_/, " ") + ": " + (o.label2 || o.label), {{ sticky: true }})
      .bindPopup(spcPopup(o));
    const g = o.geometry;
    if (g.type === "MultiPolygon") {{
      const grp = L.layerGroup();
      g.coordinates.forEach(poly => mk(poly.map(ring => ring.map(c => [c[1], c[0]]))).addTo(grp));
      return grp;
    }}
    return mk(g.coordinates[0].map(c => [c[1], c[0]]));
  }}));
  if (document.getElementById("ly_spc").checked) layers.spc.addTo(map);
}}
function toggle(id, lyr) {{
  const on = document.getElementById(id).checked;
  if (on && !map.hasLayer(lyr)) lyr.addTo(map);
  if (!on && map.hasLayer(lyr)) map.removeLayer(lyr);
}}
async function boot() {{
  DATA = await (await fetch(SITE_DATA_URL, {{cache: "no-store"}})).json();
  const S = DATA.severe || SEV;
  document.title = DATA.pageName + " - Severe";
  {_mapbox_token_js()}
  map = L.map("map", {{ zoomSnap: 0.5, maxZoom: 21 }}).setView([DATA.lat, DATA.lon], 6);
  addMapControls(map, [DATA.lat, DATA.lon], 6);
  L.circleMarker([DATA.lat, DATA.lon], {{ radius: 6, color: "#fff", weight: 2, fillColor: "#4da3ff", fillOpacity: 1 }}).addTo(map).bindTooltip(DATA.place);
  layers.ww = L.geoJSON({{ type: "FeatureCollection", features: (S.warnings || []).map(w => ({{ type: "Feature", properties: w, geometry: w.geometry }})) }},
    {{ style: styleWW, onEachFeature: (f, l) => {{ l.bindPopup(warnPopup(f)); l.bindTooltip((f.properties.event || "") + (f.properties.tor ? " 🌪" : "") + "<br/>" + (f.properties.areaDesc || ""), {{ sticky: true }}); }} }});
  buildSpcLayer(S);
  document.getElementById("spcDay").onchange = () => buildSpcLayer(S);
  layers.reports = L.layerGroup((S.reports && S.reports.rows || []).map(r => {{
    if (!r.lat || !r.lon) return null;
    const c = r.kind === "T" ? "#ff1744" : r.kind === "W" ? "#ffb74d" : "#4da3ff";
    return L.circleMarker([r.lat, r.lon], {{ radius: 6, color: c, weight: 2, fillOpacity: .9 }})
      .bindTooltip((r.kind === "T" ? "Tornado" : r.kind === "W" ? "Wind" : "Hail") + " report" + (r.comment ? ": " + r.comment : ""));
  }}).filter(Boolean));
  layers.cells = L.layerGroup((DATA.storm.cells || []).filter(c => c.lat && c.lon && c.dbz != null).map(c =>
    L.circleMarker([c.lat, c.lon], {{ radius: 8, color: "#fff", weight: 1.5,
      fillColor: c.dbz >= 55 ? "#ff1744" : c.dbz >= 45 ? "#ffb74d" : "#aed581", fillOpacity: .85 }})
      .bindTooltip("AI cell " + c.dbz.toFixed(0) + " dBZ")));
  toggle("ly_ww", layers.ww); toggle("ly_reports", layers.reports); toggle("ly_cells", layers.cells);
  for (const id of ["ly_ww", "ly_spc", "ly_reports", "ly_cells"])
    document.getElementById(id).onchange = () => toggle(id, layers[id]);
}}
boot();
</script>
"""
    return _page("Severe", "severe.html", body)


def page_meso(d):
    """SPC mesoscale analysis: hourly SFCOA fields per sector, layered + animated."""
    try:
        from data.meso import meso_bundle
        meso = meso_bundle()
    except Exception:  # noqa: BLE001
        meso = {}
    body = f"""
<header class="hero"><h1>🔬 SPC Mesoanalysis</h1>
<div class="sub">Hourly SPC objective analysis (SFCOA) — every diagnostic field, layered like SPC's own viewer. Analysis: <b id="anl">{(meso or {}).get('analysis', '...')}</b> · auto-refreshes on every site update.</div></header>

<div class="card">
  <div class="stack" id="stack">
    <img id="imgField" class="layer" alt="field"/>
    <img id="imgRadar" class="layer" style="display:none" alt="radar"/>
    <img id="imgWarns" class="layer" style="display:none" alt="warnings"/>
    <img id="imgOtlk" class="layer" style="display:none" alt="outlook"/>
  </div>
  <div class="ctl" style="margin-top:12px">
    <b>Field</b>
    <select id="fld"></select>
    <b>Sector</b>
    <select id="sec"></select>
    <button id="btnPlay">▶ Animate 6 h</button>
    <span class="frame" id="fr">{ (meso or {}).get('analysis', '') }</span>
  </div>
  <div class="src">The <b>East Tennessee (zoom)</b> sector magnifies SPC's national analysis to a Greeneville-centered box using SPC's own map projection — same fields, overlays, and animation, zoomed ~9.6×.</div>
  <div class="ctl" style="margin-top:8px">
    <b>Overlays</b>
    <label><input type="checkbox" id="ovRadar"/> Radar</label>
    <label><input type="checkbox" id="ovWarns"/> Warnings</label>
    <label><input type="checkbox" id="ovOtlk"/> SPC outlook</label>
    <span class="src" id="stale"></span>
  </div>
  <div class="src">Images: NOAA/SPC Storm Prediction Center mesoscale analysis (public domain), updated hourly at :00. Field filled with SPC's official color palettes; overlays stack on top. The 6-hour animation steps through SPC's archived hourly frames.</div>
</div>

<script>
const MESO = {json.dumps(meso)};
const SEC = MESO.sectors || {{}};
let curSec = (SEC.ET ? "ET" : "19"), curFld = "sbcp";   // East TN zoom when available
function baseFor(sec) {{ return SEC[sec] && SEC[sec].fields[curFld] ? SEC[sec].fields[curFld].url : null; }}
function show() {{
  const u = baseFor(curSec);
  const el = document.getElementById("imgField");
  if (u) {{ el.src = u + "?" + Date.now(); el.style.display = ""; }}
  else {{ el.removeAttribute("src"); }}
  for (const [id, key] of [["imgRadar","radar"],["imgWarns","warns"],["imgOtlk","otlk"]]) {{
    const ov = SEC[curSec] && SEC[curSec].overlays && SEC[curSec].overlays[key];
    const box = document.getElementById("ov" + key[0].toUpperCase() + key.slice(1));
    const im = document.getElementById(id);
    if (ov && box.checked) {{ im.src = ov.url + "?" + Date.now(); im.style.display = ""; }}
    else {{ im.removeAttribute("src"); im.style.display = "none"; }}
  }}
}}
function fillPickers() {{
  const fs = document.getElementById("fld");
  const groups = MESO.fieldGroups || [];
  fs.innerHTML = groups.map(([g, fields]) =>
    `<optgroup label="${{g}}">` + fields.map(([c, l]) =>
      `<option value="${{c}}"${{c === curFld ? " selected" : ""}}>${{l}}</option>`).join("") + `</optgroup>`).join("");
  const ss = document.getElementById("sec");
  ss.innerHTML = (MESO.sectorOrder || []).map(s =>
    `<option value="${{s}}"${{s === curSec ? " selected" : ""}}>${{(MESO.sectorNames || {{}})[s] || s}}</option>`).join("");
}}
function tick() {{
  document.getElementById("anl").textContent = MESO.analysis || "";
  document.getElementById("fr").textContent = MESO.analysis || "";
  show();
}}
fillPickers();
document.getElementById("fld").onchange = e => {{ curFld = e.target.value; if (!baseFor(curSec)) curSec = "19"; document.getElementById("sec").value = curSec; tick(); }};
document.getElementById("sec").onchange = e => {{ curSec = e.target.value; tick(); }};
for (const id of ["ovRadar", "ovWarns", "ovOtlk"]) document.getElementById(id).onchange = show;
/* 6-hour animation through SPC's hourly archive images
   (data/meso.py pre-fetches field_yymmddhh.gif for the past 6 hours) */
let animT = null, animI = 0;
function archStamp(hoursBack) {{
  const d = new Date(Date.now() - hoursBack * 3600e3);
  const p = n => String(n).padStart(2, "0");
  return `${{p(d.getUTCFullYear() % 100)}}${{p(d.getUTCMonth() + 1)}}${{p(d.getUTCDate())}}${{p(d.getUTCHours())}}`;
}}
function play() {{
  if (animT) {{ stopAnim(); return; }}
  document.getElementById("btnPlay").textContent = "⏸";
  animI = 0;
  const H = (MESO.historyHours || 6);
  animT = setInterval(() => {{
    const el = document.getElementById("imgField");
    if (animI % (H + 1) === H || animI % (H + 1) === 0 && animI > 0 && !animT) {{ }}
    const step = animI % (H + 1);
    const back = H - step;                     // H..0
    if (back === 0) {{
      tick();                                   // live image + real label
    }} else {{
      const u = baseFor(curSec);
      if (u) {{
        const live = u.split("?")[0];           // ../meso/s19/sbcp.gif | ../meso/sET/sbcp.png
        const arc = live.replace(/(\\/?meso\\/s[A-Z0-9]+\\/[a-z0-9_]+)\\.(gif|png)$/, `$1_${{archStamp(back)}}.$2`);
        el.onerror = () => {{ el.onerror = null; tick(); }};
        el.src = arc + "?" + Date.now();
      }}
      document.getElementById("fr").textContent = (MESO.analysis || "") + `  −${{back}} h`;
    }}
    animI++;
  }}, 900);
}}
function stopAnim() {{ clearInterval(animT); animT = null; document.getElementById("btnPlay").textContent = "▶ Animate 6 h"; }}
document.getElementById("btnPlay").onclick = play;
tick();
setInterval(async () => {{
  try {{
    const d = await (await fetch(SITE_DATA_URL, {{cache: "no-store"}})).json();
    if (d.meso) {{ MESO.analysis = d.meso.analysis; Object.assign(MESO.sectors || {{}}, d.meso.sectors || {{}}); tick(); }}
  }} catch (_e) {{}}
}}, 180000);
</script>
"""
    return _page("Mesoanalysis", "meso.html", body)


def page_obs(d):
    obs = d.get("obs") or {}
    stations = obs.get("stations") or []
    us_obs = obs.get("us") or []
    cities = obs.get("cities") or []
    snd = d.get("sounding") or {"hours": {}}
    snd_home = d["place"]
    snd_hours = sorted(snd.get("hours", {}).get(snd_home, {}).keys(), key=int)

    city_rows = "".join(
        f'<tr><td><b>{html.escape(c["city"])}</b></td>'
        f'<td>{c["tempF"] if c["tempF"] is not None else "-"}°F</td>'
        f'<td>{c["dewF"] if c["dewF"] is not None else "-"}°F</td>'
        f'<td>{html.escape(c["windDir"] or "-")} {c["windMph"] if c["windMph"] is not None else "-"}</td>'
        f'<td>{c["gustMph"] if c["gustMph"] is not None else "-"}</td>'
        f'<td>{c["rh"] if c["rh"] is not None else "-"}%</td>'
        f'<td class="src">{html.escape(c["station"] or "")} · {html.escape(c["desc"] or "")}</td></tr>'
        for c in cities)

    hour_opts = "".join(
        f'<option value="{h}"{" selected" if h == "0" else ""}>'
        + ("Now (F000)" if h == "0" else f"+{int(h)} h") + "</option>"
        for h in snd_hours)

    snd0 = (snd.get("hours", {}).get(snd_home, {}).get(snd_hours[0] if snd_hours else "0", {}) or {}).get("url", "")

    body = f"""
<header class="hero"><h1>🌡️ Observations & Skew-T</h1>
<div class="sub">Live METARs for East Tennessee and the whole US · city board · RAP 13 km soundings at 8 locations · updated {d["generated"]}</div></header>

<div class="card">
  <div id="map" class="map-dark"></div>
  <div class="ctl" style="margin-top:10px">
    <select id="obsSel">
      <option value="tn">East Tennessee stations</option>
      <option value="us">All US stations</option>
    </select>
  </div>
  <div class="src" id="obsCount"></div>
</div>

<div class="card"><h2>🏙️ East Tennessee observations (every city)</h2>
  <div style="overflow-x:auto"><table class="cells">
    <tr><th>City</th><th>Temp</th><th>Dew point</th><th>Wind</th><th>Gust</th><th>Humidity</th><th>Station</th></tr>
    {city_rows or '<tr><td colspan="7" class="src">Observations unavailable.</td></tr>'}
  </table></div>
</div>

<div class="card"><h2>🎈 Skew-T / Log-P sounding (RAP 13 km · MetPy)</h2>
  <div class="ctl">
    <select id="sndLoc">{""}</select>
    <select id="sndH">{hour_opts}</select>
  </div>
  <img id="sndImg" class="natimg" loading="lazy" src="{snd0}" alt="Skew-T sounding"/>
  <div class="cap src" id="sndCap"></div>
</div>

<script>
const SND = {json.dumps(snd)};
function tempFill(t) {{
  return t == null ? "#777" : (t >= 85 ? "#ff9f43" : t >= 65 ? "#ffd54f" : t >= 45 ? "#aed581" : "#4da3ff");
}}
function obsTip(s) {{
  return `<b>${{s.id}}</b>${{s.name && s.name !== s.id ? " " + s.name : ""}}<br/>${{s.tempF == null ? "-" : s.tempF + "°F"}} · dew ${{s.dewF == null ? "-" : s.dewF + "°F"}}<br/>${{s.windDir || ""}} ${{s.windMph == null ? "" : s.windMph + " mph"}} ${{s.desc || ""}} ${{s.time || ""}}`;
}}
async function boot() {{
  DATA = await (await fetch(SITE_DATA_URL, {{cache: "no-store"}})).json();
  document.title = DATA.pageName + " - Obs & Skew-T";
  {_mapbox_token_js()}
  map = L.map("map", {{ zoomSnap: 0.5, maxZoom: 21 }}).setView([DATA.lat, DATA.lon], 7);
  addMapControls(map, [DATA.lat, DATA.lon], 7);
  L.circleMarker([DATA.lat, DATA.lon], {{ radius: 6, color: "#fff", weight: 2, fillColor: "#4da3ff", fillOpacity: 1 }}).addTo(map).bindTooltip(DATA.place);
  const rend = L.canvas({{ padding: 0.5 }});
  let obsLayer = null;
  function drawObs() {{
    if (obsLayer) map.removeLayer(obsLayer);
    const scope = document.getElementById("obsSel").value;
    const src = scope === "us" ? ((DATA.obs && DATA.obs.us) || []) : ((DATA.obs && DATA.obs.stations) || []);
    obsLayer = L.layerGroup(src.map(s =>
      L.circleMarker([s.lat, s.lon], {{ radius: 4, renderer: rend, color: "#fff", weight: 1.5,
        fillColor: tempFill(s.tempF), fillOpacity: .95 }}).bindTooltip(obsTip(s))));
    obsLayer.addTo(map);
    const c = document.getElementById("obsCount");
    if (c) c.textContent = src.length + " stations " + (scope === "us" ? "nationwide" : "near " + DATA.place) + " - colored by temperature, hover for details.";
  }}
  document.getElementById("obsSel").onchange = drawObs;
  drawObs();
  const locSel = document.getElementById("sndLoc"), hSel = document.getElementById("sndH");
  const locs = Object.keys((SND.hours || {{}}));
  if (locs.length) {{
    locSel.innerHTML = locs.map(l => `<option value="${{l}}">${{l}}</option>`).join("");
    if (!locs.includes("{snd_home}")) locSel.value = locs[0];
  }}
  const setSnd = () => {{
    const e = ((SND.hours || {{}})[locSel.value] || {{}})[hSel.value];
    const img = document.getElementById("sndImg");
    if (!e) {{ document.getElementById("sndCap").textContent = "This location is still rendering - ready within a few update cycles."; img.removeAttribute("src"); return; }}
    img.src = e.url;
    document.getElementById("sndCap").textContent = "RAP cycle " + (e.meta.cycle || "") + " - valid " + (e.meta.valid || "");
  }};
  locSel.onchange = setSnd; hSel.onchange = setSnd;
  if (locSel.value && hSel.value) setSnd();
}}
boot();
function onDataRefresh(d2) {{ /* static charts; data refreshes footer */ }}
</script>
"""
    return _page("Obs & Skew-T", "obs.html", body)


def page_charts(d):
    charts = (d.get("forecastCharts") or {}).get("locations") or {}
    mos = d.get("mos") or {}
    mos_stations = mos.get("stations") or {}
    home = d["place"]

    home_c = charts.get(home) or {}
    home_imgs = "".join(
        f'<img class="natimg" loading="lazy" src="{c["url"]}" alt="{home} {c["tag"]} chart"/>'
        for c in home_c.get("charts", []))
    if not home_imgs:
        home_imgs = '<div class="src">Charts are rendering - ready within a couple of update cycles.</div>'

    city_cards = ""
    for name, v in sorted(charts.items()):
        if name == home or not v.get("charts"):
            continue
        imgs = "".join(
            f'<img loading="lazy" src="{c["url"]}" alt="{html.escape(name)} {c["tag"]}" style="width:100%;border-radius:8px;margin-top:6px"/>'
            for c in v["charts"])
        city_cards += f'<details class="card" style="padding:12px"><summary style="cursor:pointer;font-weight:700">📍 {html.escape(name)}</summary>{imgs}</details>'

    if mos.get("available"):
        mos_bits = ""
        for name, m in sorted(mos_stations.items()):
            rows = m.get("rows") or []
            if not rows:
                continue
            first = rows[0]
            mos_bits += (f'<div class="alert" style="border-left-color:#4da3ff">'
                         f'<b>📍 {html.escape(name)} ({html.escape(m.get("model", "GFS LAMP"))})</b>'
                         f'<span>init {html.escape(m.get("init", ""))} · {len(rows)} hourly rows · '
                         f'F{first["fhr"]:03d}: {first.get("tmp", "?") or "?"}°F, dew {first.get("dpt") or "?"}°F, '
                         f'PoP {first.get("pop") or 0}%, sky {first.get("sky") or "?"}%</span></div>')
        mos_html = mos_bits or '<div class="alert ok">No MOS bulletins parsed yet.</div>'
        mos_note = ""
    else:
        mos_html = ('<div class="alert ok">MOS bulletins are temporarily unavailable - the public '
                    'bulletin service (Iowa Environmental Mesonet) is down for maintenance right now. '
                    'The charts on this page come straight from the NWS forecast grid and stay live; '
                    'MOS rows will appear here automatically once the service returns.</div>')
        mos_note = '<div class="src">MOS source: GFS-LAMP / MAV bulletins, hourly out to +60 h for 7 stations.</div>'

    body = f"""
<header class="hero"><h1>📈 Forecast charts & MOS</h1>
<div class="sub">48-hour NWS gridpoint charts for every East TN city · GFS MOS guidance · updated {d["generated"]}</div></header>

<div class="card"><h2>📊 {html.escape(home)} - 48-hour forecast charts</h2>
  {home_imgs}
  <div class="src">Straight from the NWS hourly forecast grid (same guidance as the forecast page), rendered locally with MetPy-style styling.</div>
</div>

<div class="card"><h2>🧪 MOS - Model Output Statistics</h2>
  {mos_html}
  {mos_note}
</div>

<div class="card"><h2>🏙️ Every East Tennessee city</h2>
  <div class="src">Click a city to expand its temperature/dew-point, precipitation-chance, and wind charts.</div>
  {city_cards or '<div class="src">City charts are rendering - ready within a couple of update cycles.</div>'}
</div>
"""
    return _page("Charts & MOS", "charts.html", body)


def page_national(d):
    nat = _national_payload()
    wpc = nat["wpc"]
    upper = nat["upperAir"]
    wpc_opts = "".join(f'<option value="{w["url"]}">{html.escape(w["title"])}</option>' for w in wpc)
    levels = sorted({u["level"] for u in upper})
    upper_opts = "".join(f'<option value="{html.escape(lv)}">{html.escape(lv)}</option>' for lv in levels)
    body = f"""
<header class="hero"><h1>🗺️ National products</h1>
<div class="sub">WPC operational charts + SPC observed upper-air analyses · updated {d["generated"]}</div></header>

<div class="card"><h2>🌧️ WPC charts</h2>
  <div class="ctl"><select id="wpcSel" style="max-width:420px">{wpc_opts}</select></div>
  <img id="wpcImg" class="natimg" loading="lazy" src="{wpc[0]["url"] if wpc else ""}" alt="WPC chart"/>
  <div class="cap src" id="wpcCap">{html.escape(wpc[0]["title"] + " - " + wpc[0]["desc"]) if wpc else "WPC charts unavailable."}</div>
</div>

<div class="card"><h2>🎈 Upper-air analyses (SPC obswx)</h2>
  <div class="ctl">
    <select id="uaLevel">{upper_opts}</select>
    <select id="uaTime"></select>
  </div>
  <img id="uaImg" class="natimg" loading="lazy" src="{upper[0]["url"] if upper else ""}" alt="Upper-air analysis"/>
  <div class="cap src" id="uaCap">{"Click a level to switch analyses." if upper else "Upper-air analyses unavailable right now."}</div>
  <div class="src">Levels: Surface, 925, 850, 700, 500, 300, 250 mb · valid 00Z / 12Z rawinsonde analyses.</div>
</div>

<script>
const WPC = {json.dumps(wpc)};
const UA = {json.dumps(upper)};
const wpcSel = document.getElementById("wpcSel");
wpcSel.onchange = () => {{
  const w = WPC.find(x => x.url === wpcSel.value);
  document.getElementById("wpcImg").src = w.url;
  document.getElementById("wpcCap").textContent = w.title + " - " + w.desc;
}};
const uaLevel = document.getElementById("uaLevel"), uaTime = document.getElementById("uaTime");
function fillUaTimes() {{
  const rows = UA.filter(u => u.level === uaLevel.value);
  uaTime.innerHTML = rows.map((u, i) => `<option value="${{i}}">${{u.time}}</option>`).join("");
  showUa();
}}
function showUa() {{
  const rows = UA.filter(u => u.level === uaLevel.value);
  const u = rows[+uaTime.value || 0]; if (!u) return;
  document.getElementById("uaImg").src = u.url;
  document.getElementById("uaCap").textContent = u.title;
}}
uaLevel.onchange = fillUaTimes; uaTime.onchange = showUa;
if (UA.length) fillUaTimes();
</script>
"""
    return _page("National", "national.html", body)


def page_forecast(d):
    days_html = "".join(
        f'<div class="day"><div style="font-weight:700;color:#cdd7e4">{html.escape(day["name"])}</div>'
        f'<div style="font-size:30px;margin:4px 0">{_icon(day["text"])}</div>'
        f'<div class="hi">{day["hi"]}°F</div><div class="tx">{html.escape(day["text"])}</div>'
        f'<div style="color:#7d8794;font-size:12px">💧 {day["pop"]}% · 💨 {html.escape(day["wind"])}</div></div>'
        for day in d["days"][:7])
    hourly_html = "".join(
        f'<div class="hr"><span>{h["t"]}</span><b>{h["temp"]}°F</b><span>💧{h["pop"]}%</span></div>'
        for h in d["hourly"][:24])
    if d["alerts"]:
        alerts_html = "".join(
            f'<div class="alert" style="border-left-color:{_alert_color(a["event"])}">'
            f'<b>{html.escape(a["event"])}</b><span>{html.escape(a["areaDesc"])} · until {a["expires"] or "further notice"}</span></div>'
            for a in d["alerts"])
    else:
        alerts_html = '<div class="alert ok">No active alerts for this area.</div>'
    # ---- every East TN city: expandable 7-day card ----
    cfs = d.get("cityForecasts") or []
    city_cards = []
    for k, cf in enumerate(cfs):
        city = html.escape(cf["city"])
        periods = cf.get("periods") or []
        if not periods:
            city_cards.append(f'<div class="cfcard"><button class="cftoggle" data-k="{k}"><b>{city}</b>'
                              f'<span class="cfsum">forecast unavailable</span><span class="chev">▾</span></button>'
                              f'<div class="cfbody" id="cf{k}"><div class="src">NWS forecast did not return for this point - retrying next update cycle.</div></div></div>')
            continue
        todays = [p for p in periods if "Night" not in p["name"]][:1]
        now_t = periods[0]
        summary = f"{now_t['tempF']}°F · {html.escape(now_t['short'])}" if now_t.get("tempF") is not None else html.escape(now_t.get("short", ""))
        hi = next((p["tempF"] for p in periods if p.get("name", "").startswith("Today") or "Day" in p.get("name", "")), None)
        rows = "".join(
            f'<div class="cfrow"><div class="cfd">{html.escape(p["name"])}</div>'
            f'<div style="font-size:22px">{_icon(p["short"])}</div>'
            f'<div class="cft">{p["tempF"]}°F</div>'
            f'<div class="cfs">{html.escape(p["short"])}</div>'
            f'<div class="cfw">💧 {p["pop"] if p.get("pop") is not None else "-"}% · 💨 {html.escape(p.get("wind") or "")}</div></div>'
            for p in periods)
        det = next((p.get("detailed") for p in periods if p.get("detailed")), "")
        city_cards.append(
            f'<div class="cfcard"><button class="cftoggle" data-k="{k}"><b>{city}</b>'
            f'<span class="cfsum">{summary}{f" · high {hi}°F" if hi else ""}</span><span class="chev">▾</span></button>'
            f'<div class="cfbody" id="cf{k}">{rows}'
            + (f'<div class="src" style="margin-top:6px">{html.escape(det)}</div>' if det else "")
            + '</div></div>')
    city_html = ("".join(city_cards) or '<div class="src">City forecasts load on the next update cycle.</div>')
    body = f"""
<header class="hero"><h1>📋 Forecast & alerts</h1><div class="sub">{html.escape(d["place"])} + every East Tennessee city · National Weather Service · updated {d["generated"]}</div></header>
<div class="card"><h2>⚠️ Active alerts</h2><div class="alerts">{alerts_html}</div></div>
<div class="card"><h2>📅 7-day — {html.escape(d["place"])}</h2><div class="grid cards7">{days_html}</div></div>
<div class="card"><h2>⏱️ Next 24 hours</h2><div class="hourly">{hourly_html}</div></div>
<div class="card"><h2>🏙️ 7-day forecast — every East Tennessee city</h2>
<div class="src" style="margin-bottom:8px">Tap a city to open its full NWS 7-day day/night forecast.</div>
{city_html}
</div>
<style>
.cfcard {{ border:1px solid #263041; border-radius:10px; margin:8px 0; overflow:hidden; background:#111722; }}
.cftoggle {{ width:100%; display:flex; align-items:center; gap:12px; background:none; border:none; color:#e8edf4;
  padding:10px 14px; font-size:15px; cursor:pointer; text-align:left; }}
.cftoggle b {{ min-width:170px; }}
.cfsum {{ color:#9fb0c3; font-size:13px; flex:1; }}
.chev {{ color:#7d8794; transition:transform .15s; }}
.cftoggle.open .chev {{ transform:rotate(180deg); }}
.cfbody {{ display:none; padding:4px 14px 12px; border-top:1px solid #1c2432; }}
.cfbody.open {{ display:block; }}
.cfrow {{ display:grid; grid-template-columns:110px 34px 64px 1fr 170px; gap:8px; align-items:center;
  padding:7px 0; border-bottom:1px solid #171e2b; font-size:13.5px; }}
.cfrow:last-of-type {{ border-bottom:none; }}
.cfd {{ color:#cdd7e4; font-weight:600; }} .cft {{ color:#ffd54f; font-weight:700; }}
.cfs {{ color:#9fb0c3; }} .cfw {{ color:#7d8794; font-size:12.5px; }}
@media (max-width:640px) {{ .cfrow {{ grid-template-columns:90px 30px 56px 1fr; }} .cfw {{ display:none; }} .cftoggle b {{ min-width:110px; }} }}
</style>
<script>
document.querySelectorAll(".cftoggle").forEach(b => b.onclick = () => {{
  b.classList.toggle("open");
  document.getElementById("cf" + b.dataset.k).classList.toggle("open");
}});
</script>
"""
    return _page("Forecast", "forecast.html", body)


# ---------------------------------------------------------------- build
_DISK_BUDGETS = {          # max bytes per cache dir (age prunes handle the rest)
    # Streamlit disables static serving when static/ passes 1 GB TOTAL, so
    # these must sum well under that (currently ~920 MB worst case)
    "nexrad_sites": 350_000_000,
    "mrms": 150_000_000,
    "goes": 100_000_000,
    "hrrr": 60_000_000,
    "herbie": 40_000_000,    # GRIB download cache - re-downloadable, not served
    "soundings": 60_000_000,
    "aimodels": 60_000_000,
    "model_maps": 60_000_000,
    "nws_radar": 40_000_000,
}


def enforce_disk_budget():
    """Keep rendered-frame caches under size budgets (oldest files first).

    Streamlit DISABLES static file serving when the folder passes 1 GB,
    which silently breaks every app map - so this runs every site cycle.
    Per-directory budgets sum to well under the limit even with slack.
    """
    removed = 0
    # renderer state - deleting these blinds the satellite page even when
    # frames are fresh (the budget once wiped them and zeroed every band)
    protected = {"registry.json", "descriptors.json", "listings.json", "cells.json"}
    for sub, budget in _DISK_BUDGETS.items():
        root = os.path.join("static", sub)
        if not os.path.isdir(root):
            continue
        entries = []
        total = 0
        for dirpath, _, files in os.walk(root):
            for fn in files:
                if fn in protected:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                    entries.append((st.st_mtime, st.st_size, p))
                    total += st.st_size
                except OSError:
                    continue
        if total <= budget:
            continue
        entries.sort()                       # oldest first
        for _, size, p in entries:
            if total <= budget:
                break
            try:
                os.remove(p)
                total -= size
                removed += 1
            except OSError:
                continue
    if removed:
        _log_disk(f"disk budget: removed {removed} old frame files")
    return removed


def _log_disk(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open(os.path.join(".freebuff", "site-updater.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def generate_site():
    """Collect live data and write the whole site. Returns SITE_DIR or None."""
    try:
        enforce_disk_budget()
        d = collect_data()
        d["models"] = _model_manifest()
        d["psu"] = _psu_manifest()
        pages = {
            "index.html": page_index(d),
            "radar.html": page_radar(d),
            "satellite.html": page_satellite(d),
            "models.html": page_models(d),
            "tropical.html": page_tropical(d),
            "severe.html": page_severe(d),
            "meso.html": page_meso(d),
            "obs.html": page_obs(d),
            "charts.html": page_charts(d),
            "national.html": page_national(d),
            "forecast.html": page_forecast(d),
        }
        os.makedirs(SITE_DIR, exist_ok=True)
        tmp = os.path.join(SITE_DIR, "data.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, os.path.join(SITE_DIR, "data.json"))
        for name, content in pages.items():
            ptmp = os.path.join(SITE_DIR, name + ".tmp")
            with open(ptmp, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(ptmp, os.path.join(SITE_DIR, name))
        return SITE_DIR
    except Exception:  # noqa: BLE001 - site must never crash the app
        import traceback
        traceback.print_exc()
        return None


_SEED = [("GFS", 24, "500_vort", "us"), ("GFS", 24, "850_tmp", "etn"),
         ("NAM", 12, "500_vort", "etn"), ("RAP", 6, "sfc_mslp", "etn")]


def _seed_model_maps():
    """Render a few popular products once per process (best-effort)."""
    try:
        from data.model_maps import find_cycle, render_product_map
        for model, fh, prod, region in _SEED:
            try:
                cyc = find_cycle(model)
                if cyc is not None:
                    render_product_map(model, cyc, fh, prod, region=region)
            except Exception:  # noqa: BLE001 - seeding is best-effort
                continue
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    out = generate_site()
    print(f"site written: {out}" if out else "site generation failed")

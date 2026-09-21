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
from data import _tz

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
    from data.nowcast import nowcast_bundle
    from data.severe import spc_outlooks, severe_forecast
    from data.sevmaps import sevmaps_bundle
    from data.winter import winter_bundle
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
        "expires": _tz.iso_z(a.get("expires")),
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
    if not nowcast:
        # RainViewer's free API intermittently serves an EMPTY nowcast list
        # (and its tilecache has outages) - build our own +10/+20/+30 min
        # extrapolation from the MRMS mosaic we already render every 2 min
        try:
            nowcast = nowcast_bundle()
        except Exception:      # noqa: BLE001 - nowcast must never kill the site
            nowcast = []
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

    # SPC mesoscale discussions (structured polygons) + storm reports
    md, reports = [], {}
    try:
        import requests
        from data.mcd import bundle as mcd_bundle
        md = mcd_bundle().get("features") or []
        UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}
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
    # per-city tomorrow-peak WBGT + heat-risk flag (30-min cache) merged
    # into each forecast entry for the 7-day city cards
    try:
        from data.wbgt import city_wbgt_tomorrow
        _cw = city_wbgt_tomorrow()
        for cf in city_fc:
            cf["wbgtTomorrow"] = _cw.get(cf.get("city"))
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
            feats = s.get("features") or []
            polys = [f["geometry"] for f in feats if f.get("geometry", {}).get("type") == "Polygon"]
            lines = [f["geometry"] for f in feats if f.get("geometry", {}).get("type") == "LineString"]
            entry = {k: s.get(k) for k in
                     ("name", "classification", "intensity", "pressure", "lat", "lon")}
            entry["cone"] = polys[0] if polys else None
            entry["track"] = lines[0] if lines else None
            entry["trackFcst"] = lines[1] if len(lines) > 1 else None
            entry["points"] = [f["geometry"] for f in feats
                               if f.get("geometry", {}).get("type") == "Point"][:12]
            entry["movement"] = s.get("movement") or ""
            entry["lastUpdate"] = s.get("lastUpdate") or ""
            entry["advisoryUrl"] = s.get("advisoryUrl") or ""
            # coastal watches/warnings near this storm, matched from NWS alerts
            slat, slon = s.get("lat"), s.get("lon")
            near = set()
            if slat is not None:
                for a in ww:
                    ev = a.get("event") or ""
                    if not any(k in ev for k in ("Hurricane", "Tropical Storm", "Storm Surge")):
                        continue
                    g = a.get("geometry") or {}
                    rings = g.get("coordinates") or []
                    ring = rings[0] if g.get("type") == "Polygon" else (rings[0][0] if rings else [])
                    if ring and abs(ring[0][1] - slat) <= 5 and abs(ring[0][0] - slon) <= 6:
                        near.add(ev)
            entry["watches"] = sorted(near)
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
        from data.model_maps import PRODUCTS_BY_MODEL, PRODUCTS, NBM_LABELS
        for model, prods in PRODUCTS_BY_MODEL.items():
            model_catalog[model] = {
                "label": model,
                "products": [{"key": p, "label": PRODUCTS.get(p, {}).get("label")
                              or NBM_LABELS.get(p, p)} for p in prods],
            }
    except Exception:  # noqa: BLE001
        model_catalog = {}

    # MPAS + FV3 (SHiELD) into the same menu: products served from the
    # pre-rendered official frame loops (mpasShield payload), not REND
    try:
        from data.shield_mpas import MPAS_PRODUCTS, SHIELD_PRODUCTS, mpas_is_archive
        try:
            _mpas_archive = bool(mpas_is_archive())
        except Exception:  # noqa: BLE001
            _mpas_archive = False
        model_catalog["MPAS"] = {
            "label": ("NCAR MPAS (3.75 km global) — 2025 demo archive (new season pending)"
                      if _mpas_archive else "NCAR MPAS (3.75 km global)"),
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
    mpas_archive = False
    try:
        from data.shield_mpas import (MPAS_PRODUCTS, MPAS_DOMAINS, SHIELD_PRODUCTS,
                                      SHIELD_REGIONS, mpas_product, shield_product,
                                      mpas_is_archive)
        try:
            mpas_archive = bool(mpas_is_archive())
        except Exception:  # noqa: BLE001
            mpas_archive = False
        mpas_shield["mpasArchive"] = mpas_archive
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
        _shield_sets = list(SHIELD_PRODUCTS)   # rotate through the FULL catalog

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

    lightning = {"frames": []}
    try:
        from data.lightning import bundle as glm_bundle
        lightning = glm_bundle()
    except Exception:  # noqa: BLE001
        pass
    ltg_history = {"frames": [], "cells": []}
    try:
        from data.lightning import storm_history as _ltg_hist
        ltg_history = _ltg_hist()
    except Exception:  # noqa: BLE001
        pass

    # Never ship references to frames the disk sweeper already removed: a
    # sweep racing the build otherwise publishes dead image links.
    def _live(frames):
        out = []
        for f in frames:
            if not isinstance(f, dict):
                out.append(f)
                continue
            ref = (f.get("pngUrl") or "").replace("\\", "/")
            if not ref or ref.startswith("http"):
                out.append(f)
                continue
            # Frame refs come in three flavors (mirrors github_deploy.staticrel):
            #   /app/static/x/y.png  (renderer modules' app-route form)
            #   ../dir/x.png         (project-root-relative)
            #   dir/x.png            (static/-relative, post-rewrite style)
            if ref.startswith("/app/static/"):
                local = "static/" + ref[len("/app/static/"):]
            elif ref.startswith("../"):
                local = ref[len("../"):]
            elif ref.startswith("static/"):
                local = ref
            else:
                local = "static/" + ref
            if os.path.isfile(local):
                out.append(f)
        return out

    past = _live(past)
    nowcast = _live(nowcast)
    future["frames"] = _live(future["frames"])
    future["ready"] = len(future["frames"])
    mrms["frames"] = _live(mrms["frames"])
    mrms_loops = {pk: _live(mrms_bundle(pk)["frames"])[-8:]
                  for pk in ("cref", "lowref", "l0050", "l0200", "l0400", "l0800",
                             "l1500", "zdr050", "rho050", "rots", "mesh",
                             "azshr", "azshr36", "etop", "vil", "shi",
                             "prate", "qpe1h")}

    return {
        "generated": _tz.full(dt.datetime.now(dt.timezone.utc)),
        "dataEpochMs": int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
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
            "time": _tz.hm(dt.datetime.fromisoformat(cur["timestamp"].replace("Z", "+00:00")))
                    if cur.get("timestamp") else "",
        },
        "days": days,
        "hourly": hourly,
        "alerts": alerts,
        "spc": spc_days,
        "storm": {
            "summary": summarize(cells) if cells else None,
            # storm_tracker emits dbz_max (not "dbz") - the mismatch shipped
            # dbz: null for every cell and the page filtered them all out
            # ("No storm cells detected" under a 21-cell summary, 2026-09-18)
            "cells": [{"lat": c.get("lat"), "lon": c.get("lon"),
                       "dbz": c.get("dbz_max", c.get("dbz"))}
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
        "wbgt": _wbgt_bundle_safe(),
        "wbgtUs": _wbgt_bundle_us_safe(),
        "heatIndex": _heat_index_safe(),
        "afd": _afd_safe(),
        "sun": _sun_safe(),
        "tropModels": _trop_models_safe(),
        "climate": _climate_safe(),
        "mrmsProducts": {k: v.get("label", k) for k, v in MRMS_CATALOG.items()},
        # per-product MRMS loops for the radar page's level picker (disk reads;
        # the shared background renderer fills each over time) - pre-filtered
        # into mrms_loops above so only frames still on disk ship
        "mrmsLoops": mrms_loops,
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
            "ltgHistory": ltg_history,
            "forecast": severe_forecast(),
            "maps": sevmaps_bundle(),
        },
        "winter": winter_bundle(),
        "rivers": _rivers_safe(),
        "dashboard": _dashboard_safe(),
        "space": _space_safe(),
        "wbgt": _wbgt_bundle_safe(),
        "wbgtUs": _wbgt_bundle_us_safe(),
        "heatIndex": _heat_index_safe(),
        "afd": _afd_safe(),
        "sun": _sun_safe(),
        "tropModels": _trop_models_safe(),
        "climate": _climate_safe(),
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
        "lightning": lightning,
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
        frames = sorted((f, fn) for c, f, fn in items if c == newest)[-8:]
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
        # loops: group by ONE init cycle (the newest on disk). Mixing cycles
        # interleaved old-cycle frames into the animation when multi-hour
        # loops arrived (2026-09-15).
        cyc = max(c for _fh, c, _fn in items)
        frames = sorted(x for x in items if x[1] == cyc)
        out.append({
            "model": model,
            "product": prod,
            "region": region,
            "count": len(frames),
            "cycle": cyc,
            "frames": [{"fh": fh, "url": f"../model_maps/{fn}"} for fh, _cyc, fn in frames[-24:]],
        })
    # newest cycles first
    out.sort(key=lambda g: g["cycle"], reverse=True)
    return out


def _wbgt_bundle_us_safe():
    """National WBGT bundle (never breaks the site build; may be cold)."""
    try:
        from data.wbgt import wbgt_bundle_us
        return wbgt_bundle_us()
    except Exception:                                  # noqa: BLE001
        return {"ok": False, "frames": []}


def _heat_index_safe():
    """Heat-Stress Index bundle (never breaks the site build)."""
    try:
        from data.heatidx import heat_index_bundle
        return heat_index_bundle()
    except Exception:                                  # noqa: BLE001
        return {"ok": False}


def _trop_models_safe():
    """ATCF spaghetti guidance + intensity charts (never breaks the build)."""
    try:
        from data.tropical_models import bundle as _tmb
        return {"ok": True, "storms": _tmb()}
    except Exception:                                  # noqa: BLE001
        return {"ok": False, "storms": []}


def _climate_safe():
    """CPC long-range outlooks + ENSO (never breaks the build)."""
    try:
        from data.climate import bundle as _clb
        return _clb()
    except Exception:                                  # noqa: BLE001
        return {"ok": False, "groups": [], "oni": []}


def _wbgt_bundle_safe():
    """WBGT heat-map bundle (never breaks the site build)."""
    try:
        from data.wbgt import wbgt_bundle
        return wbgt_bundle()
    except Exception:                              # noqa: BLE001
        return {"ok": False, "frames": []}


def _afd_safe():
    """NWS Area Forecast Discussion (never breaks the site build)."""
    try:
        from data.discussion import afd_bundle
        return afd_bundle()
    except Exception:                              # noqa: BLE001
        return {"ok": False, "keyMessages": [], "sections": []}


def _sun_safe():
    """Sun/moon card bundle (never breaks the site build)."""
    try:
        from data.sunmoon import sun_bundle
        return sun_bundle()
    except Exception:                              # noqa: BLE001
        return {"ok": False}


def _dashboard_safe():
    """Dashboard bundle - a source failure must never break the build."""
    try:
        from data.dashboard import dashboard_bundle
        return dashboard_bundle()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return {}


def _rivers_safe():
    """River-gauge bundle (never breaks the site build)."""
    try:
        from data.rivers import rivers_bundle
        return rivers_bundle()
    except Exception:                              # noqa: BLE001
        return {"ok": False, "gauges": []}


def _space_safe():
    """Space-weather bundle (never breaks the site build)."""
    try:
        from data.space import space_bundle
        return space_bundle()
    except Exception:                              # noqa: BLE001
        return {"ok": False, "kp": []}


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
  .ltg-row { display:flex; align-items:flex-end; gap:12px; flex-wrap:wrap; margin-top:10px; }
  .wpcfig { flex:1 1 300px; max-width:49%; min-width:260px; margin:0; }
  .wpcfig img { width:100%; height:auto; display:block; border-radius:10px; }
  .wpcfig figcaption { font-size:12px; opacity:.75; margin-top:4px; }
  .ltg-cell { background:#10151f; border:1px solid var(--line); border-radius:10px; padding:8px 10px; min-width:130px; flex:0 0 auto; }
  .ltg-cell b { display:block; font-size:15px; }
  .ltg-cell .h { color:var(--dim); font-size:11px; }
  .ltg-bars { display:flex; align-items:flex-end; gap:2px; height:34px; margin-top:6px; }
  .ltg-bars i { flex:1 1 0; min-width:4px; background:#ffeb3b; border-radius:2px 2px 0 0; opacity:.9; }
  .ltg-badge { font-size:11px; font-weight:800; padding:2px 8px; border-radius:8px; color:#102015; }
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
  /* radar load-state badge (set by the player: ok / loading / empty) */
  #map { position: relative; }
  #map::after {
    content: "";
    position: absolute; top: 8px; left: 8px; z-index: 800;
    background: rgba(10, 14, 20, .75); color: #9fe199; border: 1px solid rgba(255,255,255,.15);
    border-radius: 6px; padding: 2px 9px; font-size: 11.5px; pointer-events: none;
  }
  #map[data-radar-state="loading"]::after { content: "radar loading\u2026"; color: #ffd54f; }
  #map[data-radar-state="empty"]::after { content: "radar unavailable - retrying"; color: #ff8a80; }
  #map[data-radar-state="ok"]::after { content: "radar live"; color: #9fe199; }
  /* hide Leaflet's default home marker (the player draws its own) */
  .leaflet-marker-icon.home-marker { display: none; }
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
  if (typeof drawHomeMarker === "function") drawHomeMarker(map, home);
}
function drawHomeMarker(map, home) {
  if (map._tnwxHome) return;
  map._tnwxHome = L.circleMarker(home, { radius: 7, color: "#fff", weight: 2,
    fillColor: "#ff5252", fillOpacity: 1 }).addTo(map).bindTooltip((typeof DATA_PLACE !== "undefined" && DATA_PLACE) || "Home");
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
  window.DATA_LAT = DATA.lat; window.DATA_LON = DATA.lon; window.DATA_PLACE = DATA.place;
  /* zoomAnimation: false - the frame loop + soft refresh rebuild overlays
     continuously; a CSS zoom animation colliding with a rebuild gets
     cancelled and snaps back to the old zoom ("map won't zoom", 2026-09-18).
     Instant zoom has nothing to cancel and always sticks. */
  map = L.map("map", {{ zoomSnap: 0.5, zoomAnimation: false, maxZoom: 21 }}).setView([DATA.lat, DATA.lon], 6);
  addMapControls(map, [DATA.lat, DATA.lon], 6);
  L.circleMarker([DATA.lat, DATA.lon], {{ radius: 7, color: "#fff", weight: 2, fillColor: "#ff5252", fillOpacity: 1 }})
    .addTo(map).bindTooltip(DATA.place);
}}
function fmt(ts) {{ return new Date(ts * 1000).toLocaleTimeString([], {{ hour: "2-digit", minute: "2-digit", hour12: false }}); }}
function clear() {{ for (const l of curLayers) {{ try {{ map.removeLayer(l); }} catch (_e) {{}} }} curLayers = []; }}
let glmLayer = null;
/* RainViewer is THE real-time radar layer (visitor preference, 2026-09-10):
   no auto-switching to other layers. During a RainViewer outage the page
   shows its "radar unavailable - retrying" badge and keeps retrying. */
/* Tile layers are CACHED PER FRAME and re-attached, never recreated: the old
   code built a fresh tileLayer every 700 ms animation tick, re-requesting
   every tile each loop (hundreds of req/min) and tripping RainViewer's
   per-IP rate limit (429). With reuse, each frame's tiles fetch ONCE. */
const _tileCache = new Map();
let _builtKind = null;
let _curTileUrl = null;
/* Real-time layer = RainViewer when it works; while their tilecache is down
   (500/429 outages), the SAME layer draws our own NOAA NEXRAD composite PNGs
   (rendered every 2 min, no rate limit) with a transparent note, and flips
   back to RainViewer automatically once a probe sees their tiles recover. */
let rvState = "ok";      /* "ok" | "down" */
let rvDowns = 0;
let rvProbed = false;
const RV_NOTE = "RainViewer is down right now - showing the official NOAA NEXRAD composite. Back to RainViewer automatically the moment it recovers.";
const RV_RETRY = "RainViewer is having trouble serving tiles right now - retrying automatically. If it keeps failing, the official NOAA NEXRAD mosaic takes over within a couple of minutes.";
/* The page may have been served with a STALE frame list (CDN lag): RainViewer
   rotates old frames out of their tilecache within hours, so old paths 500
   forever. This live refresh pulls their public CORS-open frame list and
   updates DATA.radar.past/nowcast in place - stale pages heal themselves. */
let _rvRefT = 0;
async function rvRefresh() {{
  const now = Date.now();
  if (now - _rvRefT < 120000) return;   /* at most every 2 min */
  _rvRefT = now;
  try {{
    const r = await fetch("https://api.rainviewer.com/public/weather-maps.json", {{cache: "no-store"}});
    if (!r.ok) return;
    const j = await r.json();
    const rad = j.radar || {{}};
    const conv = a => (a || []).map(f => ({{time: f.time, path: f.path, label: fmt(f.time), kind: (rad.nowcast || []).includes(f) ? "nowcast" : "past"}}));
    const past = conv(rad.past);
    const now2 = conv(rad.nowcast);
    if (DATA.radar && past.length) {{
      const changed = !DATA.radar.past.length || DATA.radar.past[DATA.radar.past.length - 1].time !== past[past.length - 1].time;
      DATA.radar.past = past;
      if ((DATA.radar.nowcast || []).length !== now2.length) DATA.radar.nowcast = now2;
      _tileCache.forEach(l => {{ try {{ map.removeLayer(l); }} catch (_e) {{}} }});
      _tileCache.clear();
      if (kind === "past" && changed && rvState === "ok") {{ frames = framesFor("past"); show(frames.length - 1); }}
      if (kind === "nowcast" && DATA.radar.nowcast.length) {{ frames = framesFor("nowcast"); show(frames.length - 1); }}
    }}
  }} catch (_e) {{}}
}}
setInterval(rvRefresh, 120000);
/* Status-aware probe: fetch() can read the HTTP code, Image() cannot.
   429 = rate limit (our own fault) -> back off, do NOT count as outage.
   5xx / network error = their outage -> 2 strikes (~2 min) flips to NEXRAD.
   Any success recovers instantly. */
async function rvProbe() {{
  const fr = (DATA.radar && DATA.radar.past) || [];
  const f = fr.length && fr[fr.length - 1];
  if (!f || !f.path) return;
  const url = "https://tilecache.rainviewer.com" + f.path + "/256/6/16/25/2/1_1.png";
  try {{
    const r = await fetch(url, {{ cache: "no-store" }});
    if (r.ok) {{
      rvDowns = 0;
      if (rvState === "down") {{ rvState = "ok"; if (kind === "past") build(); }}
      return;
    }}
    if (r.status === 429) return;   /* backed off, not down */
    rvDowns++;
  }} catch (_e) {{ rvDowns++; }}
  if (rvState === "ok" && rvDowns >= 2) {{ rvState = "down"; if (kind === "past") build(); }}
}}
function drawGlm(ts) {{
  const box = document.getElementById("ly_glm");
  if (!box) return;   /* lightning overlay is radar-page only */
  if (glmLayer) {{ map.removeLayer(glmLayer); glmLayer = null; }}
  if (!box.checked) return;
  const frames = (DATA.lightning && DATA.lightning.frames) || [];
  if (!frames.length) return;
  let f = frames[0];
  for (const fr of frames) {{
    const t = Date.parse((fr.time || "").replace("Z", "+00:00"));
    if (!isNaN(t) && Math.abs(t - ts) < Math.abs(Date.parse((f.time || "").replace("Z", "+00:00")) - ts)) f = fr;
  }}
  const b = f.bounds;
  const lb = (Array.isArray(b) && !Array.isArray(b[0])) ? L.latLngBounds([[b[0], b[1]], [b[2], b[3]]]) : b;
  glmLayer = L.imageOverlay(f.pngUrl, lb, {{ opacity: Math.min(1, OPACITY() + 0.15), interactive: false }}).addTo(map);
}}

function show(i) {{
  idx = i; clear();
  const f = frames[i]; if (!f) return;
  let frameBad = 0;   /* failed tiles this frame (read by the note below) */
  const spec = LAYERS[kind] || {{}};
  if (spec.mode === "tiles" && f.path) {{
    /* modern RainViewer URL: no legacy _{time} suffix (their tilecache
       500s that form at high zooms - 2026-09-11 outage) */
    const url = "https://tilecache.rainviewer.com" + f.path + spec.path + ".png";
    _curTileUrl = url;
    let tl = _tileCache.get(url);
    if (!tl) {{
      tl = L.tileLayer(url, {{ opacity: OPACITY(), maxNativeZoom: 10, maxZoom: 21 }});
      let okT = 0;
      frameBad = 0;
      tl.on("tileload", () => {{
        okT++;
        if (okT === 1) {{ setRadarState("ok"); tl._loadedOnce = true; }}
      }});
      tl.on("tileerror", () => {{
        frameBad++;
        /* 429 blips trip this fast otherwise - require most of the frame's
           tiles to fail before showing "empty", and only then probe. */
        if (!okT && frameBad >= 8) {{ setRadarState("empty"); if (!rvProbed) rvProbe(); }}
      }});
      _tileCache.set(url, tl);
    }}
    tl.setOpacity(OPACITY());
    tl.addTo(map);
    curLayers.push(tl);
    frameEl.textContent = fmt(f.time);
    drawGlm(f.time * 1000);
    setRadarState(tl._loadedOnce ? "ok" : "loading");
  }} else if (f.pngUrl && f.bounds) {{
    const b = f.bounds;
    const lb = (Array.isArray(b) && !Array.isArray(b[0])) ? L.latLngBounds([[b[0], b[1]], [b[2], b[3]]]) : b;
    setRadarState("loading");
    const ly = L.imageOverlay(f.pngUrl, lb, {{ opacity: OPACITY(), maxZoom: 21 }}).addTo(map);
    const el = ly.getElement();
    if (el) {{
      el.onload = () => setRadarState("ok");
      el.onerror = () => setRadarState("empty");
    }}
    curLayers.push(ly);
    frameEl.textContent = f.label || "";
    wbMarkers(f);
    /* NWS-fallback frames carry an ISO string 'time', RV frames an epoch int:
       coerce both to ms so the GLM overlay match cannot throw (a throw here
       killed show() mid-frame and left the map stuck "retrying"). */
    const tMs = (typeof f.time === "number") ? f.time * 1000 : Date.parse((f.time || "").replace("Z", "+00:00"));
    if (!isNaN(tMs)) drawGlm(tMs);
  }} else {{ frameEl.textContent = "rendering\\u2026"; setRadarState("empty"); }}
  preload(idx);
  if (document.getElementById("pend")) {{
    const r = DATA.radar || {{}};
    const tileTrouble = (kind === "past" && rvState === "ok" && frameBad >= 8);
    document.getElementById("pend").textContent =
      (kind === "past" && rvState === "down") ? RV_NOTE :
      (tileTrouble ? RV_RETRY :
      ((typeof queuedNote !== "undefined" && queuedNote) ||
      ((kind === "future" && r.futureReady < r.futureTotal)
        ? "Rendering future radar: " + r.futureReady + "/" + r.futureTotal + " hours ready - new hours appear automatically." : "")));
  }}
}}
function play() {{ playing = true; document.getElementById("play").textContent = "\\u23f8"; timer = setInterval(() => show((idx + 1) % frames.length), 1500); }}
/* preload: warm the NEXT frame so stepping never stalls on a cold fetch;
   kept to 1 frame - bulk prewarming trips RainViewer's per-IP rate limit */
const _preloaded = new Set();
function preload(i) {{
  if (!frames.length) return;
  for (let k = 1; k <= 1; k++) {{
    const f = frames[(i + k) % frames.length];
    if (!f || !f.pngUrl) continue;
    const url = f.pngUrl;
    if (_preloaded.has(url)) continue;
    _preloaded.add(url);
    const im = new Image();
    im.onload = () => {{}};
    im.onerror = () => _preloaded.delete(url);
    im.src = url;
  }}
}}
function setRadarState(state) {{
  const mapEl = document.getElementById("map");
  if (!mapEl) return;
  mapEl.dataset.radarState = state;   // ok | loading | empty (CSS badge)
}}
function pause() {{ playing = false; document.getElementById("play").textContent = "\\u25b6"; clearInterval(timer); }}
function framesFor(k) {{
  if (k === "past")   /* real-time layer: RainViewer frames, NEXRAD while down */
    return rvState === "down" ? ((DATA.radar && DATA.radar.nws) || [])
                              : ((DATA.radar && DATA.radar.past) || []);
  if (k === "wbgt") return wbFrames();   /* scope-aware (East TN / US) */
  const spec = LAYERS[k] || {{}};
  if (spec.framesKey) return (DATA.radar && DATA.radar[spec.framesKey]) || [];
  if (spec.satKey) return (DATA.satBands[spec.satKey] || {{}}).frames || [];
  return [];
}}
/* Leaflet never refetches failed tiles: while the radar shows "empty"
   (RainViewer 429/outage), drop the current frame's cached layer every 60 s
   so the next show() recreates it with FRESH requests - recovery is automatic. */
setInterval(() => {{
  const m = document.getElementById("map");
  if (!m || m.dataset.radarState !== "empty" || !_curTileUrl || rvState === "down") return;
  const l = _tileCache.get(_curTileUrl);
  if (l) {{ try {{ map.removeLayer(l); }} catch (_e) {{}} _tileCache.delete(_curTileUrl); }}
}}, 60000);
setInterval(rvProbe, 60000);   /* recovery check while RainViewer is down */
function build() {{
  if (!DATA) return;   /* data.json fetch failed/truncated - retry lands via refresh */
  if (kind === "past" && !rvProbed) {{ rvProbed = true; rvProbe(); }}
  if (_builtKind !== kind) {{   /* layer switched: drop cached tile layers */
    _tileCache.forEach(l => {{ try {{ map && map.removeLayer(l); }} catch (_e) {{}} }});
    _tileCache.clear();
    _builtKind = kind;
  }}
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
  /* these pickers exist on the radar page; the satellite page reuses this
     build() but has none of them - guard so a missing element can't crash
     the whole map (satellite was blank since the WBGT scope-picker addition) */
  const mp = document.getElementById("mrmsProd"); if (mp) mp.style.display = kind === "mrms" ? "" : "none";
  const wbh = document.getElementById("wbHour"); if (wbh) wbh.style.display = kind === "wbgt" ? "" : "none";
  const wbs = document.getElementById("wbScope"); if (wbs) wbs.style.display = kind === "wbgt" ? "" : "none";
  wbMarkers();
  if (kind === "wbgt") wbFill();
  const sel = document.getElementById("layer"); if (sel && sel.value !== kind) sel.value = kind;
  if (!frames.length) {{ frameEl.textContent = "no frames yet"; setRadarState("empty"); return; }}
  setRadarState("loading");
  if (!timer) {{ show(frames.length - 1); if (playing) play(); }} else show(frames.length - 1);
}}
/* ---- WBGT heat-stress layer: region + hour pickers + per-point markers ---- */
function wbFrames() {{
  const scope = (document.getElementById("wbScope") || {{}}).value || "etn";
  const src = scope === "us" ? DATA.wbgtUs : DATA.wbgt;
  return (src && src.frames) || [];
}}
let wbHourSel = null, wbMarkerLayer = null;
function wbCatColor(v) {{
  return v >= 93 ? "#c828c8" : v >= 90 ? "#eb3c3c" : v >= 88 ? "#ff783c"
       : v >= 85 ? "#ffb242" : v >= 82 ? "#ffe066" : "#81c784";
}}
function wbFill() {{
  wbHourSel = wbHourSel || document.getElementById("wbHour");
  const prev = wbHourSel.value;
  const fr = wbFrames();
  wbHourSel.innerHTML = fr.map(f => `<option value="${{f.id}}">${{f.label}}</option>`).join("");
  if (prev && fr.some(x => x.id === prev)) wbHourSel.value = prev;
}}
function wbActive() {{
  if (kind !== "wbgt") return null;
  wbHourSel = wbHourSel || document.getElementById("wbHour");
  const fr = wbFrames();
  return fr.find(x => x.id === wbHourSel.value) || fr[fr.length - 1] || null;
}}
function wbMarkers(f) {{
  f = f || wbActive();
  if (wbMarkerLayer) {{ try {{ map.removeLayer(wbMarkerLayer); }} catch (_e) {{}} wbMarkerLayer = null; }}
  const box = document.getElementById("ly_wb");
  if (!f || !f.vals || !f.vals.length || (box && !box.checked)) return;
  wbMarkerLayer = L.layerGroup();
  for (const p of f.vals) {{
    const c = wbCatColor(p[2]);
    wbMarkerLayer.addLayer(L.circleMarker([p[0], p[1]], {{
      radius: 5, color: "#1b2027", weight: 1.5, fillColor: c, fillOpacity: .95,
    }}).bindTooltip(`WBGT ${{Math.round(p[2])}}\u00b0F`));
  }}
  wbMarkerLayer.addTo(map);
}}
document.addEventListener("change", e => {{
  if (e.target && e.target.id === "wbHour") {{
    /* explicit hour choice: stop the loop so the selection sticks */
    if (playing) pause();
    const i = frames.findIndex(x => x.id === e.target.value);
    show(i >= 0 ? i : frames.length - 1);
  }}
  if (e.target && e.target.id === "wbScope") {{
    wbFill(); frames = framesFor("wbgt");
    if (playing) pause();
    idx = frames.length - 1; show(idx);
  }}
  if (e.target && e.target.id === "ly_wb") {{
    if (e.target.checked) wbMarkers(); else wbMarkers({{vals: []}});
  }}
}});
function refresh(d) {{ if (!d) return; DATA = d; if (map) build(); }}
window.onDataRefresh = function (d) {{ refresh(d); }};   /* soft auto-refresh: the 90 s cycle updates frames in place - a hard reload here kills zoom/pinch gestures mid-move ("map won't zoom", 2026-09-18) */
"""


def _page(title, active, body, extra_head=""):
    pages = [("index.html", "Home"), ("radar.html", "Radar"), ("satellite.html", "Satellite"),
             ("models.html", "Models"), ("tropical.html", "NHC"),
             ("tropmodels.html", "Trop Models"), ("climate.html", "Climate"),
             ("severe.html", "Severe"),             ("winter.html", "Winter Forecast"),
             ("rivers.html", "Rivers"), ("fire.html", "Fire"), ("dashboard.html", "Dashboard"),
             ("meso.html", "Mesoanalysis"),
             ("obs.html", "Obs & Skew-T"), ("charts.html", "Charts & MOS"), ("national.html", "National"),
             ("forecast.html", "Forecast"), ("education.html", "Education")]
    nav = "".join(
        f'<a class="pg{" on" if p == active else ""}" href="{p}">{label}</a>'
        for p, label in pages
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)} - {config.PAGE_NAME}</title>
<meta property="og:title" content="{html.escape(title)} - {config.PAGE_NAME}"/>
<meta property="og:type" content="website"/>
<meta property="og:description" content="Live radar, future cast, satellite, forecast models, severe weather and East Tennessee forecasts - free, no keys, always updating."/>
<meta property="og:url" content="{getattr(config, 'PUBLIC_SITE_URL', '')}"/>
<meta property="og:image" content="{getattr(config, 'PUBLIC_SITE_URL', '').rstrip('/')}/og.png"/>
<meta property="og:site_name" content="{config.PAGE_NAME}"/>
<meta name="twitter:card" content="summary_large_image"/>
<meta name="twitter:image" content="{getattr(config, 'PUBLIC_SITE_URL', '').rstrip('/')}/og.png"/>
<meta name="description" content="Live East Tennessee weather: radar, future cast, satellite, all forecast models, severe weather, and city forecasts. Updated continuously."/>
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
/* GitHub Pages caches data.json up to 10 min (max-age=600); the timestamp
   query makes the browser fetch a fresh copy from the Pages CDN instead of
   reusing a stale cached one. Public data publishes ~every 10 min, so the
   footer age normally reads 0-10 min - that is the honest cadence. */
function dataUrl() {{
  try {{ return SITE_DATA_URL + "?t=" + Date.now(); }} catch (_e) {{ return SITE_DATA_URL; }}
}}
/* live-update watchdog: re-fetch data.json every 3 min and show its age.
   The footer age is computed from dataEpochMs (server epoch), NOT from the
   human-readable 'generated' string (now "... 12:17 AM ET" - unparseable). */
let SITE_DATA = null;
function fmtAge(mins) {{
  if (mins < 1) return "just now";
  if (mins < 60) return mins + " min ago";
  const h = Math.floor(mins / 60), m = mins % 60;
  return h + " h " + (m ? m + " min " : "") + "ago";
}}
function updTick() {{
  if (!SITE_DATA) return;
  const el = document.getElementById("upd");
  if (!el) return;
  const ms = Date.now() - (SITE_DATA.dataEpochMs || 0);
  if (!SITE_DATA.dataEpochMs || ms < 0) {{ el.textContent = "\\u2705 Live data"; return; }}
  const mins = Math.floor(ms / 60000);
  /* Public site: GitHub publishes ~every 10 min, so age up to ~12 min is the
     normal cadence - warn only past that. Pages' own max-age=600 means the
     fetch itself can lag a couple minutes behind the publish. */
  if (mins <= 12) el.textContent = "\\u2705 Live data \\u00b7 refreshed " + fmtAge(mins);
  else if (mins <= 30) el.textContent = "\\u23f3 Data " + fmtAge(mins) + " old \\u00b7 next publish soon";
  else el.textContent = "\\u26a0\\ufe0f Data " + fmtAge(mins) + " old \\u00b7 checking for updates";
}}
async function siteRefresh() {{
  try {{
    SITE_DATA = await (await fetch(dataUrl(), {{cache: "no-store"}})).json();
    if (typeof onDataRefresh === "function") onDataRefresh(SITE_DATA);
    updTick();
    /* auto-heal: if the served copy is still 30+ min old on two consecutive
       polls (6 min apart), force a cache-busted reload once - recovers from
       a stuck CDN copy or a missed publish without looping. */
    if (SITE_DATA.dataEpochMs && Date.now() - SITE_DATA.dataEpochMs > 30 * 60000) {{
      siteRefresh._stale = (siteRefresh._stale || 0) + 1;
      if (siteRefresh._stale >= 2 && !sessionStorage.getItem("tnwxHealed")) {{
        sessionStorage.setItem("tnwxHealed", "1");
        const u = new URL(location.href); u.searchParams.set("t", Date.now());
        location.replace(u); return;
      }}
    }} else siteRefresh._stale = 0;
  }} catch (e) {{ /* offline: keep last data */ }}
}}
siteRefresh();
setInterval(updTick, 30000);
setInterval(siteRefresh, 180000);
/* page auto-refresh: soft (map pages define onDataRefresh) or hard reload.
   Hard reloads MUST cache-bust: GitHub Pages sends max-age=600, so a plain
   location.reload() can serve the same stale HTML for up to 10 minutes. */
function hardReload() {{
  try {{
    const u = new URL(location.href);
    u.searchParams.set("t", Date.now());
    location.replace(u);
  }} catch (_e) {{ location.reload(); }}
}}
let AUTO_LEFT = 90;
const autoCnt = document.getElementById("autoCnt"), autoChk = document.getElementById("autoChk");
document.getElementById("refreshBtn").onclick = () => {{
  const b = document.getElementById("refreshBtn");
  if (b) {{ b.textContent = "\u2026"; }}
  hardReload();
}};
try {{ autoChk.checked = localStorage.getItem("tnwxAuto") !== "off"; }} catch (_e) {{}}
autoChk.onchange = () => {{ AUTO_LEFT = 90; try {{ localStorage.setItem("tnwxAuto", autoChk.checked ? "on" : "off"); }} catch (_e) {{}} }};
/* interacting with the page postpones the auto cycle: a hard reload in the
   middle of a zoom/pinch/scrub reads as "the map won't zoom" (2026-09-18) */
["pointerdown", "wheel", "touchstart"].forEach(function (ev) {{
  document.addEventListener(ev, function () {{ AUTO_LEFT = 90; }}, {{ passive: true, capture: true }});
}});
setInterval(() => {{
  if (!autoChk.checked) {{ if (autoCnt) autoCnt.textContent = "\\u221e"; return; }}
  AUTO_LEFT -= 1;
  if (autoCnt) autoCnt.textContent = AUTO_LEFT;
  if (AUTO_LEFT <= 0) {{
    AUTO_LEFT = 90;
    if (typeof onDataRefresh === "function") siteRefresh(); else hardReload();
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
def _trop_card_js():
    """Home-page tropical mini-map: active NHC storms + detail popups.

    Same cone/track/wind-radii drawing and popup fields as the NHC page, in a
    small non-interactive-basemap card linking to tropical.html. Renders from
    data.json (tropical.storms / tropical.windRadii) and re-renders on every
    live data refresh; shows a quiet-tropics note when no storms are active.
    Dark base is the site's .map-dark CSS invert - deliberate: setBase() keeps
    module-level tile state that would collide across map rebuilds here.
    """
    return r"""<script>
/* Tropical outlook mini-map: active NHC storms with cone/track, wind radii
   and full detail popups. Re-renders from data.json on every live refresh. */
let tmap = null, tLayer = null;
function tcls(c) { return ({ "HU": "Hurricane", "MH": "Major Hurricane", "TS": "Tropical Storm",
  "TD": "Tropical Depression", "SD": "Subtropical Depression", "SS": "Subtropical Storm",
  "PTC": "Post-tropical Cyclone" })[c] || c || "Storm"; }
function tkt(v) { return Math.round((parseFloat(v) || 0) * 1.15078); }
function tdeg(dg) {
  const dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
  const n = parseFloat(dg);
  return isNaN(n) ? "" : dirs[Math.round(n / 22.5) % 16];
}
function tpopup(s) {
  const mv = (s.movement || "").trim();
  const m = mv.match(/^([\d.]+)\s*kt\s*@?\s*([\d.]+)\s*deg$/);
  const moveTxt = m ? m[1] + " kt (" + tkt(m[1]) + " mph) toward the " + tdeg(m[2]) : (mv || "movement n/a");
  const w = s.watches || [];
  return "<b>\ud83c\udf00 " + (s.name || "Storm") + " (" + tcls(s.classification) + ")</b>"
    + "<br/>Winds: <b>" + (s.intensity || "?") + " kt</b> (" + tkt(s.intensity) + " mph)"
    + "<br/>Pressure: <b>" + (s.pressure || "?") + " mb</b>"
    + "<br/>Movement: <b>" + moveTxt + "</b>"
    + "<br/>Position: " + (s.lat != null ? Math.abs(s.lat) + (s.lat >= 0 ? "\u00b0N" : "\u00b0S") : "?")
    + ", " + (s.lon != null ? Math.abs(s.lon) + (s.lon >= 0 ? "\u00b0W" : "\u00b0E") : "?")
    + (w.length ? "<br/><b>\u26a0\ufe0f " + w.join("</b><br/><b>\u26a0\ufe0f ") + "</b>"
                : "<br/><span class=src>No coastal watches/warnings in effect</span>")
    + (s.lastUpdate ? "<br/><span class=src>Advisory " + s.lastUpdate + "</span>" : "");
}
function buildTropMap() {
  if (tmap) { tmap.remove(); tmap = null; }
  tmap = L.map("tropMap", { zoomSnap: 0.5, maxZoom: 21, attributionControl: false });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxNativeZoom: 19, maxZoom: 21 }).addTo(tmap);
  document.getElementById("tropMap").classList.add("map-dark");
}
function tropRender(d2) {
  const box = document.getElementById("tropBody");
  if (!box) return;
  const T = (d2 && d2.tropical) || (typeof SITE_DATA !== "undefined" && SITE_DATA ? SITE_DATA.tropical : null) || {};
  const list = (T.storms || []).filter(s => s.lat != null && s.lon != null);
  const count = document.getElementById("tropCount");
  if (!list.length) {
    box.innerHTML = '<span class="src">\ud83c\udf00 Tropics are quiet right now - no active NHC cyclones.</span>';
    if (count) count.textContent = "Atlantic + East Pacific";
    if (tmap) { tmap.remove(); tmap = null; }
    return;
  }
  if (count) count.textContent = list.length + " active storm" + (list.length > 1 ? "s" : "") + " \u00b7 click one for details";
  box.innerHTML = '<div id="tropMap" style="height:260px;border-radius:10px"></div>'
    + '<div class="legend"><span><i style="background:#e1bee7"></i>Cone + track</span>'
    + '<span><i style="background:#ff8a80"></i>34-kt wind radii</span>'
    + '<span style="margin-left:auto"><a href="tropical.html">Full NHC map \u2197</a></span></div>';
  if (!tmap || !document.getElementById("tropMap")._leaflet_id) buildTropMap();
  if (tLayer) tLayer.remove();
  tLayer = L.layerGroup().addTo(tmap);
  list.forEach(s => {
    const g = (geo, style) => { if (geo) L.geoJSON({ type: "Feature", properties: {}, geometry: geo }, { style }).addTo(tLayer); };
    g(s.cone, { color: "#e1bee7", weight: 1.5, fillOpacity: 0.08 });
    g(s.track, { color: "#e1bee7", weight: 2.5, dashArray: "6 6" });
    g(s.trackFcst, { color: "#ff5252", weight: 3 });
    (s.points || []).forEach(p => { if (p) L.geoJSON({ type: "Feature", properties: {}, geometry: p },
      { color: "#fff", fillColor: "#e1bee7", weight: 1, fillOpacity: .9 }).addTo(tLayer); });
    L.circleMarker([s.lat, s.lon], { radius: 8, color: "#fff", weight: 2, fillColor: "#e1bee7", fillOpacity: .95 })
      .bindPopup(tpopup(s), { maxWidth: 300 }).addTo(tLayer);
  });
  (T.windRadii || []).slice(0, 20).forEach(w => {
    if (w.geometry) L.geoJSON({ type: "Feature", properties: w, geometry: w.geometry },
      { style: { color: "#ff8a80", weight: 1.5, fillOpacity: 0.1 } }).addTo(tLayer);
  });
  const b = L.latLngBounds(list.map(s => [s.lat, s.lon]));
  setTimeout(() => { if (tmap) { tmap.invalidateSize(); tmap.fitBounds(b.pad(0.65)); } }, 80);
}
/* wrap (not replace) the city card's refresh hook so both cards live-update */
(function () {
  const prev = window.onDataRefresh;
  window.onDataRefresh = function (d2) { if (typeof prev === "function") prev(d2); tropRender(d2); };
})();
if (typeof SITE_DATA !== "undefined" && SITE_DATA) tropRender(SITE_DATA);
</script>"""


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

    # Active tropical cyclones -> mini-map card linking to the NHC page
    trop_storms = [s for s in ((d.get("tropical") or {}).get("storms") or [])
                   if s.get("lat") is not None and s.get("lon") is not None]
    trop_js = _trop_card_js()

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

    CITY_JS = r"""<script>
/* City weather selector: current + 7-day for any East TN city, from data.json
   (obs.cities + cityForecasts) - re-renders on every live data refresh. */
function _esc(s) { return (s == null ? "" : String(s)).replace(/[&<>\"]|\//g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","/":"\/"}[c])); }
function _wicon(t) {
  t = (t || "").toLowerCase();
  if (/tornado|hurricane/.test(t)) return "\ud83c\udd2b";
  if (/thunder|storm|tstorm/.test(t)) return "\u26a1";
  if (/snow|flurr|sleet|freez|wintry/.test(t)) return "\u2744\ufe0f";
  if (/fog|haze|smoke/.test(t)) return "\ud83c\udf2b";
  if (/drizzl|rain|shower/.test(t)) return "\ud83c\udf27";
  if (/cloud|overcast/.test(t)) return "\u2601\ufe0f";
  if (/sunny|clear/.test(t)) return "\u2600\ufe0f";
  return "\ud83c\udf24";
}
function cityRender() {
  if (typeof SITE_DATA === "undefined" || !SITE_DATA) return;
  const obs = (SITE_DATA.obs && SITE_DATA.obs.cities) || [];
  const fcs = SITE_DATA.cityForecasts || [];
  const sel = document.getElementById("citySel");
  if (!sel || !obs.length) return;
  if (!sel.options.length) {
    obs.forEach(o => { const op = document.createElement("option"); op.value = o.city; op.textContent = o.city; sel.appendChild(op); });
    let saved = null; try { saved = localStorage.getItem("tnwxCity"); } catch (_e) {}
    if (saved && obs.some(o => o.city === saved)) sel.value = saved;
    else { const g = obs.find(o => /Greeneville/i.test(o.city)); if (g) sel.value = g.city; }
    sel.onchange = () => { try { localStorage.setItem("tnwxCity", sel.value); } catch (_e) {} cityRender(); };
  }
  const o = obs.find(x => x.city === sel.value) || obs[0];
  const fc = fcs.find(x => x.city === o.city) || { periods: [] };
  const t = o.tempF == null ? "--" : Math.round(o.tempF);
  document.getElementById("cityNow").innerHTML =
    '<div class="big">' + t + '&deg;F</div><div>' +
    '<div style="font-size:18px;color:#cdd7e4">' + _wicon(o.desc) + " " + _esc(o.desc || "no observation") + "</div>" +
    '<div class="meta">\ud83d\udca7 Dew point ' + (o.dewF == null ? "n/a" : Math.round(o.dewF)) + "&deg;F \u00b7 \ud83d\udca8 " +
    _esc((o.windDir || "") + (o.windMph == null ? "" : " " + Math.round(o.windMph) + " mph")) +
    " \u00b7 Humidity " + (o.rh == null ? "n/a" : Math.round(o.rh)) + "%" +
    "<br/>Obs " + _esc(o.stationName || "-") + (o.miles != null ? " \u00b7 " + o.miles + " mi away" : "") +
    (o.time ? " \u00b7 " + _esc(o.time) : "") + "</div></div>";
  const days = []; const seen = new Set();
  for (const p of fc.periods) {
    if (!p || !p.name || seen.has(p.name) || /Night/i.test(p.name)) continue;
    seen.add(p.name); days.push(p); if (days.length === 7) break;
  }
  document.getElementById("cityDays").innerHTML = days.map(p =>
    '<div class="day"><div class="tx" style="font-weight:700;color:#cdd7e4">' + _esc(p.name) + "</div>" +
    '<div style="font-size:30px;margin:4px 0">' + _wicon(p.short) + "</div>" +
    '<div class="hi">' + (p.tempF == null ? "-" : Math.round(p.tempF)) + "&deg;F</div>" +
    '<div class="tx">' + _esc(p.short || "") + "</div>" +
    '<div style="color:#7d8794;font-size:12px">\ud83d\udca7 ' + (p.pop || 0) + "% \u00b7 \ud83d\udca8 " + _esc(p.wind || "") + "</div></div>"
  ).join("") || '<span class="src">City forecast unavailable right now.</span>';
}
window.onDataRefresh = function () { cityRender(); };
cityRender();
</script>"""

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

<div class="card"><h2>🌀 Tropical outlook</h2>
  <p class="src" id="tropCount">{len(trop_storms)} active tropical cyclone(s) · live from NHC</p>
  <div id="tropBody"></div>
</div>

<div class="card">{cells_html or '<h2>🧠 AI storm tracker</h2><span class="src">No storm cells detected in the current HRRR forecast window.</span>'}</div>

<div class="card"><h2>📅 7-day forecast</h2><div class="grid cards7">{days_html}</div></div>

<div class="card"><h2>🏙️ City weather</h2>
  <p class="src">Pick any East Tennessee city for its live observation and full 7-day forecast.</p>
  <div class="ctl" style="margin-bottom:8px"><label style="font-weight:600;color:#cdd7e4">City:</label>
    <select id="citySel" style="min-width:220px;background:#1b2027;color:#e8eef5;border:1px solid #333c46;border-radius:8px;padding:8px 10px"></select>
  </div>
  <div class="now" id="cityNow"><span class="src">Loading city weather…</span></div>
  <div class="grid cards7" id="cityDays" style="margin-top:10px"></div>
</div>

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
    return _page("Live", "index.html", body, extra_head=CITY_JS + trop_js)


def page_radar(d):
    layers = {
        "past": {"label": "Real-time (RainViewer)", "mode": "tiles", "framesKey": "past",
                 "path": "/256/{z}/{x}/{y}/2/1_1", "fallbacks": ["nws"]},
        "nowcast": {"label": "Nowcast (+10-30 min)", "mode": "tiles", "framesKey": "nowcast",
                    "path": "/256/{z}/{x}/{y}/2/1_1", "fallbacks": ["past", "nws"]},
        "future": {"label": "Future radar (HRRR + NAM, 48 h)", "mode": "png", "framesKey": "future"},
        "mrms": {"label": "MRMS mosaic (official)", "mode": "png", "framesKey": "mrms"},
        "nws": {"label": "NWS mosaic (official)", "mode": "png", "framesKey": "nws"},
        "wbgt": {"label": "Heat stress - WBGT (next 24 h)", "mode": "png", "framesKey": None},
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
      <option value="wbgt">🌡️ Heat stress - WBGT (next 24 h)</option>
      <optgroup label="Individual radar sites" id="siteGroup">{site_opts}</optgroup>
    </select>
    <select id="sitePick"></select>
    <select id="siteMode" style="display:none" title="Radar mode"></select>
    <select id="mrmsProd" style="display:none" title="MRMS product / level">{mrms_opts}</select>
    <select id="wbScope" style="display:none" title="WBGT region">
      <option value="etn">East Tennessee</option>
      <option value="us">United States</option>
    </select>
    <select id="wbHour" style="display:none" title="WBGT forecast hour"></select>
    <input type="range" id="opacity" min="20" max="100" value="80"/>
  </div>
  <div class="ctl" style="margin-top:12px">
    <label><input type="checkbox" id="ly_glm" checked/> ⚡ Lightning (GLM)</label>
    <label><input type="checkbox" id="ly_obs" checked/> Observations</label>
    <label><input type="checkbox" id="ly_wb" checked/> 🌡️ WBGT readings</label>
    <label><input type="checkbox" id="ly_mcd" checked/> 📌 SPC MCDs</label>
    <select id="obsSel">
      <option value="tn">East Tennessee stations</option>
      <option value="us">All US stations</option>
    </select>
  </div>
  <div class="src" id="pend"></div>
  <div class="src">⚡ Lightning: GOES-19 Geostationary Lightning Mapper flash density (NOAA STAR, ~5-min cadence) — overlays every radar layer. Future radar: HRRR 3 km (0-18 h) + NAM 3 km nest (18-48 h). Individual sites: every NWS radar serves 5 modes — super-res reflectivity, velocity, hybrid scan, 1-hour + storm-total precip — full 460 km range, all animated. MRMS picker: height levels 0.5–15 km, dual-pol (ZDR/RhoHV), azimuthal shear, rotation tracks, hail, echo tops, precip. Everything renders in over the first few update cycles.</div>
</div>

<script>
{_player_js(json.dumps(layers))}
document.getElementById("play").onclick = () => playing ? pause() : play();
document.getElementById("opacity").oninput = () => {{ for (const l of curLayers) if (l.setOpacity) l.setOpacity(OPACITY()); }};
document.getElementById("layer").onchange = (e) => {{
  kind = e.target.value;
  if (!kind.startsWith("site:")) queuedNote = "";
  document.getElementById("mrmsProd").style.display = kind === "mrms" ? "" : "none";
  document.getElementById("wbHour").style.display = kind === "wbgt" ? "" : "none";
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
let mcdLayer = null;
function drawMcd() {{
  if (mcdLayer) {{ map.removeLayer(mcdLayer); mcdLayer = null; }}
  const box = document.getElementById("ly_mcd");
  if (!box || !box.checked) return;
  const feats = (DATA.severe && DATA.severe.md) || [];
  if (!feats.length) return;
  mcdLayer = L.layerGroup(feats.map(f => {{
    const ring = f.geometry.coordinates[0].map(c => [c[1], c[0]]);
    return L.polygon(ring, {{ color: f.current ? "#ff5722" : "#b0bec5", weight: 2, dashArray: "6 4",
        fillColor: f.current ? "#ff5722" : "#90a4ae", fillOpacity: f.current ? 0.18 : 0.08 }})
      .bindTooltip(`<b>SPC MD ${{f.num}}${{f.current ? " (active)" : ""}}</b><br/>${{f.concerning || ""}}<br/>${{(f.areas || "").slice(0, 90)}}${{f.prob != null ? "<br/>Watch prob " + f.prob + "%" : ""}}`, {{ sticky: true }})
      .bindPopup(`<b>SPC Mesoscale Discussion ${{f.num}}</b>${{f.current ? " · <b style=color:#ff5722>ACTIVE</b>" : " (expired)"}}<br/>${{f.validStart !== "-" ? "Valid " + f.validStart + " - " + f.validEnd + "<br/>" : ""}}<a href="${{f.url}}" target="_blank">Full product (SPC) ↗</a>`);
  }}));
  mcdLayer.addTo(map);
}}
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
  try {{
    const r = await fetch(dataUrl(), {{cache: "no-store"}});
    if (!r.ok) throw new Error("data " + r.status);
    DATA = await r.json();
  }} catch (_e) {{
    /* packaging race / server blip: retry once before giving up */
    await new Promise(res => setTimeout(res, 4000));
    try {{
      const r2 = await fetch(dataUrl(), {{cache: "no-store"}});
      DATA = await r2.json();
    }} catch (_e2) {{ DATA = null; }}
  }}
  if (!DATA || !DATA.pageName) {{
    const mapEl = document.getElementById("map");
    if (mapEl) mapEl.dataset.radarState = "empty";
    frameEl.textContent = "data loading - retrying\u2026";
    setTimeout(boot, 10000);   /* self-heal: try again until the data lands */
    return;
  }}
  document.title = DATA.pageName + " - Radar";
  initMap(); build(); drawObs(); drawMcd(); fillSitePicker();
  document.getElementById("sitePick").onchange = (e) => pickSite(e.target.value);
  document.getElementById("obsSel").onchange = () => drawObs();
  document.getElementById("ly_obs").onchange = (e) => {{
    if (e.target.checked && !map.hasLayer(obsLayer)) obsLayer.addTo(map);
    if (!e.target.checked && map.hasLayer(obsLayer)) map.removeLayer(obsLayer);
  }};
  document.getElementById("ly_glm").onchange = () => show(idx);
  document.getElementById("ly_mcd").onchange = () => drawMcd();
  setInterval(async () => {{ refresh(await (await fetch(dataUrl(), {{cache: "no-store"}})).json()); drawObs(); drawMcd(); fillSitePicker(); }}, 180000);
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
  /* one failed fetch used to leave the page blank until a manual reload -
     retry a few times before giving up (auto-refresh heals it after that) */
  for (let tries = 0; tries < 4; tries++) {
    try { DATA = await (await fetch(dataUrl(), {cache: "no-store"})).json(); break; }
    catch (_e) { if (tries === 3) return; await new Promise(r => setTimeout(r, 2500)); }
  }
  document.title = DATA.pageName + " - Satellite";
  LAYERS = {};
  for (const [k, v] of Object.entries(DATA.satBands || {}))
    if ((v.frames || []).length) LAYERS[k] = { label: v.label, mode: "png", satKey: k };
  try { const sv = localStorage.getItem("tnwxSatKind"); if (sv && LAYERS[sv]) kind = sv; } catch (_e) {}
  if (LAYERS[DATA.satHome]) kind = DATA.satHome;
  else if (Object.keys(LAYERS).length) kind = Object.keys(LAYERS)[0];
  initMap(); fillLayerPicker(); build();
  setInterval(async () => { refresh(await (await fetch(dataUrl(), {cache: "no-store"})).json()); }, 180000);
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
const msArch = document.getElementById("msArchiveNote");
function msArchCheck() {
  if (!msArch) return;
  const stale = msSrc.value === "mpas" && MS.mpasArchive && msFrames.length &&
                (msFrames[0].url || "").indexOf("_2025") !== -1;
  msArch.style.display = stale ? "" : "none";
}
function msBuild() {
  msStop();
  const e = (MS[msSrc.value] || {})[msProd.value];
  msFrames = (e && e.frames) || [];
  msIdx = Math.max(0, msFrames.length - 1);
  msShow();
  msArchCheck();
  if (msFrames.length > 1) {
    msPlaying = true; msPlay.textContent = "\u23f8";
    msTimer = setInterval(() => { msIdx = (msIdx + 1) % msFrames.length; msShow(); }, 900);
  }
}
msSrc.onchange = () => { msFill(); msBuild(); msArchCheck(); };
msProd.onchange = msBuild;
msPlay.onclick = () => {
  if (!msFrames.length) return;
  if (msPlaying) msStop();
  else { msPlaying = true; msPlay.textContent = "\u23f8";
    msTimer = setInterval(() => { msIdx = (msIdx + 1) % msFrames.length; msShow(); }, 900); }
};
msFill(); msBuild(); msArchCheck();
if (MS.mpasArchive) {
  const mo = msSrc.querySelector("option[value=mpas]");
  if (mo) mo.textContent = "NCAR MPAS (3.75 km global) \u2014 2025 archive";
}
"""


_MPROG_JS = """
/* models-page render progress: how much of the full catalog is on disk.
   Boot counts the combos shipped in this build's data.json; a 5-min poll
   re-counts from fresh data so the bar fills live as the updater renders. */
const mprogBar = document.getElementById("mprogBar"), mprogTxt = document.getElementById("mprogTxt");
function mprogFrom(data) {
  const cat = (data && data.modelCatalog) || CAT;
  const rend = (data && data.renderIndex) || REND;
  let total = 0, have = 0;
  const rows = [];
  for (const m of Object.keys(cat)) {
    const prods = (cat[m].products || []).map(p => p.key);
    let mt = 0, mh = 0;
    if (m === "MPAS" || m.indexOf("FV3") === 0) {
      const key = m === "MPAS" ? "mpas" : "shield";
      for (const p of prods) {
        mt++;
        const e = (MS[key] || {})[p];
        if (e && e.frames && e.frames.length) mh++;
      }
    } else {
      const set = new Set(rend.filter(c => c.model === m).map(c => c.product + "|" + c.region));
      for (const p of prods) for (const r of ["etn", "us"]) {
        mt++;
        if (set.has(p + "|" + r)) mh++;
      }
    }
    total += mt; have += mh;
    if (mt) rows.push({ label: (cat[m] && cat[m].label) || m, have: mh, total: mt,
                        pct: Math.round(100 * mh / mt) });
  }
  const pct = total ? Math.round(100 * have / total) : 0;
  mprogBar.style.width = pct + "%";
  mprogBar.style.background = pct >= 99 ? "#2e7d32" : (pct >= 60 ? "#ef6c00" : "#c62828");
  mprogTxt.textContent = pct >= 99
    ? "All " + total + " model maps are rendered. Fresh cycles keep them current."
    : have + " of " + total + " model maps rendered (" + pct + "%) - " + (total - have) +
      " pending. The updater renders about 72 more every hour, missing ones first; " +
      "this bar refills itself every 5 minutes.";
  /* per-model completeness strip - starving models (lowest %) float to the
     top so a stuck downloader is visible at a glance (2026-09-14) */
  const mbox = document.getElementById("mprogModels");
  if (mbox) {
    rows.sort((a, b) => a.pct - b.pct);
    mbox.innerHTML = rows.map(r =>
      '<div style="display:flex;align-items:center;gap:8px;margin:3px 0">' +
        '<span title="' + r.label + '" style="width:170px;flex:none;font-size:12px;' +
          'white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + r.label + '</span>' +
        '<div style="flex:1;background:#e5e7eb;border-radius:4px;height:10px;overflow:hidden">' +
          '<div style="width:' + r.pct + '%;height:100%;border-radius:4px;background:' +
            (r.pct >= 99 ? "#2e7d32" : (r.pct >= 50 ? "#ef6c00" : "#c62828")) + '"></div></div>' +
        '<span style="width:84px;flex:none;text-align:right;font-size:12px;color:#555">' +
          r.have + '/' + r.total + '</span>' +
      '</div>').join("");
  }
}
mprogFrom(null);                       /* boot: count what shipped in this build */
setInterval(async () => {
  try {
    const nd = await (await fetch("data.json?t=" + Date.now(), {cache: "no-store"})).json();
    mprogFrom(nd);
  } catch (_e) { /* offline tick - keep the last known state */ }
}, 300000);
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

<div class="card"><h2>📊 Render progress</h2>
  <div style="background:#e5e7eb;border-radius:8px;height:22px;overflow:hidden;margin:6px 0 4px">
    <div id="mprogBar" style="height:100%;width:0;background:#ef6c00;border-radius:8px;transition:width .6s"></div>
  </div>
  <div class="src" id="mprogTxt">Counting rendered maps…</div>
  <div id="mprogModels" style="margin-top:10px"></div>
  <div class="src" style="margin-top:4px">Per-model completeness - lowest first, so a starved or broken model shows at the top. Refills every 5 minutes.</div>
</div>

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
  <div class="src">All 20 models (NWS global + CAM + AI + MPAS + FV3) × every product/level, animated side-by-side — each pane plays its own loop and the big clock advances every pane that has that hour. Missing hours snap to the nearest earlier frame; unrendered combos queue and fill in automatically — watch the 📊 render-progress bar at the top of this page.</div>
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
  <div class="alert" id="msArchiveNote" style="display:none;border-left-color:#ffb74d">📝 MPAS is showing its archived October 2025 demonstration run — NCAR's real-time experiment ended and a new season hasn't started. Frames stay browsable and this clears itself the moment NCAR publishes a new run.</div>
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
let catSpeed = 900;   /* loop frame interval (ms) — shared by the catalog loop player, persists across combos */
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
    msg.textContent = `${{m}} ${{p}} (${{r}}) - init ${{String(c.cycle).slice(-6,-2)}}Z ${{String(c.cycle).slice(-2)}}Z, ${{c.frames.length}}-frame loop`;
    const im = new Image(); im.src = c.frames[c.frames.length - 1].url;
    im.style.width = "100%"; im.style.borderRadius = "10px"; wrap.appendChild(im);
    const st = document.createElement("div"); st.className = "stepper";
    const sel = document.createElement("select");
    sel.innerHTML = c.frames.map(f => `<option value="${{f.fh}}">F${{String(f.fh).padStart(3, "0")}}</option>`).join("");
    sel.value = c.frames[c.frames.length - 1].fh;
    sel.onchange = () => {{ im.src = c.frames.find(f => f.fh === +sel.value).url; }};
    const back = document.createElement("button"); back.textContent = "\u25c0";
    const fwd = document.createElement("button"); fwd.textContent = "\u25b6";
    const play = document.createElement("button"); play.textContent = "\u23f8";
    const spd = document.createElement("select"); spd.title = "Loop speed"; spd.className = "catSpeed";
    spd.innerHTML = `<option value="2400">0.4x</option><option value="1500">0.6x</option><option value="900">1x</option><option value="600">1.5x</option><option value="400">2.5x</option><option value="200">4.5x</option>`;
    spd.value = String(catSpeed);
    let at = c.frames.length - 1, timer = null;
    const show = i => {{ const f = c.frames[(i + c.frames.length) % c.frames.length];
      sel.value = f.fh; im.src = f.url; }};
    const step = dd => {{ at = (at + dd + c.frames.length) % c.frames.length; show(at); }};
    const start = () => {{ clearInterval(timer); timer = setInterval(() => step(1), catSpeed); }};
    back.onclick = () => step(-1); fwd.onclick = () => step(1);
    spd.onchange = () => {{ catSpeed = +spd.value; if (timer) start(); }};   /* live speed change, keeps playing */
    play.onclick = () => {{
      if (timer) {{ clearInterval(timer); timer = null; play.textContent = "\u25b6\u25b6"; }}
      else {{ start(); play.textContent = "\u23f8"; }}
    }};
    if (c.frames.length > 1) {{ at = 0; show(0); start(); }}   /* autoplay the loop */
    else play.disabled = true;
    st.append(back, sel, play, fwd, spd); wrap.appendChild(st);
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
{_MPROG_JS}
{_CMP4_JS}
{_MSVIEWER_JS}
</script>
"""
    return _page("Models", "models.html", body)


def page_tropical(d):
    trop = d.get("tropical") or {}
    storms = trop.get("storms") or []

    storm_html = ""
    for s in storms:
        storm_html += (f'<div class="alert" style="border-left-color:#e1bee7"><b>🌀 {html.escape(s.get("name") or "Storm")} '
                       f'({html.escape(s.get("classification") or "?")})</b>'
                       f'<span>{html.escape(str(s.get("intensity") or ""))} kt · {html.escape(str(s.get("pressure") or ""))} mb · '
                       f'{s.get("lat", "?")}, {s.get("lon", "?")} · cone + track + wind field on the map</span></div>')
    if not storm_html:
        storm_html = '<div class="alert ok">No active tropical storms (NHC).</div>'

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

<script>
const TROP = {json.dumps(trop)};
let map, layers = [];
function toggle(id, lyr) {{
  const on = document.getElementById(id).checked;
  if (on && !map.hasLayer(lyr)) lyr.addTo(map);
  if (!on && map.hasLayer(lyr)) map.removeLayer(lyr);
}}
async function boot() {{
  DATA = await (await fetch(dataUrl(), {{cache: "no-store"}})).json();
  const T = DATA.tropical || TROP;
  document.title = DATA.pageName + " - NHC";
  {_mapbox_token_js()}
  map = L.map("map", {{ zoomSnap: 0.5, maxZoom: 21 }}).setView([25, -78], 4);
  addMapControls(map, [25, -78], 4);
  layers.storms = L.layerGroup();
  const clsName = c => ({{ "HU": "Hurricane", "MH": "Major Hurricane", "TS": "Tropical Storm",
    "TD": "Tropical Depression", "SD": "Subtropical Depression", "SS": "Subtropical Storm",
    "PTC": "Post-tropical Cyclone" }})[c] || c || "Storm";
  const kt = v => Math.round((parseFloat(v) || 0) * 1.15078);
  const degToCompass = d => {{
    const dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
    const n = parseFloat(d);
    return isNaN(n) ? "" : dirs[Math.round(n / 22.5) % 16];
  }},
  stormPopup = s => {{
    const mv = (s.movement || "").trim();
    const m = mv.match(/^([\\d.]+)\\s*kt\\s*@\\s*([\\d.]+)\\s*deg$/);
    const moveTxt = m ? `${{m[1]}} kt (${{kt(m[1])}} mph) toward the ${{degToCompass(m[2])}}`
                    : (mv || "movement n/a");
    const watches = (s.watches || []).length
      ? "<br/><b>⚠️ " + s.watches.join("</b><br/><b>⚠️ ") + "</b>"
      : "<br/><span class=src>No coastal watches/warnings in effect</span>";
    return "<b>🌀 " + s.name + " (" + clsName(s.classification) + ")</b>"
      + "<br/>Winds: <b>" + (s.intensity || "?") + " kt</b> (" + kt(s.intensity) + " mph)"
      + "<br/>Pressure: <b>" + (s.pressure || "?") + " mb</b>"
      + "<br/>Movement: <b>" + moveTxt + "</b>"
      + "<br/>Position: " + (s.lat != null ? Math.abs(s.lat) + (s.lat >= 0 ? "°N" : "°S") : "?")
      + ", " + (s.lon != null ? Math.abs(s.lon) + (s.lon >= 0 ? "°W" : "°E") : "?")
      + watches
      + (s.lastUpdate ? "<br/><span class=src>Advisory " + s.lastUpdate + "</span>" : "")
      + (s.advisoryUrl ? "<br/><a href='" + s.advisoryUrl + "' target='_blank'>Full NHC advisory ↗</a>" : "");
  }};
  (T.storms || []).forEach(s => {{
    if (s.lat != null && s.lon != null)
      L.circleMarker([s.lat, s.lon], {{ radius: 9, color: "#fff", weight: 2, fillColor: "#e1bee7", fillOpacity: .95 }})
        .bindTooltip("🌀 " + s.name + " - " + (s.intensity || "?") + " kt")
        .bindPopup(stormPopup(s), {{ maxWidth: 340 }}).addTo(layers.storms);
    const addG = (g, style) => {{ if (!g) return;
      L.geoJSON({{ type: "Feature", properties: {{}}, geometry: g }}, {{ style }}).addTo(layers.storms); }};
    addG(s.cone, {{ color: "#e1bee7", weight: 1.5, fillOpacity: 0.08 }});
    addG(s.track, {{ color: "#e1bee7", weight: 2.5, dashArray: "6 6" }});
    addG(s.trackFcst, {{ color: "#ff5252", weight: 3 }});
    (s.points || []).forEach(p => addG(p, {{ color: "#fff", fillColor: "#e1bee7", weight: 1, fillOpacity: .9 }}));
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


def page_tropmodels(d):
    tm = d.get("tropModels") or {}
    try:
        from data.tropical_models import FAMILY_COLORS as _FAMCOL
    except Exception:                                  # noqa: BLE001
        _FAMCOL = {}
    storms = tm.get("storms") or []
    n_mods = sum(len(s.get("models") or []) for s in storms)
    n_charts = sum(len(s.get("charts") or []) for s in storms)

    body = f"""
<header class="hero"><h1>🌪️ Tropical Model Guidance</h1>
<div class="sub">Track spaghetti + intensity forecasts from every global & regional model
· NHC ATCF aid-decks · updated {d["generated"]}</div></header>

<div class="card">
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
    <label class="src" for="stormPick">Storm:</label>
    <select id="stormPick" class="sel"></select>
    <span class="src" id="tmSummary">{len(storms)} storm(s) · {n_mods} guidance members · {n_charts} chart(s)</span>
  </div>
  <div id="map" class="map-dark"></div>
  <div class="legend" id="famLegend">
    <span><i style="background:#ffffff"></i>Official (OFCL)</span>
    <span><i style="background:#4fc3f7"></i>GFS</span>
    <span><i style="background:#ce93d8"></i>ECMWF</span>
    <span><i style="background:#ffb74d"></i>UKMET</span>
    <span><i style="background:#a5d6a7"></i>CMC</span>
    <span><i style="background:#ff8a80"></i>HWRF</span>
    <span><i style="background:#ffab91"></i>HMON</span>
    <span><i style="background:#80cbc4"></i>GFDL</span>
    <span><i style="background:#bcaaa4"></i>Navy</span>
    <span class="src">checkboxes toggle model families · dots = +24/48/72/120 h, colored by intensity</span>
  </div>
  <div id="famChecks" style="display:flex;gap:12px;flex-wrap:wrap;margin-top:6px"></div>
</div>

<div class="card" id="chartsCard"><h2>📈 Intensity guidance</h2><div id="charts"></div></div>
<div class="card" id="tblCard"><h2>🧭 Guidance members</h2><div id="tbl"></div></div>

<script>
const TMS = {json.dumps(tm)};
const FAMCOL = {json.dumps(_FAMCOL)};
let map, famGroups = {{}};
const KT_COL = kt => kt >= 137 ? "#d32f2f" : kt >= 113 ? "#e64a19" : kt >= 96 ? "#f57c00"
  : kt >= 83 ? "#ffa000" : kt >= 64 ? "#fbc02d" : kt >= 34 ? "#03a9f4" : "#90a4ae";
function drawStorm(idx) {{
  const s = (TMS.storms || [])[idx];
  // Array.isArray guard: a storm without guidance used to ship tracks as an
  // object, .forEach threw, and the whole map boot died (2026-09-20).
  if (!s || !Array.isArray(s.tracks)) return;
  Object.values(famGroups).forEach(g => map.removeLayer(g));
  famGroups = {{}};
  s.tracks.forEach(tr => {{
    const fam = tr.family || "ens";
    if (!famGroups[fam]) {{
      famGroups[fam] = L.layerGroup();
      famGroups[fam].addTo(map);
    }}
    const w = tr.isOfficial ? 4 : 2, dash = tr.isOfficial ? null : "5 5";
    L.geoJSON({{ type: "Feature", properties: {{}}, geometry: tr.geo }},
      {{ style: {{ color: tr.color, weight: w, opacity: .85, dashArray: dash }} }})
      .bindTooltip((tr.tech || fam) + " track")
      .addTo(famGroups[fam]);
    (tr.geo.props || []).forEach(p => {{
      if ([24, 48, 72, 120].includes(p.hour) && p.kt != null)
        L.circleMarker([p.lat, p.lon], {{ radius: 4.5, color: "#fff", weight: 1,
          fillColor: KT_COL(parseFloat(p.kt) || 0), fillOpacity: .95 }})
          .bindPopup("<b>" + (tr.tech || fam) + "</b><br/>+" + p.hour + " h · " +
            Math.round(parseFloat(p.kt)) + " kt<br/>" +
            (p.mslp && parseInt(p.mslp) > 800 ? parseInt(p.mslp) + " mb" : ""))
          .addTo(famGroups[fam]);
    }});
  }});
  // fit to official track (or all)
  const ofcl = (s.tracks || []).find(t => t.isOfficial) || (s.tracks || [])[0];
  if (ofcl && ofcl.geo && ofcl.geo.coordinates && ofcl.geo.coordinates.length)
    map.fitBounds(L.latLngBounds(ofcl.geo.coordinates.map(c => [c[1], c[0]])).pad(0.35));
  // family checkboxes (one per family actually present on this storm)
  const fc = document.getElementById("famChecks");
  fc.innerHTML = "";
  Object.keys(famGroups).sort((a, b) =>
    (a === "OFCL" ? -1 : b === "OFCL" ? 1 : a.localeCompare(b))).forEach(fam => {{
    const lab = document.createElement("label");
    lab.style.cssText = "display:flex;gap:5px;align-items:center;cursor:pointer;font-size:13px";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = true;
    cb.onchange = () => {{
      const g = famGroups[fam];
      if (!g) return;
      if (cb.checked) {{ if (!map.hasLayer(g)) g.addTo(map); }}
      else {{ if (map.hasLayer(g)) map.removeLayer(g); }}
    }};
    const dot = document.createElement("span");
    dot.style.cssText = "width:11px;height:11px;border-radius:3px;display:inline-block;background:"
      + (FAMCOL[fam] || "#b0bec5");
    lab.appendChild(cb); lab.appendChild(dot);
    lab.appendChild(document.createTextNode(fam + " (" + (s.tracks || []).filter(t => (t.family || "ens") === fam).length + ")"));
    fc.appendChild(lab);
  }});
  // charts
  document.getElementById("charts").innerHTML = (s.charts || []).map(c =>
    `<div><img src="${{c.href}}" alt="${{c.name}}" style="max-width:100%;border-radius:10px"/></div>`)
    .join("") || '<span class=src>No intensity chart for this storm yet.</span>';
  // model table
  const rows = (s.models || []).map(m =>
    `<tr><td><span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:${{m.color}}"></span></td>` +
    `<td><b>${{m.tech}}</b></td><td>${{m.name}}</td><td>${{m.family}}</td>` +
    `<td>+${{m.max_hour}} h</td></tr>`).join("");
  document.getElementById("tbl").innerHTML = rows
    ? `<table><tr><th></th><th>ID</th><th>Model</th><th>Family</th><th>Max lead</th></tr>${{rows}}</table>`
    : '<span class=src>No aid-deck guidance parsed for this storm yet.</span>';
}}
async function boot() {{
  DATA = await (await fetch(dataUrl(), {{cache: "no-store"}})).json();
  const T = DATA.tropModels || TMS;
  document.title = DATA.pageName + " - Tropical Models";
  // Empty season: NHC currently reports zero storms. Say so plainly instead
  // of leaving a blank map and empty cards (2026-09-17).
  if (!(T.storms || []).length) {{
    const mapEl = document.getElementById("map");
    if (mapEl) mapEl.outerHTML =
      '<div class="alert ok" style="margin:4px 0"><b>🌪️ Quiet Atlantic & Pacific — no active tropical cyclones.</b><br/>' +
      'NHC reports zero active storms right now, so there are no tracks or intensity guidance to plot. ' +
      'Spaghetti tracks, model tables and intensity charts appear here automatically the moment the next system gets an ATCF ID. ' +
      'The official NHC development outlooks below always stay current.</div>';
    const sp = document.getElementById("stormPick");
    if (sp) sp.style.display = "none";
    const cc = document.getElementById("chartsCard");
    if (cc) cc.style.display = "none";
    const tc = document.getElementById("tblCard");
    if (tc) tc.style.display = "none";
    const sm = document.getElementById("tmSummary");
    if (sm) sm.textContent = "0 active storms · guidance auto-appears when one forms";
    // NHC 2-day/7-day development outlooks - hotlinked live from NHC, so
    // they stay current every time NHC republishes (verified 200 on 09-18).
    // (Same always-live graphics the NHC Tropical tab shows.)
    const outlook = document.createElement("div");
    outlook.className = "card";
    outlook.innerHTML =
      '<h2>🌧️ NHC Tropical Weather Outlooks</h2>' +
      '<div class="src">Atlantic · 2-day and 7-day tropical formation chances - updated with each NHC issuance</div>' +
      '<div style="display:flex;gap:12px;flex-wrap:wrap">' +
      '<img src="https://www.nhc.noaa.gov/xgtwo/two_atl_2d0.png" alt="Atlantic 2-day outlook" style="max-width:49%;min-width:280px;border-radius:10px"/>' +
      '<img src="https://www.nhc.noaa.gov/xgtwo/two_atl_7d0.png" alt="Atlantic 7-day outlook" style="max-width:49%;min-width:280px;border-radius:10px"/>' +
      '</div>' +
      '<div class="src" style="margin-top:8px">Eastern Pacific · 2-day and 7-day formation chances</div>' +
      '<div style="display:flex;gap:12px;flex-wrap:wrap">' +
      '<img src="https://www.nhc.noaa.gov/xgtwo/two_pac_2d0.png" alt="East Pacific 2-day outlook" style="max-width:49%;min-width:280px;border-radius:10px"/>' +
      '<img src="https://www.nhc.noaa.gov/xgtwo/two_pac_7d0.png" alt="East Pacific 7-day outlook" style="max-width:49%;min-width:280px;border-radius:10px"/>' +
      '</div>';
    // chartsCard sits inside the page container (not directly under body),
    // so insert relative to ITS parent - inserting vs document.body threw
    // NotFoundError and silently killed the whole quiet branch (2026-09-18).
    if (cc && cc.parentNode) cc.parentNode.insertBefore(outlook, cc);
    else document.body.appendChild(outlook);
    return;                    // skip map/plot setup entirely
  }}
  {_mapbox_token_js()}
  map = L.map("map", {{ zoomSnap: 0.5, maxZoom: 21 }}).setView([26, -82], 5);
  addMapControls(map, [26, -82], 5);
  const sel = document.getElementById("stormPick");
  (T.storms || []).forEach((s, i) => {{
    const nM = Array.isArray(s.models) ? s.models.length : 0;
    const o = document.createElement("option");
    o.value = i;
    o.textContent = `${{s.name || "Storm"}} (${{s.classification || "?"}}) - ${{nM}} models`;
    sel.appendChild(o);
  }});
  sel.onchange = () => drawStorm(parseInt(sel.value) || 0);
  // Open on the storm that actually has guidance: a newly-minted NHC storm
  // with no aid-deck file yet used to open first, shipped an empty shape,
  // and killed the boot (2026-09-20).
  let defIdx = 0, best = -1;
  (T.storms || []).forEach((s, i) => {{
    const nM = Array.isArray(s.models) ? s.models.length : 0;
    if (nM > best) {{ best = nM; defIdx = i; }}
  }});
  if (sel.options[defIdx]) sel.selectedIndex = defIdx;
  drawStorm(defIdx);
}}
boot();
function onDataRefresh(d) {{ /* picker keeps place; reload for new storms */ }}
</script>
"""
    return _page("Tropical Models", "tropmodels.html", body)


def page_climate(d):
    cl = d.get("climate") or {}
    groups = cl.get("groups") or []
    oni = cl.get("oni") or []
    enso = cl.get("enso") or {}
    phase = cl.get("ensoPhase") or "unknown"
    phase_col = cl.get("ensoPhaseColor") or "#9e9e9e"

    outlook_html = ""
    for g in groups:
        imgs = g.get("images") or {}
        t, p = imgs.get("temp"), imgs.get("prcp")
        if not (t or p):
            continue
        outlook_html += f'<div class="card"><h2>🗺️ {html.escape(g.get("label") or g.get("id"))}</h2>'
        for kind, href in (("Temperature", t), ("Precipitation", p)):
            if href:
                outlook_html += (f'<div style="margin:6px 0"><div class="src">{kind} outlook</div>'
                                 f'<img src="{href}" alt="{g.get("id")} {kind}" '
                                 f'style="max-width:100%;border-radius:10px" loading="lazy"/></div>')
        outlook_html += "</div>"
    if not outlook_html:
        outlook_html = '<div class="alert">CPC outlook graphics unavailable this cycle.</div>'

    oni_rows = "".join(
        f'<tr><td>{r["season"]}</td><td style="color:{"#ef5350" if r["anom"] >= 0.5 else "#42a5f5" if r["anom"] <= -0.5 else "inherit"}">'
        f'{r["anom"]:+.2f}°C</td></tr>' for r in oni[-8:])
    enso_paras = "".join(f'<p style="margin:6px 0">{html.escape(p)}</p>'
                         for p in (enso.get("paragraphs") or [])[:3])

    body = f"""
<header class="hero"><h1>🗓️ Climate & Long-Range</h1>
<div class="sub">CPC 6-10 day · 8-14 day · monthly · seasonal outlooks · ENSO status
· updated {d["generated"]}</div></header>

<div class="card">
  <h2>🌊 ENSO Status</h2>
  <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
    <span style="background:{phase_col};color:#0b0f14;font-weight:700;border-radius:20px;padding:6px 16px">{html.escape(phase.upper())}</span>
    <b class="src">{html.escape(enso.get("title") or "")}</b>
  </div>
  {enso_paras or '<span class=src>ENSO discussion unavailable this cycle.</span>'}
  <div style="margin-top:10px"><b class="src">Oceanic Niño Index - recent seasons</b>
  <table>{'<tr><th>Season</th><th>SST anomaly (Nino 3.4)</th></tr>'}{oni_rows}</table>
  <span class="src">El Niño threshold +0.5°C · La Niña threshold -0.5°C (CPC ONI)</span></div>
</div>

{outlook_html}

<div class="card"><span class="src">Sources: NOAA CPC long-range outlooks (updated daily-weekly),
CPC ONI & ENSO Diagnostic Discussion (updated monthly-ish or as advisories change).
Graphics mirrored from CPC so they load fast; refresh happens on the site update cycle.</span></div>

<script>
async function boot() {{
  DATA = await (await fetch(dataUrl(), {{cache: "no-store"}})).json();
  document.title = DATA.pageName + " - Climate";
}}
boot();
</script>
"""
    return _page("Climate", "climate.html", body)


def page_status(d):
    """Tiny status page: updater heartbeat, data age, public publish lag.

    Self-contained JS - measures the PUBLIC lag live from the visitor's
    browser against this build's dataEpochMs, so it stays honest on both
    the local mirror and GitHub Pages.
    """
    body = f"""
<header class="hero"><h1>🩺 Site Status</h1>
<div class="sub">Updater heartbeat · data age · public publish lag - measured live in your browser</div></header>

<div class="kpis">
  <div class="kpi"><span>Data build (this server)</span><b id="stBuild">…</b></div>
  <div class="kpi"><span>Data age (vs your clock)</span><b id="stAge">…</b></div>
  <div class="kpi"><span>Public GitHub copy</span><b id="stPub">…</b></div>
  <div class="kpi"><span>Publish pipeline</span><b id="stPipe">…</b></div>
</div>

<div class="card"><h2>💓 Updater heartbeat</h2>
<div id="stBeat" class="src">watching for the next rebuild…</div>
<p style="margin:6px 0">The updater rebuilds all data + maps every ~2-3 minutes and pushes to
GitHub Pages every ~10 minutes. A missing heartbeat means the Windows task
"TNWN-WeatherCenter" is stopped - start it from Task Scheduler or run
`python startup_task.py` in the project folder.</p></div>

<div class="card"><h2>What the numbers mean</h2>
<table>
<tr><th>Indicator</th><th>Healthy</th><th>Action</th></tr>
<tr><td><b>Data age</b></td><td>under 5 min</td><td>over 15 min: updater stopped - run the Windows task or startup_task.py</td></tr>
<tr><td><b>Public copy</b></td><td>under 15 min behind</td><td>over 30 min: publish pipeline trouble; the updater auto-recovers (watchdog), check `.freebuff/PUBLIC_STALE.alert`</td></tr>
<tr><td><b>Heartbeat</b></td><td>advancing every 2-3 min</td><td>frozen: updater process dead - see above</td></tr>
</table></div>

<script>
const BUILT = {int(d.get("dataEpochMs") or 0)};
function mins(ms) {{ const m = Math.round(ms / 60000); return m < 1 ? "<1 min" : m + " min"; }}
function fnum(n) {{ return n.toLocaleString("en-US"); }}
async function boot() {{
  DATA = await (await fetch(dataUrl(), {{cache: "no-store"}})).json();
  document.title = DATA.pageName + " - Status";
  let built = DATA.dataEpochMs || BUILT;
  const bd = new Date(built);
  document.getElementById("stBuild").textContent =
    bd.toLocaleTimeString("en-US", {{hour: "numeric", minute: "2-digit"}});
  const setAge = () => {{
    const a = Date.now() - built;
    const el = document.getElementById("stAge");
    el.textContent = mins(a) + " ago";
    el.style.color = a < 5 * 60000 ? "#7ddc7d" : a < 15 * 60000 ? "#ffc46b" : "#ff6b6b";
  }};
  setAge(); setInterval(setAge, 15000);
  // public lag: compare the live public data.json against this build
  fetch("https://rpleasant12.github.io/http-localhost-8765-/data.json?t=" + Date.now(),
        {{cache: "no-store"}}).then(r => r.json()).then(pub => {{
    const lag = built - (pub.dataEpochMs || 0);
    const el = document.getElementById("stPub");
    el.textContent = lag <= 0 ? "this build or newer" : mins(lag) + " behind";
    el.style.color = lag < 15 * 60000 ? "#7ddc7d" : lag < 30 * 60000 ? "#ffc46b" : "#ff6b6b";
    const pipe = document.getElementById("stPipe");
    pipe.textContent = lag < 30 * 60000 ? "✅ publishing normally" : "⚠️ lagging - auto-recovery should catch it";
    pipe.style.color = lag < 30 * 60000 ? "#7ddc7d" : "#ffc46b";
  }}).catch(() => {{
    document.getElementById("stPub").textContent = "unreachable";
  }});
  /* heartbeat: poll data.json every 60 s; each NEW build stamp = one beat */
  let lastBuilt = built, beats = 0, lastBeatAt = Date.now();
  setInterval(async () => {{
    try {{
      const nd = await (await fetch(dataUrl(), {{cache: "no-store"}})).json();
      if ((nd.dataEpochMs || 0) > lastBuilt) {{
        beats++; lastBuilt = nd.dataEpochMs; lastBeatAt = Date.now();
        document.getElementById("stBuild").textContent =
          new Date(lastBuilt).toLocaleTimeString("en-US", {{hour: "numeric", minute: "2-digit"}});
        built = lastBuilt;   // the age card tracks the newest build too
      }}
    }} catch (_e) {{}}
    const since = Date.now() - lastBeatAt;
    const el = document.getElementById("stBeat");
    el.textContent = "\u2764\ufe0f " + beats + " rebuild" + (beats === 1 ? "" : "s") +
      " while you watched \u00b7 last " + Math.round(since / 60000) +
      " min ago \u00b7 next expected within " +
      Math.max(0, 3 - Math.round(since / 60000)) + " min";
    el.style.color = since < 6 * 60000 ? "#7ddc7d" : "#ffc46b";
  }}, 60000);
}}
boot();
</script>
"""
    return _page("Status", "status.html", body)


def page_education(d):
    """Learning hub: how to read the site's own maps + official NOAA/NWS resources."""

    tools = [
        ("radar.html", "📡", "Radar & RainViewer",
         "Colors are reflectivity (dBZ): green = light rain, yellow = moderate, red = heavy rain or hail. "
         "The Futurecast layers are a 30-60 min projection of where storms are moving - great for \"is it about to rain on me?\"."),
        ("models.html", "🧭", "Forecast Models",
         "GFS, NAM, RRFS and AI models each divide the atmosphere into stacked pressure levels (500 mb ≈ 18,000 ft, "
         "850 mb ≈ 5,000 ft). Lower vorticity values usually mark storm systems. Compare several models - when they agree, confidence is high."),
        ("obs.html", "🎈", "Obs & Skew-T",
         "A sounding is a vertical snapshot of the atmosphere. Watch for CAPE (fuel for thunderstorms) and lifted index; "
         "the wind profile tells you whether storms will rotate."),
        ("severe.html", "🌪️", "Severe & SPC Outlooks",
         "SPC outlooks rank storm risk by probability: MRGL (marginal) < SLGT (slight) < ENH (enhanced) < MDT (moderate) < HIGH. "
         "Hatched areas mean significant (EF2+, 75+ mph, 2\"+ hail) events are possible."),
        ("rivers.html", "🌊", "River Gauges",
         "AHPS river gauges show stage against official flood categories: action, minor, moderate, major. "
         "When a gauge crosses minor-flood stage, low-lying roads near the river flood first."),
        ("fire.html", "🔥", "Fire Weather",
         "SPC Fire Weather Outlooks flag areas where wind + dry air + dry fuels let fires spread dangerously. "
         "A Red Flag Warning means no outdoor burning - embers can start wildfires miles away."),
        ("forecast.html", "🥵", "Heat Index vs WBGT",
         "Heat index is shade comfort; WBGT adds sun and humidity stress - what coaches and outdoor crews use. "
         "Both are on the Heat-Stress card with color-coded risk bands."),
        ("tropmodels.html", "🌀", "Tropical Spaghetti",
         "Each colored line is one hurricane model's idea of a storm's future path. The bunch of lines = uncertainty; "
         "where they converge is where the storm is most likely to go. The official forecast is the white line."),
        ("satellite.html", "🛰️", "Satellite Bands",
         "Satellites see beyond visible light. Water vapor (6.9 µm) shows the rivers of moisture steering storms; "
         "the infrared window (10.3 µm) shows cloud tops - colder = higher = stronger storms, day or night."),
        ("meso.html", "🔬", "Mesoanalysis",
         "This zooms into the storm environment right now: surface winds, instability (CAPE), shear. Forecasters "
         "watch the overlap of high CAPE + strong shear - that is where rotating storms become possible."),
        ("charts.html", "📈", "MOS & Charts",
         "MOS is a statistical correction of model output for each airport - often better than raw model numbers "
         "for temperature and wind. The hourly tables show dew point, wind and precip odds hour by hour."),
        ("winter.html", "❄️", "Winter Maps",
         "Snow maps differ: 6-hourly bars (NBM) show short bursts; storm-total fields (GFS/GEFS) accumulate. "
         "Ensemble spread is the honesty meter - wide spread means the snow band's position is still uncertain."),
        ("dashboard.html", "📊", "Dashboard",
         "One screen, whole county: station temperatures with 24-hour trends, river stages, and what changed "
         "since yesterday. Built for the morning glance before you head out."),
    ]
    tools_html = "".join(
        f'<div class="card"><h2>{ic} {html.escape(t)}</h2><p style="margin:6px 0">{txt}</p>'
        f'<a class="src" href="{href}">Open the {html.escape(t.split(" &")[0])} page →</a></div>'
        for href, ic, t, txt in tools)

    learn = [
        ("JetStream - NOAA's weather school",
         "Self-paced lessons on how storms, fronts and the atmosphere work. The classic starting point.",
         "https://www.noaa.gov/jetstream"),
        ("NWS Training Portal - How radar works",
         "Official explanations of reflectivity, velocity and dual-pol products.",
         "https://training.weather.gov/nws_courses/"),
        ("SKYWARN storm spotter program",
         "Free volunteer training to report severe weather to the NWS - many sessions are online.",
         "https://www.weather.gov/skywarn/"),
        ("NOAA/NWS Severe Weather 101 (NSSL)",
         "Plain-language science on tornadoes, hail, lightning and flash floods.",
         "https://www.nssl.noaa.gov/education/"),
        ("Weather Safety - NWS",
         "Tornado, flood, lightning, heat and winter safety pages with checklists.",
         "https://www.weather.gov/safety"),
        ("CoCoRaHS - be a rain gauge volunteer",
         "Community rain-reporting network; your observations feed real hydrology.",
         "https://www.cocorahs.org/"),
        ("NOAA SciJinks (for kids)",
         "Games and simple explanations of weather for younger learners.",
         "https://scijinks.gov/"),
        ("NWS Knoxville office (our home office)",
         "Local forecasts, spotter training schedules and East TN climatology.",
         "https://www.weather.gov/mrx/"),
    ]
    learn_html = "".join(
        f'<div class="card"><h2>🎓 {html.escape(t)}</h2><p style="margin:6px 0">{txt}</p>'
        f'<a href="{u}" target="_blank" rel="noopener" class="src">{u.split("//")[1].split("/")[0]} ↗</a></div>'
        for t, txt, u in learn)

    safety = [
        ("🌪️", "Tornado", "In a warning: lowest floor, interior room, away from windows. Mobile homes and vehicles are unsafe - get to a sturdy building.", "#ef5350"),
        ("🌊", "Flash flood", "Turn Around Don't Drown® - never drive through a flooded road. Six inches of moving water can knock you down; a foot floats many cars.", "#42a5f5"),
        ("⚡", "Lightning", "When thunder roars, go indoors! Wait 30 minutes after the last thunder before going back outside.", "#ffb74d"),
        ("🥵", "Heat", "Water, rest, shade. At WBGT 90°F+, cancel outdoor exertion. Check on elderly neighbors.", "#ff8a65"),
        ("❄️", "Winter storms", "Dress in layers, keep a car blanket and charger. Black ice forms first on bridges and shaded curves.", "#90caf9"),
    ]
    safety_html = "".join(
        f'<div class="card" style="border-left:4px solid {c}"><h2>{ic} {html.escape(t)} safety</h2>'
        f'<p style="margin:6px 0">{txt}</p></div>' for ic, t, txt, c in safety)

    gloss = [
        ("CAPE", "Instability fuel for storms - higher = stronger updrafts possible. 1000+ J/kg storms; 2500+ severe potential."),
        ("Dew point", "Moisture measure - 55°F muggy-ish, 65°F+ oppressive, and thunderstorms feed on it."),
        ("Vorticity", "Spin in the airflow - maxima aloft often kick off storm systems and heavy rain."),
        ("dBZ", "Radar echo strength - 20 light rain, 40 heavy rain, 55+ likely hail."),
        ("WBGT", "Wet-Bulb Globe Temperature - heat stress in the sun, used by schools and athletic programs."),
        ("Skew-T", "The atmospheric vertical profile chart - temperature, moisture and wind from ground to jet stream."),
        ("Ensemble", "Many model runs with tiny tweaks - the spread between members shows forecast confidence."),
        ("SPC MCD", "Mesoscale Discussion - SPC's short-fuse technical note on where organized severe weather is about to develop."),
        ("Helicity", "Wind that turns with height - storms ingest it and rotate. 150+ m²/s² in a storm environment gets forecasters' attention."),
        ("Lapse rate", "How fast air cools with height - steep rates (8°C/km+) make air rise explosively and fuel storms."),
        ("PWAT", "Precipitable water - the rain in a column if it all fell at once. 2+ inches = torrential-rain flood potential."),
        ("MOS", "Model Output Statistics - raw model output statistically corrected for each airport's climate; often beats the raw model."),
        ("AI models", "Forecasts (FourCastNet, GraphCast, AIFS) learned from decades of data - fast and often skillful, but new and still audited."),
        ("Dew point vs RH", "Dew point is real moisture (better for comfort & storms); relative humidity changes with temperature alone."),
        ("Flood stage", "Gauge height where a river starts causing impacts: action → minor → moderate → major."),
        ("AFD", "Area Forecast Discussion - the local NWS office's plain-language notes on WHY the forecast is what it is."),
    ]
    gloss_rows = "".join(f'<tr><td><b>{k}</b></td><td>{v}</td></tr>' for k, v in gloss)

    mini = [
        ("🧭", "How to compare two models (and know which to trust)",
         "Open the Models page in two tabs - GFS in one, NAM in the same product and hour. Where the 500-mb vorticity "
         "and surface lows line up, confidence is high. Where they diverge, check the ensemble spread: tight spread + "
         "diverging deterministic runs usually means the ensemble mean wins. Rule of thumb: days 1-3 trust the high-res "
         "models (HRRR/NAM), days 4-7 trust the global ensembles (GEFS/EPS), day 8+ treat everything as a pattern hint."),
        ("🌪️", "Reading a severe weather setup in 5 minutes",
         "Start on Mesoanalysis: is CAPE above 1500 J/kg? Is the 0-6 km shear vector crossing the warm front at 40 kt+? "
         "Then check SPC's outlook for probability and hatching. Finally watch the radar for discrete cells ahead of any "
         "line - those are the ones that rotate. If all three agree, that is when the Skywarn group chat lights up."),
        ("🌊", "Why river flooding lags the rain",
         "Rain must first fill the soil, then the hollows, then the tributaries before the main stem crests. That is why "
         "the Rivers page matters days after a storm: the Nolichucky can keep rising 12-24 h after the sky clears. "
         "Compare the gauge trend (rising/steady/falling) - the trend matters more than the number."),
        ("❄️", "The 32°F line and why elevation wins in East Tennessee",
         "Our valleys routinely sit 5-8°F warmer than the ridges at night. A 34°F valley rain can be a 28°F ridge ice "
         "event. When winter maps show blue over the plateau but not the valley, that is not a model error - that is "
         "the actual elevation profile of East Tennessee. Always check the temperature column below the snow map."),
        ("📡", "What the radar actually measures",
         "Radar sends microwaves and listens for echoes off raindrops. Reflectivity (dBZ) is echo strength - but it "
         "measures DROPS, not flood impact: drizzle with huge drops can out-echo a steady soaker. That is why the "
         "site pairs radar with MRMS gauge-calibrated QPE - the radar shape with real rain-gauge truth."),
    ]
    mini_html = "".join(
        f'<details class="card" style="margin:8px 0"><summary style="cursor:pointer;font-weight:700">'
        f'{ic} {html.escape(t)}</summary><p style="margin:8px 0 2px">{txt}</p></details>'
        for ic, t, txt in mini)

    body = f"""
<header class="hero"><h1>📚 Education & Resources</h1>
<div class="sub">Learn to read weather data like a forecaster - official NOAA/NWS learning links,\nsafety guides, and how to use every tool on this site · free, always here</div></header>

<div class="card" style="background:linear-gradient(135deg,#1d3557,#2a6f97);color:#fff">
  <h2 style="color:#fff">Weather school, in order</h2>
  <p style="margin:6px 0">New to meteorology? Do these three: <b>1)</b> skim NOAA JetStream for the big picture,\n  <b>2)</b> read the radar &amp; model guides below while looking at this site's real data,\n  <b>3)</b> join SKYWARN for local, hands-on severe-weather training. That path takes most people\n  from \"just curious\" to reading soundings in a few weeks.</p>
</div>

<h2 style="margin:14px 0 6px">Learn with this site</h2>
<div class="kpis">{tools_html}</div>

<h2 style="margin:14px 0 6px">Forecaster mini-lessons</h2>
{mini_html}

<h2 style="margin:14px 0 6px">Official learning resources</h2>
<div class="kpis">{learn_html}</div>

<h2 style="margin:14px 0 6px">Safety essentials</h2>
<div class="kpis">{safety_html}</div>

<div class="card"><h2>📖 Glossary - words used on this site</h2>
<table>{'<tr><th>Term</th><th>What it means</th></tr>'}{gloss_rows}</table>
<span class="src">Deeper definitions: NOAA JetStream glossary and the NWS glossary at weather.gov/glossary.</span></div>

<div class="card"><span class="src">All links go to NOAA / NWS / official programs (open in a new tab).\nThis site is a free, independent service for East Tennessee - no accounts, no cost.</span></div>
"""
    return _page("Education", "education.html", body)


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

    def _mcd_card(m):
        cur = ' · <b style="color:#ff5722">ACTIVE</b>' if m.get("current") else " (expired)"
        prob = f" · watch probability {m['prob']}%" if m.get("prob") is not None else ""
        wfos = f' · WFOs: {html.escape(m["wfos"])}' if m.get("wfos") else ""
        return (f'<div class="card"><h2>📍 SPC Mesoscale Discussion {m["num"]}{cur}</h2>'
                f'<div class="alert" style="border-left-color:#ff5722"><b>{html.escape(m.get("concerning") or "")}</b>'
                f'<span>{html.escape(m.get("areas") or "")} · valid {m.get("validStart", "-")} - {m.get("validEnd", "-")}{prob}</span></div>'
                f'<div class="pre">{html.escape(m.get("summary") or "")}</div>'
                f'<div class="pre">{html.escape(m.get("discussion") or "")}</div>'
                f'<div class="src"><a href="{m.get("url", "#")}" target="_blank">Full product on SPC ↗</a>{wfos}</div></div>')
    md_html = ("".join(_mcd_card(m) for m in md[:2])
               or '<div class="card"><h2>📍 SPC Mesoscale Discussions</h2><div class="src">No discussions in the last day.</div></div>')

    hail = sev.get("forecast") or {}
    if hail.get("ok") and hail.get("hours"):
        from data._tz import iso_local
        peak = hail.get("peak") or {}
        prot = hail.get("peakRot") or {}
        pk = peak.get("cat")

        def _et(t):
            try:
                return iso_local(dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ")
                                 .replace(tzinfo=dt.timezone.utc))
            except (ValueError, TypeError):
                return (t or "-").replace("T", " ").replace("Z", "")
        pk_line = (f'<b style="color:{peak.get("catColor", "#81c784")}">'
                   f'{peak.get("mm", 0):.0f} mm ({pk})</b> at {_et(peak.get("time"))}'
                   if pk else f'<b style="color:#81c784">{peak.get("mm", 0):.0f} mm</b> - no severe hail expected')
        pr_line = (f'<b style="color:{prot.get("catColor", "#81c784")}">'
                   f'{prot.get("val", 0):.0f} m²/s² ({prot.get("cat")})</b> at {_et(prot.get("time"))}'
                   if prot.get("cat") else f'<b style="color:#81c784">{prot.get("val", 0):.0f} m²/s²</b> - no rotating storms forecast')

        def _hrow(h):
            hcat_cell = (f'<b style="color:{h["catColor"]}">{h["cat"]}</b>'
                         if h.get("cat") else "—")
            ucat_cell = (f'<b style="color:{h["ucatColor"]}">{h["ucat"]}</b>'
                         if h.get("ucat") else "—")
            return (f'<tr><td>{_et(h.get("time"))}</td>'
                    f'<td>{(h.get("tn_max") if h.get("tn_max") is not None else 0):.0f} mm</td>'
                    f'<td>{hcat_cell}</td>'
                    f'<td>{(h.get("tn_uphl") if h.get("tn_uphl") is not None else 0):.0f}</td>'
                    f'<td>{ucat_cell}</td></tr>')
        rows = "".join(_hrow(h) for h in hail["hours"])
        cities = {k: v for k, v in (hail.get("cities") or {}).items()
                  if v.get("hail") or v.get("uphl")}
        city_rows = "".join(
            f'<tr><td>{html.escape(k)}</td>'
            f'<td>{v["hail"]:.0f} mm</td><td>{v["hcat"] or "—"}</td>'
            f'<td>{v["uphl"]:.0f}</td><td>{v["ucat"] or "—"}</td></tr>'
            for k, v in sorted(cities.items(),
                               key=lambda kv: -(kv[1]["hail"] + kv[1]["uphl"])))
        hail_html = (
            '<div class="card"><h2>🧊 Hail &amp; 🌪️ tornado-rotation forecast - next 8 hours (HRRR)</h2>'
            '<div class="kpis">'
            f'<div class="kpi"><span>East TN peak hail</span>{pk_line}</div>'
            f'<div class="kpi"><span>Peak rotation (tornado proxy)</span>{pr_line}</div>'
            f'<div class="kpi"><span>Model run</span><b>{hail.get("cycle", "-")}</b></div></div>'
            '<div class="ltg-row">'
            '<table class="minitable" style="min-width:430px"><tr><th>Valid (ET)</th><th>Hail max</th><th>Size class</th><th>UPHL</th><th>Rotation</th></tr>'
            + rows + '</table>'
            + (f'<table class="minitable" style="min-width:300px"><tr><th>City</th><th>Hail</th><th>Class</th><th>UPHL</th><th>Rotation</th></tr>'
               + city_rows + '</table>' if city_rows else
               '<div class="src">No hail or rotating storms forecast at any East TN city today.</div>')
            + '</div>'
            '<div class="src">HRRR HAIL (max hail diameter, mm) + UPHL (2-5 km updraft helicity, m²/s² - the standard HRRR tornado proxy; 130+ = mesocyclone-strength rotation, 250+ = tornado threat signal). Size classes: small &lt;19 · 3/4-1 in · 1-1.75 in (severe) · 1.75-2.75 in · 2.75+ in. Rotation: weak &lt;75 · rotation &lt;130 · strong 130-250 · TORNADO THREAT 250+. Updates every model cycle.</div></div>')
    else:
        hail_html = ('<div class="card"><h2>🧊 Hail &amp; 🌪️ tornado-rotation forecast</h2>'
                     '<div class="src">Forecast temporarily unavailable - the next model cycle will fill it in.</div></div>')

    ltg = sev.get("ltgHistory") or {}
    ltg_cells = [c for c in (ltg.get("cells") or []) if c.get("peak", 0) > 0]
    ltg_cells = ltg_cells[:8]
    ltg_tot = ltg.get("totals") or {}

    def _ltg_trend(t):
        if t >= 1.4: return ("⚡ Building", "#ff1744")
        if t >= 0.75: return ("Steady", "#ffd54f")
        return ("Fading", "#81c784")

    def _spark(hist, peak):
        if not hist or peak <= 0: return ""
        bars = "".join(f'<i style="height:{max(6, int(100 * v / peak))}%"></i>' for v in hist)
        return f'<div class="ltg-bars">{bars}</div>'

    ltg_cell_html = "".join(
        f'<div class="ltg-cell"><b>{_ltg_trend(c["trend"])[0]}</b>'
        f'<span class="h">{c["now"]:,} det. · peak {c["peak"]:,} · trend {c["trend"]:,.2f}×</span>'
        f'{_spark(c.get("history"), c["peak"])}</div>'
        for c in ltg_cells)
    if ltg_cells:
        ltg_html = (f'<div class="card"><h2>⚡ Lightning activity - last hour</h2>'
                    f'<div class="kpis">'
                    f'<div class="kpi"><span>Detections in latest scan</span><b style="color:#ffeb3b">{ltg_tot.get("latest", 0):,}</b></div>'
                    f'<div class="kpi"><span>Hour peak</span><b style="color:#ffb74d">{ltg_tot.get("peak", 0):,}</b></div>'
                    f'<div class="kpi"><span>Storms tracked</span><b style="color:#4da3ff">{ltg_tot.get("storms", 0)}</b></div>'
                    f'</div>'
                    f'<div class="ltg-row">{ltg_cell_html}</div>'
                    f'<div class="src">GOES-19 GLM flash density · 5-minute scans · bar strip = flashes per scan over the last hour · as of {(ltg.get("asOf") or "-")}</div></div>')
    else:
        ltg_html = ('<div class="card"><h2>⚡ Lightning activity - last hour</h2>'
                    '<div class="src">No lightning detected in the last hour of GOES-19 GLM scans.</div></div>')

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
    <span><i style="background:#ff5722"></i>SPC MCD (active)</span>
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
    <label><input type="checkbox" id="ly_mcd" checked/> SPC MCDs</label>
    <label><input type="checkbox" id="ly_fc" checked/> HRRR forecast maps</label>
    <select id="fcKind" title="Forecast hazard layer">
      <option value="severe">Severe chance (combined)</option>
      <option value="hail">Hail forecast</option>
      <option value="tornado">Tornado rotation (UPHL)</option>
    </select>
    <select id="fcHour"></select>
  </div>
  <div class="src">{len(ww)} warning polygons · {len(outlooks)} outlook areas · {len(tn)} TN alerts · HRRR forecast maps: hail size classes · UPHL rotation · combined severe chance · basemap {'Mapbox' if _MAPBOX_TOKEN else 'OpenStreetMap'}</div>
</div>

<div class="card"><h2>📊 Storm reports today (SPC)</h2>{rep_html}</div>
{hail_html}
{ltg_html}
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
function fmtAlertET(iso) {{
  /* ISO UTC alert expiry -> visitor-local '9/11, 2:00 PM' (labeled ET at home) */
  try {{
    const dte = new Date(iso);
    if (isNaN(dte)) return iso;
    return dte.toLocaleString([], {{ month: "numeric", day: "numeric",
      hour: "numeric", minute: "2-digit" }});
  }} catch (e) {{ return iso; }}
}}
function warnPopup(f) {{
  const p = f.properties || {{}};
  const torTag = p.tor === "POSSIBLE" ? " · <b style='color:#ff1744'>TORNADO POSSIBLE</b>"
               : p.tor ? " · <b style='color:#ff1744'>TORNADO " + p.tor + "</b>" : "";
  return "<b>" + (p.event || "Alert") + "</b>" + torTag
    + (p.severity ? "<br/><span class=src>Severity: " + p.severity + "</span>" : "")
    + (p.areaDesc ? "<br/>" + p.areaDesc : "")
    + (p.headline ? "<br/><span class=src>" + p.headline + "</span>" : "")
    + (p.expires ? "<br/><span class=src>Until " + fmtAlertET(p.expires) + "</span>" : "")
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
function mdPopup(f) {{
  return "<b>SPC Mesoscale Discussion " + f.num + "</b>" + (f.current ? " · <b style='color:#ff5722'>ACTIVE</b>" : " (expired)")
    + (f.concerning ? "<br/>" + f.concerning : "")
    + (f.areas ? "<br/>" + f.areas : "")
    + (f.validStart && f.validStart !== "-" ? "<br/><span class=src>Valid " + f.validStart + " - " + f.validEnd + "</span>" : "")
    + (f.prob != null ? "<br/><span class=src>Probability of watch issuance: " + f.prob + "%</span>" : "")
    + (f.summary ? "<br/><span class=src>" + f.summary + "</span>" : "")
    + (f.url ? "<br/><a href='" + f.url + "' target='_blank'>Full product (SPC) ↗</a>" : "");
}}
function buildMcdLayer(S) {{
  if (layers.mcd) map.removeLayer(layers.mcd);
  layers.mcd = L.layerGroup((S.md || []).map(f => {{
    const ring = f.geometry.coordinates[0].map(c => [c[1], c[0]]);
    return L.polygon(ring, {{ color: f.current ? "#ff5722" : "#b0bec5", weight: 2, dashArray: "6 4",
        fillColor: f.current ? "#ff5722" : "#90a4ae", fillOpacity: f.current ? 0.18 : 0.08 }})
      .bindTooltip("<b>SPC MD " + f.num + (f.current ? " (active)" : "") + "</b><br/>" + (f.concerning || ""), {{ sticky: true }})
      .bindPopup(mdPopup(f));
  }}));
  if (document.getElementById("ly_mcd").checked) layers.mcd.addTo(map);
}}
function toggle(id, lyr) {{
  const on = document.getElementById(id).checked;
  if (on && !map.hasLayer(lyr)) lyr.addTo(map);
  if (!on && map.hasLayer(lyr)) map.removeLayer(lyr);
}}
async function boot() {{
  DATA = await (await fetch(dataUrl(), {{cache: "no-store"}})).json();
  const S = DATA.severe || SEV;
  document.title = DATA.pageName + " - Severe";
  {_mapbox_token_js()}
  map = L.map("map", {{ zoomSnap: 0.5, maxZoom: 21 }}).setView([DATA.lat, DATA.lon], 6);
  addMapControls(map, [DATA.lat, DATA.lon], 6);
  L.circleMarker([DATA.lat, DATA.lon], {{ radius: 6, color: "#fff", weight: 2, fillColor: "#4da3ff", fillOpacity: 1 }}).addTo(map).bindTooltip(DATA.place);
  layers.ww = L.geoJSON({{ type: "FeatureCollection", features: (S.warnings || []).map(w => ({{ type: "Feature", properties: w, geometry: w.geometry }})) }},
    {{ style: styleWW, onEachFeature: (f, l) => {{ l.bindPopup(warnPopup(f)); l.bindTooltip((f.properties.event || "") + (f.properties.tor ? " 🌪" : "") + "<br/>" + (f.properties.areaDesc || ""), {{ sticky: true }}); }} }});
  buildSpcLayer(S);
  buildMcdLayer(S);
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
  toggle("ly_mcd", layers.mcd);
  for (const id of ["ly_ww", "ly_spc", "ly_reports", "ly_cells", "ly_mcd"])
    document.getElementById(id).onchange = () => toggle(id, layers[id]);
  /* ---- HRRR forecast map overlays (hail / tornado-rotation / severe chance) ---- */
  const FC = (S.maps && S.maps.frames) || null;
  const fcHour = document.getElementById("fcHour");
  let fcLayer = null;
  function fcFillHours() {{
    const kind = document.getElementById("fcKind").value;
    const frames = (FC && FC[kind]) || [];
    fcHour.innerHTML = frames.map(f => `<option value="${{f.id}}">${{f.label}}</option>`).join("");
    if (frames.length) fcHour.value = frames[frames.length - 1].id;   // newest hour
  }}
  function fcShow() {{
    if (fcLayer) {{ map.removeLayer(fcLayer); fcLayer = null; }}
    const kind = document.getElementById("fcKind").value;
    const frames = (FC && FC[kind]) || [];
    const f = frames.find(x => x.id === fcHour.value);
    if (!f || !f.pngUrl || !f.bounds) return;
    fcLayer = L.imageOverlay(f.pngUrl, [[f.bounds[0], f.bounds[1]], [f.bounds[2], f.bounds[3]]],
      {{ opacity: .75, interactive: false }});
    if (document.getElementById("ly_fc").checked) fcLayer.addTo(map);
  }}
  if (FC) {{
    fcFillHours();
    document.getElementById("fcKind").onchange = () => {{ fcFillHours(); fcShow(); }};
    fcHour.onchange = fcShow;
    document.getElementById("ly_fc").onchange = fcShow;
    fcShow();     /* checkbox is off by default - show() respects that */
  }} else {{
    document.getElementById("fcKind").disabled = true;
    fcHour.disabled = true;
  }}
}}
boot();
</script>
"""
    return _page("Severe", "severe.html", body)


def page_winter(d):
    """Winter weather: HRRR snow/ice overlays, multi-model snowfall maps,
    WPC winter desks, CPC extended outlooks, winter alerts."""
    wnt = d.get("winter") or {}
    frames = wnt.get("frames") or {}
    alerts = wnt.get("alerts") or {}
    wpc = wnt.get("wpc") or []
    cpc = wnt.get("cpc") or []
    msnow = wnt.get("modelSnow") or {}

    us_n = alerts.get("usCount", 0)
    if us_n:
        a_color, a_word = "#ff9f43", f"{us_n} active winter alert{'s' if us_n != 1 else ''} nationwide"
    else:
        a_color, a_word = "#81c784", "No active winter alerts nationwide right now"
    tn_rows = "".join(
        f'<tr><td><b style="color:#4da3ff">{html.escape(a.get("event") or "")}</b></td>'
        f'<td>{html.escape(a.get("area") or "")}</td>'
        f'<td>{html.escape(a.get("expires") or "")}</td></tr>'
        for a in (alerts.get("tn") or []))
    tn_html = (f'<table class="minitable" style="min-width:420px">'
               '<tr><th>Alert</th><th>Area</th><th>Expires (ET)</th></tr>' + tn_rows + '</table>'
               if tn_rows else
               '<div class="alert ok">No winter alerts for Tennessee - exactly what a quiet day looks like.</div>')

    wpc_tiles = "".join(
        f'<figure class="wpcfig"><img loading="lazy" src="../winter/wpc/{p["file"]}" alt="{html.escape(p["label"])}"/'
        f'<figcaption>{html.escape(p["label"])}</figcaption></figure>'
        for p in wpc)

    cpc_tiles = "".join(
        f'<figure class="wpcfig"><img loading="lazy" src="../winter/cpc/{p["file"]}" alt="{html.escape(p["label"])}"/'
        f'<figcaption>{html.escape(p["label"])}</figcaption></figure>'
        for p in cpc)

    body = f"""
<header class="hero"><h1>❄️ Winter Weather</h1>
<div class="sub">HRRR snow &amp; ice forecast maps · WPC Winter Weather Desk · winter alerts - updated {d["generated"]}</div></header>

<div class="card">
  <div class="kpis">
    <div class="kpi"><span>Nationwide winter alerts</span><b style="color:{a_color}">{a_word}</b></div>
    <div class="kpi"><span>HRRR cycle</span><b>{wnt.get("cycle") or "-"}</b></div>
  </div>
  <div id="map" class="map-dark" style="height:460px"></div>
  <div class="legend">
    <span><i style="background:#add8e6"></i>0.1-1&quot;</span>
    <span><i style="background:#78bee6"></i>1-2&quot;</span>
    <span><i style="background:#5096e6"></i>2-4&quot;</span>
    <span><i style="background:#3c6ed7"></i>4-6&quot;</span>
    <span><i style="background:#783cc8"></i>6-12&quot;</span>
    <span><i style="background:#e63ce6"></i>12&quot;+</span>
    <span><i style="background:#ffb3c8"></i>ice 0.1-0.25&quot;</span>
    <span><i style="background:#ff3c64"></i>ice 0.25&quot;+</span>
  </div>
  <div class="ctl" style="margin-top:12px">
    <label><input type="checkbox" id="ly_fc" checked/> HRRR forecast map</label>
    <select id="fcKind" title="Winter hazard">
      <option value="snow">Snowfall accumulation</option>
      <option value="ice">Freezing rain accumulation</option>
    </select>
    <select id="fcHour"></select>
  </div>
  <div class="ctl" style="margin-top:8px">
    <label><input type="checkbox" id="ly_ms"/> Model snowfall</label>
    <select id="msModel" title="Forecast model"></select>
    <select id="msHour" title="Forecast hour"></select>
    <span class="src" id="msCycle"></span>
  </div>
  <div class="src">HRRR {wnt.get("cycle") or ""} - accumulated snowfall (ASNOW) and freezing rain (FROZR) through each window, decoded from NOAA's open-data bucket and rendered here. On warm days the maps are honestly empty; the first cold storm paints them automatically.</div>
</div>

<div class="card"><h2>🌨️ Model snowfall forecasts - every model with a snow field</h2>
<div class="src">Same maps the Models tab renders, overlaid on the winter map: <b>GFS</b> (day 1-10 accumulated snowfall), <b>GEFS ensemble mean + spread</b> (spread = where runs disagree - wide spread means low forecast confidence), and the <b>NBM 6-hour blend</b>. Tick <b>Model snowfall</b>, pick a model and hour. The rotation keeps these current every model cycle; hours appear as they render.</div>
</div>

<div class="card"><h2>⚠️ Winter alerts</h2>
<div class="kpis"><div class="kpi"><span>Tennessee</span><b>{len(alerts.get("tn") or [])} alert(s)</b></div></div>
{tn_html}
</div>

<div class="card"><h2>🗺️ WPC Winter Weather Desk (national)</h2>
<div class="src">Weather Prediction Center winter desks run twice daily in the cold season; probabilities are for at least the amount shown through each day.</div>
<div class="ltg-row">{wpc_tiles}</div>
</div>

<div class="card"><h2>📅 Weeks 2-4: CPC extended outlooks</h2>
<div class="src">Climate Prediction Center 6-10 and 8-14 day outlooks - the standard extended-range winter guidance. Below-normal temperatures (blues) + a wet signal = the pattern that produces Tennessee Valley snow.</div>
<div class="ltg-row">{cpc_tiles}</div>
</div>

<script>
const WNT = {json.dumps(wnt)};
let map, wLayer = null;
async function boot() {{
  DATA = await (await fetch(dataUrl(), {{cache: "no-store"}})).json();
  const W = (DATA.winter || WNT);
  document.title = DATA.pageName + " - Winter";
  {_mapbox_token_js()}
  map = L.map("map", {{ zoomSnap: 0.5, maxZoom: 21 }}).setView([DATA.lat, DATA.lon], 5);
  addMapControls(map, [DATA.lat, DATA.lon], 5);
  L.circleMarker([DATA.lat, DATA.lon], {{ radius: 6, color: "#fff", weight: 2, fillColor: "#4da3ff", fillOpacity: 1 }}).addTo(map).bindTooltip(DATA.place);
  const fcKind = document.getElementById("fcKind"), fcHour = document.getElementById("fcHour");
  function fillHours() {{
    const frames = ((W.frames || {{}})[fcKind.value]) || [];
    fcHour.innerHTML = frames.map(f => `<option value="${{f.id}}">${{f.label}}</option>`).join("");
  }}
  function show() {{
    if (wLayer) {{ map.removeLayer(wLayer); wLayer = null; }}
    const frames = ((W.frames || {{}})[fcKind.value]) || [];
    const f = frames.find(x => x.id === fcHour.value);
    if (!f || !f.pngUrl || !f.bounds) return;
    wLayer = L.imageOverlay(f.pngUrl, [[f.bounds[0], f.bounds[1]], [f.bounds[2], f.bounds[3]]],
      {{ opacity: .8, interactive: false }});
    if (document.getElementById("ly_fc").checked) wLayer.addTo(map);
  }}
  fillHours();
  fcKind.onchange = () => {{ fillHours(); show(); }};
  fcHour.onchange = show;
  document.getElementById("ly_fc").onchange = show;
  show();
  // ---- multi-model snowfall overlays (GFS / GEFS / GEFS-Spread / NBM) ----
  const MS = W.modelSnow || {{}};
  const msModel = document.getElementById("msModel");
  const msHour = document.getElementById("msHour");
  const msCycle = document.getElementById("msCycle");
  let msLayer = null;
  function msModels() {{
    msModel.innerHTML = Object.keys(MS).map(k =>
      `<option value="${{k}}">${{MS[k].label}} (${{MS[k].frames.length}})</option>`).join("");
  }}
  function msHours() {{
    const m = MS[msModel.value];
    msHour.innerHTML = (m ? m.frames : []).map(f =>
      `<option value="${{f.id}}">${{f.label}}</option>`).join("");
    msCycle.textContent = m ? ("cycle " + m.cycle.slice(6, 8) + "/"
      + m.cycle.slice(8, 10) + "Z") : "";
  }}
  function msShow() {{
    if (msLayer) {{ map.removeLayer(msLayer); msLayer = null; }}
    const m = MS[msModel.value];
    const f = m && (m.frames.find(x => x.id === msHour.value));
    if (!f || !f.bounds) return;
    msLayer = L.imageOverlay(f.pngUrl, [[f.bounds[0], f.bounds[1]], [f.bounds[2], f.bounds[3]]],
      {{ opacity: .82, interactive: false }});
    if (document.getElementById("ly_ms").checked) msLayer.addTo(map);
  }}
  if (Object.keys(MS).length) {{
    msModels(); msHours();
    msModel.onchange = () => {{ msHours(); msShow(); }};
    msHour.onchange = msShow;
    document.getElementById("ly_ms").onchange = msShow;
  }} else {{
    const row = document.getElementById("msModel").closest(".ctl");
    if (row) row.style.display = "none";
  }}
}}
boot();
</script>
"""
    return _page("Winter", "winter.html", body)


def page_dashboard(d):
    """Marine-style dashboard (LakeErieWX model): station panels with current
    readings big + 24 h temp sparkline, river gauge panels with the official
    NWPS hydrograph embedded, KPI strip on top."""
    db = d.get("dashboard") or {}
    stations = db.get("stations") or []
    gauges = db.get("gauges") or []
    kpi = db.get("kpi") or {}
    kpis = [
        ("Stations reporting", str(kpi.get("stations") or len(stations)), None),
        ("Warmest right now", (f"{kpi['tempMax']}\u00b0F" if kpi.get("tempMax") is not None else "-"), "#ef6c00"),
        ("Coolest right now", (f"{kpi['tempMin']}\u00b0F" if kpi.get("tempMin") is not None else "-"), "#0288d1"),
        ("Peak gust", (f"{kpi['gustMax']} mph" if kpi.get("gustMax") else "calm"), None),
        ("Rivers in flood", str(kpi.get("riversInFlood") or 0),
         "#d32f2f" if kpi.get("riversInFlood") else "#43a047"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><span>{lbl}</span>'
        f'<b style="{f"color:{color}" if color else ""}">{val}</b></div>'
        for lbl, val, color in kpis)
    body = f"""
<header class="hero"><h1>🖥️ Live Dashboard</h1>
<div class="sub">Marine-style condition boards - every panel shows current readings with a 24 h trend \u00b7 updated {d["generated"]}</div></header>

<div class="card"><div class="kpis">{kpi_html}</div></div>

<div class="card"><h2>🌦️ Station panels - current + 24 h trend</h2>
<div class="src">Live observations from the NWS station network (ASOS/AWOS). The bar under each reading is the last 24 hours of temperature - watch it climb or fall through the day.</div>
<div id="stGrid" class="dash-grid"></div>
</div>

<div class="card"><h2>📏 River gauge panels - live stage + official hydrograph</h2>
<div class="src">Stage from the NWS water prediction service; each chart is the OFFICIAL NWPS hydrograph (click through for the full one).</div>
<div id="rvGrid" class="dash-grid"></div>
</div>

<style>
.dash-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(310px,1fr)); gap:14px; }}
.dpanel {{ background:#12161d; border:1px solid #232b37; border-radius:10px; padding:12px 14px; }}
.dpanel .pname {{ font-weight:700; margin-bottom:6px; display:flex; justify-content:space-between; align-items:baseline; gap:8px; }}
.dpanel .ptime {{ font-size:.72rem; color:#8b97a5; font-weight:400; white-space:nowrap; }}
.dpanel .big {{ font-size:2rem; font-weight:800; line-height:1.1; }}
.dpanel .unit {{ font-size:.9rem; color:#8b97a5; font-weight:600; }}
.dpanel .row {{ display:flex; gap:14px; flex-wrap:wrap; margin-top:6px; font-size:.85rem; }}
.dpanel .row b {{ font-size:.95rem; }}
.dpanel .spark {{ margin-top:8px; width:100%; height:44px; }}
.dpanel .dtag {{ display:inline-block; padding:1px 8px; border-radius:4px; font-size:.72rem; font-weight:700; color:#fff; }}
.dpanel img.hydro {{ width:100%; border-radius:6px; margin-top:8px; background:#fff; }}
.dpanel .quiet {{ color:#8b97a5; font-size:.8rem; }}
</style>

<script>
const DST = {json.dumps(stations)};
const DRV = {json.dumps(gauges)};
function spark(el, hist) {{
  // inline SVG 24 h temperature sparkline
  if (!hist || hist.length < 2) return;
  const vs = hist.map(h => h[1]);
  const lo = Math.min(...vs), hi = Math.max(...vs), span = Math.max(1, hi - lo);
  const W = 280, H = 40, P = 3;
  const pts = hist.map((h, i) =>
    [P + i * (W - 2 * P) / (hist.length - 1), H - P - (h[1] - lo) * (H - 2 * P) / span]);
  const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
  el.innerHTML = `<svg viewBox="0 0 ${{W}} ${{H}}" preserveAspectRatio="none" width="100%" height="44">`
    + `<path d="${{d}}" fill="none" stroke="#ffb74d" stroke-width="2"/>`
    + `<text x="2" y="12" font-size="10" fill="#8b97a5">24h ${{lo}}\u00b0-${{hi}}\u00b0F</text></svg>`;
}}
function fmtTime(t) {{
  if (!t) return "";
  try {{ return new Date(t).toLocaleTimeString("en-US", {{hour: "numeric", minute: "2-digit", timeZone: "America/New_York"}}); }}
  catch (_e) {{ return t; }}
}}
function stPanel(s) {{
  const gust = s.gustMph ? `<span>gusting <b>${{s.gustMph}}</b> mph</span>` : "";
  return `<div class="dpanel">
    <div class="pname"><span>${{s.name}}</span><span class="ptime">${{fmtTime(s.time)}} ET</span></div>
    <div><span class="big">${{s.tempF ?? "-"}}</span><span class="unit">\u00b0F</span>
      <span class="dtag" style="background:#37474f">${{s.desc || "-"}}</span></div>
    <div class="row">
      <span>dew <b>${{s.dewF ?? "-"}}\u00b0</b></span>
      <span>rh <b>${{s.rh ?? "-"}}%</b></span>
      <span>wind <b>${{s.windDir ?? "-"}} ${{s.windMph ?? 0}}</b> mph ${{gust}}</span>
      <span>pres <b>${{s.pressureHg ?? "-"}}</b> in</span>
      <span>vis <b>${{s.visMi ?? "-"}}</b> mi</span>
    </div>
    <div class="spark"></div>
  </div>`;
}}
function rvPanel(g) {{
  const thr = g.thresholds || {{}};
  const floodAt = thr.minor || thr.action || thr.moderate || thr.major;
  const rel = (floodAt && g.stage != null) ? ` <span class="quiet">(${{(floodAt - g.stage).toFixed(1)}} ft to flood stage)</span>` : "";
  return `<div class="dpanel">
    <div class="pname"><span>${{g.name}}</span><span class="ptime">${{g.river}}</span></div>
    <div><span class="big">${{g.stage ?? "-"}}</span><span class="unit">${{g.stageUnit || "ft"}} stage</span>
      <span class="dtag" style="background:${{g.catColor || "#666"}}">${{g.catWord || ""}}</span>${{rel}}</div>
    <div class="row"><span>flow <b>${{g.flow ?? "-"}}</b> ${{g.flowUnit || ""}}</span>
      <span>obs <b>${{fmtTime(g.obsTime)}}</b></span></div>
    <a href="${{g.url}}" target="_blank" rel="noopener"><img class="hydro" src="${{g.chart}}" alt="hydrograph ${{g.lid}}" loading="lazy"></a>
  </div>`;
}}
function build() {{
  const sg = document.getElementById("stGrid");
  sg.innerHTML = DST.length ? DST.map(stPanel).join("") : '<span class=src>No station data right now.</span>';
  DST.forEach((s, i) => spark(sg.children[i].querySelector(".spark"), s.history));
  document.getElementById("rvGrid").innerHTML =
    DRV.length ? DRV.map(rvPanel).join("") : '<span class=src>No gauge data right now.</span>';
}}
build();
</script>
"""
    return _page("Dashboard", "dashboard.html", body)


def page_rivers(d):
    """River gauges: NWS NWPS (AHPS) stages, flood status, map + table."""
    rv = d.get("rivers") or {}
    gauges = rv.get("gauges") or []
    counts = rv.get("counts") or {}
    flood_n = rv.get("floodCount") or 0
    if flood_n:
        f_color, f_word = ("#d32f2f",
                           f"{flood_n} river{'s' if flood_n != 1 else ''} IN FLOOD right now")
    else:
        f_color, f_word = "#43a047", "No flooding on any monitored river"
    gauges_js = json.dumps(gauges)
    body = f"""
<header class="hero"><h1>🌊 Rivers &amp; Flooding</h1>
<div class="sub">NWS Northwest River Prediction Center gauges - stage, flood category and forecasts · updated {d["generated"]}</div></header>

<div class="card">
  <div class="kpis">
    <div class="kpi"><span>Status</span><b style="color:{f_color}">{f_word}</b></div>
    <div class="kpi"><span>Gauges reporting</span><b>{len(gauges)}</b></div>
    <div class="kpi"><span>Categories</span><b>{" \u00b7 ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:4])}</b></div>
  </div>
  <div id="map" class="map-dark" style="height:470px"></div>
  <div class="legend">
    <span><i style="background:#d32f2f"></i>major</span>
    <span><i style="background:#ef6c00"></i>moderate</span>
    <span><i style="background:#f9a825"></i>minor</span>
    <span><i style="background:#0288d1"></i>near stage</span>
    <span><i style="background:#43a047"></i>no flooding</span>
    <span><i style="background:#9e9e9e"></i>no data</span>
  </div>
  <div class="src">Nolichucky · French Broad · Holston · Watauga · Doe · Pigeon · Little Pigeon · Clinch · Powell · Hiwassee · Ocoee · Obed · Cumberland · Stones · Harpeth · Duck · Elk · Buffalo and more - {len(gauges)} gauges across Tennessee. Click a dot for stage vs flood level + the official hydrograph link.</div>
</div>

<div class="card"><h2>📏 All gauges - worst first</h2>
<div class="ctl"><label>Filter river: <select id="riverSel"><option value="">All rivers</option></select></label></div>
<div id="tbl"></div>
</div>

<script>
const GAUGES = {gauges_js};
async function boot() {{
  DATA = await (await fetch(dataUrl(), {{cache: "no-store"}})).json();
  document.title = DATA.pageName + " - Rivers";
  {_mapbox_token_js()}
  map = L.map("map", {{ zoomSnap: 0.5, maxZoom: 21 }}).setView([35.9, -84.2], 6);
  addMapControls(map, [35.9, -84.2], 6);
  L.circleMarker([DATA.lat, DATA.lon], {{ radius: 6, color: "#fff", weight: 2, fillColor: "#4da3ff", fillOpacity: 1 }}).addTo(map).bindTooltip(DATA.place);
  const groups = {{}};
  GAUGES.forEach(g => {{
    if (g.lat == null) return;
    const m = L.circleMarker([g.lat, g.lon], {{ radius: 6.5, color: "#1b2027", weight: 1.5,
      fillColor: g.catColor || "#9e9e9e", fillOpacity: .95 }}).addTo(map);
    m.bindPopup(`<b>${{g.name}}</b><br/>Stage: <b>${{g.stage ?? "?"}} ${{g.stageUnit || "ft"}}</b> - ${{g.catWord || ""}}<br/>` +
      (g.fcstStage != null ? `Forecast: ${{g.fcstStage}} ${{g.stageUnit || "ft"}}<br/>` : "") +
      `<a href="${{g.url}}" target="_blank" rel="noopener">Official hydrograph (NWPS)</a>`);
    (groups[g.group] = groups[g.group] || []).push(g);
  }});
  // river filter + table
  const sel = document.getElementById("riverSel");
  Object.keys(groups).sort().forEach(r => {{
    const o = document.createElement("option"); o.value = r; o.textContent = `${{r}} (${{groups[r].length}})`;
    sel.appendChild(o);
  }});
  function table() {{
    const rows = GAUGES.filter(g => !sel.value || g.group === sel.value).map(g =>
      `<tr><td><span style="display:inline-block;width:11px;height:11px;border-radius:3px;background:${{g.catColor}}"></span></td>` +
      `<td><b>${{g.group}}</b></td><td>${{g.name.replace(", TN", "")}}</td>` +
      `<td><b>${{g.stage ?? "?"}}</b> ${{g.stageUnit || "ft"}}</td>` +
      `<td style="color:${{g.catColor}}"><b>${{g.catWord}}</b></td>` +
      (g.fcstStage != null ? `<td>fcst ${{g.fcstStage}} ft</td>` : `<td>-</td>`) +
      `<td><a href="${{g.url}}" target="_blank" rel="noopener">hydrograph</a></td></tr>`).join("");
    document.getElementById("tbl").innerHTML = rows
      ? `<table><tr><th></th><th>River</th><th>Gauge</th><th>Stage</th><th>Status</th><th>Forecast</th><th></th></tr>${{rows}}</table>`
      : '<span class=src>No gauges in this filter right now.</span>';
  }}
  sel.onchange = table;
  table();
}}
boot();
function onDataRefresh(d) {{ /* statuses refresh with the page data */ }}
</script>
"""
    return _page("Rivers", "rivers.html", body)


def page_fire(d):
    """Fire weather: SPC fire outlooks, red-flag warnings, fire-danger HRRR fields."""
    try:
        from data.fire import fire_bundle
        fw = fire_bundle()
    except Exception:                              # noqa: BLE001
        fw = {}
    rfw = fw.get("redFlag") or {{}} if False else (fw.get("redFlag") or {})
    rfw_n = rfw.get("usCount") or 0
    rfw_rows = "".join(
        f'<tr><td><b style="color:#ff7043">{html.escape(a.get("event") or "Red Flag Warning")}</b></td>'
        f'<td>{html.escape(a.get("area") or "")}</td>'
        f'<td>{html.escape(a.get("expires") or "")}</td></tr>'
        for a in (rfw.get("sample") or []))
    rfw_html = (f'<table class="minitable">'
                '<tr><th>Alert</th><th>Area</th><th>Expires (ET)</th></tr>' + rfw_rows + '</table>'
                if rfw_rows else '<div class="alert ok">No Red Flag Warnings anywhere in the US right now.</div>')
    tn_fires = (fw.get("tnAlerts") or [])
    tn_html = ("".join(
        f'<tr><td><b style="color:#ff7043">{html.escape(a.get("event") or "")}</b></td>'
        f'<td>{html.escape(a.get("area") or "")}</td></tr>'
        for a in tn_fires) or "")
    tn_block = (f'<table class="minitable"><tr><th>Alert</th><th>Area</th></tr>{tn_html}</table>'
                if tn_html else
                '<div class="alert ok">No fire-related alerts for Tennessee.</div>')
    d1 = fw.get("day1Url") or ""
    d2 = fw.get("day2Url") or ""
    # ../fire/... form: the packager rewrites + copies it (bare "fire/..."
    # refs are invisible to its scanner - images 404'd, 2026-09-20)
    d1u = d1.replace("/app/static/", "../") if d1 else ""
    d2u = d2.replace("/app/static/", "../") if d2 else ""
    body = f"""
<header class="hero"><h1>🔥 Fire Weather</h1>
<div class="sub">SPC Fire Weather Outlooks · Red Flag Warnings · fire-danger forecasts · updated {d["generated"]}</div></header>

<div class="card">
  <div class="kpis">
    <div class="kpi"><span>Red Flag Warnings (US)</span><b style="color:{'#ff7043' if rfw_n else '#43a047'}">{rfw_n} active</b></div>
    <div class="kpi"><span>Tennessee fire alerts</span><b>{len(tn_fires)}</b></div>
  </div>
  <div class="src">SPC Fire Weather Outlooks highlight areas where critical fire-weather conditions (dry fuels + strong wind + low humidity) support dangerous fire spread. Day 1 = today through tonight; Day 2 = tomorrow.</div>
  <div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:10px">
    {f'<figure class="wpcfig"><img loading="lazy" src="{d1u}" alt="Day 1 fire outlook"/><figcaption>Day 1 Fire Weather Outlook (SPC)</figcaption></figure>' if d1u else ''}
    {f'<figure class="wpcfig"><img loading="lazy" src="{d2u}" alt="Day 2 fire outlook"/><figcaption>Day 2 Fire Weather Outlook (SPC)</figcaption></figure>' if d2u else ''}
  </div>
</div>

<div class="card"><h2>🚒 Fire alerts</h2>
<h3 style="margin:6px 0 4px">Tennessee</h3>
{tn_block}
<h3 style="margin:14px 0 4px">Nationwide Red Flag Warnings</h3>
{rfw_html}
</div>

<div class="src">Wildfire safety: never burn on dry, windy days - embers travel. If a wildfire threatens, follow Tennessee Division of Forestry and local emergency-management evacuation orders immediately.</div>
"""
    return _page("Fire", "fire.html", body)


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
  <div class="src">The <b>East Tennessee (zoom)</b> sector magnifies SPC's national analysis to a Greeneville-centered view of East TN and its surroundings (34.3-37.3N, 85.8-80.2W) — with real state borders (Tennessee in white) and city markers drawn on every frame, plus the same fields, overlays, and animation as the native sectors.</div>
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
let bootET = !!SEC.ET, userPicked = false;               // stale-HTML self-heal state
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
document.getElementById("fld").onchange = e => {{ userPicked = true; curFld = e.target.value; if (!baseFor(curSec)) curSec = "19"; document.getElementById("sec").value = curSec; tick(); }};
document.getElementById("sec").onchange = e => {{ userPicked = true; curSec = e.target.value; tick(); }};
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
async function pollData() {{
  try {{
    const d = await (await fetch(dataUrl(), {{cache: "no-store"}})).json();
    if (d.meso) {{
      MESO.analysis = d.meso.analysis;
      Object.assign(MESO.sectors || {{}}, d.meso.sectors || {{}});
      if (d.meso.sectorOrder) MESO.sectorOrder = d.meso.sectorOrder;
      if (d.meso.sectorNames) MESO.sectorNames = d.meso.sectorNames;
      if (d.meso.fieldGroups) MESO.fieldGroups = d.meso.fieldGroups;
      const ss = document.getElementById("sec");
      if (MESO.sectorOrder && ss && ss.options.length !== MESO.sectorOrder.length) fillPickers();
      if (!bootET && MESO.sectors.ET && !userPicked) {{ bootET = true; curSec = "ET"; document.getElementById("sec").value = "ET"; }}
      tick();
    }}
  }} catch (_e) {{}}
}}
pollData();
setInterval(pollData, 180000);
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
  DATA = await (await fetch(dataUrl(), {{cache: "no-store"}})).json();
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
            # full hour-by-hour table (ET labels from the payload's 'valid')
            trs = ""
            for r in rows:
                wdr = r.get("wdr")
                wdr_txt = f'{int(wdr):03d}°' if wdr is not None else "-"
                valid = r.get("valid") or ("F%03d" % (r.get("fhr") or 0))
                trs += (f'<tr><td>{html.escape(str(valid))}</td>'
                        f'<td><b>{r.get("tmp") if r.get("tmp") is not None else "-"}°F</b></td>'
                        f'<td>{r.get("dpt") if r.get("dpt") is not None else "-"}°F</td>'
                        f'<td>{r.get("pop") if r.get("pop") is not None else 0}%</td>'
                        f'<td>{r.get("sky") if r.get("sky") is not None else "-"}%</td>'
                        f'<td>{r.get("wsp") if r.get("wsp") is not None else "-"} mph {wdr_txt}</td></tr>')
            mos_bits += (f'<details class="card" style="padding:12px;margin-bottom:10px">'
                         f'<summary style="cursor:pointer;font-weight:700">📍 {html.escape(name)} '
                         f'({html.escape(m.get("model", "GFS LAMP"))}) - {len(rows)} hourly rows, '
                         f'init {html.escape(m.get("init", ""))}</summary>'
                         f'<div style="max-height:420px;overflow-y:auto">'
                         f'<table class="minitable" style="min-width:420px">'
                         f'<tr><th>Valid (ET)</th><th>Temp</th><th>Dew</th><th>PoP</th><th>Sky</th><th>Wind</th></tr>'
                         f'{trs}</table></div></details>')
        mos_html = mos_bits or '<div class="alert ok">No MOS guidance parsed yet.</div>'
        mos_note = ('<div class="src">Guidance source: GFS-LAMP / MAV MOS bulletins via the Iowa '
                    'Environmental Mesonet, with NWS NDFD hourly guidance (MOS-blend) as automatic '
                    'fallback — hourly out to +60 h for 7 stations, times in Eastern.</div>')
    else:
        mos_html = ('<div class="alert ok">MOS guidance is temporarily unavailable - both the '
                    'bulletin service (Iowa Environmental Mesonet) and the NDFD fallback are '
                    'unreachable right now. The charts on this page come straight from the NWS '
                    'forecast grid and stay live; guidance rows return automatically.</div>')
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
    sp = d.get("space") or {}
    kp_rows = sp.get("kp") or []
    kp_vals = [k.get("kp") for k in kp_rows[-16:] if isinstance(k.get("kp"), (int, float))]
    kp_n = len(kp_vals)
    kp_pts = "".join(
        f'<span title="Kp {v}" style="flex:1;height:{max(3, min(28, v * 3.2)):.0f}px;'
        f'background:{"#f9a825" if v >= 5 else "#0288d1"};border-radius:2px"></span>'
        for v in kp_vals)
    kp_now = sp.get("latestKp")
    kp_word = sp.get("kpWord") or "Space-weather data unavailable"
    kp_color = sp.get("kpColor") or "#9e9e9e"
    fcst = sp.get("forecast") or {}
    fcst_daily = fcst.get("dailyMax") or {}
    fcst_txt = " \u00b7 ".join(f"{k}: max Kp {v}" for k, v in list(fcst_daily.items())[:3])
    wpc_opts = "".join(f'<option value="{w["url"]}">{html.escape(w["title"])}</option>' for w in wpc)
    levels = sorted({u["level"] for u in upper})
    upper_opts = "".join(f'<option value="{html.escape(lv)}">{html.escape(lv)}</option>' for lv in levels)
    body = f"""
<header class="hero"><h1>🗺️ National products</h1>
<div class="sub">WPC operational charts + SPC observed upper-air analyses · updated {d["generated"]}</div></header>

<div class="card"><h2>🌌 Aurora & space weather (NOAA SWPC)</h2>
  <div class="kpis">
    <div class="kpi"><span>Current Kp index</span><b style="color:{kp_color}">{kp_now if kp_now is not None else "-"} - {kp_word}</b></div>
    <div class="kpi"><span>3-day forecast peak</span><b>{sp.get("forecastMaxKp") if sp.get("forecastMaxKp") is not None else "-"}</b></div>
  </div>
  {f'<div style="display:flex;align-items:flex-end;gap:2px;height:32px;margin:6px 0">{kp_pts}</div><div class="src">Kp, past 48 h (3-h values). Kp 7+ means the auroral oval can reach Tennessee&apos;s latitude - look north after dark.</div>' if kp_n else '<div class="src">Space-weather feed unavailable right now.</div>'}
  {f'<div class="src">SWPC 3-day outlook: {html.escape(fcst_txt or fcst.get("headline", ""))}</div>' if (fcst_txt or fcst.get("headline")) else ""}
  <figure style="margin:10px 0 0"><img loading="lazy" src="{(sp.get("ovationUrl") or "").replace("/app/static/", "")}" alt="OVATION aurora nowcast" style="max-width:100%;border-radius:10px"/>
  <figcaption class="src">OVATION aurora forecast (view from the north) - mirrored from SWPC each update.</figcaption></figure>
</div>

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


def _hi_cat_color(hi):
    """Heat-index risk color for the hourly curve chips."""
    from data.heatidx import _hi_cat
    return (_hi_cat(hi)[1] if hi is not None else "#777") or "#777"


def _wb_cat_color(wf):
    """WBGT risk color for the hourly curve chips."""
    from data.wbgt import _cat
    return (_cat(wf)[1] if wf is not None else "#777") or "#777"


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
        wb = cf.get("wbgtTomorrow") or {}
        wb_chip = (f' · <span style="color:{wb["color"]};font-weight:700" '
                   f'title="Tomorrow peak heat stress index">🔥 {wb["peakF"]}°F</span>' if wb else "")
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
            f'<span class="cfsum">{summary}{f" · high {hi}°F" if hi else ""}{wb_chip}</span><span class="chev">▾</span></button>'
            f'<div class="cfbody" id="cf{k}">{rows}'
            + (f'<div class="src" style="margin-top:6px">🔥 Tomorrow\'s peak heat stress: '
               f'<b style="color:{wb["color"]}">{wb["peakF"]}°F WBGT</b> ({html.escape(wb["cat"])}) '
               f'around {html.escape(wb["peakTime"])} ET</div>' if wb else "")
            + (f'<div class="src" style="margin-top:6px">{html.escape(det)}</div>' if det else "")
            + '</div></div>')
    city_html = ("".join(city_cards) or '<div class="src">City forecasts load on the next update cycle.</div>')
    # ---- Heat-Stress Index card (heat index + WBGT combined) ----
    hx = d.get("heatIndex") or {}
    hh = hx.get("home") or {}
    if hx.get("ok") and hh:
        curve = "".join(
            f'<div class="hr"><span>{p["t"]}</span><b style="color:{_hi_cat_color(p["hi"])}">'
            f'{p["hi"] if p["hi"] is not None else "-"}\u00b0F</b>'
            f'<span style="color:{_wb_cat_color(p["wbgt"])}">W {p["wbgt"]}</span></div>'
            for p in (hh.get("hours") or []))
        city_rows = "".join(
            f'<tr><td>{html.escape(c)}</td>'
            f'<td><b style="color:{v["peakHiColor"]}">{v["peakHi"]}\u00b0F</b></td>'
            f'<td>{html.escape(v["peakHiCat"])}</td>'
            f'<td><b style="color:{_wb_cat_color(v["peakWbgt"])}">{v["peakWbgt"]}\u00b0F</b></td></tr>'
            for c, v in sorted((hx.get("cities") or {}).items(),
                               key=lambda kv: -(kv[1].get("peakHi") or 0)))
        heat_card = (f'<div class="card"><h2>\U0001f321\ufe0f Heat-Stress Index \u2014 how hot it feels (next 24 h)</h2>'
                     f'<div class="kpis">'
                     f'<div class="kpi"><span>Feels like now (heat index)</span><b style="color:{hh.get("nowHiColor")}">{hh.get("nowHi")}\u00b0F</b>'
                     f'<span class="h">{html.escape(hh.get("nowHiCat") or "")} \u00b7 peak {hh.get("peakHi")}\u00b0F {html.escape(hh.get("peakHiTime") or "")}</span></div>'
                     f'<div class="kpi"><span>In the sun / exertion (WBGT)</span><b style="color:{hh.get("nowWbgtColor")}">{hh.get("nowWbgt")}\u00b0F</b>'
                     f'<span class="h">{html.escape(hh.get("nowWbgtCat") or "")} \u00b7 peak {hh.get("peakWbgt")}\u00b0F {html.escape(hh.get("peakWbgtTime") or "")}</span></div>'
                     f'</div>'
                     f'<div class="hourly">{curve}</div>'
                     f'<details style="margin-top:10px"><summary style="cursor:pointer">City peaks (heat index, next 24 h)</summary>'
                     f'<table class="cells" style="margin-top:8px"><tr><th>City</th><th>Peak feels-like</th><th>Risk</th><th>Peak WBGT</th></tr>{city_rows}</table></details>'
                     f'<div class="src">Heat index = shade comfort (NWS Rothfusz); WBGT = sun + exertion stress \u2014 at WBGT 90\u00b0F+ outdoor work/rest cycles become critical. '
                     f'Risk bands: caution 80\u00b0F \u00b7 extreme caution 90\u00b0F (HI) / 85\u00b0F (WBGT) \u00b7 danger 103\u00b0F (HI) / 90\u00b0F (WBGT) \u00b7 as of {html.escape(hx.get("fetched") or "-")}</div></div>')
    else:
        heat_card = ""
    # ---- WBGT heat map card ----
    wb = d.get("wbgt") or {}
    if wb.get("ok"):
        legend = "".join(f'<span><i style="background:{c}"></i>{html.escape(lbl)}</span>'
                         for c, lbl in wb.get("legend", []))
        wbgt_card = (f'<div class="card"><h2>🌡️ Heat stress - WBGT map (next 24 h)</h2>'
                     f'<div id="wbmap" class="map-dark" style="height:420px"></div>'
                     f'<div class="legend">{legend}</div>'
                     f'<div class="ctl" style="margin-top:10px">'
                     f'<label><input type="checkbox" id="ly_wb" checked/> WBGT forecast</label>'
                     f'<select id="wbScope"><option value="etn">East Tennessee</option><option value="us">United States</option></select>'
                     f'<select id="wbHour"></select></div>'
                     f'<div class="src">Wet-bulb globe temperature - the heat-stress index used by coaches and crews '
                     f'(sun-exposed estimate, NWS official forecast). Unpainted = below 82\u00b0F. '
                     f'At 90\u00b0F+ outdoor work/rest cycles and extra hydration become critical.</div></div>')
    else:
        wbgt_card = ""
    # ---- Forecaster's discussion (AFD) - the WHY behind the forecast ----
    afd = d.get("afd") or {}
    if afd.get("ok"):
        key_html = "".join(f"<li style='margin:4px 0'>{html.escape(m)}</li>"
                           for m in afd.get("keyMessages") or [])
        secs = "".join(
            f'<details style="margin:6px 0"><summary style="cursor:pointer">'
            f'{html.escape(s["title"])}</summary>'
            f'<pre style="white-space:pre-wrap;font-size:12.5px;color:#b8c4d4;margin:6px 0 0">'
            f'{html.escape(s["text"])}</pre></details>'
            for s in afd.get("sections") or [])
        afd_card = (
            f'<div class="card"><h2>🧑‍🔬 Forecaster\'s discussion - {html.escape(afd.get("officeName") or "NWS")}</h2>'
            f'<div class="src">Issued {html.escape(afd.get("issued") or "-")} · the meteorologists who write the local forecast explain their reasoning.</div>'
            + (f'<ul style="margin:10px 0 4px 20px;padding:0">{key_html}</ul>' if key_html else "")
            + secs
            + '<a class="src" href="https://www.weather.gov/mrx/" target="_blank" rel="noopener">Full product at NWS Morristown ↗</a></div>')
    else:
        afd_card = ""
    # ---- Today at a glance (computed from the 24 h hourly + sun card) ----
    sun = d.get("sun") or {}
    hours24 = d.get("hourly") or []
    peak_h = None
    rain_h = None
    for h in hours24:
        if peak_h is None or (h.get("temp") or 0) > (peak_h.get("temp") or 0):
            peak_h = h
        if rain_h is None and (h.get("pop") or 0) >= 40:
            rain_h = h
    glance = []
    if peak_h:
        glance.append(("🌡️ Peak temp", f'{peak_h["temp"]}°F around {peak_h["t"]}'))
    glance.append(("🌧️ First rain window",
                   (f'{rain_h["pop"]}% around {rain_h["t"]}' if rain_h else "None in 24 h")))
    day0 = (d.get("days") or [{}])[0]
    if day0.get("hi") is not None:
        glance.append(("📅 Today's high", f'{day0["hi"]}°F'))
    if sun.get("ok"):
        glance.append(("🌅 Sunrise", sun.get("sunrise") or "-"))
        glance.append(("🌇 Sunset", sun.get("sunset") or "-"))
    glance_html = "".join(
        f'<div class="kpi"><span>{lbl}</span><b>{val}</b></div>' for lbl, val in glance)
    glance_card = (f'<div class="card"><h2>🧭 Today at a glance</h2><div class="kpis">{glance_html}</div>'
                   f'<div class="src">Rain window = first hour at 40%+ chance; timing shifts - check the radar loop before heading out.</div></div>')
    # ---- Sun & moon card ----
    if sun.get("ok"):
        sun_card = (
            f'<div class="card"><h2>{sun.get("moonIcon") or "🌙"} Sun &amp; moon - {html.escape(d["place"])}</h2>'
            f'<div class="kpis">'
            f'<div class="kpi"><span>🌅 Sunrise</span><b>{sun.get("sunrise") or "-"}</b><span class="h">civil twilight {sun.get("civilBegin") or "-"}</span></div>'
            f'<div class="kpi"><span>🌇 Sunset</span><b>{sun.get("sunset") or "-"}</b><span class="h">civil twilight {sun.get("civilEnd") or "-"}</span></div>'
            f'<div class="kpi"><span>☀️ Day length</span><b>{sun.get("dayLength") or "-"}</b><span class="h">right now: {html.escape(sun.get("dayPhase") or "-")}</span></div>'
            f'<div class="kpi"><span>{sun.get("moonIcon") or "🌙"} Moon</span><b>{html.escape(sun.get("moonPhase") or "-")}</b><span class="h">illuminated ≈ {int(round((sun.get("moonFrac") or 0) * 100))}%</span></div>'
            f'</div>'
            f'<div class="src">Eastern times for {html.escape(d["place"])} (api.sunrise-sunset.org). Late September sheds ~2 min of daylight a day - the outdoor-work window shrinks with it.</div></div>')
    else:
        sun_card = ""
    body = f"""
<header class="hero"><h1>📋 Forecast & alerts</h1><div class="sub">{html.escape(d["place"])} + every East Tennessee city · National Weather Service · updated {d["generated"]}</div></header>
<div class="card"><h2>⚠️ Active alerts</h2><div class="alerts">{alerts_html}</div></div>
{glance_card}
{afd_card}
{heat_card}
{sun_card}
{wbgt_card}
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
<script>
/* WBGT heat-stress map (only when the payload has frames) */
(async () => {{
  const el = document.getElementById("wbmap");
  if (!el) return;
  const DATA = await (await fetch(dataUrl(), {{ cache: "no-store" }})).json();
  const wbSrc = () => (document.getElementById("wbScope").value === "us") ? (DATA.wbgtUs || {{}}) : (DATA.wbgt || {{}});
  const frames = wbSrc().frames || [];
  if (!((DATA.wbgt || {{}}).ok || (DATA.wbgtUs || {{}}).ok)) return;
  if (!frames.length) {{
    /* East TN ready but US still rendering: still show the ET map */
    if (!(DATA.wbgt || {{}}).ok) return;
  }}
  {_mapbox_token_js()}
  const wmap = L.map("wbmap", {{ zoomSnap: 0.5, maxZoom: 21 }}).setView([DATA.lat, DATA.lon], 6);
  addMapControls(wmap, [DATA.lat, DATA.lon], 6);
  const sel = document.getElementById("wbHour");
  const wbColor = v => v >= 93 ? "#c828c8" : v >= 90 ? "#eb3c3c" : v >= 88 ? "#ff783c"
                     : v >= 85 ? "#ffb242" : v >= 82 ? "#ffe066" : "#81c784";
  let wLayer = null, wMarks = null;
  function wbMarks(f) {{
    if (wMarks) {{ wmap.removeLayer(wMarks); wMarks = null; }}
    if (!f || !f.vals || !f.vals.length) return;
    wMarks = L.layerGroup();
    for (const p of f.vals) {{
      wMarks.addLayer(L.circleMarker([p[0], p[1]], {{ radius: 5, color: "#1b2027", weight: 1.5,
        fillColor: wbColor(p[2]), fillOpacity: .95 }}).bindTooltip(`WBGT ${{Math.round(p[2])}}\u00b0F`));
    }}
    wMarks.addTo(wmap);
  }}
  function wbShow() {{
    const fr = wbSrc().frames || [];
    if (!fr.length) return;
    const f = fr.find(x => x.id === sel.value) || fr[fr.length - 1];
    if (!f || !f.bounds) return;
    if (wLayer) {{ wmap.removeLayer(wLayer); wLayer = null; }}
    wLayer = L.imageOverlay(f.pngUrl, [[f.bounds[0], f.bounds[1]], [f.bounds[2], f.bounds[3]]],
      {{ opacity: .8, interactive: false }});
    if (document.getElementById("ly_wb").checked) {{ wLayer.addTo(wmap); wbMarks(f); }}
    else if (wMarks) {{ wmap.removeLayer(wMarks); wMarks = null; }}
  }}
  function wbFillSel() {{
    const fr = wbSrc().frames || [];
    const prev = sel.value;
    sel.innerHTML = fr.map(f => `<option value="${{f.id}}">${{f.label}}</option>`).join("");
    if (prev && fr.some(x => x.id === prev)) sel.value = prev;
  }}
  wbFillSel();
  sel.onchange = wbShow;
  document.getElementById("wbScope").onchange = () => {{ wbFillSel(); wbShow(); }};
  document.getElementById("ly_wb").onchange = wbShow;
  wbShow();
}})();
</script>
"""
    return _page("Forecast", "forecast.html", body)


# ---------------------------------------------------------------- build
_DISK_BUDGETS = {          # max bytes per cache dir (age prunes handle the rest)
    # GitHub holds every published byte (gh-pages branch, no history growth) -
    # local static/ is ONLY a serving cache for the local preview, so budgets
    # are kept lean (user request 2026-09-14: "save the maps to github not my
    # hard drive"). site_updater.prune_published_local() additionally deletes
    # local frames older than their display window after every confirmed push.
    "nexrad_sites": 40_000_000,
    "mrms": 25_000_000,
    "goes": 110_000_000,    # ALL ABI bands x last frames x ~600 KB (~100 MB);
    # a 25 MB cap made the sweeper delete frames minutes after the site build
    # snapshotted them -> packaging always found them gone -> satellite page
    # served 0 frames on every band (2026-09-17)
    "meso": 80_000_000,
    "hrrr": 25_000_000,
    "herbie": 15_000_000,    # GRIB download cache - re-downloadable, not served
    "soundings": 20_000_000,
    "aimodels": 40_000_000,   # MPAS+SHiELD official frames; fresh init every 6h
    "sevmaps": 12_000_000,    # HRRR hail/rotation/severe-chance forecast overlays
    "winter": 12_000_000,     # HRRR snow/ice forecast overlays + WPC graphics
    "wbgt": 4_000_000,        # WBGT heat-stress overlays (small PNGs, self-pruning)
    # model_maps MUST hold the FULL catalog (~440 combos x <=7 loop frames
    # each, palette-quantized ~90 KB avg): a budget under that creates a
    # treadmill - the rotation renders backlog maps, the budget deletes them
    # as "oldest" to fit the newest cycle, and the models page can never
    # fill in (RAP 38->2, GEFS 29->5 seen 2026-09-14). Loops raised the
    # ceiling 240->600 MB (2026-09-15); bounded, never grows past it.
    "model_maps": 600_000_000,
    "nws_radar": 20_000_000,
    "glm": 20_000_000,        # GOES GLM lightning density frames
    "tropics": 3_000_000,     # tropical guidance intensity charts (small PNGs)
    "climate": 8_000_000,     # CPC outlook GIF mirrors: a COMPLETE 8-map set
    # is ~3.6 MB (seasonal_temp alone is 2 MB); a 3 MB cap made the sweeper
    # delete half the set every cycle while the 1 h bundle cache kept the
    # stale refs - four permanently broken images on climate.html
    # (2026-09-15). 8 MB always fits the full set; mirrors refresh in place.
}


# mirror-style folders: a fixed set of images re-fetched only when the
# upstream graphic changes (never time-rotating). The budget sweeper must
# never delete from these - a deleted mirror breaks its page until CPC/NHC
# next republishes, and "oldest mtime" is exactly the file that is current.
_MIRROR_DIRS = {"climate", "tropics"}
# subfolders with the same mirror semantics, inside rotating budget dirs
_MIRROR_SUBDIRS = {"winter/wpc"}


def _current_payload_files():
    """Abs paths of every image the latest site-build snapshot references.

    The sweeper, the site build and the packager all run concurrently: the
    built data.json is the CURRENT served content until the next build
    replaces it, so any file in it must stay on disk even when a budget is
    exceeded (2026-09-17: a sweep seconds after a build deleted all 113
    satellite frames the build had just snapshotted -> the satellite page
    went dark until the next cycle). Keeping these only delays eviction of
    soon-dead files: the next build drops their refs once fresher frames
    exist, and then the budget applies as usual.
    """
    refs = set()
    try:
        with open(os.path.join("static", "site", "data.json"), encoding="utf-8") as f:
            snap = json.load(f)
    except Exception:                                  # noqa: BLE001
        return refs

    def _walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in _FRAME_REF_KEYS and isinstance(v, str) and not v.startswith("http"):
                    p = _resolve_static_ref(v)
                    if p:
                        refs.add(os.path.abspath(p))
                else:
                    _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)

    _walk(snap)
    return refs


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
    # frames the current served payload still points at are load-bearing
    payload_files = _current_payload_files()
    for sub, budget in _DISK_BUDGETS.items():
        root = os.path.join("static", sub)
        if not os.path.isdir(root):
            continue
        # mirror-style folders hold ONE fixed set of images that are
        # re-downloaded only when the source changes (CPC outlooks, tropical
        # intensity charts). Deleting any of them breaks the page until the
        # source itself updates - size-capping these is self-defeating.
        if sub in _MIRROR_DIRS:
            continue
        entries = []
        total = 0
        for dirpath, _, files in os.walk(root):
            # mirror subfolders (e.g. winter/wpc WPC graphics): fixed set,
            # re-fetched hourly - deleting them between refreshes breaks the page
            rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
            if any(rel_dir == sd or rel_dir.startswith(sd + "/") for sd in _MIRROR_SUBDIRS):
                continue
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
        now_s = time.time()
        for mtime, size, p in entries:
            if total <= budget:
                break
            # Freshness guard: never delete a frame younger than 15 min.
            # The site build snapshots frames into static/site/data.json and
            # the packager reads that file up to a publish interval later -
            # deleting in that window ships dead image links (MRMS frames
            # vanished minutes after download, 2026-09-16).
            if now_s - mtime < 900:
                continue
            # Payload guard: this file is part of what the site is serving
            # RIGHT NOW - a budget overrun is cheaper than a dark page.
            if os.path.abspath(p) in payload_files:
                continue
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


def _resolve_static_ref(ref):
    """Resolve a payload image ref to an existing file path, or None.

    Handles the three shapes the payload uses: static/-relative
    ("mrms/mrms_cref_...png"), repo-relative ("../static/...") and bare
    filenames ("day1_psnow_gt_04.gif", which live in a static subfolder).
    Exact-path matches only for foldered refs - no basename guessing there.
    """
    rel = (ref.replace("/app/static/", "static/")
              .replace("../", "static/").replace("\\", "/").lstrip("/"))
    if os.path.isfile(rel):
        return rel
    # foldered refs are normally static/-relative ("hrrr/x.png" lives in
    # static/hrrr/) - try that before giving up (future-radar frames were
    # all stripped as "dead" without this, 2026-09-18)
    if not rel.startswith("static/") and os.path.isfile(f"static/{rel}"):
        return f"static/{rel}"
    if "/" in rel:
        return None                     # foldered ref must exist at its path
    static = "static"
    if os.path.isdir(static):
        for sub in os.listdir(static):
            p = f"{static}/{sub}/{rel}"
            if os.path.isfile(p):
                return p
    return None


_FRAME_REF_KEYS = ("pngUrl", "url", "file")


def _strip_dead_frames(obj):
    """Recursively drop frame dicts whose local image file no longer exists.

    The disk-budget sweeper runs concurrently with site builds, so a payload
    can reference frames deleted moments earlier (MRMS radar, GLM lightning,
    WPC winter gifs, stale-cycle MPAS/SHiELD - all seen 2026-09-16). Filtering
    HERE at payload assembly covers every current and future section at once;
    the public packager then only ever copies files that exist.
    """
    if isinstance(obj, dict):
        return {k: _strip_dead_frames(v) for k, v in obj.items()}
    if isinstance(obj, list):
        out = []
        for v in obj:
            if isinstance(v, dict):
                ref = next((v[k] for k in _FRAME_REF_KEYS if isinstance(v.get(k), str)), None)
                if ref and not ref.startswith("http") and \
                        any(ext in ref.lower() for ext in (".png", ".gif", ".jpg")):
                    if _resolve_static_ref(ref) is None:
                        continue        # dead frame - drop from the payload
            out.append(_strip_dead_frames(v))
        return out
    return obj


def generate_site():
    """Collect live data and write the whole site. Returns SITE_DIR or None."""
    try:
        _seed_model_maps()      # keep the models-page catalog stocked (async)
        enforce_disk_budget()
        d = collect_data()
        d["models"] = _model_manifest()
        d["psu"] = _psu_manifest()
        d = _strip_dead_frames(d)
        pages = {
            "index.html": page_index(d),
            "radar.html": page_radar(d),
            "satellite.html": page_satellite(d),
            "models.html": page_models(d),
            "tropical.html": page_tropical(d),
            "tropmodels.html": page_tropmodels(d),
            "climate.html": page_climate(d),
            "severe.html": page_severe(d),
            "winter.html": page_winter(d),
            "rivers.html": page_rivers(d),
            "dashboard.html": page_dashboard(d),
            "fire.html": page_fire(d),
            "meso.html": page_meso(d),
            "obs.html": page_obs(d),
            "charts.html": page_charts(d),
            "national.html": page_national(d),
            "forecast.html": page_forecast(d),
            "education.html": page_education(d),
            "status.html": page_status(d),
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


# Priority combos: the default 4-pane comparison + the most-used panels.
# The rotation below covers EVERY model x product x region combo over time;
# these are just rendered first each pass.
_SEED = [("GFS", 27, "500_vort", "us"), ("GFS", 27, "500_vort", "etn"),
         ("NAM", 13, "500_vort", "etn"), ("RRFS", 12, "500_vort", "etn"),
         ("AIFS", 42, "500_vort", "etn"), ("RAP", 7, "sfc_mslp", "etn"),
         # SREF + EPS-Weekly (added 2026-09-20): make sure both new models
         # show maps on the models page from the very first rotation pass
         ("SREF", 24, "sref_500_vort", "etn"), ("SREF", 24, "sref_500_vort", "us"),
         ("SREF", 15, "sfc_mslp", "etn"), ("SREF", 6, "sref_csnow", "etn"),
         ("EPS-Weekly", 168, "sfc_mslp", "us"), ("EPS-Weekly", 168, "sfc_mslp", "etn"),
         ("EPS-Weekly", 240, "500_vort", "us"), ("EPS-Weekly", 240, "snow", "us"),
         ("GFS", 27, "850_tmp", "etn"), ("GFS", 27, "700_rh", "etn"),
         ("NBM", 9, "nbm_dew", "etn"), ("NBM", 9, "nbm_tstm", "etn"),
         ("NBM", 9, "nbm_qpf", "etn"), ("NBM", 9, "nbm_pwat", "etn"),
         ("NBM", 9, "nbm_snow06", "etn"), ("HREF", 9, "500_vort", "etn"),
         ("REFS", 9, "500_vort", "etn"),   # new 2026-09-14 products first
         ("AI-GraphCast", 42, "sfc_mslp", "us"), ("AI-GraphCast", 42, "500_vort", "us"),
         ("AI-GraphCast", 42, "sfc_mslp", "etn"), ("AI-Pangu", 42, "sfc_mslp", "us"),
         ("AI-Pangu", 42, "500_vort", "us"), ("AI-FourCastNet", 42, "sfc_mslp", "us"),
         ("AI-FourCastNet", 42, "pwat", "us"), ("AI-Aurora", 42, "sfc_mslp", "us"),
         ("AI-Aurora", 42, "500_vort", "us")]  # AI models: never let them starve
# seed hours are loop hours (_loop_hours subsampling skips f24/f06 etc.),
# so seeded frames land INSIDE every model's animation (2026-09-15)

_SEED_LAST = [0.0]      # last seed attempt (monotonic-ish wall clock)
_ROT_RUNNING = [False]  # a rotation pass is in progress - never stack more
_ROT_POS = [0]          # rotation cursor across the FULL combo space
_ROT_BATCH = 36         # combos per pass (missing/stale first)
_ROT_GATE = 1800.0      # min seconds between passes (~72 combos/hour)


def _all_model_combos():
    """Every (model, fh, product, region) the models page can offer.

    fh = ALL loop hours per model, snapped to its native hour_step
    (user request 2026-09-15: "ADD LOOP TO ALL MODELS" - one frame per
    combo was a still image; the page's steppers/animators need the full
    time series). Subsampled to <=7 frames per combo to bound disk + the
    GitHub Pages site cap (see _loop_hours). Regions: both etn and us.
    """
    combos = []
    try:
        from data.model_maps import PRODUCTS_BY_MODEL, MAP_MODELS
    except Exception:                          # noqa: BLE001
        return combos
    for model, prods in PRODUCTS_BY_MODEL.items():
        info = MAP_MODELS.get(model) or {}
        max_h = int(info.get("max_hour") or 24)
        step = int(info.get("hour_step") or 3)
        hours = _loop_hours(max_h, step)
        for prod in prods:
            for region in ("etn", "us"):
                for fh in hours:
                    combos.append((model, fh, prod, region))
    return combos


def _loop_hours(max_h, step):
    """Loop hours for a model: native steps subsampled to <=7 frames.

    The page animates <=16 frames; 7 gives a smooth fast loop while keeping
    the whole catalog (~440 combos) within disk + Pages budgets.
    """
    hours = list(range(step, max_h + 1, step)) or [step]
    if len(hours) > 7:
        idx = [round(i * (len(hours) - 1) / 6) for i in range(7)]
        hours = sorted({hours[i] for i in idx})
    return hours


def _seed_model_maps():
    """Render the ENTIRE models-page catalog via a rolling rotation.

    The static explorer can only show pre-rendered PNGs, so the updater
    walks the full ~400-combo space (every model x product x region),
    rendering a batch each hour: priority _SEED combos first, then the
    rotation cursor (missing/stale combos before fresh ones - os.path.getmtime
    of the combo's newest frame). The whole catalog completes in ~10-20 h
    depending on NOAA download speed, then refresh rolls around forever.
    Best-effort: a failing model never breaks the site build.
    """
    import threading
    import os.path as _op

    def _work():
        import time as _t
        if _ROT_RUNNING[0]:
            # A pass is still rendering. Stacking a second (third, ...)
            # thread used to be normal: after the catalog grew past the
            # 30-min gate, every build spawned another pass, ten ran at
            # once and starved the whole build (3 min -> 65 min builds,
            # 2026-09-19). One pass at a time, ever.
            return
        if _t.time() - _SEED_LAST[0] < _ROT_GATE:
            return
        _SEED_LAST[0] = _t.time()
        _ROT_RUNNING[0] = True
        try:
            from data.model_maps import find_cycle, render_product_map, MAP_DIR
            cycles = {}

            def _cyc(model):
                if model not in cycles:
                    try:
                        cycles[model] = find_cycle(model)
                    except Exception:              # noqa: BLE001
                        cycles[model] = None
                return cycles[model]

            batch = list(_SEED)
            rot = _all_model_combos()
            if rot:
                # rendered-map count per model: thin models must fill first so
                # every model visibly gains maps each pass (fair share)
                counts = {}
                # ONE directory scan per pass: _age used to re-list MAP_DIR
                # (~1k PNGs) for every combo in the batch - 60+ listdirs of
                # stat churn per pass, on top of the renders (2026-09-19)
                import re as _re
                _mre = _re.compile(r"^(.+)_(f\d{3})_(\d{10})_([a-z0-9]+)\.png$")
                newest_by_key = {}
                for fn in os.listdir(MAP_DIR):
                    counts[fn.split("_", 1)[0]] = counts.get(fn.split("_", 1)[0], 0) + 1
                    _m = _mre.match(fn)
                    if not _m:
                        continue
                    try:
                        _mt = _op.getmtime(_op.join(MAP_DIR, fn))
                    except OSError:
                        continue
                    _key = (_m.group(1), _m.group(3), _m.group(4))
                    if _mt > newest_by_key.get(_key, 0.0):
                        newest_by_key[_key] = _mt

                def _age(combo):
                    model, fh, prod, region = combo
                    cyc = _cyc(model)
                    if cyc is None:
                        return 0.0                 # unfindable -> try anyway
                    return newest_by_key.get(
                        (f"{model}_{prod}", f"{cyc:%Y%m%d%H}", region), 0.0)
                # missing combos (age 0) first; within them, thinnest models
                # first - otherwise the stable sort walks catalog order and a
                # late-catalog model (HREF) waits many passes (2026-09-14)
                rot.sort(key=lambda c: (_age(c), counts.get(c[0], 0)))
                # take the FRONT of the sorted list: missing combos (age 0)
                # must render before any cached map is refreshed. The old
                # cursor (rot[start:start+batch]) ignored the sort order and
                # re-rendered fresh maps for many passes while 200+ combos
                # sat unrendered (HREF stuck at 4/28 - 2026-09-14).
                batch += rot[:_ROT_BATCH]

            ok = fail = 0
            skipped = set()
            failed_models = set()
            for model, fh, prod, region in batch:
                try:
                    cyc = _cyc(model)
                    if cyc is not None:
                        render_product_map(model, cyc, fh, prod, region=region)
                        ok += 1
                    else:
                        # cycle probe came back empty (NOAA lag/partial cycle):
                        # silently eating batch slots looked like a download
                        # outage on the page (NBM 2026-09-14) - count it
                        skipped.add(model)
                except Exception:                  # noqa: BLE001 - best-effort
                    fail += 1
                    failed_models.add(model)
                    continue

            # one visible line per pass - a model failing EVERY combo means
            # its fetch path broke (dead NOAA dir, herbie source gone)
            print(f"model-map rotation: {ok} rendered, {fail} failed"
                  + (f" ({', '.join(sorted(failed_models))})" if failed_models else "")
                  + (f" | no live cycle: {', '.join(sorted(skipped))}" if skipped else ""),
                  flush=True)
        except Exception:                          # noqa: BLE001
            pass
        finally:
            _ROT_RUNNING[0] = False

    try:
        threading.Thread(target=_work, daemon=True, name="model-map-seed").start()
    except Exception:                          # noqa: BLE001 - seeding must never break the build
        pass


if __name__ == "__main__":
    out = generate_site()
    print(f"site written: {out}" if out else "site generation failed")

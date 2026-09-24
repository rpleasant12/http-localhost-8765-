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
import hashlib
import html
import json
import os
import urllib.parse
import re
import shutil
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
            # official graphic + TN threat + Facebook share image
            entry["graphicUrl"] = _storm_graphic_url(entry.get("advisoryUrl"))
            entry["tnThreat"] = _tn_threat(entry, ww)
            png = _storm_share_png(entry, entry["graphicUrl"])
            if not png and entry.get("advisoryUrl"):
                # NHC throttles the graphics pages sometimes - fall back to
                # the last good PNG on disk so the share link stays alive
                code = (_basin_code(entry.get("advisoryUrl")) or "").upper()
                stale = (os.path.join("static", "share", f"storm_{code}.png")
                         if code else None)
                if code and os.path.exists(stale):
                    png = stale.replace("\\", "/")
                    _storm_share_page(os.path.join("static", "share"),
                                      code, entry)
            entry["sharePng"] = png
            entry["sharePage"] = ((entry["sharePng"] or "").replace(
                ".png", ".html") if entry["sharePng"] else None)
            # persist this advisory into the per-storm history archive
            archive_bundle = _archive_storm(s, entry)
            if archive_bundle:
                entry["archiveCount"] = archive_bundle["count"]
            entry["shareUrl"] = (config.PUBLIC_SITE_URL.rstrip("/")
                                 + "/" + entry["sharePage"].replace("static/", "", 1)
                                 if entry["sharePage"] else None)
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
        # fingerprint of the site code that produced this payload: pages use
        # it to detect "the site was updated since my settings were saved"
        # and discard stale persisted preferences (auto-refresh off flag).
        "siteVersion": _site_fingerprint(),
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
        "roadCams": _roadcams_safe(),
        "tropModels": _trop_models_safe(),
        "climate": _climate_safe(),
        "elNino": _enso_safe(),
        "mrmsProducts": {k: v.get("label", k) for k, v in MRMS_CATALOG.items()},
        # per-product MRMS loops for the radar page's level picker (disk reads;
        # the shared background renderer fills each over time) - pre-filtered
        # into mrms_loops above so only frames still on disk ship
        "mrmsLoops": mrms_loops,
        "hailCase": _hail_case_safe(),
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
        "roadCams": _roadcams_safe(),
        "tropModels": _trop_models_safe(),
        "climate": _climate_safe(),
        "elNino": _enso_safe(),
        "mrmsProducts": {k: v.get("label", k) for k, v in MRMS_CATALOG.items()},
        # per-product MRMS loops for the radar page's level picker (disk reads;
        # the shared background renderer fills each over time) - pre-filtered
        # into mrms_loops above so only frames still on disk ship
        "mrmsLoops": mrms_loops,
        "hailCase": _hail_case_safe(),
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
        "tropical": _trop_carry_block(storms, nhc_gfx, wr_geo, out_geo),


        "stormArchive": _storm_archive_list(),
        "modelCatalog": model_catalog,
        "renderIndex": _render_index(),
        "pivotUs": _pivot_us(),
        "pivotEtn": _pivot_us("etn"),
        "sevTowns": _sev_towns_safe(),
        "pivotRegions": _pivot_regions(),
        "mpasShield": mpas_shield,
        "forecastCharts": charts,
        "mos": mos,
        "meso": meso,
        "lightning": lightning,
    }


def _storm_archive_list():
    """Gallery bundles for every archived storm, most advisories first."""
    try:
        if not os.path.isdir(_ARCH_DIR):
            return []
        out = []
        for sid in sorted(os.listdir(_ARCH_DIR)):
            sdir = os.path.join(_ARCH_DIR, sid)
            if not os.path.isdir(sdir):
                continue
            name, cls = sid.upper(), ""
            try:
                metas = sorted(f for f in os.listdir(sdir)
                               if f.endswith("_meta.json"))
                if metas:
                    m = json.load(open(os.path.join(sdir, metas[-1]),
                                       encoding="utf-8"))
                    name = m.get("name") or name
                    cls = m.get("classification") or ""
            except (OSError, ValueError):
                pass
            b = _archive_bundle(sid, name, cls)
            if b:
                out.append(b)
        out.sort(key=lambda b: (b["advisories"][0]["stamp"]
                                if b.get("advisories") else ""), reverse=True)
        return out
    except Exception:  # noqa: BLE001
        return []


def page_storms(d):
    """Storm history gallery: every archived advisory per storm."""
    arch = d.get("stormArchive") or []
    cards = ""
    for b in arch:
        cls_lbl = {"HU": "Hurricane", "MH": "Major Hurricane",
                   "TS": "Tropical Storm", "TD": "Tropical Depression",
                   "SS": "Subtropical Storm", "SD": "Subtropical Depression",
                   "PTC": "Post-tropical"}.get(b.get("class") or "", "")
        rows = ""
        for a in b["advisories"]:
            threat = ("<span style=\"color:#ff8a80;font-weight:700\">\u26a0 TN</span>"
                      if a.get("tnThreat") == "watch"
                      else ("<span style=\"color:#e0a458\">\u2192 TN</span>"
                            if a.get("tnThreat") == "track" else ""))
            sum_img = (f'<a href="{a["summary"]}" target="_blank" rel="noopener">'
                       f'<img src="{a["summary"]}" loading="lazy" alt="summary" '
                       'style="width:100%;max-width:210px;border-radius:8px"/></a>'
                       if a.get("summary") else "")
            rows += (
                f'<div class="day" style="text-align:left">'
                f'<div class="dname" style="font-size:13px">{html.escape(str(a["lastUpdate"]))} {threat}</div>'
                f'<a href="{a["cone"]}" target="_blank" rel="noopener">'
                f'<img src="{a["cone"]}" loading="lazy" alt="cone" '
                'style="width:100%;max-width:210px;border-radius:8px;margin-top:4px"/></a>'
                + sum_img +
                f'<div style="color:#7d8794;font-size:12px;margin-top:4px">'
                f'{a.get("intensity") or "?"} kt \u00b7 {a.get("pressure") or "?"} mb</div>'
                f'</div>')
        cards += (
            f'<div class="card"><h2>\U0001f32f {html.escape(b["name"])} '
            f'<span style="color:#7d8794;font-size:14px;font-weight:400">'
            f'{html.escape(cls_lbl)} \u00b7 {b["count"]} archived advisor'
            f'{"y" if b["count"] == 1 else "ies"}</span></h2>'
            f'<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(230px,1fr))">'
            f'{rows}</div></div>')
    if not cards:
        cards = ('<div class="card"><span class="src">No archived advisories '
                 'yet - storms appear here automatically as NHC issues them.</span></div>')
    body = f"""
<header class="hero"><h1>\U0001f4bc <span style="color:var(--acc)">Storm history</span></h1>
<div class="sub">Every archived NHC advisory cone + summary card, newest first \u00b7 updated {html.escape(d["generated"])}</div></header>
{cards}
"""
    return _page("Storms", "storms.html", body)


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


def _pivot_us(region="us"):
    """Model frames for the forecast-collage wall, across cycles.

    _render_index keeps only each combo's newest cycle, which scatters the
    wall: fast AI models finish the 12Z run while the globals sit on 00Z,
    so no single 'newest' cycle holds them all at once. This returns, per
    product, the frames of the three fullest cycles (most models first,
    newer cycle breaks ties) so the models page can assemble a crowded
    same-valid-time wall Pivot-style. Runs per region: "us" powers the
    zoomable CONUS wall, "etn" the native East-Tennessee wall.
    """
    base = "static/model_maps"
    rx = re.compile(r"([A-Za-z0-9\-]+)_(\w+)_f(\d+)_(\d{10})_(\w+)\.png$")
    per = {}  # (prod, cyc, model) -> [(fh, fn)]
    try:
        names = os.listdir(base)
    except OSError:
        names = []
    for fn in names:
        m = rx.match(fn)
        if not m:
            continue
        model, prod, fh, cyc, reg = m.groups()
        if reg != region:
            continue
        per.setdefault((prod, cyc, model), []).append((int(fh), fn))
    walls = {}  # (prod, cyc) -> {model: [(fh, fn)]}
    for (prod, cyc, model), items in per.items():
        walls.setdefault((prod, cyc), {})[model] = sorted(items)[-8:]
    by_prod = {}
    for (prod, cyc), models in walls.items():
        by_prod.setdefault(prod, []).append((len(models), int(cyc), cyc, models))
    out = []
    for prod, rows in by_prod.items():
        rows.sort(key=lambda t: (-t[0], -t[1]))
        for _n, _ci, cyc, models in rows[:3]:
            for model, frames in models.items():
                out.append({"model": model, "product": prod, "cycle": cyc,
                            "frames": [{"fh": fh, "url": f"../model_maps/{fn}"}
                                       for fh, fn in frames]})
    return out


_PIVOT_PRESETS = (
    # (key, group, button label, lon/lat box or None for the full map).
    # These are collage CAMERA presets, not render regions - every one is
    # cut live in the browser from the shared US tiles. Boxes are padded
    # around each metro/area so the square-ified crop keeps context;
    # longitude width is scaled by ~1/cos(lat) so equal labels land
    # near-equal on-screen size (Lambert stretches x as latitude rises).
    ("us", "Regions", "🗺️ Full US", None),
    # -- broader regions --
    ("pl", "Regions", "🌾 Plains", (-114.0, -90.0, 25.5, 49.0)),
    ("mw", "Regions", "🌽 Midwest", (-106.0, -78.0, 33.0, 49.0)),
    ("se", "Regions", "🌊 Southeast", (-100.0, -75.0, 24.0, 40.0)),
    ("ne", "Regions", "🍎 Northeast", (-88.0, -66.0, 34.0, 47.5)),
    ("sc", "Regions", "⛰️ South Central", (-110.0, -88.0, 25.0, 41.0)),
    ("sw", "Regions", "🌵 Southwest", (-125.0, -102.0, 26.0, 42.0)),
    ("nw", "Regions", "🌲 Northwest", (-125.0, -104.0, 38.0, 51.0)),
    ("lc", "Regions", "🪵 Lower Great Lakes", (-95.0, -74.0, 36.0, 48.0)),
    ("oh", "Regions", "🌊 Ohio Valley", (-92.0, -79.0, 34.0, 43.0)),
    ("ap", "Regions", "⛰️ Appalachians", (-89.0, -74.0, 31.5, 42.5)),
    ("ms", "Regions", "🌾 Mid-South", (-95.0, -81.0, 30.0, 41.0)),
    ("gv", "Regions", "🏞️ Gulf Coast", (-100.0, -80.0, 24.0, 34.0)),
    ("at", "Regions", "🏖️ Atlantic Coast", (-84.0, -68.0, 28.0, 45.5)),
    ("pc", "Regions", "🌉 Pacific Coast", (-126.0, -114.0, 30.0, 50.0)),
    ("gl", "Regions", "🧊 Great Lakes", (-96.0, -74.0, 38.0, 51.0)),
    ("cb", "Regions", "🌾 High Plains", (-108.0, -94.0, 34.5, 51.0)),
    ("rz", "Regions", "🏜️ Four Corners", (-114.5, -104.5, 31.0, 41.5)),
    # -- states & cities --
    ("tn", "States & cities", "🏞️ Tennessee", (-92.0, -79.5, 32.5, 38.5)),
    ("et", "States & cities", "⛰️ East Tennessee", (-86.5, -81.5, 33.5, 37.5)),
    ("tx", "States & cities", "🤠 Texas", (-106.8, -93.0, 25.5, 37.0)),
    ("fl", "States & cities", "🌴 Florida", (-88.0, -77.0, 23.5, 32.5)),
    ("co", "States & cities", "🏔️ Colorado", (-109.5, -99.5, 34.5, 42.5)),
    ("ny", "States & cities", "🗽 New York City", (-76.8, -71.6, 39.4, 42.2)),
    ("cl", "States & cities", "🌆 Chicago & Lake MI", (-90.5, -84.5, 39.5, 44.0)),
    ("sv", "States & cities", "🎰 Desert SW / Vegas", (-118.5, -111.5, 32.5, 39.5)),
    ("ca", "States & cities", "🌉 California", (-124.5, -113.5, 31.5, 43.0)),
    ("pn", "States & cities", "☕ Pacific NW", (-125.5, -116.0, 41.5, 50.5)),
    ("gs", "States & cities", "🍑 Georgia & Carolinas", (-86.5, -76.5, 30.5, 38.5)),
    ("im", "States & cities", "🍂 Upper Midwest Metro", (-96.0, -86.0, 40.5, 48.5)),
    # -- severe-weather heritage regions --
    ("ta", "Regions", "🌪️ Tornado Alley", (-104.0, -94.0, 32.0, 40.5)),
    ("da", "Regions", "🌪️ Dixie Alley", (-92.5, -84.5, 30.0, 36.5)),
    ("np", "Regions", "🌾 Northern Plains", (-104.0, -92.0, 42.5, 49.5)),
    ("ma", "Regions", "🦀 Mid-Atlantic", (-82.0, -72.0, 35.0, 43.5)),
    ("ng", "Regions", "🍁 New England", (-76.0, -66.5, 40.5, 48.0)),
    ("gb", "Regions", "🏜️ Great Basin", (-120.0, -108.0, 34.5, 45.0)),
    ("sr", "Regions", "🏔️ Southern Rockies", (-112.0, -102.0, 31.0, 40.0)),
    # -- local tri-state & metro close-ups --
    ("kt", "States & cities", "🏙️ Knoxville & Smokies", (-85.8, -82.5, 34.7, 36.9)),
    ("tc", "States & cities", "⛰️ Tri-Cities & W NC", (-83.8, -80.5, 35.4, 37.9)),
    ("ns", "States & cities", "🎸 Nashville", (-88.5, -85.5, 34.5, 37.0)),
    ("mem", "States & cities", "🎤 Memphis & Delta", (-92.3, -88.5, 32.8, 36.6)),
    ("atl", "States & cities", "🍑 Atlanta", (-86.2, -82.5, 31.8, 35.6)),
    ("dfw", "States & cities", "🐎 Dallas-Fort Worth", (-99.3, -95.0, 31.3, 34.6)),
    ("okc", "States & cities", "🌾 Oklahoma City", (-99.8, -95.5, 33.0, 36.6)),
)
# Historical measured constant: every US map render lands its map axes on
# the SAME pixel rect inside the tight-cropped PNG (rows 54..686; left
# spine col 11, right spine col 1129 - the tight bbox moves only the
# colorbar width on the right). Used as the fallback when live PNG
# measurement is unavailable.
_PIVOT_MAP_RECT_FALLBACK = (11, 54, 1129, 686)


def _png_axes_rect(path):
    """Pixel rect of the map axes (its black spines) inside a saved US PNG.

    The renders draw default dark spines around the map on a white
    background, so the axes frame is findable in pixels: the first/last
    near-black row spanning over half the image width, then the full-height
    dark columns. The first two such columns are the MAP's left/right
    spines (the colorbar's outline columns come later and must be skipped,
    or the rect wrongly includes it). Returns (left, top, right, bottom)
    or None when the frame can't be found.
    """
    try:
        import numpy as np
        from PIL import Image
        im = np.asarray(Image.open(path).convert("RGB")).astype(int)
    except Exception:                        # noqa: BLE001 - any bad file skips
        return None
    h, w, _ = im.shape
    dark = im.sum(axis=2) < 240
    rows = dark.sum(axis=1)
    top = int(np.argmax(rows > w * 0.5))
    bot = h - 1 - int(np.argmax(rows[::-1] > w * 0.5))
    if top >= bot:
        return None
    span = dark[top:bot + 1, :].sum(axis=0) / (bot - top + 1)
    cols = np.where(span > 0.95)[0]
    if len(cols) < 2:
        return None
    left, right = int(cols[0]), int(cols[1])   # map spines; colorbar later
    # plausibility: the map must dominate the image, else we matched text
    if (right - left) < w * 0.6 or (bot - top) < h * 0.6:
        return None
    return left, top, right, bot


_PIVOT_REG_CACHE = None


def _pivot_regions():
    """Camera calibration for the forecast collage's zoom/pan (map fractions).

    The collage tiles all share one fixed LambertConformal US render, so a
    region like Tennessee can be cut live in the browser from the existing
    tiles - no re-render - but the crop must be measured, not guessed: in
    Lambert space a lon/lat box is NOT a linear slice of the image, and the
    tight-cropped PNG layout (title, colorbar) varies per product.

    Calibration ships two things:
    - ``mapRect``  the map-axes pixel rect inside every US PNG, measured
      from the real files by scanning for the black spines (median across
      a product-strided sample). The client divides each image's natural
      size by this to normalize canvases that differ in colorbar width.
    - ``regions``  preset camera boxes as fractions OF THE MAP AREA
      (projection-exact), computed on a minimal replica figure whose axes
      bbox is asserted to match the measured PNG rect's aspect.

    Cached per-process (the layout only changes when code changes, which
    restarts the updater). Falls back to the historical constants on any
    failure - the zoom feature degrades, the page never breaks.
    """
    global _PIVOT_REG_CACHE
    if _PIVOT_REG_CACHE is not None:
        return _PIVOT_REG_CACHE
    res = None
    try:
        res = _measure_pivot_regions()
    except Exception:                        # noqa: BLE001 - optional sugar
        res = None
    if not res or not res.get("regions"):
        l, t, r, b = _PIVOT_MAP_RECT_FALLBACK
        res = {"mapRect": {"left": l, "top": t, "right": r, "bottom": b},
               "regions": {"us": {"label": "🗺️ Full US", "left": 0.0,
                                  "top": 0.0, "width": 1.0, "height": 1.0}}}
    _PIVOT_REG_CACHE = res
    return res


def _measure_pivot_regions():
    """Live calibration: spine-scan real PNGs + replica projection window."""
    from data.model_maps import MAP_DIR, MAP_REGIONS

    # 1) map-axes pixel rect, median across a product-strided PNG sample
    try:
        names = sorted(n for n in os.listdir(MAP_DIR) if n.endswith("_us.png"))
    except OSError:
        names = []
    rects = []
    if names:
        step = max(1, len(names) // 6)
        for n in names[::step][:6]:
            r = _png_axes_rect(os.path.join(MAP_DIR, n))
            if r:
                rects.append(r)
    if len(rects) >= 2:
        l, t, r, b = (sorted(rc[i] for rc in rects)[len(rects) // 2]
                      for i in range(4))
    else:
        l, t, r, b = _PIVOT_MAP_RECT_FALLBACK

    # 2) projection window + axes-relative region fractions from a minimal
    #    replica (xlim/ylim depend only on figsize/projection/extent, not
    #    on colorbar/title, so no decoration reproduction is needed)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    proj = ccrs.LambertConformal(central_longitude=-96, central_latitude=39)
    trans = ccrs.PlateCarree()
    fig = plt.figure(figsize=(13, 8), dpi=110)
    ax = plt.axes(projection=proj)
    ax.set_extent(MAP_REGIONS["us"]["extent"], crs=trans)
    ax.add_feature(cfeature.STATES.with_scale("110m"), linewidth=0.5)
    ax.add_feature(cfeature.COASTLINE.with_scale("110m"), linewidth=0.6)
    ax.add_feature(cfeature.BORDERS.with_scale("110m"), linewidth=0.8)
    fig.canvas.draw()
    bb = ax.get_window_extent(fig.canvas.get_renderer())
    plt.close(fig)
    aw, ah = bb.x1 - bb.x0, bb.y1 - bb.y0
    # the replica must agree with the real PNGs' measured map rect, else
    # every fraction would be silently skewed - degrade to fallback instead
    asp_fig, asp_png = aw / ah, (r - l) / (b - t)
    if abs(asp_fig - asp_png) > 0.02 * asp_png:
        raise ValueError(f"replica aspect {asp_fig:.4f} vs PNG map rect {asp_png:.4f}")

    def frac(lon, lat):
        x, y = proj.transform_point(lon, lat, trans)[:2]
        px, py = ax.transData.transform((x, y))
        return (px - bb.x0) / aw, 1.0 - (py - bb.y0) / ah

    regions = {}
    for key, grp, label, box in _PIVOT_PRESETS:
        if box is None:
            regions[key] = {"label": label, "group": grp, "left": 0.0, "top": 0.0,
                            "width": 1.0, "height": 1.0}
            continue
        xa, ya = frac(box[0], box[3])            # west/north corner
        xb, yb = frac(box[1], box[2])            # east/south corner
        regions[key] = {"label": label, "group": grp, "left": min(xa, xb),
                        "top": min(ya, yb),
                        "width": abs(xb - xa), "height": abs(yb - ya)}
    return {"mapRect": {"left": int(l), "top": int(t), "right": int(r), "bottom": int(b)},
            "mapRectEtn": _pivot_etn_rect(),
            "regions": regions}

_PIVOT_ETN_RECT_CACHE = None


def _pivot_etn_rect():
    """Map-axes pixel rect inside East-Tennessee renders (spine scan, cached).

    Same idea as the US mapRect: the client needs to know which part of the
    tight-cropped PNG is the actual map before it can down-sample pixels for
    ensemble comparison. Measured live from the PNGs; falls back to None
    (client then skips ETN outlier detection rather than guessing).
    """
    global _PIVOT_ETN_RECT_CACHE
    if _PIVOT_ETN_RECT_CACHE is not None:
        return _PIVOT_ETN_RECT_CACHE
    from data.model_maps import MAP_DIR
    rect = None
    try:
        names = sorted(n for n in os.listdir(MAP_DIR) if n.endswith("_etn.png"))
    except OSError:
        names = []
    if names:
        step = max(1, len(names) // 6)
        cands = [r for r in (_png_axes_rect(os.path.join(MAP_DIR, n))
                             for n in names[::step][:6]) if r]
        if len(cands) >= 2:
            rect = tuple(sorted(c[i] for c in cands)[len(cands) // 2]
                         for i in range(4))
    _PIVOT_ETN_RECT_CACHE = rect
    return rect


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


def _sev_towns_safe():
    """Severe-composite town ranking (never breaks the site build)."""
    try:
        from data.sev_towns import severe_towns_bundle
        return severe_towns_bundle()
    except Exception:                                  # noqa: BLE001
        return {"ok": False, "ranked": [], "windows": []}


# ------------------------------------------- last-good tropical carry-forward
_TROP_LAST_GOOD = {"t": 0.0, "tropical": None, "tropModels": None}
_TROP_TTL = 24 * 3600.0   # never serve carried data older than a day


def _trop_remember(key, value):
    """Record a non-empty tropical payload as the last-good fallback."""
    _TROP_LAST_GOOD[key] = value
    _TROP_LAST_GOOD["t"] = time.time()


def _trop_carry_block(storms, nhc_gfx, wr_geo, out_geo):
    """Tropical block with empty-fetch carry-forward (up to 24 h).

    NHC/ATCF calls occasionally fail (throttling, network blip); an empty
    result then publishes "No active storms" mid-hurricane - exactly what
    happened on 2026-09-21 with 3 storms active. Reuse the most recent
    non-empty block until live data flows again. Trade-off: a storm that
    just dissolved can linger up to a day if every confirming fetch also
    fails; live data always wins as soon as any fetch succeeds.
    """
    if storms or nhc_gfx or wr_geo or out_geo:
        block = {"storms": storms, "graphics": nhc_gfx,
                 "windRadii": wr_geo, "outlook": out_geo}
        _trop_remember("tropical", block)
        return block
    cached = _TROP_LAST_GOOD.get("tropical")
    if cached and time.time() - _TROP_LAST_GOOD.get("t", 0.0) < _TROP_TTL:
        age = int((time.time() - _TROP_LAST_GOOD["t"]) / 60)
        print(f"tropical: live fetch empty - carrying forward last good data "
              f"({age} min old)", flush=True)
        return cached
    return {"storms": [], "graphics": nhc_gfx,
            "windRadii": wr_geo, "outlook": out_geo}


# --------------------------------------- storm graphics / TN threat / sharing
_TN_BBOX = (-90.31, 34.98, -81.65, 36.69)   # west, south, east, north of TN
_TN_CENTER = (35.86, -86.35)                # geographic center of Tennessee
_GFX_CACHE = {}                             # basin code -> (ts, url|None)


def _basin_code(advisory_url):
    """'AT1' from .../MIATCPAT1.shtml -> lowercase graphics-page code."""
    m = re.search(r"MIATCP([A-Z]{2}\d+)", advisory_url or "")
    return m.group(1).lower() if m else None


def _storm_graphic_url(advisory_url):
    """Latest official 5-day cone PNG for one storm (NHC graphics page).

    The refresh stamp inside /storm_graphics/... changes with every
    advisory, so the deterministic URL must be scraped from the storm's
    graphics page (graphics_at1.shtml). Cached 30 min; failures 10 min so
    a dead NHC page never slows the build cycle. None when unavailable.
    """
    code = _basin_code(advisory_url)
    if not code:
        return None
    ts, url = _GFX_CACHE.get(code, (0.0, None))
    ttl = 1800.0 if url else 600.0
    if time.time() - ts < ttl:
        return url
    url = None
    try:
        import requests as _rq
        r = _rq.get(f"https://www.nhc.noaa.gov/graphics_{code}.shtml",
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; "
                            "Win64; x64) tnwx/1.0"}, timeout=20)
        if r.status_code == 200:
            m = re.search(r'(/storm_graphics/[A-Z]{2}\d{2}/refresh/[^\s"]*?'
                          r'_5day_cone_sm\+png/[^\s"]+?\.png)', r.text)
            if m:
                url = "https://www.nhc.noaa.gov" + m.group(1)
    except Exception:  # noqa: BLE001
        url = None
    _GFX_CACHE[code] = (time.time(), url)
    return url


def _tn_threat(entry, ww):
    """Shared TN-threat check (data.tropical_threat) - site delegate."""
    from data.tropical_threat import tn_threat
    return tn_threat(entry, ww)


def _storm_share_page(out_dir, code, entry):
    """One-click Facebook share landing page for a storm (share/<code>.html).

    Facebook's sharer only renders a preview for a real page with OG tags -
    a raw PNG link shares as a bare URL. This page carries the storm image
    as og:image plus a big "Share on Facebook" button, so posting the
    summary to the TNWN page is one click. Rewritten only when content
    changes (keeps publish diffs clean). Never raises.
    """
    try:
        pub = config.PUBLIC_SITE_URL.rstrip("/")
        page_url = f"{pub}/share/storm_{code}.html"
        img_url = f"{pub}/share/storm_{code}.png"
        name = (entry.get("name") or "Storm").upper()
        cls = entry.get("classification") or ""
        cls_lbl = {"HU": "Hurricane", "MH": "Major Hurricane",
                   "TS": "Tropical Storm", "TD": "Tropical Depression",
                   "SS": "Subtropical Storm", "SD": "Subtropical Depression",
                   "PTC": "Post-tropical"}.get(cls, cls or "Cyclone")
        kt = entry.get("intensity")
        kt = int(float(kt)) if kt else 0
        threat = entry.get("tnThreat")
        title = f"{cls_lbl} {name} - Tennessee Weather Network"
        desc = (f"{kt} kt winds, pressure {entry.get('pressure') or '?'} mb, "
                f"{entry.get('movement') or 'movement n/a'}. "
                "Official NHC cone + forecast track.")
        if threat == "watch":
            desc += " Watch/warning area includes Tennessee."
        elif threat == "track":
            desc += " Forecast track toward Tennessee."
        sharer = ("https://www.facebook.com/sharer/sharer.php?u="
                  + urllib.parse.quote(page_url, safe=""))
        doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:type" content="article">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:image" content="{img_url}">
<meta property="og:url" content="{page_url}">
<title>{html.escape(title)}</title>
<style>body{{margin:0;background:#12151a;color:#e8eef5;font:16px/1.5 system-ui,sans-serif;text-align:center}}
img{{max-width:min(94vw,900px);border-radius:12px;margin:18px auto 6px;display:block}}
a.btn{{display:inline-block;margin:14px 6px 30px;padding:13px 26px;border-radius:10px;
background:#1877f2;color:#fff;font-weight:700;text-decoration:none;font-size:18px}}
a.alt{{background:#2a313b;color:#cdd7e4;font-weight:500;font-size:15px}}
.src{{color:#7d8794;font-size:13px;margin:8px 0 40px}}</style></head><body>
<img src="{img_url}" alt="{html.escape(title)}">
<a class="btn" href="{sharer}" target="_blank" rel="noopener">Share on Facebook</a>
<a class="btn alt" href="{img_url}" target="_blank" rel="noopener">Open full image</a>
<div class="src">{html.escape(entry.get('lastUpdate') or '')} \u00b7 <a href=\"https://www.facebook.com/tennesseeweathernetwork\" style=\"color:#7d8794\">Tennessee Weather Network</a>
</div></body></html>"""
        dst = os.path.join(out_dir, f"storm_{code}.html")
        if not os.path.exists(dst) or open(dst, encoding="utf-8").read() != doc:
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(doc)
    except Exception:  # noqa: BLE001
        pass


def _storm_share_png(entry, graphic_url):
    """Branded storm-summary graphic (static/share/storm_<code>.png).

    Downloads NHC's official cone graphic and composes it with a TNWN info
    panel (intensity, pressure, movement, watches, TN threat, footer) so a
    Facebook-ready image exists at a stable per-storm URL. Rebuilt each
    cycle when the advisory graphic changes; returns a static/relative path
    or None. Never raises.
    """
    if not graphic_url:
        return None
    code = (_basin_code(entry.get("advisoryUrl")) or "storm").upper()
    out_dir = os.path.join("static", "share")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"storm_{code}.png")
    stamp_file = out + ".src"
    try:
        import requests as _rq
        from PIL import Image, ImageDraw, ImageFont
        fresh = os.path.exists(stamp_file) and \
            open(stamp_file, encoding="utf-8").read().strip() == graphic_url
        if fresh and os.path.exists(out):
            _storm_share_page(out_dir, code, entry)
            return out.replace("\\", "/")
        r = _rq.get(graphic_url, headers={"User-Agent": "Mozilla/5.0 tnwx/1.0"},
                    timeout=30)
        if r.status_code != 200:
            return None
        from io import BytesIO
        gfx = Image.open(BytesIO(r.content)).convert("RGB")
        W, H = 1000, 640
        canvas = Image.new("RGB", (W, H), (18, 21, 26))
        gw = 620
        gh = int(gfx.height * gw / gfx.width)
        if gh > 520:
            gh = 520
            gw = int(gfx.width * gh / gfx.height)
        canvas.paste(gfx.resize((gw, gh)), (24, 24))
        d = ImageDraw.Draw(canvas)

        def _font(sz, bold=False):
            for p in ("C:/Windows/Fonts/segoeuib.ttf" if bold else
                      "C:/Windows/Fonts/segoeui.ttf",
                      "C:/Windows/Fonts/arialbd.ttf" if bold else
                      "C:/Windows/Fonts/arial.ttf"):
                try:
                    return ImageFont.truetype(p, sz)
                except OSError:
                    continue
            return ImageFont.load_default()

        x, y = gw + 60, 34
        acc = (255, 170, 60)
        d.text((x, y), "TENNESSEE WEATHER NETWORK", font=_font(22, True),
               fill=acc)
        y += 44
        name = (entry.get("name") or "Storm").upper()
        cls = entry.get("classification") or ""
        cls_lbl = {"HU": "Hurricane", "MH": "Major Hurricane",
                   "TS": "Tropical Storm", "TD": "Tropical Depression",
                   "SS": "Subtropical Storm", "SD": "Subtropical Depression",
                   "PTC": "Post-tropical"}.get(cls, cls or "Cyclone")
        d.text((x, y), f"{cls_lbl} {name}", font=_font(34, True),
               fill=(240, 244, 250))
        y += 56
        kt = entry.get("intensity")
        kt = int(float(kt)) if kt else 0
        rows = [
            ("Winds", f"{kt} kt ({int(kt * 1.15078)} mph)"),
            ("Pressure", f"{entry.get('pressure') or '?'} mb"),
            ("Movement", entry.get("movement") or "n/a"),
            ("Advisory", entry.get("lastUpdate") or ""),
        ]
        for lbl, val in rows:
            d.text((x, y), lbl.upper(), font=_font(17), fill=(122, 132, 144))
            d.text((x + 120, y - 2), str(val), font=_font(20, True),
                   fill=(225, 232, 240))
            y += 34
        threat = entry.get("tnThreat")
        if threat:
            msg = ("WATCH/WARNING AREA INCLUDES TENNESSEE" if threat == "watch"
                   else "FORECAST TRACK TOWARD TENNESSEE")
            d.rounded_rectangle((x, y + 4, W - 30, y + 42), 8, fill=(178, 34, 40))
            d.text((x + 12, y + 12), msg, font=_font(17, True),
                   fill=(255, 240, 240))
            y += 58
        y = max(y, 560)
        d.text((24, H - 34), f"Official NHC cone \u00b7 {entry.get('lastUpdate') or ''}"
               "  \u00b7  facebook.com/tennesseeweathernetwork",
               font=_font(17), fill=(122, 132, 144))
        d.rectangle((0, 0, W, 6), fill=acc)
        canvas.save(out, "PNG", optimize=True)
        with open(stamp_file, "w", encoding="utf-8") as fh:
            fh.write(graphic_url)
        _storm_share_page(out_dir, code, entry)
        return out.replace("\\", "/")
    except Exception:  # noqa: BLE001
        return os.path.exists(out) and out.replace("\\", "/") or None


def _trop_models_safe():
    """ATCF spaghetti guidance + intensity charts (never breaks the build)."""
    try:
        from data.tropical_models import bundle as _tmb
        storms = _tmb()
        out = {"ok": True, "storms": storms}
        if storms:
            _trop_remember("tropModels", out)
        return out
    except Exception as exc:                           # noqa: BLE001
        # silent-ok:False hid a live outage (3 active storms, empty page) for
        # a full day (2026-09-20/21) - surface the reason to the updater log
        print(f"tropical-models bundle failed: {type(exc).__name__}: {exc}", flush=True)
        last = _TROP_LAST_GOOD.get("tropModels")
        if last and time.time() - _TROP_LAST_GOOD.get("t", 0.0) < _TROP_TTL:
            age = int((time.time() - _TROP_LAST_GOOD["t"]) / 60)
            print(f"tropical-models: carrying forward last good guidance "
                  f"({age} min old)", flush=True)
            return last
        return {"ok": False, "storms": []}


def _enso_safe():
    """El Nino / ENSO bundle (never breaks the site build)."""
    try:
        from data.enso import bundle
        return bundle()
    except Exception:                              # noqa: BLE001
        return {"oni": [], "sst": [], "chips": [], "figures": []}


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


def _roadcams_safe():
    """TDOT SmartWay traffic cameras (never breaks the site build)."""
    try:
        from data.roadcams import roadcams_bundle
        return roadcams_bundle()
    except Exception:                              # noqa: BLE001
        return {"ok": False, "cams": [], "events": []}


def _hail_case_safe():
    """Annotated hail-core teaching cut (data.hail_ed) or None - a quiet
    hail week or a fetch hiccup must never break the build."""
    try:
        from data.hail_ed import case_bundle
        return case_bundle()
    except Exception:                              # noqa: BLE001
        return None


_HAIL_LESSON_CSS = """
#hailLesson .hlTabs { display:flex; gap:8px; margin:10px 0 12px; flex-wrap:wrap; }
#hailLesson .hlTabs button { background:#1d2432; color:#cdd7e4; border:1px solid var(--line);
  border-radius:8px; padding:8px 14px; font-size:14px; cursor:pointer; }
#hailLesson .hlTabs button.on { background:var(--acc); color:#fff; border-color:var(--acc); }
#hailLesson .hlImgWrap { position:relative; display:inline-block; max-width:100%; line-height:0; }
#hailLesson img { max-width:100%; height:auto; border-radius:10px; border:1px solid var(--line);
  cursor:crosshair; touch-action:manipulation; }
#hailLesson #hlMark { display:none; position:absolute; width:16px; height:16px; margin:-8px 0 0 -8px;
  border:2px solid #ff5252; border-radius:50%; pointer-events:none;
  box-shadow:0 0 0 1px #000; }
#hailLesson .hlFb { margin-top:10px; padding:10px 12px; border-radius:8px; font-size:14px; line-height:1.45; display:none; }
#hailLesson .hlFb.good { display:block; background:rgba(76,175,80,.15); border:1px solid #4caf50; }
#hailLesson .hlFb.mid  { display:block; background:rgba(255,193,7,.12); border:1px solid #ffc107; }
#hailLesson .hlFb.bad  { display:block; background:rgba(255,82,82,.12); border:1px solid #ff5252; }
#hailLesson .hlKey { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:8px; margin-top:12px; }
#hailLesson .hlKey div { background:#12161f; border:1px solid var(--line); border-radius:8px; padding:9px 11px; font-size:13px; line-height:1.45; }
#hailLesson .hlKey b { font-size:13.5px; }
#hailLesson .hlCap { color:var(--dim); font-size:12px; margin-top:10px; line-height:1.5; }
#hailLesson ol { margin:8px 0 0 20px; padding:0; font-size:14px; line-height:1.6; }
#hailLesson .hlPrompt { font-size:15px; margin:0 0 10px; color:#e8edf4; }
#hailLesson .hlHint { color:var(--dim); font-size:12.5px; margin-top:8px; }
#hailLesson .hlRecord { margin-top:10px; font-size:12.5px; color:var(--dim); }
#hailLesson .hlQ { margin-top:12px; padding:12px; border:1px solid var(--line); border-radius:8px; }
#hailLesson .hlQ .q { font-weight:700; margin-bottom:8px; }
#hailLesson .hlQ button { display:block; width:100%; text-align:left; margin:6px 0; padding:9px 12px; background:#1b222e; color:#e8eef5; border:1px solid #333c46; border-radius:8px; cursor:pointer; font-size:14px; }
#hailLesson .hlQ button:hover { background:#232c3a; }
#hailLesson .hlQ button.right { border-color:#4caf50; background:rgba(76,175,80,.12); }
#hailLesson .hlQ button.wrong { border-color:#ff5252; background:rgba(255,82,82,.12); }
#hailLesson .hlQ .why { margin-top:8px; font-size:13.5px; color:#cdd7e4; display:none; }
#hailLesson #hlQuizScore { font-weight:700; margin-top:10px; }
"""

# plain string (not f-string): the __CASE_JSON__ placeholder is swapped at
# render time so the lesson's JavaScript braces never touch the page's
# f-string escaping
_HAIL_LESSON_JS = """
(function(){
  const HC = __CASE_JSON__;
  function hlTab(name){
    for (const t of ["guide","practice","reveal"]){
      const tab = document.getElementById("hlTab"+t), btn = document.getElementById("hlB"+t);
      if (tab) tab.style.display = (t===name) ? "" : "none";
      if (btn) btn.classList.toggle("on", t===name);
    }
    if (name==="reveal" && window.__hlTap){
      const el = document.getElementById("hlWhere");
      if (el) el.textContent = window.__hlTap;
    }
  }
  window.hlTab = hlTab;
  const img = document.getElementById("hlImgPlain"), mk = document.getElementById("hlMark"), fb = document.getElementById("hlFb");

  // drill record - kept across visits in localStorage, like the education page's quiz scores
  const KEY = "tnwx_hail_drill_v1";
  function loadRec(){ try { return JSON.parse(localStorage.getItem(KEY)) || { hits:0, tries:0, bestKm:null }; } catch(e){ return { hits:0, tries:0, bestKm:null }; } }
  function saveRec(r){ try { localStorage.setItem(KEY, JSON.stringify(r)); } catch(e){} }
  function paintRec(){
    const el = document.getElementById("hlRecord"); if (!el) return;
    const r = loadRec();
    el.textContent = r.tries
      ? "Drill record: " + r.hits + " core" + (r.hits===1?"":"s") + " found in " + r.tries + " drill" + (r.tries===1?"":"s") + (r.bestKm!=null ? " - best " + r.bestKm.toFixed(1) + " km off the verified core" : "")
      : "Drill record: no taps yet - every guess is scored and remembered.";
  }
  paintRec();

  if (img){
    img.addEventListener("click", function(e){
      const r = img.getBoundingClientRect();
      const fx = (e.clientX - r.left)/r.width, fy = (e.clientY - r.top)/r.height;
      const lat = HC.crop.latTop + fy*(HC.crop.latBot - HC.crop.latTop);
      const lon = HC.crop.lonL  + fx*(HC.crop.lonR  - HC.crop.lonL);
      const ky = 111.32, kx = 111.32*Math.cos(HC.core.lat*Math.PI/180);
      const dKm = Math.hypot((lat-HC.core.lat)*ky, (lon-HC.core.lon)*kx);
      mk.style.display = "block"; mk.style.left = (fx*100)+"%"; mk.style.top = (fy*100)+"%";
      let msg, cls;
      if (dKm < 8)       { msg = "<b>🎯 Bullseye - that is the hail core.</b> Your pick was "+dKm.toFixed(0)+" km from the storm's hardest echo. 65+ dBZ this tight and this cold-colored is the hail factory: big stones grow where the updraft is strongest."; cls="good"; }
      else if (dKm < 20) { msg = "<b>✅ Close - inside the strong echo.</b> Your pick was "+dKm.toFixed(0)+" km off the core. The core is the tightest, hardest-colored cluster inside the storm, usually on its inflow side."; cls="good"; }
      else if (dKm < 40) { msg = "<b>🌧️ That is the forward flank.</b> Your pick was "+dKm.toFixed(0)+" km from the core. Big smooth echo like that is the rain core. The hail factory sits on the storm's inflow (notch) side, where the echo shapes into an appendage."; cls="mid"; }
      else               { msg = "<b>❄️ Off the storm.</b> Your pick was "+dKm.toFixed(0)+" km away. Scan for the coldest colors - the magenta/white ring - and remember: reds are heavy rain, magenta-and-above is where hail lives."; cls="bad"; }
      fb.className = "hlFb "+cls; fb.innerHTML = msg;
      window.__hlTap = "Your practice pick was "+dKm.toFixed(0)+" km from the verified core ("+HC.core.lat.toFixed(2)+", "+HC.core.lon.toFixed(2)+").";
      const rec = loadRec(); rec.tries++;
      if (dKm < 20) { rec.hits++; if (rec.bestKm == null || dKm < rec.bestKm) rec.bestKm = dKm; }
      saveRec(rec); paintRec();
    });
  }

  const QUIZ = [
    { q: "Where does hail actually grow inside a supercell?",
      opts: ["Inside the heaviest rain of the forward flank",
             "In the vault above the inflow notch - strong updraft, little rain falling through",
             "Along the leading gust front, where new cells keep firing"],
      c: 1,
      why: "The updraft is strongest in the vault, so stones hang aloft through more growth layers instead of being dumped early. That is why the radar core hugs the inflow side - and why the biggest stones fall near the notch, not in the downpour." },
    { q: "The cut shows 65-70 dBZ stacked deep with an inflow notch on the flank. What is the smart read?",
      opts: ["Rain-cooled outflow is undercutting the updraft - the storm should weaken soon",
             "A deep, tight echo column beside a clean inflow notch - the classic significant-hail signature",
             "Radar beam blockage - the real storm is farther north"],
      c: 1,
      why: "Deep high-dBZ columns plus a crisp inflow notch = a strong, steady updraft feeding the hail factory. That pairing is exactly what NWS warning forecasters look for before issuing a large-hail warning." }
  ];
  const qz = document.getElementById("hlQuiz");
  if (qz) {
    let correct = 0, answered = 0;
    QUIZ.forEach(function(item, qi){
      const box = document.createElement("div");
      box.className = "hlQ";
      box.innerHTML = '<div class="q">' + (qi+1) + ". " + item.q + "</div>" +
        item.opts.map(function(o, oi){ return '<button data-o="' + oi + '">' + o + "</button>"; }).join("") +
        '<div class="why"></div>';
      box.querySelectorAll("button").forEach(function(b){
        b.onclick = function(){
          if (box.dataset.done) return;
          box.dataset.done = "1"; answered++;
          const ok = (+b.dataset.o === item.c);
          if (ok) correct++;
          box.querySelectorAll("button").forEach(function(bb, bi){
            bb.disabled = true;
            if (bi === item.c) bb.classList.add("right");
            else if (bb === b) bb.classList.add("wrong");
          });
          const w = box.querySelector(".why");
          w.style.display = "block";
          w.innerHTML = (ok ? "\u2705 <b>Correct.</b> " : "\u274c <b>Not quite.</b> ") + item.why;
          const sc = document.getElementById("hlQuizScore");
          if (sc) sc.textContent = "Quiz: " + correct + " of " + answered + " correct" +
            (answered === QUIZ.length ? (correct === QUIZ.length ? " - you read storms like a forecaster." : " - reread the annotations; every pattern is there.") : "");
        };
      });
      qz.appendChild(box);
    });
  }
})();
"""


def _hail_lesson_card(d):
    """Interactive hail-signature lesson card for the radar page.

    Three tabs: read the annotated cut of this week's worst verified hail
    core, practice finding the core on the unannotated twin (click -> score
    against the verified core), then reveal with a guided reading order.
    A quiet hail week degrades to an honest pointer at the MESH layer.
    """
    if not d.get("hailCase"):
        return ("<div class=\"card\"><h2>🧊 Hail signatures - read a real storm</h2>"
                "<p class=\"src\">No verified hail case in the past week (quiet "
                "pattern). This lesson appears automatically with the next hail "
                "report. Meanwhile pick <b>MRMS → MESH (max hail size)</b> in the "
                "layer picker above to hunt today's storms yourself.</p></div>")
    c = d["hailCase"]
    rep = c.get("report") or {}
    where = ", ".join(p for p in (rep.get("city"), rep.get("st")) if p) or "unknown location"
    valid = (rep.get("valid") or "")
    when = f"{valid[:10]} at {valid[11:16]} UTC" if len(valid) >= 16 else valid
    mag = rep.get("mag")
    src = rep.get("source") or "NWS Local Storm Report"
    cap = (f"Case: {where} - {mag:.1f}-inch hail reported {when} ({src}, via IEM archive). "
           f"Radar frame {rep.get('frame', '?')} - CONUS composite reflectivity, cropped "
           f"~450 km around the storm, NWS dBZ color ramp. The lesson re-cases itself "
           f"to the biggest hail report of the past 7 days whenever a new one lands.")
    url_ann = (c.get("url") or "").replace("/app/static/", "../")
    url_pln = (c.get("urlPlain") or "").replace("/app/static/", "../")
    js = _HAIL_LESSON_JS.replace("__CASE_JSON__", json.dumps(c))
    return f"""<div class="card" id="hailLesson">
<style>{_HAIL_LESSON_CSS}</style>
<h2>🧊 Hail signatures - read a real storm, hands-on</h2>
<p class="src">Today's worst verified hail core, straight off the NEXRAD archive. Read the annotations, then find the core yourself before revealing.</p>
<div class="hlTabs">
  <button id="hlBguide" class="on" onclick="hlTab('guide')">📖 The annotated case</button>
  <button id="hlBpractice" onclick="hlTab('practice')">🎯 Find the core (practice)</button>
  <button id="hlBreveal" onclick="hlTab('reveal')">✅ Reveal + reading order</button>
</div>
<div id="hlTabguide">
  <div class="hlImgWrap"><img src="{url_ann}" alt="Annotated composite-reflectivity cut of this week's biggest hail core" loading="lazy"/></div>
  <div class="hlKey">
    <div><b style="color:#fff">⚪ White ring - hail core</b><br/>65+ dBZ: the storm's hail factory, where the updraft is strong enough to keep stones aloft growing.</div>
    <div><b style="color:#00e5ff">✛ Cyan cross - verified report</b><br/>Ground truth: where {mag:.1f}-inch stones actually fell ({when}).</div>
    <div><b style="color:#ffdc00">🌸 Yellow arc - inflow notch</b><br/>The storm's intake: warm air feeding in, carved where the echo bends inward on the storm's flank.</div>
    <div><b style="color:#a0dcff">🔵 Blue ring - forward flank</b><br/>The broad rain core downwind. Rain, not hail - a common trap when reading a storm.</div>
  </div>
  <div class="hlCap">{cap}</div>
</div>
<div id="hlTabpractice" style="display:none">
  <p class="hlPrompt">👆 Tap the map where <b>you</b> think the hail core is - the strongest echo (65+ dBZ).</p>
  <div class="hlImgWrap">
    <img id="hlImgPlain" src="{url_pln}" alt="Unannotated reflectivity cut - find the hail core yourself" loading="lazy"/>
    <span id="hlMark"></span>
  </div>
  <div id="hlFb" class="hlFb"></div>
  <div id="hlRecord" class="hlRecord"></div>
  <p class="hlHint">Tip: hail lives in the coldest colors - the tight magenta/white cluster, not the wide red rain shield. Your pick is scored against the verified core position.</p>
</div>
<div id="hlTabreveal" style="display:none">
  <div class="hlImgWrap"><img src="{url_ann}" alt="Annotated cut - revealed" loading="lazy"/></div>
  <p style="margin:12px 0 0;font-weight:700">The 4-step reading order:</p>
  <ol>
    <li><b>Find the hardest echo</b> - the tight magenta/white cluster. That's the hail core, the updraft's engine.</li>
    <li><b>Check the shape</b> - a core knuckled onto the storm's flank as an appendage means the updraft is tilted into the inflow, a classic severe signature.</li>
    <li><b>Find the inflow notch</b> - the carved-in indentation on the storm's intake side. Tight echo gradient there means strong rising motion.</li>
    <li><b>Compare the forward flank</b> - the broad rain shield downwind. If you called THAT the core, you read rain as hail; the real core sits back toward the notch.</li>
  </ol>
  <p class="hlCap" id="hlWhere">Practice first, then this shows how far your pick landed from the verified core.</p>
  <div id="hlQuiz"></div>
  <div id="hlQuizScore"></div>
  <div class="hlCap">{cap}</div>
</div>
<script>{js}</script>
</div>"""


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
        # psu_hrrr returns app-route '/app/static/...' file paths; the static
        # site must serve the docs-relative form or every <img src> 404s and
        # the player shows a broken image (spotted 2026-09-23).
        frames = []
        for f in loop.get("frames", []):
            ref = (f.get("file") or "").replace("\\", "/")
            if ref.startswith("/app/static/"):
                ref = "../" + ref[len("/app/static/"):]
            elif not ref.startswith("../"):
                ref = "../" + ref.removeprefix("static/")
            frames.append({"url": ref, "label": f["label"]})
        return {"init": loop.get("init"), "cycle": loop.get("cycle"),
                "frames": frames}
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
_SITE_FP = None


def _site_fingerprint():
    """Stable fingerprint of the site code (this file), cached per process.

    Ships in every data.json as siteVersion. Pages compare it against the
    fingerprint their persisted settings were saved under, so a preference
    like auto-refresh=off can't silently survive a site update - the flag
    from before the update is treated as stale and ignored once.
    """
    global _SITE_FP
    if _SITE_FP is None:
        try:
            with open(__file__, "rb") as f:
                _SITE_FP = hashlib.md5(f.read()).hexdigest()[:12]
        except OSError:
            _SITE_FP = "unknown"
    return _SITE_FP


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
  table.cells th.srt { cursor:pointer; user-select:none; white-space:nowrap; }
  table.cells th.srt:hover { color:var(--ink); }
  table.cells th.srt .dir { font-size:10px; margin-left:2px; color:var(--accent); }
  .trend { font-size:11px; font-weight:700; margin-left:3px; }
  .trend.up { color:#66bb6a; } .trend.dn { color:#ef5350; } .trend.fl { color:var(--dim); font-weight:400; }
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
             ("models.html", "Models"), ("tropical.html", "NHC"), ("storms.html", "Storms"),
             ("tropmodels.html", "Trop Models"), ("climate.html", "Climate"),
             ("enso.html", "El Niño"),
             ("severe.html", "Severe"),             ("winter.html", "Winter Forecast"),
             ("rivers.html", "Rivers"), ("fire.html", "Fire"), ("dashboard.html", "Dashboard"),
             ("traffic.html", "Traffic"),
             ("meso.html", "Mesoanalysis"),
             ("obs.html", "Obs & Skew-T"), ("charts.html", "Charts & MOS"), ("national.html", "National"),
             ("forecast.html", "Forecast"), ("education.html", "Education"),
             ("fieldguide.html", "Field Guide")]
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
    try {{ syncAutoPref(); }} catch (_e) {{}}   /* version now known: apply/wipe stale auto pref */
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
/* paused-banner: the STOP shortcut writes PAUSED.json, so any page that
   still loads while updates are paused tells the truth instead of showing
   a stale timestamp that looks like normal lag. START removes the marker. */
(async function () {{
  try {{
    const pr = await fetch("PAUSED.json?t=" + Date.now(), {{ cache: "no-store" }});
    if (!pr.ok) return;   /* 404 = running normally */
    const p = await pr.json();
    const pb = document.createElement("div");
    pb.style.cssText = "position:fixed;left:0;right:0;top:0;z-index:99999;"
      + "background:#b3261e;color:#fff;text-align:center;font-weight:700;"
      + "padding:8px 12px;font-size:14px;font-family:inherit;";
    pb.textContent = "\\u23f8 Weather updates are PAUSED - press the START shortcut "
      + "to resume. Data on this page is frozen"
      + (p && p.since ? " (paused " + String(p.since).replace(/^paused\\s*/, "") + ")" : "") + ".";
    document.body.appendChild(pb);
    document.body.style.paddingTop = "38px";
    if (autoCnt) autoCnt.textContent = "\\u221e";   /* freeze the auto-reload countdown too */
  }} catch (_e) {{ /* marker unreadable: treat as running */ }}
}})();
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
/* auto-refresh preference is remembered per siteVersion (a fingerprint of
   the site code, served in data.json). A saved "off" therefore cannot
   survive a site update: after new code pushes, the flag belongs to an old
   version, is wiped, and auto-refresh resumes - an update always re-opens
   the refresh tap. Before the first data pull resolves the version is
   unknown, so the checkbox defaults ON and re-syncs the moment data lands. */
function autoPrefKey() {{
  /* SITE_DATA is a script-level let - reachable by name in this scope but
     never as window.SITE_DATA, so the bare (guarded) reference is required */
  let v = "";
  try {{ v = (typeof SITE_DATA !== "undefined" && SITE_DATA && SITE_DATA.siteVersion) || ""; }} catch (_e) {{}}
  return v ? "tnwxAuto." + v : "";
}}
function syncAutoPref() {{
  const k = autoPrefKey();
  if (!k) return;
  try {{
    /* wipe the legacy flag and flags saved under other versions */
    Object.keys(localStorage)
      .filter(x => x === "tnwxAuto" || (x.indexOf("tnwxAuto.") === 0 && x !== k))
      .forEach(x => localStorage.removeItem(x));
    autoChk.checked = localStorage.getItem(k) !== "off";
  }} catch (_e) {{}}
}}
syncAutoPref();
autoChk.onchange = () => {{
  AUTO_LEFT = 90;
  const k = autoPrefKey();
  try {{ if (k) localStorage.setItem(k, autoChk.checked ? "on" : "off"); }} catch (_e) {{}}
}};
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
  window.TNWN_PUBLIC_URL = pub; /* reusable share target (certificate, etc.) */
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
    + (s.lastUpdate ? "<br/><span class=src>Advisory " + s.lastUpdate + "</span>" : "")
    + (s.graphicUrl ? "<br/><a href=\"" + s.graphicUrl + "\" target=\"_blank\" rel=\"noopener\">Official NHC graphic \u2197</a>" : "")
    + (s.sharePage ? "<br/><a href=\"" + s.sharePage + "\" target=\"_blank\" rel=\"noopener\" title=\"Open the shareable summary graphic\"><img src=\"" + s.sharePng + "\" alt=\"storm summary\" style=\"width:100%;max-width:270px;border-radius:8px;margin-top:6px\"/></a>" : "");
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
    + '<span style="margin-left:auto"><a href="tropical.html">Full NHC map \u2197</a></span></div>'
    + '<div style="display:flex;flex-wrap:wrap;gap:12px;margin-top:10px">'
    + list.filter(s => s.graphicUrl).map(s => {
        const img = s.sharePng || s.graphicUrl;
        const pg = s.sharePage || s.sharePng || s.graphicUrl;
        const fb = "https://www.facebook.com/sharer/sharer.php?u="
          + (s.shareUrl ? encodeURIComponent(s.shareUrl) : encodeURIComponent(location.origin + location.pathname));
        return '<span style="display:inline-flex;align-items:center;gap:6px">'
          + '<a href="' + pg + '" target="_blank" rel="noopener" title="Open shareable graphic">'
          + '<img src="' + img + '" alt="" loading="lazy" '
          + 'style="width:76px;height:48px;object-fit:cover;border-radius:6px;border:1px solid #333c46"></a>'
          + '<span style="display:inline-flex;flex-direction:column;gap:2px">'
          + '<a href="tropical.html">' + (s.name || "?") + ' cone \u2197</a>'
          + '<a href="' + fb + '" target="_blank" rel="noopener" style="color:#1877f2;font-weight:600">\ud83d\udce3 Share to FB</a>'
          + '</span></span>';
      }).join("")
    + '</div>';
  /* prominent banner when a storm threatens Tennessee */
  const bann = document.getElementById("tnBanner");
  if (bann) {
    const hit = list.find(s => s.tnThreat === "watch");
    const trk = list.find(s => s.tnThreat === "track");
    if (hit) {
      bann.innerHTML = '<div class="alert" style="border-left-color:#b22228;background:#2a1214;font-size:16px">'
        + '<b>\ud83d\udea8 ' + (hit.name || "Storm") + ': watch/warning area includes Tennessee</b>'
        + '<span>Monitor the NHC page and local alerts closely.</span></div>';
    } else if (trk) {
      bann.innerHTML = '<div class="alert" style="border-left-color:#e0a458;background:#241c10;font-size:16px">'
        + '<b>\ud83c\udf00 ' + (trk.name || "Storm") + ': forecast track toward Tennessee</b>'
        + '<span>Follow the cone and updates on the NHC page.</span></div>';
    } else bann.innerHTML = "";
  }
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


# --------------------------------------------------------- storm advisory archive
_ARCH_DIR = os.path.join("static", "archive")
_ARCH_KEEP = 24            # advisories kept per storm
_ARCH_MAX_BYTES = 60 * 1024 * 1024   # global cap across all storms
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0"}


def _arch_stamp(last_update):
    """YYYYMMDDHHMM from an NHC '2026-09-24 15:00Z' advisory timestamp."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})", last_update or "")
    if m:
        return "".join(m.groups())
    return time.strftime("%Y%m%d%H%M", time.gmtime())


def _archive_storm(raw, entry):
    """Persist one advisory for a storm; return its gallery bundle.

    Files land in static/archive/<id>/: <stamp>_cone.png (official NHC
    graphic), <stamp>_summary.png (the branded TNWN card) and a meta
    json. Writing is idempotent per advisory stamp, so the every-cycle
    build only downloads when NHC issues a new advisory. Returns
    {id,name,class,count,advisories:[latest 12, newest first]} or None.
    Never raises.
    """
    try:
        import requests as _rq
        sid = re.sub(r"[^a-z0-9]", "", (raw.get("id") or
                                        entry.get("name") or "storm").lower())
        sdir = os.path.join(_ARCH_DIR, sid)
        os.makedirs(sdir, exist_ok=True)
        stamp = _arch_stamp(entry.get("lastUpdate"))
        cone_p = os.path.join(sdir, f"{stamp}_cone.png")
        sum_p = os.path.join(sdir, f"{stamp}_summary.png")
        meta_p = os.path.join(sdir, f"{stamp}_meta.json")
        if not os.path.exists(cone_p):
            url = entry.get("graphicUrl")
            if not url:
                return None
            r = _rq.get(url, headers=_UA, timeout=30)
            if r.status_code != 200:
                return None
            tmp = cone_p + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(r.content)
            os.replace(tmp, cone_p)
        if not os.path.exists(sum_p) and entry.get("sharePng"):
            src = entry["sharePng"]
            if os.path.exists(src):
                shutil.copyfile(src, sum_p)
        if not os.path.exists(meta_p):
            meta = {k: entry.get(k) for k in
                    ("name", "classification", "intensity", "pressure",
                     "movement", "lastUpdate", "lat", "lon", "tnThreat",
                     "watches", "advisoryUrl", "graphicUrl")}
            meta["stamp"] = stamp
            meta["archivedAt"] = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime())
            tmp = meta_p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(meta, fh)
            os.replace(tmp, meta_p)
        _archive_prune_storm(sdir)
        return _archive_bundle(sid, entry.get("name") or "Storm",
                               entry.get("classification") or "")
    except Exception:  # noqa: BLE001
        return None


def _archive_prune_storm(sdir):
    """Keep only the newest _ARCH_KEEP advisories in one storm dir."""
    try:
        stamps = {}
        for f in os.listdir(sdir):
            m = re.match(r"(\d{12})_", f)
            if m:
                stamps.setdefault(m.group(1), []).append(
                    os.path.join(sdir, f))
        extra = sorted(stamps)[:-_ARCH_KEEP] if len(stamps) > _ARCH_KEEP else []
        for st in extra:
            for p in stamps[st]:
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception:  # noqa: BLE001
        pass


def _archive_enforce_budget():
    """Global cap: delete oldest-mtime archive files beyond 60 MB."""
    try:
        files = []
        total = 0
        for root, _dirs, fnames in os.walk(_ARCH_DIR):
            for f in fnames:
                p = os.path.join(root, f)
                try:
                    sz = os.path.getsize(p)
                    files.append((os.path.getmtime(p), sz, p))
                    total += sz
                except OSError:
                    continue
        if total <= _ARCH_MAX_BYTES:
            return
        for _mt, sz, p in sorted(files):
            if total <= _ARCH_MAX_BYTES:
                break
            try:
                os.remove(p)
                total -= sz
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass


def _archive_bundle(sid, name, cls):
    """Gallery bundle for one storm from its archive dir (latest 12)."""
    sdir = os.path.join(_ARCH_DIR, sid)
    if not os.path.isdir(sdir):
        return None
    stamps = {}
    for f in os.listdir(sdir):
        m = re.match(r"(\d{12})_(cone\.png|summary\.png|meta\.json)$", f)
        if m:
            stamps.setdefault(m.group(1), {})[m.group(2)] = \
                os.path.join(sdir, f).replace("\\", "/")
    advisories = []
    for st in sorted(stamps, reverse=True)[:12]:
        parts = stamps[st]
        meta = {}
        if "meta.json" in parts:
            try:
                meta = json.load(open(parts["meta.json"], encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        advisories.append({
            "stamp": st,
            "lastUpdate": meta.get("lastUpdate") or st,
            "intensity": meta.get("intensity"),
            "pressure": meta.get("pressure"),
            "tnThreat": meta.get("tnThreat"),
            "cone": "static/archive/" + sid + f"/{st}_cone.png",
            "summary": ("static/archive/" + sid + f"/{st}_summary.png"
                        if "summary.png" in parts else None),
        })
    if not advisories:
        return None
    return {"id": sid, "name": name, "class": cls,
            "count": len(stamps), "advisories": advisories}


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
  <div id="tnBanner"></div>
  <div id="tropBody"></div>
</div>

<div class="card">{cells_html or '<h2>🧠 AI storm tracker</h2><span class="src">No storm cells detected in the current HRRR forecast window.</span>'}</div>

<div class="card"><h2>📅 7-day forecast</h2><div class="grid cards7">{days_html}</div></div>

<div class="card"><h2>🏙️ City weather</h2>
  <p class="src">Pick any East Tennessee, Southwest Virginia or Western North Carolina city for its live observation and full 7-day forecast.</p>
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
    hail_lesson_card = _hail_lesson_card(d)
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

{ hail_lesson_card }
<script>{_player_js(json.dumps(layers))}
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
      " pending. The updater renders about 180 more every hour, missing ones first; " +
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


_PIVOT_JS = """
/* US forecast collage: every model on one wall at the same valid time
   (Pivot-schema style). The hour rail snaps the whole wall to any forecast
   hour; click a tile to blow it up with the full animated loop. */
(function(){
  const pvProd = document.getElementById("pvProd"), pvInit = document.getElementById("pvInit"),
        pvHours = document.getElementById("pvHours"), pvGrid = document.getElementById("pvGrid"),
        pvFocus = document.getElementById("pvFocus"), pvFh = document.getElementById("pvFh"),
        pvPlay = document.getElementById("pvPlay"),
        pvRegion = document.getElementById("pvRegion"), pvReset = document.getElementById("pvReset"),
        pvSrc = document.getElementById("pvSrc");
  if (!pvGrid || !pvGrid.isConnected) return;
  const PV_LBL = { sfc_mslp: "Surface - MSLP + wind", "500_vort": "500 mb - vorticity",
    "500_tmp": "500 mb - temperatures", "850_tmp": "850 mb - temperatures",
    "925_tmp": "925 mb - temperatures", "600_tmp": "600 mb - temperatures (melt layer)",
    "600_rh": "600 mb - RH (dendritic zone)", "700_rh": "700 mb - RH",
    thickness: "1000-500 mb thickness + 540 line", "700_w": "700 mb - omega (ascent)",
    sfc_dew: "2 m dew point + MSLP", shear06: "0-6 km bulk shear (severe)",
    "850_vort": "850 mb - vorticity + winds (tropical)",
    "200_div": "200 mb - divergence + winds (outflow)",
    "3var_fronts": "Surface - fronts analysis (isobars + 540 line + temps)",
    frz_lvl: "0C isotherm height (freezing level)",
    lr75: "700-500 mb lapse rate (hail)",
    scp: "Supercell Composite (CAPE x shear x helicity)",
    ehi: "Energy Helicity Index (CAPE x helicity)",
    stp: "Significant Tornado Parameter (SPC colors)",
    ship: "Significant Hail Parameter (SPC colors)",
    "250_jet": "250 mb - jet stream", "300_jet": "300 mb - jet stream", "200_jet": "200 mb - jet stream",
    pwat: "Precipitable water", tcdc: "Total cloud cover", snow: "Snowfall",
    cape_wind: "CAPE + 10 m wind (SPC colors)", mucape: "MUCAPE (SPC colors)", vis: "Visibility", qpf: "QPF (precip)" };
  const mLabel = m => (CAT[m] && CAT[m].label) || m;
  /* two wall sources: the zoomable CONUS render wall and the native East
     Tennessee render wall (the site renders both regions for every model).
     Each collects REND + the fuller-cycle PIVOT payload, deduped. */
  const collect = (rend, extra) => {
    const byP = {}, seen = {};
    rend.forEach(c => {
      const k = c.model + "|" + c.product + "|" + c.cycle;
      if (seen[k]) return; seen[k] = 1;
      (byP[c.product] = byP[c.product] || []).push(c);
    });
    (extra || []).forEach(c => {
      const k = c.model + "|" + c.product + "|" + c.cycle;
      if (seen[k]) return; seen[k] = 1;
      (byP[c.product] = byP[c.product] || []).push(c);
    });
    return { combos: byP,
             prods: Object.keys(byP).filter(p => byP[p].length >= 1)
               .sort((a, b) => byP[b].length - byP[a].length) };
  };
  const SRC = {
    us:  collect(REND.filter(c => c.region === "us"), window.PIVOTUS),
    etn: collect(REND.filter(c => c.region === "etn"), window.PIVOTETN),
  };
  let wall = "us";
  function fillProds() {
    const { combos, prods } = SRC[wall];
    if (!prods.length) {
      pvProd.innerHTML = "";
      pvGrid.innerHTML = "<span class=src>No " + (wall === "us" ? "US" : "East Tennessee")
        + " model renders yet - the updater fills this wall as maps finish.</span>";
      return;
    }
    pvProd.innerHTML = prods.map(p =>
      `<option value="${p}">${(PV_LBL[p] || p)}  (${combos[p].length} models)</option>`).join("");
    // boot to the fullest wall available: the (product, init) pair with the
    // most models on it. Models finish cycles at different speeds (AI fast,
    // globals slow), so the newest init is often the emptiest — the Pivot
    // behavior is a crowded wall first, freshness second.
    let best = null;
    prods.forEach(p => (combos[p] || []).forEach(c => {
      const cy = String(c.cycle);
      const n = combos[p].filter(x => String(x.cycle) === cy).length;
      if (!best || n > best.n) best = { p, n };
    }));
    if (best) pvProd.value = best.p;
  }
  /* ---- shared camera: zoom/pan in map-fraction units. Every tile renders
     the same CONUS Lambert map, so one camera moves the whole wall
     together - zoom to Tennessee and compare all models on the same
     neighborhood. Calibration (PIVOTREG, measured server-side from the
     real PNGs) says where the map area sits inside each image (colorbar
     width differs per product) and where the region presets land. A
     square fraction box keeps crops undistorted: the map area has one
     fixed aspect, so square fractions = aspect-true. */
  const REGC = window.PIVOTREG || {};
  const PV_MR = REGC.mapRect || { left: 11, top: 54, right: 1129, bottom: 686 };
  const PV_AR = (PV_MR.right - PV_MR.left) / (PV_MR.bottom - PV_MR.top) || 1.769;
  const PV_PRESETS = REGC.regions
    || { us: { label: "🗺️ Full US", left: 0, top: 0, width: 1, height: 1 } };
  let cam = null;                       // {cx, cy, w} in fractions; null = full view
  const pvClamp = c => {
    const w = Math.min(Math.max(c.w, 0.05), 1);
    return { w, cx: Math.min(Math.max(c.cx, w / 2), 1 - w / 2),
             cy: Math.min(Math.max(c.cy, w / 2), 1 - w / 2) };
  };
  const pvBox = () => cam || { cx: 0.5, cy: 0.5, w: 1 };
  function pvClear(img) {              // native walls: no camera, plain img
    img.style.position = "relative";
    img.style.left = ""; img.style.top = "";
    img.style.width = "100%"; img.style.cursor = "zoom-in";
    const box = img.parentElement;
    if (box) box.style.height = "";
  }
  function pvApply(img) {
    const box = img.parentElement;
    if (!box || !box.classList.contains("pvCamBox")) return;
    if (wall !== "us") return pvClear(img);
    const nw = img.naturalWidth, nh = img.naturalHeight;
    if (!nw || !nh) return;             // not decoded yet - onload re-applies
    const Wd = box.clientWidth || box.getBoundingClientRect().width;
    if (!Wd) return;
    box.style.height = (Wd / PV_AR).toFixed(1) + "px";
    const ax0 = PV_MR.left / nw, ay0 = PV_MR.top / nh;
    const axs = (PV_MR.right - PV_MR.left) / nw, ays = (PV_MR.bottom - PV_MR.top) / nh;
    const b = pvBox();
    const L = ax0 + (b.cx - b.w / 2) * axs, T = ay0 + (b.cy - b.w / 2) * ays;
    const s = Wd / (b.w * axs * nw);    // display px per source px
    img.style.position = "absolute";
    img.style.width = (nw * s).toFixed(1) + "px";
    img.style.height = "auto";
    img.style.left = (-L * nw * s).toFixed(1) + "px";
    img.style.top = (-T * nh * s).toFixed(1) + "px";
  }
  const pvApplyAll = () => document.querySelectorAll(".pvCamBox img")
    .forEach(img => wall === "us" ? pvApply(img) : pvClear(img));
  function pvFrac(e, img) {             // cursor position -> map-fraction coords
    const r = img.getBoundingClientRect();
    const nw = img.naturalWidth, nh = img.naturalHeight;
    const ax0 = PV_MR.left / nw, axs = (PV_MR.right - PV_MR.left) / nw;
    const ay0 = PV_MR.top / nh, ays = (PV_MR.bottom - PV_MR.top) / nh;
    return [ax0 + ((e.clientX - r.left) / r.width) * axs,
            ay0 + ((e.clientY - r.top) / r.height) * ays];
  }
  function pvZoom(f, fx, fy) {          // keep the point under the cursor fixed
    const b0 = pvBox();
    const w1 = Math.min(Math.max(b0.w * f, 0.05), 1);
    cam = pvClamp({ w: w1, cx: fx + (b0.cx - fx) * (w1 / b0.w),
                    cy: fy + (b0.cy - fy) * (w1 / b0.w) });
    pvApplyAll();
  }
  let pvDrag = null, pvMoved = false;
  function pvWheel(e) {
    const img = e.target.closest("img");
    if (!img || wall !== "us") return;   // native walls scroll the page
    if (!img.naturalWidth || !img.parentElement.classList.contains("pvCamBox")) return;
    e.preventDefault();
    const p = pvFrac(e, img);
    pvZoom(e.deltaY < 0 ? 0.85 : 1 / 0.85, p[0], p[1]);
  }
  function pvDown(e) {
    const img = e.target.closest("img");
    if (!img || wall !== "us" || !img.naturalWidth) return;
    pvDrag = { img, x: e.clientX, y: e.clientY, b: pvBox() };
    pvMoved = false;
  }
  function pvMove(e) {
    if (!pvDrag) return;
    if (Math.abs(e.clientX - pvDrag.x) + Math.abs(e.clientY - pvDrag.y) > 5) pvMoved = true;
    const Wd = pvDrag.img.parentElement.clientWidth || 1;
    cam = pvClamp({ w: pvDrag.b.w,
                    cx: pvDrag.b.cx - (e.clientX - pvDrag.x) * pvDrag.b.w / Wd,
                    cy: pvDrag.b.cy - (e.clientY - pvDrag.y) * pvDrag.b.w / Wd });
    pvApplyAll();
  }
  function pvUp() { pvDrag = null; }
  [pvGrid, pvFocus].forEach(el => {
    el.addEventListener("wheel", pvWheel, { passive: false });
    el.addEventListener("mousedown", pvDown);
  });
  window.addEventListener("mousemove", pvMove);
  window.addEventListener("mouseup", pvUp);
  window.addEventListener("resize", pvApplyAll);
  if (pvReset) pvReset.onclick = () => { cam = null; pvApplyAll(); };
  if (pvRegion) {
    // 29 views in one select: broad regions grouped first, then states &
    // cities - the group headers keep the list scannable.
    const grp = {};
    Object.keys(PV_PRESETS).forEach(k => {
      const r = PV_PRESETS[k], g = r.group || "Regions";
      (grp[g] = grp[g] || []).push([k, r.label || k]);
    });
    pvRegion.innerHTML = ["Regions", "States & cities"]
      .filter(g => grp[g] && grp[g].length)
      .map(g => `<optgroup label="${g}">` +
        grp[g].map(([k, l]) => `<option value="${k}">${l}</option>`).join("") +
        `</optgroup>`).join("");
    pvRegion.onchange = () => {
      const r = PV_PRESETS[pvRegion.value];
      if (!r) return;
      if (r.width >= 0.999) cam = null;  // Full US preset
      else {
        // square-ify in fraction space around the preset's centre: covers
        // the whole box with no distortion (fraction-square = aspect-true)
        const s = Math.min(1, Math.max(r.width, r.height));
        cam = pvClamp({ w: s, cx: r.left + r.width / 2, cy: r.top + r.height / 2 });
      }
      pvApplyAll();
    };
  }
  let curFh = null, pvTimer = null;
  const curCombos = () => (SRC[wall].combos[pvProd.value] || []).filter(c => !pvInit.value || String(c.cycle) === pvInit.value);
  function fillInit() {
    const all = SRC[wall].combos[pvProd.value] || [];
    const cycs = [...new Set(all.map(c => String(c.cycle)))];
    const cnt = cycs.map(c => ({ c, n: all.filter(x => String(x.cycle) === c).length }));
    // fullest cycle wins (a wall of 14 models beats a fresh 1-model run);
    // newest cycle breaks ties so the wall prefers the freshest full set
    cnt.sort((a, b) => b.n - a.n || (a.c < b.c ? 1 : -1));
    // "All cycles" first: CAMs run hourly off their own inits while globals
    // sit 6-hourly, so staggered-cycle products (sfc_gust at HRRR 15Z + RAP
    // 15Z vs GFS/RRFS 12Z) never show one full wall in any single cycle -
    // the mixed view is the honest default and matches the dropdown's
    // model count (2026-09-23). Nearest-earlier snapping keeps the tiles
    // comparable on the hour rail.
    const allN = all.length;
    pvInit.innerHTML = `<option value="">All cycles (${allN} models)</option>`
      + cnt.map(x => `<option value="${x.c}">${x.c.slice(-6, -2)}Z ${x.c.slice(-2)} (${x.n} models)</option>`).join("");
    // default to All cycles unless one single cycle holds 4+ models
    // (then that crowded same-cycle wall is the better default)
    if (!(cnt.length && cnt[0].n >= 4)) pvInit.value = "";
  }
  function hourRail() {
    const fhs = [...new Set(curCombos().flatMap(c => c.frames.map(f => f.fh)))].sort((a, b) => a - b);
    pvHours.innerHTML = "";
    fhs.forEach(fh => {
      const b = document.createElement("button");
      b.className = "pvH";
      b.style.cssText = "padding:3px 8px;border-radius:7px;border:1px solid #345;background:#0d1117;color:#cbd5e1;font-size:11px;cursor:pointer";
      b.textContent = "F" + String(fh).padStart(3, "0");
      b.onclick = () => { pvStop(); setFh(fh); };
      pvHours.appendChild(b);
    });
    return fhs;
  }
  function setFh(fh) {
    curFh = fh;
    pvFh.textContent = "F" + String(fh).padStart(3, "0");
    pvHours.querySelectorAll(".pvH").forEach(b =>
      b.style.background = +b.textContent.slice(1) === fh ? "#2b80ff" : "");
    pvGrid.innerHTML = curCombos()
      .sort((a, b) => mLabel(a.model).localeCompare(mLabel(b.model)))
      .map(c => {
        const avail = c.frames.filter(f => f.fh <= fh);
        const f = avail[avail.length - 1] || c.frames[0];
        if (!f) return "";
        const exact = f.fh === fh;
        return `<figure class="pvTile" data-m="${c.model}" style="margin:0;background:#0d1117;border:1px solid #23304a;border-radius:10px;overflow:hidden">`
          + `<div class="pvCamBox" style="position:relative;overflow:hidden;background:#0d1117">`
          + `<img src="${f.url}" style="display:block" alt="${mLabel(c.model)}"/></div>`
          + `<figcaption style="font-size:11px;padding:4px 7px;color:#8fa3bf">${mLabel(c.model)} - F${String(f.fh).padStart(3, "0")}`
          + (exact ? "" : ` <i>(nearest to F${String(fh).padStart(3, "0")})</i>`) + "</figcaption></figure>";
      }).join("");
    pvGrid.querySelectorAll(".pvTile").forEach(t => {
      const img = t.querySelector("img");
      img.style.cursor = wall === "us" ? "grab" : "zoom-in";
      img.onload = () => pvApply(img);
      if (img.complete && img.naturalWidth) pvApply(img);
      t.onclick = () => { if (!pvMoved) focus(t.dataset.m); };
    });
    spotOutlier();
  }
  function redraw() {
    pvLegendUpdate();   // every wall path flows through here (share-links set the select programmatically, no change event)
    if (!SRC[wall].prods.length) return;   // fillProds already messaged the grid
    outState = { fh: null, wall: null, timer: null };
    const fhs = hourRail();
    if (!fhs.length) { pvGrid.innerHTML = "<span class=src>No frames for this combo yet.</span>"; return; }
    const cs = curCombos(), thresh = Math.ceil(cs.length / 2);
    let def = null;
    fhs.forEach(fh => {
      if (cs.filter(c => c.frames.some(f => f.fh === fh)).length >= thresh) def = fh;
    });
    setFh(def != null ? def : fhs[Math.floor(fhs.length / 2)]);
  }
  /* ---- outlier spotlight: which tile disagrees most with the wall?
     Each tile's map area is down-sampled onto a 48x27 canvas, compared
     pairwise to every other tile, and the tile whose median absolute
     difference to the rest is largest is flagged. Renders cross products
     (colorbars at different x), so only the map rect is compared - the
     PIVOTREG mapRect/mapRectEtn pixel constants calibrated from the real
     PNGs make every tile comparable. */
  const OUT_W = 48, OUT_H = 27;
  let outState = { fh: null, wall: null, timer: null };
  function outBadge(model) { return document.querySelector(`.pvTile[data-m="${CSS.escape(model)}"] .pvOut`); }
  function outClearAll() { document.querySelectorAll(".pvOut").forEach(b => b.remove()); }
  function outData(img) {
    const cv = document.createElement("canvas");
    cv.width = OUT_W; cv.height = OUT_H;
    const cx = cv.getContext("2d", { willReadFrequently: true });
    const mr = wall === "us" ? PV_MR : (REGC.mapRectEtn || null);
    if (!mr) return null;
    try { cx.drawImage(img, mr.left, mr.top, mr.right - mr.left, mr.bottom - mr.top, 0, 0, OUT_W, OUT_H); }
    catch (err) { return null; }
    const d = cx.getImageData(0, 0, OUT_W, OUT_H).data;
    // lazy-loaded images outside the viewport may report naturalWidth but
    // still have deferred pixels - drawImage then paints nothing (blank
    // black canvas would poison every pairwise score). Detect all-black
    // samples and treat them as not-yet-sampled.
    let br = 0;
    for (let i = 0; i < d.length; i += 400) br += d[i];
    if (br < 2) return null;
    return d;
  }
  function outScore(d, set) {          // mean |px - other| over the other tiles
    let tot = 0;
    for (let i = 0; i < d.length; i += 4) {
      for (let k = 0; k < set.length; k++) {
        tot += Math.abs(d[i] - set[k][i]) + Math.abs(d[i + 1] - set[k][i + 1])
             + Math.abs(d[i + 2] - set[k][i + 2]);
      }
    }
    return tot / (d.length / 4) / set.length / 3;
  }
  /* ---- SPC-composite axis outlier (scp/mucape walls): the composite's
     axis IS the story on these walls, so instead of generic pixel scoring
     each tile's fill is decoded back to parameter units through the SPC
     palette (data/model_maps.py _SPC_STOPS, 0..8 composite units,
     contourf alpha .85 over the white figure face). Un-blending and
     nearest-matching the stops recovers the value to ~0.25 (the fill
     step); off-palette pixels (contours, labels, state lines) fail the
     tolerance gate. The axis = centroid of value >= 1 (supercell/significant
     air) pixels, and the model whose axis sits farthest from the models'
     mean axis - with the rest genuinely clustered - is flagged on its
     tile. mucape shares the comparison because its US wall is fully
     populated while scp's US tiles are still in the render backlog. */
  const SCP_STOPS = [[0, [193, 233, 193]], [.2, [102, 205, 170]], [.4, [255, 255, 0]],
                     [.6, [255, 140, 0]], [.8, [255, 0, 0]], [1, [255, 0, 255]]];
  const SCP_MAX = 8, SCP_ALPHA = .85, SCP_TOL = 45;
  const SCP_WALLS = new Set(["scp", "mucape"]);
  const SCP_LUT = (() => {
    const lut = [];
    for (let t = 0; t < 256; t++) {
      const v = t / 255;
      let a = SCP_STOPS[0], b = SCP_STOPS[SCP_STOPS.length - 1];
      for (let i = 0; i < SCP_STOPS.length - 1; i++)
        if (v >= SCP_STOPS[i][0] && v <= SCP_STOPS[i + 1][0]) { a = SCP_STOPS[i]; b = SCP_STOPS[i + 1]; break; }
      const f = (v - a[0]) / Math.max(1e-9, b[0] - a[0]);
      lut.push([0, 1, 2].map(k => a[1][k] + (b[1][k] - a[1][k]) * f));
    }
    return lut;
  })();
  /* compass direction of the offset: image y grows DOWNWARD = south, so
     atan2(y, x) reads E/SE/S/SW/W/NW/N/NE at the 8 compass points */
  const SCP_DIR8 = ["E", "SE", "S", "SW", "W", "NW", "N", "NE"];
  function scpAxisOf(d) {
    const n = d.length / 4;
    let sw = 0, sx = 0, sy = 0, peak = 0, hits = 0;
    for (let p = 0; p < n; p++) {
      const i = p * 4;
      const r = (d[i] - 255 * (1 - SCP_ALPHA)) / SCP_ALPHA,
            g = (d[i + 1] - 255 * (1 - SCP_ALPHA)) / SCP_ALPHA,
            b = (d[i + 2] - 255 * (1 - SCP_ALPHA)) / SCP_ALPHA;
      let best = 0, bd = 1e9;
      for (let t = 0; t < 256; t++) {
        const c = SCP_LUT[t];
        const dd = (r - c[0]) * (r - c[0]) + (g - c[1]) * (g - c[1]) + (b - c[2]) * (b - c[2]);
        if (dd < bd) { bd = dd; best = t; }
      }
      if (bd > SCP_TOL * SCP_TOL) continue;
      const v = (best / 255) * SCP_MAX;
      if (v > peak) peak = v;
      if (v >= 1) { hits++; sw += v; sx += (p % OUT_W) * v; sy += ((p / OUT_W) | 0) * v; }
    }
    if (hits < Math.max(6, n * .005)) return null;   // no supercell air
    return { x: sx / sw / OUT_W, y: sy / sw / OUT_H, peak };
  }
  function scpAxisPass(fh, pool) {
    const sum = document.getElementById("pvOutSum");
    const axes = pool.map(img => ({ img, m: img.closest(".pvTile").dataset.m,
                                    ax: scpAxisOf(img._outData) }))
                      .filter(s => s.ax);
    if (axes.length < 3) {
      if (sum) sum.textContent = "🎯 " + (pvProd ? pvProd.value : "composite")
        + " axis: too little composite >= 1 air on the wall this hour ("
        + axes.length + "/" + pool.length + " models have an axis) - nothing to compare.";
      return;
    }
    const mx = axes.reduce((s, a) => s + a.ax.x, 0) / axes.length,
          my = axes.reduce((s, a) => s + a.ax.y, 0) / axes.length;
    axes.forEach(a => { a.d = Math.hypot(a.ax.x - mx, a.ax.y - my); });
    axes.sort((a, b) => b.d - a.d);
    const top = axes[0], rest = axes.slice(1);
    const restMean = rest.reduce((s, a) => s + a.d, 0) / rest.length;
    const dir = SCP_DIR8[((Math.round(Math.atan2(top.ax.y - my, top.ax.x - mx) / (Math.PI / 4)) % 8) + 8) % 8];
    const pct = Math.round(top.d * 100);
    // a real outlier: clearly displaced (>= 10% of the map) AND far above
    // how tightly the other models cluster; agreeing walls get no flag
    const off = top.d >= 0.10 && top.d > restMean * 1.8;
    outClearAll();
    if (off) {
      const b = document.createElement("span");
      b.className = "pvOut";
      b.style.cssText = "position:absolute;top:4px;right:4px;background:#c62828;color:#fff;"
        + "font-size:10px;font-weight:600;padding:2px 7px;border-radius:8px;z-index:2"
        + ";box-shadow:0 1px 4px rgba(0,0,0,.5)";
      b.textContent = "⚠ axis " + pct + "% " + dir;
      b.title = mLabel(top.m) + "'s composite >= 1 axis sits " + pct + "% " + dir
        + " of the " + axes.length + "-model mean position; the other " + rest.length
        + " models cluster within " + Math.round(restMean * 100)
        + "%. Its peak value is " + top.ax.peak.toFixed(1) + ".";
      const tile = top.img.closest(".pvTile");
      tile.style.position = "relative";
      tile.appendChild(b);
    }
    if (sum) {
      const pn = pvProd ? (pvProd.options[pvProd.selectedIndex] || {}).text || pvProd.value : "composite";
      if (off) sum.innerHTML = "🎯 " + pn + " axis at F" + String(fh).padStart(3, "0")
        + ": <b>" + mLabel(top.m) + "</b> is the outlier - its axis sits "
        + pct + "% " + dir + " of the " + axes.length + "-model mean (the rest within "
        + Math.round(restMean * 100) + "%). Peak " + top.ax.peak.toFixed(1) + ".";
      else sum.textContent = "🤝 " + pn + " axes agree at F" + String(fh).padStart(3, "0")
        + " (" + axes.length + " models within "
        + Math.round(Math.max(...axes.map(a => a.d)) * 100)
        + "% of the mean axis) - no axis outlier this hour.";
    }
  }
  function spotOutlier() {
    const fh = curFh;
    if (outState.timer) { clearTimeout(outState.timer); outState.timer = null; }
    if (fh === outState.fh && wall === outState.wall) return;
    outState.fh = fh; outState.wall = wall;
    outState.tries = 0;
    for (const img of document.querySelectorAll("#pvGrid .pvCamBox img")) img._outData = undefined;
    outClearAll();
    if (fh == null) return;             // no hour selected yet
    const sum = document.getElementById("pvOutSum");
    if (sum) sum.textContent = "🕵️ Watching for outlier models\u2026";
    const run = () => {
      if (curFh !== fh || wall !== outState.wall) return;   // wall moved on
      const ready = [];
      for (const img of document.querySelectorAll("#pvGrid .pvCamBox img")) {
        if (!img.naturalWidth) continue;                    // still decoding
        if (img._outData === undefined) img._outData = outData(img);
        if (img._outData) { ready.push(img); continue; }
        // blank sample: force the lazy image's pixels to decode, retry next pass
        if (img.decode) img.decode().catch(() => {}).then(() => { img._outData = undefined; });
      }
      outState.tries = (outState.tries || 0) + 1;
      if (ready.length < 4 && outState.tries < 24) {   // wait for decodes
        outState.timer = setTimeout(run, 350);
        return;
      }
      if (ready.length < 4) {           // wall too sparse to judge
        if (sum) sum.textContent = "🕵️ Outlier watch paused - not enough tile images loaded yet (scroll the wall into view and switch hours to retry).";
        return;
      }
      // Compare only tiles DISPLAYING the same frame hour (from each
      // tile's figcaption): nearest-earlier snapping means a CFS F006
      // tile and an AI F042 tile show different valid times, and mixing
      // them measures the clock, not the models. The biggest same-hour
      // group on the wall is the comparison pool.
      const groups = {};
      for (const img of ready) {
        const cap = img.closest(".pvTile").querySelector("figcaption");
        const mm = cap ? cap.textContent.match(/F([0-9]{3})/) : null;
        const dh = mm ? +mm[1] : -1;
        (groups[dh] = groups[dh] || []).push(img);
      }
      let pool = null;
      for (const dh of Object.keys(groups))
        if (groups[dh].length >= 4 && (!pool || groups[dh].length > pool.length)) pool = groups[dh];
      const sum = document.getElementById("pvOutSum");
      if (!pool) {
        if (sum) sum.textContent = "🕵️ Outlier watch needs 4+ models showing the same hour - this hour snaps too unevenly.";
        return;
      }
      /* On the SPC-composite walls the axis comparison IS the right
         measure - the generic pixel scoring would just measure
         colorbar/UI noise. */
      if (SCP_WALLS.has(pvProd.value)) { scpAxisPass(fh, pool); return; }
      const scored = pool.map(img => ({ img, m: img.closest(".pvTile").dataset.m,
                                        d: img._outData }));
      scored.forEach(s => { s.score = outScore(s.d, scored.filter(o => o !== s).map(o => o.d)); });
      scored.sort((a, b) => b.score - a.score);
      const top = scored[0];
      // a real outlier stands clearly above the runner-up; walls of
      // roughly-agreeing models cluster within ~20% and get no flag
      if (top.score < scored[1].score * 1.25) {
        if (sum) sum.textContent = "🤝 Wall in agreement at F" + String(fh).padStart(3, "0")
          + " (" + scored.length + " models compared) - no outlier this hour.";
        return;
      }
      const b = document.createElement("span");
      b.className = "pvOut";
      b.style.cssText = "position:absolute;top:4px;right:4px;background:#c62828;color:#fff;"
        + "font-size:10px;font-weight:600;padding:2px 7px;border-radius:8px;z-index:2"
        + ";box-shadow:0 1px 4px rgba(0,0,0,.5)";
      b.textContent = "⚠ outlier";
      b.title = top.m + " differs most from the other " + (scored.length - 1)
        + " models at this valid time: mean pixel distance " + top.score.toFixed(1)
        + "/255 vs " + scored[1].m + "'s " + scored[1].score.toFixed(1) + ".";
      const tile = top.img.closest(".pvTile");
      tile.style.position = "relative";
      tile.appendChild(b);
      if (sum) sum.innerHTML = "🕵️ <b>" + mLabel(top.m) + "</b> is the outlier at F"
        + String(fh).padStart(3, "0") + " (" + scored.length + " models compared) - hovering its \u26a0 badge shows how far from the pack it sits.";
    };
    run();
  }
  function focus(m) {
    const c = curCombos().find(x => x.model === m);
    if (!c) return;
    pvStop();
    let at = c.frames.findIndex(f => f.fh === curFh); if (at < 0) at = c.frames.length - 1;
    const camBox = document.createElement("div");
    camBox.className = "pvCamBox";
    camBox.style.cssText = "position:relative;overflow:hidden;border-radius:10px;background:#0d1117";
    const im = new Image(); im.style.cssText = "display:block;cursor:grab"; im.src = c.frames[at].url;
    const st = document.createElement("div"); st.className = "ctl"; st.style.marginTop = "8px";
    st.innerHTML = `<b>${mLabel(m)}</b>`
      + `<button id="pvB">\u25c0</button><select id="pvS">${c.frames.map(f => `<option value="${f.fh}">F${String(f.fh).padStart(3, "0")}</option>`).join("")}</select><button id="pvF">\u25b6</button>`
      + `<button id="pvP">\u23f8</button><span class="frame" id="pvL"></span>`
      + ` <a href="${c.frames[at].url}" target="_blank" style="font-size:12px">full size</a>`;
    im.onload = () => pvApply(im);
    if (im.complete && im.naturalWidth) pvApply(im);
    pvFocus.innerHTML = ""; pvFocus.append(camBox, st); camBox.append(im);
    const sel = st.querySelector("#pvS"), lab = st.querySelector("#pvL"), pb = st.querySelector("#pvP");
    const show = k => { const f = c.frames[k]; sel.value = f.fh; im.src = f.url; lab.textContent = "F" + String(f.fh).padStart(3, "0"); };
    let timer = null;
    const stop = () => { if (timer) { clearInterval(timer); timer = null; pb.textContent = "\u25b6"; } };
    const play = () => { stop(); pb.textContent = "\u23f8";
      timer = setInterval(() => show(at = (at + 1) % c.frames.length), 700); };
    show(at);
    st.querySelector("#pvB").onclick = () => { stop(); show(at = Math.max(0, at - 1)); };
    st.querySelector("#pvF").onclick = () => { stop(); show(at = Math.min(c.frames.length - 1, at + 1)); };
    pb.onclick = () => timer ? stop() : play();
    sel.onchange = () => { stop(); at = c.frames.findIndex(f => f.fh === +sel.value); show(at); };
    if (c.frames.length > 1) play();
    pvFocus.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  function pvStop() { if (pvTimer) { clearInterval(pvTimer); pvTimer = null; pvPlay.textContent = "\u25b6 Wall"; } }
  pvPlay.onclick = () => {
    if (pvTimer) return pvStop();
    const fhs = [...pvHours.querySelectorAll(".pvH")].map(b => +b.textContent.slice(1));
    if (!fhs.length) return;
    pvPlay.textContent = "\u23f8";
    let i = 0;
    pvTimer = setInterval(() => { if (i >= fhs.length) { pvStop(); return; } setFh(fhs[i++]); }, 1600);
  };
  pvProd.onchange = () => { pvStop(); fillInit(); redraw(); };
  pvInit.onchange = () => { pvStop(); redraw(); };
  /* SPC legend strip: the severe-parameter and instability walls all use
     the SPC outlook palette, so the plain-English band key shows for them
     (2026-09-22 'match the severe page colors'). */
  const SPC_PRODS = new Set(["shear06", "lr75", "scp", "ehi", "mucape", "cape_wind", "stp", "ship"]);
  const pvLegend = document.getElementById("pvLegend");
  function pvLegendUpdate() {
    if (pvLegend) pvLegend.style.display = SPC_PRODS.has(pvProd.value) ? "flex" : "none";
  }
  pvProd.addEventListener("change", pvLegendUpdate);
  function setWall(w) {
    wall = w;
    cam = null;
    if (pvRegion) pvRegion.disabled = w !== "us";
    if (pvReset) pvReset.disabled = w !== "us";
    fillProds(); fillInit(); redraw(); pvLegendUpdate();
    pvApplyAll();
  }
  if (pvSrc) {
    pvSrc.value = "us";
    pvSrc.onchange = () => { pvStop(); setWall(pvSrc.value); };
  }
  /* share: copy a link to the current wall view. The URL carries the wall
     source, product, init cycle, forecast hour and camera crop, so whoever
     opens it lands on the exact same comparison. */
  const pvShare = document.getElementById("pvShare");
  const fallbackCopy = (txt, done) => {
    const ta = document.createElement("textarea");
    ta.value = txt;
    ta.style.cssText = "position:fixed;left:-9999px;top:0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    if (ok) done(); else window.prompt("Copy this link:", txt);
  };
  const shareReset = () => { setTimeout(() => { pvShare.textContent = "🔗 Share"; }, 1800); };
  if (pvShare) pvShare.onclick = () => {
    const u = new URL(location.origin + location.pathname);
    u.searchParams.set("pv", wall);
    if (pvProd.value) u.searchParams.set("pvProd", pvProd.value);
    if (pvInit.value) u.searchParams.set("pvInit", pvInit.value);
    if (curFh != null) u.searchParams.set("pvFh", String(curFh));
    if (wall === "us" && cam)
      u.searchParams.set("pvCam", cam.cx.toFixed(4) + "," + cam.cy.toFixed(4) + "," + cam.w.toFixed(4));
    const link = u.toString();
    const done = () => { pvShare.textContent = "✓ Copied"; shareReset(); };
    if (navigator.clipboard && navigator.clipboard.writeText)
      navigator.clipboard.writeText(link).then(done, () => fallbackCopy(link, done));
    else fallbackCopy(link, done);
  };
  /* boot: a shared link (?pv=etn&pvProd=500_vort&pvInit=...&pvFh=42&pvCam=...)
     lands straight on the wall view it captured, before the auto-pick logic
     chooses its own fullest-cycle defaults. */
  const qs = new URLSearchParams(location.search);
  const pvQ = qs.get("pv") === "etn" ? "etn" : (qs.get("pv") === "us" ? "us" : null);
  if (pvQ) {
    if (pvSrc) pvSrc.value = pvQ;
    setWall(pvQ);
    const wp = qs.get("pvProd");
    if (wp && SRC[pvQ].combos[wp]) {
      // every rendered product is listed now (no visibility bar); the
      // injection stays as a belt-and-suspenders for a stale deep link
      // whose product row has since left the render index entirely
      if (![...pvProd.options].some(o => o.value === wp)) {
        const opt = document.createElement("option");
        opt.value = wp;
        opt.textContent = (PV_LBL[wp] || wp) + " (" + SRC[pvQ].combos[wp].length + " models)";
        pvProd.appendChild(opt);
      }
      pvProd.value = wp;
    }
    fillInit();
    const wi = qs.get("pvInit");
    if (wi && [...pvInit.options].some(o => o.value === wi)) pvInit.value = wi;
    redraw();
    const qf = parseInt(qs.get("pvFh"), 10);
    if (isFinite(qf) && [...pvHours.querySelectorAll(".pvH")].some(b => +b.textContent.slice(1) === qf)) setFh(qf);
    const qc = qs.get("pvCam");
    if (qc) {
      const a = qc.split(",").map(Number);
      if (a.length === 3 && a.every(Number.isFinite)) { cam = pvClamp({ cx: a[0], cy: a[1], w: a[2] }); pvApplyAll(); }
    }
  } else {
    fillProds(); fillInit(); redraw(); pvLegendUpdate();
  }
})();
/* severe-composite town ranking: re-render in place on the 90 s soft
   refresh so a newly-crossing town appears without a page reload */
window.onDataRefresh = function (d2) { if (d2 && d2.sevTowns) sevTownRender(d2.sevTowns); };
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
    <span class="src" style="margin:0">Area/View</span>
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

<div class="card"><h2>🗺️ US forecast collage — every model, same hour, one wall</h2>
  <div class="ctl">
    <span class="src" style="margin:0">Wall</span>
    <select id="pvSrc"><option value="us">US (zoomable)</option><option value="etn">East TN (native)</option></select>
    <span class="src" style="margin:0">Product</span>
    <select id="pvProd"></select>
    <span class="src" style="margin:0">Init</span>
    <select id="pvInit"></select>
    <button id="pvPlay">▶ Wall</button>
    <span class="frame" id="pvFh">--</span>
    <span class="src" style="margin:0">Area/View</span>
    <select id="pvRegion"></select>
    <button id="pvReset" title="reset the view to the full US map">⌘ Reset view</button>
    <button id="pvShare" title="copy a link that reopens this exact wall - source, product, init, hour and zoom">🔗 Share</button>
  </div>
  <div class="src" style="margin:2px 0 0">Scroll to zoom · drag to pan · one camera for the whole wall — zoom to Tennessee and every model snaps to the same neighborhood. Switch the wall to <b>East TN</b> for the site's native sharp renders of our region. <b>🔗 Share</b> copies a link that reopens this exact view.</div>
  <div id="pvHours" style="display:flex;flex-wrap:wrap;gap:4px;margin:8px 0"></div>
  <div class="src" id="pvLegend" style="display:none;margin:2px 0 0;align-items:center;flex-wrap:wrap;gap:4px 10px">
    <span style="color:#c1e9c1">&#9632;</span> unorganized — storms, if any, stay ordinary
    <span style="color:#66cdaa">&#9632;</span> MRGL — marginal, isolated severe possible
    <span style="color:#ffff00">&#9632;</span> SLGT — slight, scattered severe storms
    <span style="color:#ff8c00">&#9632;</span> ENH — enhanced, numerous severe storms
    <span style="color:#ff0000">&#9632;</span> MDT — moderate, widespread severe likely
    <span style="color:#ff00ff">&#9632;</span> HIGH — rare, long-track strong tornado / MCS outbreak
    <span style="color:#8fa3bf">— colors match the SPC outlooks on the Severe page</span>
  </div>
  <div id="pvFocus"></div>
  <div id="pvGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;margin-top:10px"></div>
  <div class="src">One tile per model at the same valid time — spot model-to-model disagreement instantly, like the Pivot forecast wall. Click a tile to blow it up with the full animated loop; ▶ Wall animates the valid hour across every model at once. Tiles missing the exact hour snap to the nearest earlier frame and say so.</div>
</div>

<div class="card" id="sevTownCard"><h2>🎯 Severe composite threat — towns in parameter air</h2>
  <div class="src" id="sevTownNote">checking the latest HRRR run…</div>
  <div id="sevTownList" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px;margin-top:8px"></div>
  <div class="src">Every East TN town is sampled at its nearest HRRR gridpoint and evaluated with the exact SCP / STP / EHI formulas the severe walls above render — towns whose peak composite crosses the SPC threshold (≥ 1) rank highest over the next ~12 h. Badge colors follow the SPC palette.</div>
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
window.PIVOTUS = {json.dumps(d.get("pivotUs") or [])};
window.PIVOTREG = {json.dumps(d.get("pivotRegions") or {})};
window.PIVOTETN = {json.dumps(d.get("pivotEtn") or [])};
const GALLERY = {json.dumps([{"i": i, "frames": g["frames"]} for i, g in enumerate(gal[:36])])};
const psu = {json.dumps(psu)};
const SEVTOWNS = {json.dumps(d.get("sevTowns") or {})};
const SEV_BADGE = {{stp: "#ff0000", scp: "#ff8c00", ehi: "#ffff00"}};
function sevTownRender(st) {{
  const list = document.getElementById("sevTownList"), note = document.getElementById("sevTownNote");
  if (!list) return;
  const ranked = (st && st.ranked) || [];
  if (!ranked.length) {{
    list.innerHTML = "";
    if (note) note.textContent = "✅ " + ((st && st.note) || "no towns cross SCP/STP/EHI ≥ 1 in the next ~12 h");
    return;
  }}
  if (note) note.textContent = "⚠️ " + ((st && st.note) || "");
  list.innerHTML = ranked.map(r => {{
    const col = SEV_BADGE[r.peakProd] || "#ff8c00";
    const hits = (r.hits || []).map(p => `<span style="background:${{SEV_BADGE[p] || "#666"}};color:${{p === "ehi" ? "#000" : "#fff"}};padding:1px 7px;border-radius:9px;font-size:11px;font-weight:700;margin-right:4px">${{p.toUpperCase()}}</span>`).join("");
    const hrs = r.firstHour <= 1 ? "starting now" : (r.lastHour > r.firstHour ? `in ~${{r.firstHour}}–${{r.lastHour}} h` : `in ~${{r.firstHour}} h`);
    const cyc = (st && st.cycle || "").slice(8, 10) + "Z HRRR";
    return `<div class="alert" style="border-left-color:${{col}};margin:0"><b>🏘️ ${{r.town}}</b> — peak ${{r.peakProd.toUpperCase()}} <b style="color:${{col}}">${{(+r.peakVal).toFixed(1)}}</b><span>${{hits}}<br>${{hrs}} · ${{cyc}} · threshold ≥ 1</span></div>`;
  }}).join("");
}}
sevTownRender(SEVTOWNS);

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
{_PIVOT_JS}
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
<div class="sub">Active storms with official cones/tracks · NHC outlooks · updated <span id="tropStamp">{d["generated"]}</span></div></header>

<div class="card">
  <div id="map" class="map-dark"></div>
  <div class="legend">
    <span><i style="background:#e1bee7"></i>Cone + track</span>
    <span><i style="background:#ff8a80"></i>34-kt wind radii</span>
    <span><i style="background:#b39ddb"></i>NHC 7-day development area</span>
  </div>
  <div class="src" id="stormCount">{len(storms)} active storm(s) · cone/track KMZ parsed from NHC, wind radii + outlook from NHC GIS</div>
</div>

<div class="card"><h2>🌀 Active storms</h2><div id="stormCards">{storm_html}</div></div>

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
  drawTrop(T);
}}
boot();
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
      + ", " + (s.lon != null ? Math.abs(s.lon) + (s.lon >= 0 ? "°E" : "°W") : "?")
      + watches
      + (s.lastUpdate ? "<br/><span class=src>Advisory " + s.lastUpdate + "</span>" : "")
      + (s.advisoryUrl ? "<br/><a href='" + s.advisoryUrl + "' target='_blank'>Full NHC advisory ↗</a>" : "");
  }};
/* soft auto-refresh: redraw cones/tracks/radii/outlooks in place from each
   90 s pull - positions move with every NHC advisory, and a hard reload here
   kills zoom/pan state (same rationale as the radar page, 2026-09-21). */
function drawTrop(T2) {{
  if (typeof map === "undefined" || !map) return;
  const T = T2 || {{}};
  ["storms", "wr", "outlook"].forEach(k => {{ if (layers[k]) map.removeLayer(layers[k]); }});
  layers.storms = L.layerGroup();
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
function onDataRefresh(d2) {{
  DATA = d2;
  const st = document.getElementById("tropStamp");
  if (st && d2.generated) st.textContent = d2.generated;
  const T = d2.tropical || {{}};
  const storms = T.storms || [];
  const sc = document.getElementById("stormCount");
  if (sc) sc.textContent = storms.length + " active storm(s) · cone/track KMZ parsed from NHC, wind radii + outlook from NHC GIS";
  const cards = document.getElementById("stormCards");
  if (cards) {{
    const esc = s => String(s == null ? "" : s).replace(/[&<>]/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;"}}[ch]));
    cards.innerHTML = storms.length ? storms.map(s =>
      `<div class="alert" style="border-left-color:#e1bee7"><b>🌀 ${{esc(s.name || "Storm")}} (${{esc(s.classification || "?")}})</b>`
      + `<span>${{esc(String(s.intensity || ""))}} kt · ${{esc(String(s.pressure || ""))}} mb · `
      + `${{s.lat == null ? "?" : Math.abs(s.lat) + (s.lat >= 0 ? "°N" : "°S")}}, `
      + `${{s.lon == null ? "?" : Math.abs(s.lon) + (s.lon >= 0 ? "°E" : "°W")}} · cone + track + wind field on the map</span></div>`).join("")
      : '<div class="alert ok">No active tropical storms (NHC).</div>';
  }}
  try {{ drawTrop(T); }} catch (_e) {{}}
}}
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
· NHC ATCF aid-decks · updated <span id="tropStamp">{d["generated"]}</span></div></header>

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
    <span><i style="background:rgba(176,190,197,.25);border:1px dashed #b0bec5"></i>Ens. spread cones</span>
    <span><i style="background:linear-gradient(90deg,#fff59d,#ffd54f,#ff8a65,#e53935)"></i>Ens. strike probability</span>
    <span class="src">checkboxes toggle model families · shaded field = strike probability from that agency's members · dotted fan = its spread cone · dots = +24/48/72/120 h, colored by intensity</span>
  </div>
  <div id="famChecks" style="display:flex;gap:12px;flex-wrap:wrap;margin-top:6px"></div>
  <div id="pointProb" style="display:none;margin-top:12px;padding-top:10px;border-top:1px dashed #555">
    <div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
      <b id="ppTitle">\u2014</b>
      <span class="src">bars = chance of the storm's center being within ~105 km (65 mi) of the town at
      that forecast hour, from the ensemble members &middot; click the map to query another spot</span>
    </div>
    <div id="ppChart" style="margin-top:6px"></div>
  </div>
  <div id="topThreats" style="display:none;margin-top:12px;padding-top:10px;border-top:1px dashed #555">
    <b>🎯 Most threatened towns</b>
    <span class="src">peak chance of the storm's center within ~65 mi at any hour, next 5 days · from the ensemble · click a chip for the full hourly curve</span>
    <div id="ttList" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px"></div>
  </div>
</div>

<div class="card" id="chartsCard"><h2>📈 Intensity guidance</h2><div id="charts"></div></div>
<div class="card" id="tblCard"><h2>🧭 Guidance members</h2><div id="tbl"></div></div>

<script>
const TMS = {json.dumps(tm)};
const FAMCOL = {json.dumps(_FAMCOL)};
let map, famGroups = {{}};
const KT_COL = kt => kt >= 137 ? "#d32f2f" : kt >= 113 ? "#e64a19" : kt >= 96 ? "#f57c00"
  : kt >= 83 ? "#ffa000" : kt >= 64 ? "#fbc02d" : kt >= 34 ? "#03a9f4" : "#90a4ae";
// ---- strike probability at a point (click the map) -------------------
// Clicking the map snaps to the nearest town in this gazetteer (TN focus
// plus Gulf/Caribbean/Mexico coasts - wherever tropical systems actually
// threaten) and shows the per-lead chance of the storm's center passing
// within RISK_KM, computed straight from the ensemble member tracks.
const TN_TOWNS = [
  [35.15, -90.05, "Memphis, TN"], [36.16, -86.78, "Nashville, TN"],
  [35.96, -83.92, "Knoxville, TN"], [35.05, -85.31, "Chattanooga, TN"],
  [36.35, -82.20, "Tri-Cities, TN"], [36.53, -87.36, "Clarksville, TN"],
  [35.61, -88.81, "Jackson, TN"], [36.16, -85.50, "Cookeville, TN"],
  [38.25, -85.76, "Louisville, KY"], [38.04, -84.50, "Lexington, KY"],
  [33.75, -84.39, "Atlanta, GA"], [33.52, -86.80, "Birmingham, AL"],
  [34.73, -86.59, "Huntsville, AL"], [35.60, -82.55, "Asheville, NC"],
  [35.23, -80.84, "Charlotte, NC"], [38.63, -90.20, "St. Louis, MO"],
  [29.95, -90.07, "New Orleans, LA"], [30.69, -88.04, "Mobile, AL"],
  [30.42, -87.22, "Pensacola, FL"], [30.16, -85.66, "Panama City, FL"],
  [27.95, -82.46, "Tampa, FL"], [25.76, -80.19, "Miami, FL"],
  [32.78, -79.93, "Charleston, SC"], [34.23, -77.94, "Wilmington, NC"],
  [35.22, -75.53, "Cape Hatteras, NC"], [36.85, -76.29, "Norfolk, VA"],
  [25.90, -97.50, "Brownsville, TX"], [27.80, -97.40, "Corpus Christi, TX"],
  [29.30, -94.80, "Galveston, TX"],
  [23.11, -82.37, "Havana, Cuba"], [21.16, -86.85, "Cancun, Mexico"],
  [19.29, -81.37, "Grand Cayman"], [25.04, -77.35, "Nassau, Bahamas"],
  [18.47, -66.11, "San Juan, PR"], [19.17, -96.13, "Veracruz, Mexico"],
  [22.25, -97.86, "Tampico, Mexico"], [23.24, -106.42, "Mazatlan, Mexico"],
  [22.90, -109.90, "Cabo San Lucas, Mexico"], [20.65, -105.24, "Puerto Vallarta, Mexico"],
  [16.85, -99.90, "Acapulco, Mexico"], [32.29, -64.78, "Bermuda"]
];
const RISK_KM = 105;                     // ~65 mi - NHC 34-kt wind-radius scale
let pointLayer = null, lastTown = null, tmCurIdx = -1;
const HAV_KM = (la1, lo1, la2, lo2) => {{
  const rad = Math.PI / 180, R = 6371;
  const a = Math.sin((la2 - la1) * rad / 2) ** 2
    + Math.cos(la1 * rad) * Math.cos(la2 * rad) * Math.sin((lo2 - lo1) * rad / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}};
function nearestTown(lat, lon) {{
  let best = null, bd = 1e9;
  TN_TOWNS.forEach(t => {{ const d = HAV_KM(lat, lon, t[0], t[1]);
    if (d < bd) {{ bd = d; best = t; }} }});
  return {{ name: best[2], lat: best[0], lon: best[1], offKm: Math.round(bd) }};
}}
// Saffir-Simpson category for a wind speed (kt). 0 = below tropical-storm
// strength (depression / post-tropical), 1 = TS, 2-7 = Cat 1-5. Colors the
// point-probability bars so each hour shows strike odds AND the strength the
// ensemble members are packing when they arrive.
const CAT_COL = ["#9be7a4", "#4fc3f7", "#ffd54f", "#ffb74d", "#ff7043", "#e57373", "#ba68c8"];
const KT_CAT = kt => kt < 34 ? 0 : kt < 64 ? 1 : kt < 83 ? 2 : kt < 96 ? 3 : kt < 113 ? 4 : kt < 137 ? 5 : 6;
const CAT_LBL = ["dissipating (<34 kt)", "tropical storm", "Cat 1", "Cat 2", "Cat 3", "Cat 4", "Cat 5"];
function pointCurve(lat, lon) {{
  const s = (TMS.storms || [])[tmCurIdx];
  if (!s || !Array.isArray(s.tracks)) return null;
  let nEns = 0;
  const inside = {{}};                     // hour -> members within RISK_KM
  const winds = {{}};                       // hour -> [kt, ...] of those members
  const seenH = {{}};                      // hour -> any member reported
  (s.tracks || []).forEach(tr => {{
    if (!tr.isEns || !tr.geo || !Array.isArray(tr.geo.props)) return;
    nEns++;
    tr.geo.props.forEach(p => {{
      if (p.hour > 120 || p.lat == null || p.lon == null) return;
      seenH[p.hour] = true;
      if (HAV_KM(lat, lon, p.lat, p.lon) <= RISK_KM) {{
        inside[p.hour] = (inside[p.hour] || 0) + 1;
        (winds[p.hour] = winds[p.hour] || []).push(p.kt || 0);
      }}
    }});
  }});
  if (!nEns) return null;
  const pts = Object.keys(seenH).map(Number).sort((a, b) => a - b)
    .map(h => {{
      const kts = (winds[h] || []).slice().sort((a, b) => a - b);
      const med = kts.length ? kts[Math.floor((kts.length - 1) / 2)] : null;
      return {{ h, p: Math.round(100 * (inside[h] || 0) / nEns),
               kt: med, cat: med == null ? -1 : KT_CAT(med) }};
    }});
  return {{ pts, nEns }};
}}
function renderPointProb(res, town) {{
  const box = document.getElementById("pointProb");
  if (!box) return;
  const ttl = document.getElementById("ppTitle");
  const chart = document.getElementById("ppChart");
  lastTown = town;
  if (!res || !res.pts.length) {{
    box.style.display = "";
    if (ttl) ttl.innerHTML = "📍 " + town.name +
      " <span class=src>\u00b7 no ensemble guidance for this storm yet</span>";
    if (chart) chart.innerHTML = "";
    return;
  }}
  const W = 760, H = 190, PL = 46, PB = 28, PT = 12, PR = 12;
  const maxH = Math.max(120, res.pts[res.pts.length - 1].h);
  const x = h => PL + (W - PL - PR) * h / maxH;
  const y = p => PT + (H - PT - PB) * (1 - p / 100);
  let bars = "", grid = "", lbls = "";
  res.pts.forEach(pt => {{
    if (pt.p <= 0) return;
    const x0 = x(Math.max(0, pt.h - 3)), x1 = x(pt.h + 3);
    const ci = Math.max(0, pt.cat);
    bars += `<rect x="${{x0.toFixed(1)}}" y="${{y(pt.p).toFixed(1)}}" width="${{(x1 - x0).toFixed(1)}}" height="${{(y(0) - y(pt.p)).toFixed(1)}}" fill="${{CAT_COL[ci]}}" opacity="0.85"><title>+${{pt.h}} h: ${{pt.p}}% within ~65 mi \u00b7 expected ${{CAT_LBL[ci]}} (${{Math.round(pt.kt)}} kt)</title></rect>`;
  }});
  [0, 25, 50, 75, 100].forEach(p => {{
    grid += `<line x1="${{PL}}" y1="${{y(p).toFixed(1)}}" x2="${{W - PR}}" y2="${{y(p).toFixed(1)}}" stroke="#888" stroke-width="0.5" opacity="0.3"/>`
      + `<text x="${{PL - 6}}" y="${{(y(p) + 3).toFixed(1)}}" font-size="9" fill="#aaa" text-anchor="end">${{p}}%</text>`;
  }});
  const step = maxH > 96 ? 24 : 12;
  for (let h = 0; h <= maxH; h += step)
    lbls += `<text x="${{x(h).toFixed(1)}}" y="${{H - 8}}" font-size="9" fill="#aaa" text-anchor="middle">+${{h}}h</text>`;  const peak = Math.max(...res.pts.map(pt => pt.p));
  const first = res.pts.find(pt => pt.p > 0);
  const strong = res.pts.filter(pt => pt.p > 0 && pt.cat >= 0)
    .sort((a, b) => b.kt - a.kt)[0];
  const cats = new Set(res.pts.filter(pt => pt.p > 0 && pt.cat >= 0).map(pt => pt.cat));
  const legend = cats.size
    ? `<div class=src style="margin:2px 0 6px">bar color = expected intensity when the center arrives: `
      + [...cats].sort((a, b) => a - b).map(c =>
        `<span style="color:${{CAT_COL[c]}}">\u25a0</span> ${{CAT_LBL[c]}}`).join(" \u00b7 ") + `</div>`
    : "";

  box.style.display = "";
  if (ttl) ttl.innerHTML = "📍 <b>" + town.name + "</b>"
    + " <span class=src>nearest town "+ town.offKm + " km from your click \u00b7 "
    + res.nEns + " ensemble members \u00b7 peak chance " + peak + "%"
    + (first ? " around +" + first.h + " h" : "")
    + (strong ? " \u00b7 strongest expected: " + CAT_LBL[strong.cat]
       + " (" + Math.round(strong.kt) + " kt) around +" + strong.h + " h" : "") + "</span>";
  if (chart) chart.innerHTML = peak > 0
    ? `<svg width="100%" viewBox="0 0 ${{W}} ${{H}}" style="background:#0d1117;border-radius:8px">${{grid}}${{bars}}${{lbls}}</svg>${{legend}}`
    : `<div class="src">No ensemble member brings this storm's center within ~105 km of ${{town.name}} through +${{maxH}} h.</div>${{legend}}`;
}}
function rankTowns() {{
  // score every gazetteer town against the current storm's ensemble:
  // peak chance of the center within RISK_KM at ANY hour <= 120. 40 towns
  // x ~1200 member points = ~50k haversines, well under a frame budget.
  const out = [];
  TN_TOWNS.forEach(t => {{
    const r = pointCurve(t[0], t[1]);
    if (!r) return;
    let peak = 0, atH = null;
    r.pts.forEach(p => {{ if (p.p > peak) {{ peak = p.p; atH = p.h; }} }});
    out.push({{ name: t[2], lat: t[0], lon: t[1], peak, atH }});
  }});
  return out.length ? out.sort((a, b) => b.peak - a.peak) : null;
}}
function renderTopThreats() {{
  const box = document.getElementById("topThreats");
  const list = document.getElementById("ttList");
  if (!box || !list) return;
  const s = (TMS.storms || [])[tmCurIdx];
  const hasEns = s && Array.isArray(s.tracks) && (s.tracks || []).some(tr => tr.isEns);
  if (!hasEns) {{ box.style.display = "none"; return; }}
  const ranked = (rankTowns() || []).filter(t => t.peak > 0);
  box.style.display = "";
  if (!ranked.length) {{
    list.innerHTML = "<span class=src>No ensemble member brings this storm's center within ~65 mi "
      + "of any tracked town in the next 5 days.</span>";
    return;
  }}
  list.innerHTML = ranked.slice(0, 5).map((t, i) =>
    `<button class="ttChip" data-i="${{i}}" style="cursor:pointer;padding:6px 10px;border-radius:8px;`
      + `border:1px solid #345;background:#0d1117;min-width:130px;text-align:left;color:inherit">`
      + `<span style="font-size:12.5px;display:block"><b>#${{i + 1}}</b> ${{t.name}}</span>`
      + `<span style="font-size:11px;color:#9fb3c8;display:block">peak ${{t.peak}}% · +${{t.atH}} h</span>`
      + `<span style="display:block;height:4px;border-radius:2px;background:#4fc3f7;width:${{Math.max(5, t.peak)}}%"></span>`
      + `</button>`).join("");
  list.querySelectorAll(".ttChip").forEach(btn => {{
    btn.onclick = () => {{
      const t = ranked[parseInt(btn.dataset.i)];
      if (pointLayer) {{ map.removeLayer(pointLayer); pointLayer = null; }}
      pointLayer = L.circleMarker([t.lat, t.lon], {{ radius: 7, color: "#fff", weight: 2,
        fillColor: "#ff5252", fillOpacity: .95 }}).bindTooltip("📍 " + t.name).addTo(map);
      renderPointProb(pointCurve(t.lat, t.lon), Object.assign({{ offKm: 0 }}, t));
      const pp = document.getElementById("pointProb");
      if (pp) pp.scrollIntoView({{ behavior: "smooth", block: "nearest" }});
    }};
  }});
}}
function drawStorm(idx) {{
  tmCurIdx = idx;
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
    const w = tr.isOfficial ? 4 : (tr.isEns ? 1.2 : 2),
          dash = tr.isOfficial ? null : (tr.isEns ? "2 4" : "5 5");
    L.geoJSON({{ type: "Feature", properties: {{}}, geometry: tr.geo }},
      {{ style: {{ color: tr.color, weight: w, opacity: tr.isEns ? .55 : .85, dashArray: dash }} }})
      .bindTooltip((tr.name || tr.tech || fam) + (tr.isEns ? " (ens. member)" : " track"))
      .addTo(famGroups[fam]);
    if (!tr.isEns) (tr.geo.props || []).forEach(p => {{
      if ([24, 48, 72, 120].includes(p.hour) && p.kt != null)
        L.circleMarker([p.lat, p.lon], {{ radius: 4.5, color: "#fff", weight: 1,
          fillColor: KT_COL(parseFloat(p.kt) || 0), fillOpacity: .95 }})
          .bindPopup("<b>" + (tr.tech || fam) + "</b><br/>+" + p.hour + " h · " +
            Math.round(parseFloat(p.kt)) + " kt<br/>" +
            (p.mslp && parseInt(p.mslp) > 800 ? parseInt(p.mslp) + " mb" : ""))
          .addTo(famGroups[fam]);
    }});
  }});
  // Per-agency ensemble spread cones - NHC-style fans built from each
  // agency's members' per-lead-time scatter around the member mean (actual
  // spread, not error climatology). Each cone lives in its agency family
  // group so the checkbox hides it with that agency's members.
  (s.ensCones || []).forEach(cone => {{
    if (!cone || !cone.geo || !Array.isArray(cone.geo.coordinates) ||
        !cone.geo.coordinates[0] || !famGroups[cone.family]) return;
    const ring = cone.geo.coordinates[0].map(c => [c[1], c[0]]);
    const radii = (cone.radiiKm || []).map(r => r[1]);
    const rr = radii.length ? Math.round(radii[radii.length - 1]) : 0;
    const col = FAMCOL[cone.family] || "#b0bec5";
    L.polygon(ring, {{ color: col, weight: 1, opacity: .55,
      fillColor: col, fillOpacity: .15, dashArray: "4 4" }})
      .bindTooltip(cone.agency + " ensemble spread cone · " + cone.nMembers +
        " members · max +" + cone.maxHour + " h · last ring ~" + rr + " km")
      .addTo(famGroups[cone.family]);
  }});
  // Ensemble strike-probability field - NHC wind-probability-style shading
  // built server-side from the ensemble members themselves: each lead's
  // spread-radius disks compounded across the forecast. Painted to an
  // offscreen canvas and stretched over its sub-box on a low pane so it
  // always sits UNDER the tracks and cones; lives in the agency's family
  // group, so the checkbox hides it with that agency's members.
  if (!map.getPane("probPane")) {{
    const pn = map.createPane("probPane");
    pn.style.zIndex = 350;            // below Leaflet's overlay pane (400)
  }}
  (s.ensProb || []).forEach(pf => {{
    if (!pf || !Array.isArray(pf.vals) || !pf.sh || !pf.sw ||
        typeof pf.lat0 !== "number" || !famGroups[pf.family]) return;
    const cv = document.createElement("canvas");
    cv.width = pf.sw; cv.height = pf.sh;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    const img = ctx.createImageData(pf.sw, pf.sh);
    const STOP = [[255, 245, 157], [255, 213, 79], [255, 138, 101], [229, 57, 53]];
    const col = p => {{
      const t = Math.max(0, Math.min(1, (p - 5) / 90));
      const x = t * (STOP.length - 1), i = Math.min(STOP.length - 2, Math.floor(x)), f = x - i;
      return [STOP[i][0] + (STOP[i + 1][0] - STOP[i][0]) * f,
              STOP[i][1] + (STOP[i + 1][1] - STOP[i][1]) * f,
              STOP[i][2] + (STOP[i + 1][2] - STOP[i][2]) * f];
    }};
    for (let i = 0; i < pf.vals.length; i++) {{
      const p = pf.vals[i] | 0;
      if (!p) continue;                     // alpha 0 = transparent outside the field
      const c = col(p);
      img.data[i * 4] = c[0]; img.data[i * 4 + 1] = c[1]; img.data[i * 4 + 2] = c[2];
      img.data[i * 4 + 3] = Math.round(28 + 150 * Math.min(1, p / 100));  // hotter = more opaque
    }}
    ctx.putImageData(img, 0, 0);
    L.imageOverlay(cv.toDataURL("image/png"),
      [[pf.lat0, pf.lon0], [pf.lat1, pf.lon1]], {{ opacity: .85, pane: "probPane" }})
      .bindTooltip(pf.agency + " ens. strike probability · " + pf.nMembers +
        " members · peak " + pf.maxProb + "% · cells ~" + pf.cellKm + " km")
      .addTo(famGroups[pf.family]);
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
  // storm switched: re-query the last clicked town against the new storm's members
  if (lastTown) renderPointProb(pointCurve(lastTown.lat, lastTown.lon), lastTown);
  renderTopThreats();
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
  map.on("click", e => {{
    const town = nearestTown(e.latlng.lat, e.latlng.lng);
    if (pointLayer) {{ map.removeLayer(pointLayer); pointLayer = null; }}
    pointLayer = L.circleMarker([town.lat, town.lon],
      {{ radius: 7, color: "#fff", weight: 2, fillColor: "#ff5252", fillOpacity: .95 }})
      .bindTooltip("📍 " + town.name).addTo(map);
    renderPointProb(pointCurve(town.lat, town.lon), town);
    const pp = document.getElementById("pointProb");
    if (pp) pp.scrollIntoView({{ behavior: "smooth", block: "nearest" }});
  }});
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
/* soft auto-refresh: the summary line tracks every pull, and a storm
   appearing/vanishing (count change) triggers ONE cache-busted reload since
   the storm picker and map can't adapt to a new or removed system.
   The baseline MUST live in a script-scope variable: window.TMS is always
   undefined (page consts do not attach to window), so a window read made
   every refresh look like a count change and the page reloaded itself
   every 90 s - the whole-page flashing of 2026-09-21. */
let tmStormCount = (TMS.storms || []).length;
function onDataRefresh(d2) {{
  DATA = d2;
  const st = document.getElementById("tropStamp");
  if (st && d2.generated) st.textContent = d2.generated;
  const T = d2.tropModels || {{}};
  const storms = T.storms || [];
  const nM = storms.reduce((a, s) => a + ((s.models || []).length), 0);
  const nC = storms.reduce((a, s) => a + ((s.charts || []).length), 0);
  const sm = document.getElementById("tmSummary");
  if (sm) sm.textContent = storms.length + " storm(s) · " + nM + " guidance members · " + nC + " chart(s)";
  if (storms.length !== tmStormCount && !window._tmReloading) {{
    window._tmReloading = true;   /* once: the reloaded page matches its data */
    const u = new URL(location.href); u.searchParams.set("t", Date.now());
    location.replace(u);
    return;
  }}
  tmStormCount = storms.length;   /* steady state: remember, don't re-fire */
}}
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


def page_enso(d):
    """El Niño & La Niña: current ENSO status, forecast, history + explainer."""
    en = d.get("elNino") or {}
    oni = en.get("oni") or []
    sst = en.get("sst") or []
    chips = en.get("chips") or []
    figures = en.get("figures") or []
    anim = en.get("anim") or {}
    plume = en.get("plume") or {}
    phase = en.get("phase") or "unknown"
    phase_col = en.get("phaseColor") or "#9e9e9e"
    alert = en.get("alert") or ""
    enso = en.get("enso") or {}
    paras = enso.get("paragraphs") or []

    chip_html = "".join(
        f'<div style="background:#101826;border:1px solid #2b4a6b;border-radius:10px;'
        f'padding:8px 12px;min-width:180px;flex:1">'
        f'<div style="font-size:24px;font-weight:800;color:#4fc3f7">{c["pct"]}%</div>'
        f'<div class="src" style="margin-top:2px">{html.escape(c["text"])}</div></div>'
        for c in chips)

    last = sst[-1] if sst else None
    sst_rows = "".join(
        f'<tr><td>{html.escape(s["label"])}</td><td>{s["n34"]:.2f}°C</td>'
        f'<td style="color:{"#ef5350" if s["anom"] >= 0.5 else "#42a5f5" if s["anom"] <= -0.5 else "inherit"}">{s["anom"]:+.2f}°C</td></tr>'
        for s in sst[-6:]) if sst else ""

    fig_html = "".join(
        f'<figure style="margin:8px 0"><img src="{f["url"]}" alt="{html.escape(f["label"])}" '
        f'style="max-width:100%;border-radius:10px" loading="lazy"/>'
        f'<figcaption class="src">{html.escape(f["label"])} - NOAA CPC ENSO Diagnostic Discussion</figcaption></figure>'
        for f in figures)
    anim_html = (f'<img src="{anim["url"]}" alt="{html.escape(anim.get("label", "SST anomalies"))}" '
                 f'style="max-width:100%;border-radius:10px" loading="lazy"/>') if anim else \
        '<span class="src">SST animation unavailable this cycle.</span>'
    plume_html = (f'<img src="{plume["url"]}" alt="{html.escape(plume.get("label", "IRI plume"))}" '
                  f'style="max-width:100%;border-radius:10px" loading="lazy"/>'
                  f'<div class="src">Each line: one dynamical or statistical model\'s forecast of Nino 3.4 '
                  f'SST anomaly through the coming seasons - the spaghetti of forecasts the official CPC '
                  f'probability statement is built from.</div>') if plume else \
        '<span class="src">IRI plume unavailable this cycle - see the advisory figures below.</span>'

    enso_paras = "".join(f'<p style="margin:6px 0">{html.escape(p)}</p>' for p in paras[:3])

    body = f"""
<header class="hero"><h1>🌊 El Niño &amp; La Niña</h1>
<div class="sub">ENSO status, forecast, and the story behind the Pacific's biggest swing
· CPC + IRI · updated {d["generated"]}</div></header>

<div class="card">
  <h2>📡 Current status</h2>
  <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
    <span style="background:{phase_col};color:#0b0f14;font-weight:800;border-radius:20px;padding:6px 18px;font-size:16px">{html.escape(phase.upper())}</span>
    {f'<span style="background:#263238;color:#eceff1;border-radius:16px;padding:5px 14px;font-weight:600">{html.escape(alert)}</span>' if alert else ''}
  </div>
  {enso_paras or '<span class=src>ENSO discussion unavailable this cycle - see climate page.</span>'}
  {f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px">{chip_html}</div>' if chip_html else ''}
  <div class="src" style="margin-top:8px">Percentages are CPC's official odds from the monthly ENSO Diagnostic Discussion - the same statement forecasters use. Strength scale: weak (+0.5 to +1.0) · moderate (+1.0 to +1.5) · strong (+1.5 to +2.0) · very strong (+2.0 and beyond).</div>
</div>

<div class="card">
  <h2>🌡️ Nino 3.4 sea-surface temperature anomaly - last 24 months</h2>
  <div id="sstChart"></div>
  <div class="src">The dashed lines are the official thresholds: sustained anomalies beyond ±0.5°C define an ENSO event; ±1.0°C moderate, ±1.5°C strong, ±2.0°C very strong. Hover for exact values.</div>
</div>

<div class="card">
  <h2>📈 Every El Niño &amp; La Niña since 1950</h2>
  <div id="oniChart"></div>
  <div class="src">Oceanic Nino Index - 3-month running mean Nino 3.4 anomaly. Red = El Niño seasons, blue = La Niña. The biggest events (1982-83, 1997-98, 2015-16) reshaped global weather; the labeled marks call out the strongest on record.</div>
</div>

<div class="card">
  <h2>🔮 The forecast</h2>
  <div class="src" style="margin-bottom:6px">IRI/CPC dynamic-model plume - what ~20 models think Nino 3.4 does next:</div>
  {plume_html}
  <div class="src" style="margin:10px 0 6px">Official CPC advisory graphics (current figures rotate with each monthly discussion):</div>
  <div style="display:flex;gap:14px;flex-wrap:wrap">{anim_html}</div>
  {fig_html}
  <div class="src">Read the plume like a spread chart: where the lines fan apart, the models disagree about how strong the event gets; where they bunch, the forecast is confident. The CPC statement above blends these models with forecaster judgment.</div>
</div>

<div class="card">
  <h2>🧭 What El Niño actually is</h2>
  <p style="margin:6px 0">Along the equator, the Pacific has two natural states. In the <b>neutral</b> state, steady
  <b>trade winds</b> blow east-to-west, piling warm water into the western Pacific (the "warm pool") while cold,
  nutrient-rich water upwells off South America. Thunderstorms fire over the warm pool, driving a huge east-west
  circulation called the <b>Walker circulation</b>.</p>
  <p style="margin:6px 0"><b>El Niño</b> is when the trade winds weaken or reverse: the warm pool sloshes back east,
  the central and eastern equatorial Pacific warms several degrees above normal, and the storm factory moves with it.
  The atmosphere and ocean reinforce each other in a feedback loop (<b>Bjerknes feedback</b>) that can push the event
  to historic strength. <b>La Niña</b> is the mirror image - stronger trades, colder eastern Pacific.</p>
  <p style="margin:6px 0">Forecasters measure it in the <b>Niño 3.4 region</b> (5°N-5°S, 170°W-120°W). The
  <b>Oceanic Nino Index (ONI)</b> is that region's 3-month running SST anomaly versus the 1991-2020 average; five
  consecutive seasons beyond +0.5°C is an El Niño event, beyond -0.5°C a La Niña. Events come every 2-7 years and
  usually peak around December ("Niño" - the Christ child - named by Peruvian fishermen for that timing).</p>
</div>

<div class="card">
  <h2>🏘️ What it means for Tennessee</h2>
  <p style="margin:6px 0"><b>El Niño winters:</b> the Pacific jet stream strengthens and sags south, steering wet
  storms across the southern U.S. The typical Tennessee read is a <b>cooler, wetter winter</b> - more Gulf-fed
  rain systems, better snow/ice odds when cold air taps in.</p>
  <p style="margin:6px 0"><b>El Niño hurricane seasons:</b> stronger upper-level winds shear Atlantic storms apart.
  The Atlantic typically sees <b>fewer, weaker hurricanes</b> - while the East Pacific gets busier (its systems'
  moisture can still flood the Southwest).</p>
  <p style="margin:6px 0"><b>La Niña winters</b> flip the pattern: the northern jet dominates, giving Tennessee
  <b>drier, warmer winters</b> - and the following spring often brings an <b>earlier, more active severe weather
  season</b> in the Tennessee Valley, plus busier Atlantic hurricane seasons.</p>
  <p class="src" style="margin-top:6px">These are typical tendencies, not guarantees - El Niño loads the dice, it
  doesn't pick the roll. Pair this page with the Climate page's official CPC seasonal outlooks.</p>
</div>

<div class="card"><span class="src">Sources: NOAA CPC ENSO Diagnostic Discussion (monthly or as advisories change),
CPC ONI &amp; monthly SST indices, IRI ENSO forecast plume. Graphics mirrored locally for speed; text refreshed
on the site update cycle. El Niño threshold +0.5°C · La Niña threshold -0.5°C (CPC ONI, 1991-2020 base period).</span></div>

<script>
const EN = {json.dumps(en)};
async function boot() {{
  DATA = await (await fetch(dataUrl(), {{cache: "no-store"}})).json();
  document.title = DATA.pageName + " - El Niño";
  drawSST(); drawONI();
}}
/* Nino 3.4 monthly anomalies - last 24 months, thresholds shaded in */
function drawSST() {{
  const rows = EN.sst || []; if (!rows.length) return;
  const W = 640, H = 240, pad = 34;
  const xs = i => pad + i * (W - pad - 8) / (rows.length - 1);
  const ys = v => pad + (2.8 - Math.max(-2.0, Math.min(2.8, v))) * (H - 2 * pad) / 5.6;
  let g = "";
  for (const th of [0.5, 1.0, 1.5, 2.0, -0.5, -1.0, -1.5])
    g += `<line x1="${{pad}}" x2="${{W - 8}}" y1="${{ys(th)}}" y2="${{ys(th)}}" stroke="${{Math.abs(th) >= 1.5 ? "#c6282866" : "#455a6488"}}" stroke-dasharray="4 4"/>`;
  g += `<line x1="${{pad}}" x2="${{W - 8}}" y1="${{ys(0)}}" y2="${{ys(0)}}" stroke="#546e7a"/>`;
  const path = rows.map((r, i) => `${{i ? "L" : "M"}}${{xs(i).toFixed(1)}},${{ys(r.anom).toFixed(1)}}`).join("");
  rows.forEach((r, i) => {{
    g += `<circle cx="${{xs(i).toFixed(1)}}" cy="${{ys(r.anom).toFixed(1)}}" r="2.6" fill="${{r.anom >= 0.5 ? "#ef5350" : r.anom <= -0.5 ? "#42a5f5" : "#90a4ae"}}"><title>${{r.label}}: Nino 3.4 ${{r.n34.toFixed(2)}}°C (${{r.anom >= 0 ? "+" : ""}}${{r.anom.toFixed(2)}}°C anomaly)</title></circle>`;
    if (i % 3 === 0) g += `<text x="${{xs(i)}}" y="${{H - 10}}" font-size="9" fill="#78909c" text-anchor="middle">${{r.label.split(" ")[0]}}</text>`;
  }});
  document.getElementById("sstChart").innerHTML =
    `<svg viewBox="0 0 ${{W}} ${{H}}" style="width:100%;background:#0d1117;border-radius:10px">${{g}}` +
    `<text x="${{pad - 4}}" y="${{ys(2) + 3}}" font-size="9" fill="#ef9a9a" text-anchor="end">+2</text>` +
    `<text x="${{pad - 4}}" y="${{ys(-1.5) + 3}}" font-size="9" fill="#90caf9" text-anchor="end">-1.5</text>` +
    `<path d="${{path}}" fill="none" stroke="#eceff1" stroke-width="1.6"/></svg>`;
}}
/* full ONI record - one mark per season since 1950, colored by phase */
function drawONI() {{
  const rows = EN.oni || []; if (rows.length < 10) return;
  const W = 960, H = 260, pad = 34;
  const xs = i => pad + i * (W - pad - 8) / (rows.length - 1);
  const ys = v => pad + (3 - Math.max(-3, Math.min(3, v))) * (H - 2 * pad) / 6;
  let g = "";
  for (const th of [0.5, 1.5, 2.0, -0.5, -1.5])
    g += `<line x1="${{pad}}" x2="${{W - 8}}" y1="${{ys(th)}}" y2="${{ys(th)}}" stroke="#455a6488" stroke-dasharray="4 4"/>`;
  g += `<line x1="${{pad}}" x2="${{W - 8}}" y1="${{ys(0)}}" y2="${{ys(0)}}" stroke="#546e7a"/>`;
  const path = rows.map((r, i) => `${{i ? "L" : "M"}}${{xs(i).toFixed(1)}},${{ys(r.anom).toFixed(1)}}`).join("");
  const area = path + `L${{xs(rows.length - 1)}},${{ys(0)}}L${{xs(0)}},${{ys(0)}}Z`;
  const clipAbove = (y) => `<clipPath id="cA"><rect x="0" y="0" width="${{W}}" height="${{y}}"/></clipPath>`;
  // strongest seasons get hover data + the record marks
  const top = rows.map((r, i) => ({{...r, i}})).sort((a, b) => b.anom - a.anom).slice(0, 3);
  const bot = rows.map((r, i) => ({{...r, i}})).sort((a, b) => a.anom - b.anom).slice(0, 3);
  let marks = "";
  for (const r of [...top, ...bot])
    marks += `<circle cx="${{xs(r.i).toFixed(1)}}" cy="${{ys(r.anom).toFixed(1)}}" r="3.4" fill="#fff"><title>${{r.season}}: ${{r.anom >= 0 ? "+" : ""}}${{r.anom.toFixed(2)}}°C</title></circle>` +
      `<text x="${{xs(r.i).toFixed(1)}}" y="${{ys(r.anom) + (r.anom > 0 ? -7 : 13)}}" font-size="9" fill="#b0bec5" text-anchor="middle">${{r.season.replace(" ", " '")}}</text>`;
  rows.forEach((r, i) => {{
    if (i % 24 !== 0) return;    // one year label per ~2 years of grid
    g += `<text x="${{xs(i)}}" y="${{H - 10}}" font-size="9" fill="#78909c" text-anchor="middle">${{r.season.split(" ")[1]}}</text>`;
  }});
  document.getElementById("oniChart").innerHTML =
    `<svg viewBox="0 0 ${{W}} ${{H}}" style="width:100%;background:#0d1117;border-radius:10px">` +
    `<defs>${{clipAbove(ys(0.5))}}<clipPath id="cB"><rect x="0" y="${{ys(-0.5)}}" width="${{W}}" height="${{H - ys(-0.5)}}"/></clipPath></defs>` +
    `<path d="${{area}}" fill="#37474f55"/>` +
    `<path d="${{area}}" fill="#ef535044" clip-path="url(#cA)"/>` +
    `<path d="${{area}}" fill="#42a5f544" clip-path="url(#cB)"/>` +
    `<path d="${{path}}" fill="none" stroke="#eceff1" stroke-width="1.2"/>${{g}}${{marks}}</svg>`;
}}
boot();
</script>
"""
    return _page("El Niño", "enso.html", body)


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
        ("traffic.html", "🚦", "Traffic Cameras",
         "TDOT SmartWay highway cameras - road conditions before you drive. Snapshots refresh every minute; "
         "during winter events watch for snow-covered shoulders and white road surfaces on I-40, I-81 and I-26."),
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

    # Self-paced meteorology classes - a free mini-course in order, each with
    # a short reading, key takeaways, and a homework exercise on this site.
    classes = [
        ("Class 1 · The atmosphere: layers and pressure",
         "The atmosphere is layered: the troposphere (0-11 km) holds almost all weather; above it the stratosphere is "
         "calm. Pressure is just the weight of air above you - about 1013 mb at sea level, and it halves roughly every "
         "18,000 ft. That is why the Models page stacks 500 mb (≈18,000 ft, storm steering) above 850 mb (≈5,000 ft, "
         "low-level moisture). Falling surface pressure = air rising = the classic storm signature.",
         "models.html", "Open Models and find the 850-mb map. Note the pressure value printed on it, then open any station on "
         "the Dashboard and compare its sea-level pressure - they should be within a few millibars of each other."),
        ("Class 2 · Temperature, dew point and humidity",
         "Temperature is energy; dew point is moisture. The dew point is the temperature air must cool to for saturation - "
         "it NEVER exceeds the air temperature. Relative humidity alone is misleading (cooler nights push it to 100% with "
         "no new moisture). Forecasters live by dew point: 55°F feels muggy, 65°F+ fuels storms, 70°F+ in Tennessee means "
         "torrential-rain potential. The overnight low often lands near the afternoon dew point - that is the model trick.",
         "forecast.html", "On Forecast, find today's dew point column. Predict tonight's low using it, then check tomorrow whether you beat the NWS number."),
        ("Class 3 · Clouds: what they tell you",
         "Clouds form when air rises and cools to its dew point. Cumulus = rising thermals (fair-weather if flat, storm if "
         "towering cumulonimbus). Stratus = gentle lifting over a wide area (drizzle). Cirrus = ice crystals 20,000+ ft up, "
         "often the first sign of an approaching warm front 24-48 h out. Mammatus under an anvil means violent turbulence "
         "- storms capable of it deserve your full attention. Satellite's IR band sees cloud-top COLDNESS: colder = taller = stronger.",
         "satellite.html", "Open Satellite, switch to the infrared band, and find the coldest cloud tops on the map. The colder the colors, the deeper the storm."),
        ("Class 4 · Air masses and fronts",
         "An air mass is a huge blob of air with uniform temperature and moisture: continental polar (cold, dry), maritime "
         "tropical (warm, humid - the Gulf does the supplying for Tennessee). A front is the battle line between two: "
         "cold fronts shove in fast with a line of storms; warm fronts slide over slowly with long stratus and steady rain. "
         "On surface maps, winds turn across the front and dew points JUMP - a 15°F dew-point jump marks a boundary better than temperature.",
         "obs.html", "On Obs, watch the wind barbs across our region. Find where winds flip direction north-to-south - that is today's front."),
        ("Class 5 · Why wind blows (pressure gradient)",
         "Wind is air flowing from high to low pressure - the tighter the isobar spacing, the stronger the wind. But Earth's "
         "rotation bends it: winds flow ALONG isobars aloft (geostrophic) and angle slightly across them near the ground "
         "(friction). Above the friction layer, jet streams race at 100-200 mph and their dips (troughs) are what spin up "
         "our storm systems. Look for isobars squeezed together on any pressure map - that squeeze is the blow.",
         "meso.html", "On Mesoanalysis, find the strongest surface winds and check whether the pressure contours around them are packed tightly."),
        ("Class 6 · Instability: CAPE, lapse rates and storm fuel",
         "A rising bubble of air stays buoyant if it is warmer than its surroundings - that surplus is instability. CAPE "
         "integrates it: under 1000 J/kg ordinary storms, 1000-2500 strong storms, 2500+ severe potential. Lapse rate is "
         "the cooling per km of height: 8°C/km+ makes air rise explosively. Instability is the engine, wind shear is the "
         "steering - engine alone gives gusty storms, engine + shear gives rotating supercells. That pairing is the whole game.",
         "severe.html", "Open Mesoanalysis, find today's CAPE maximum, then check SPC's outlook: does the risk zone sit over the CAPE bullseye?"),
        ("Class 7 · Reading radar like a forecaster",
         "Radar bounces microwaves off raindrops. Reflectivity (dBZ) = echo strength: 20 light rain, 40 heavy, 55+ hail. "
         "A bow echo = damaging straight-line winds; a hook echo with an inflow notch = possible tornado. Velocity products "
         "show motion toward/away from the radar - tightly coupled inbound/outbound couplets mean rotation. Warning: radar "
         "measures DROPS not rainfall - a big-drop drizzle can out-echo a steady soaker, which is why we pair it with MRMS gauge-calibrated totals.",
         "radar.html", "On Radar, during the next rain event, identify: the heaviest core (colors), which way cells track, and whether any show a hook shape."),
        ("Class 8 · Forecast models and how to use them",
         "Models are physics run on a grid. Global models (GFS, ECMWF) cover the world coarsely to 16+ days; mesoscale "
         "models (HRRR, NAM, SREF) zoom in with fine grids to ~2 days. Ensembles (GEFS, EPS, SREF) run the model many "
         "times with tiny tweaks - TIGHT spread = high confidence, WIDE spread = uncertain outcome. Trust rules: days 1-2 "
         "high-res, days 3-7 ensemble means, day 8+ pattern hints only. When GFS and ECMWF agree, believe it.",
         "models.html", "Open Models in GFS and ECMWF (or GEFS spread). Compare the 500-mb pattern over Tennessee: agree or diverge? Note which and how it matches the actual forecast."),
        ("Class 9 · Tropical cyclones: structure and hazards",
         "A tropical cyclone is a heat engine over warm (26°C+) ocean: air spirals inward, rises in the eyewall, and vents "
         "out the top. The eye is calm SINKING air - danger resumes when the back side arrives. Hazards are ranked by kills: "
         "1) WATER - storm surge and inland freshwater flooding, 2) wind, 3) tornadoes in outer rainbands. Spaghetti plots "
         "show each model's path idea; where lines converge, confidence is high. East Tennessee's lesson is Helene: remnants "
         "300+ miles inland still dropped catastrophic rain on our mountains.",
         "tropmodels.html", "On Tropical Models, when a storm is active, count how many members take it one way vs another. Where do the most lines agree?"),
        ("Class 10 · Winter weather: snow ratios and the 32°F fight",
         "Snow is forecast from the QPF (liquid equivalent) times a ratio. The classic 10:1 rule is a floor: Arctic air "
         "behind a front can push 15-20:1, while marginal 33-35°F air crushes it to 3-5:1 or plain rain. Watch the vertical "
         "profile on the Skew-T: a warm nose above freezing gives sleet, a deep subfreezing layer gives snow, surface "
         "melting gives freezing rain. In East Tennessee elevation wins - ridge-top events often never reach the valleys.",
         "winter.html", "Open Winter, find the storm-total snow map, then compare the temperature column below it: is anything hovering 30-35°F? Flag where ratio busting is most likely."),
        ("Class 11 · Flooding: from rain to river crest",
         "Flood forecasting is a chain: soil moisture → runoff → creeks → main-stem rivers. Saturated ground can absorb "
         "almost nothing, so the SAME rain that soaked in last month becomes a flood today. Rivers lag rain - mountain "
         "gauges can keep rising 12-24 h after skies clear. Gauge categories (action/minor/moderate/major) mark where "
         "impacts start: minor floods fields and low roads, major floods structures. The trend beats the number.",
         "rivers.html", "On Rivers, after the next heavy rain, pick one gauge and log it morning and evening for three days. Chart the rise - how long after the rain did it crest?"),        ("Class 12 · Climate: our seasons and what a 30-year normal means",
         "East Tennessee sits in the humid subtropical/Cfa border zone: hot, muggy summers (July mean near 78°F), mild "
         "winters with occasional Arctic outbreaks, and two storm seasons (March-May severe, November secondary). The "
         "Smokies wring moisture from the prevailing westerlies, so the mountains out-rain the valley nearly 2:1. A "
         "climatological 'normal' is just a 30-year average (currently 1991-2020) - a useful baseline, not a prediction: "
         "records exist precisely because weather routinely ignores it.",
         "dashboard.html", "On Dashboard, compare a station's current temperature to its 24-hour trend and think: is today running above or below our seasonal normal - and why?"),
        ("Class 13 · Satellite interpretation: choosing the right band",
         "Weather satellites measure different slices of light, and each answers a different question. VISIBLE (0.64 µm) "
         "is sunlight bounced off cloud tops - sharpest detail, but blind at night. INFRARED (10.3 µm) measures cloud-top "
         "temperature, so it works day and night: colder = higher = deeper storms, and the coldest tops often overshoot "
         "into the stratosphere on severe storms. WATER VAPOR (6.9 µm) sees moisture at mid-levels - it shows the jets, "
         "dry slots and rivers of moisture that STEER storms, often before clouds even form. The 3.9 µm shortwave window "
         "is the night-owl: low clouds glow against warmer ground (fog detection) and fires show as hot spots. Pros stack "
         "bands: visible for detail, IR for height, water vapor for the big picture, 3.9 for fog and fire.",
         "satellite.html", "Open Satellite on a cloudy night. Switch bands and answer: which band shows the storm tops, and which shows the moisture stream feeding them?"),
        ("Class 14 · Skew-T soundings: the atmosphere's vertical profile",
         "A Skew-T plots temperature and dew point from ground to jet stream on a chart whose temperature lines slant "
         "(that is the 'skew'). The gap between the red temperature and green dew point curves is moisture; where they "
         "nearly touch is a cloud layer. Forecasters mark three levels: the LCL (cloud base), the LFC (where rising air "
         "turns freely buoyant) and the EL (storm top). The area between LFC and EL is CAPE - the storm's fuel tank. "
         "A CAP (warm layer aloft, or CIN) is a lid: small caps let storms fire cleanly, big caps hold energy until "
         "something breaks them - then storms explode. The wind barbs on the right edge form the hodograph: a big "
         "clockwise curl means storm rotation is possible. Read a sounding bottom-up: is the surface moist? Is there "
         "a cap? Is CAPE loaded? Is wind turning with height? Those four answers ARE the forecast.",
         "obs.html", "On Obs, open today's nearest sounding. Find the LCL height, estimate whether a cap (CIN) is present, and describe the low-level wind curl."),
        ("Class 15 · Ensemble forecasting: forecasting the forecast",
         "A single model run is one opinion; an ensemble is a whole panel of experts. Run the same model 31 times with "
         "slightly different starting points (GEFS) or physics (SREF), and the differences reveal what the atmosphere "
         "itself is unsure about. The MEAN is the consensus; the SPREAD is the honesty meter - tight clusters mean high "
         "confidence, wide scatter means the atmosphere has not decided. Count members instead of trusting one: '23 of 31 "
         "members show snow' is a real probability you can plan around, far better than a single map's best guess. Watch "
         "for clusters - when members split into two distinct solutions (say, a northern vs southern storm track), the "
         "truth usually lands near one cluster, not in the mushy middle. Ensembles also extend range: EPS weeklies push "
         "useful pattern signals to 2-4 weeks where a single run is noise. Rules: day 1-3 use high-res deterministic, "
         "day 4+ shift to the ensemble mean, and always check spread before you trust any single frame.",
         "models.html", "Open Models and find the GEFS (or SREF) spread panel for the same product as the deterministic run. Compare: where spread is tight, how closely does the deterministic map match the mean?"),
        ("Class 16 · Radar velocity & dual-pol: the storm's insides",
         "Velocity products are the radar's motion detector: greens move TOWARD the radar, reds AWAY. A tight green-red "
         "pair in a storm's low levels is a couplet - that is rotation, and if it tightens while descending, a tornado "
         "may be forming or already on the ground. Dual-pol adds material science: ZDR (differential reflectivity) is "
         "high for big flat raindrops - and also for debris. Correlation coefficient (CC) collapses near zero when the "
         "beam mixes unlike targets: rain suddenly wrapped in non-weather objects means a tornado is lofting material "
         "(the debris ball / TDS signature) - a tornado CONFIRMED on the ground even where no spotter can see it. KDP "
         "responds only to pure liquid and pinpoints the heaviest rain cores, the flash-flood signal. The pro's rule: "
         "velocity says ROTATE, dual-pol says WHAT IS FLYING - together they turn 'possible tornado' into 'take cover now'.",
         "meso.html", "On Mesoanalysis during a storm day, check 0-1 km storm-relative helicity and 0-6 km shear. The environment tells you WHICH cells can rotate before the radar shows one doing it."),
        ("Class 17 · How the NWS decides to warn",
         "A warning is the last link of a decision chain that starts hours earlier: SPC outlooks flag the environment, "
         "watches (county-scale, hours ahead) say 'conditions favorable', and warnings are the storm-scale act-now "
         "message. The forecaster polls three streams on every radar scan: the environment (CAPE, shear, soundings), "
         "the radar trend (is that couplet tightening? is the hook strengthening?), and ground truth (spotters, law "
         "enforcement, damage reports). Severe criteria: hail 1 inch+ or winds 58+ mph. Tornado warnings carry tags - "
         "RADAR INDICATED (rotation on radar, no ground confirmation) vs CONFIRMED, plus the rare PDS (particularly "
         "dangerous situation) - and modern warning text leads with the THREAT, because 'take cover now' beats a "
         "meteorology lecture when minutes count. Warnings are storm-based polygons drawn only where the threat lives. "
         "When yours fires: lowest floor, interior room, away from windows - the warning is the end of a chain built to "
         "give you those minutes.",
         "severe.html", "On Severe, read today's SPC outlook and any MCD. Then open Home and, for any active warning, note its tag - RADAR INDICATED vs CONFIRMED - and whether your county is inside the polygon."),

    ]
    # Self-check quizzes: 3 questions per class, every answer stated in that
    # class's own lesson text. Keyed by the class number in the title so the
    # classes list above stays untouched.
    class_quizzes = {
        1: [("Roughly how quickly does atmospheric pressure halve as you climb?",
             "About every 18,000 ft - which is why the 500-mb level (half of sea-level pressure) sits near 18,000 ft."),
            ("What does falling surface pressure usually signal?",
             "Air is rising above you - the classic signature of an approaching storm system."),
            ("Which layer of the atmosphere holds almost all weather?",
             "The troposphere (0-11 km). The stratosphere above it is calm.")],
        2: [("Can the dew point ever exceed the air temperature?",
             "No - dew point never exceeds the air temperature; the two meet only at saturation (100% humidity)."),
            ("Which is the better moisture measure, dew point or relative humidity - and why?",
             "Dew point. It tracks real moisture; relative humidity changes whenever temperature changes, even with no new moisture."),
            ("What dew-point values mark 'muggy' and 'storm fuel' in Tennessee?",
             "About 55°F starts feeling muggy, 65°F+ fuels storms, and 70°F+ means torrential-rain potential.")],
        3: [("Which cloud type is often the first sign of an approaching warm front?",
             "Cirrus - ice crystals 20,000+ ft up, arriving 24-48 h ahead of the front."),
            ("On satellite infrared, what do colder cloud tops tell you?",
             "Colder = taller = stronger. The coldest tops belong to the deepest storms."),
            ("Mammatus clouds hanging under an anvil mean what?",
             "Violent turbulence - storms capable of producing mammatus deserve your full attention.")],
        4: [("Which air mass supplies Tennessee's warm, humid weather?",
             "Maritime tropical - the warm, humid air the Gulf of Mexico keeps supplying."),
            ("What surface clue marks a front better than temperature does?",
             "A dew-point jump (15°F+ marks a boundary sharply), along with winds turning across the line."),
            ("How do cold and warm fronts differ?",
             "Cold fronts shove in fast, often with a line of storms; warm fronts slide over slowly with long stratus and steady rain.")],
        5: [("What makes wind stronger on a pressure map?",
             "Tighter isobar spacing - the tighter the packing, the stronger the wind."),
            ("Why do winds aloft flow along isobars while surface winds cross them?",
             "Earth's rotation bends flow along isobars (geostrophic) aloft; near the ground, friction angles the wind slightly across them."),
            ("Which jet-stream feature helps spin up our storm systems?",
             "Its dips (troughs) - they are what spin up surface low-pressure systems.")],
        6: [("What CAPE values separate ordinary, strong, and severe-potential storms?",
             "Under 1000 J/kg ordinary; 1000-2500 strong; 2500+ severe potential."),
            ("What does a steep lapse rate do?",
             "Cools 8°C/km+ with height, so rising air stays buoyant - air rises explosively and fuels storms."),
            ("What pairing turns ordinary storms into rotating supercells?",
             "Instability (the engine) plus wind shear (the steering). Engine alone gives gusty storms; both together give supercells.")],
        7: [("Which dBZ values mark heavy rain and likely hail?",
             "About 40 for heavy rain and 55+ for likely hail (20 is light rain)."),
            ("What does a hook echo with an inflow notch suggest?",
             "Possible tornado - that shape signals rotation within the storm."),
            ("Why pair radar with MRMS gauge-calibrated totals?",
             "Radar measures drops, not rainfall - big-drop drizzle can out-echo a steady soaker; MRMS adds rain-gauge truth.")],
        8: [("What does WIDE ensemble spread tell you?",
             "The outcome is uncertain. Tight spread means the forecast is confident."),
            ("Which models do you trust days 1-2, and what about day 8+?",
             "Days 1-2 the high-res models (HRRR/NAM); days 3-7 the ensemble means; day 8+ treat everything as a pattern hint."),
            ("What does it mean when GFS and ECMWF agree?",
             "Believe it - agreement between independent global models is a strong confidence signal.")],
        9: [("What ocean temperature fuels a tropical cyclone?",
             "About 26°C+ - a tropical cyclone is a heat engine running on warm ocean water."),
            ("Rank the deadliest tropical hazards.",
             "1) Water - storm surge and inland freshwater flooding; 2) wind; 3) tornadoes in outer rainbands."),
            ("What is inside the eye of a hurricane?",
             "Calm sinking air - danger resumes when the back side of the eyewall arrives.")],
        10: [("How do you turn a snow forecast's QPF into inches?",
              "Multiply the liquid equivalent by a ratio: 10:1 is a floor, Arctic air can push 15-20:1, and marginal 33-35°F air crushes it to 3-5:1."),
             ("Which vertical profile gives sleet, and which gives freezing rain?",
              "A warm nose above freezing gives sleet; a surface melting layer gives freezing rain. A deep subfreezing layer gives snow."),
             ("Why do ridge communities get winter events the valleys miss?",
              "Elevation - valleys sit 5-8°F warmer at night, so ridge-top events often never reach the valley floor.")],
        11: [("Why can the same rain flood today when it didn't last month?",
              "Soil moisture - saturated ground absorbs almost nothing, so far more of the rain runs off into creeks."),
             ("How long after the rain stops can rivers keep rising?",
              "12-24 hours or more - mountain gauges can keep climbing long after skies clear."),
             ("What matters more than a single gauge reading?",
              "The trend - rising, steady, or falling tells you what happens next; the number alone does not.")],
        12: [("What are East Tennessee's two storm seasons?",
              "March-May is the main severe season, with a secondary peak in November."),
             ("Why do the Smokies out-rain the valley nearly 2:1?",
              "The mountains wring moisture out of the prevailing westerlies as air is forced up and over them."),
             ("What does a 30-year 'normal' actually mean?",
              "A 1991-2020 average - a useful baseline, not a prediction; records exist because weather routinely ignores it.")],
        13: [("Which satellite band works at night, and which is sharpest in daylight?",
              "Infrared (10.3 µm) works day and night by measuring cloud-top temperature; visible (0.64 µm) is sharpest but blind without sun."),
             ("What does the water vapor channel show that clouds do not?",
              "Mid-level moisture flow - jets, dry slots and moisture rivers that steer storms, often before clouds form."),
             ("Which band detects fog at night, and how?",
              "The 3.9 µm shortwave window - low clouds glow against warmer ground, and fires show as hot spots.")],
        14: [("Name the three levels that frame a storm's fuel tank.",
              "LCL (cloud base), LFC (where rising air turns freely buoyant) and EL (storm top); CAPE is the area between LFC and EL."),
             ("What is a cap (CIN), and when is it dangerous vs useful?",
              "A warm layer aloft that blocks rising air. It holds energy until something breaks it - then storms explode; no cap lets storms fire weakly and early."),
             ("What does a big clockwise curl in the hodograph wind barbs mean?",
              "Storm-scale rotation is possible - the low-level winds turn strongly with height, feeding rotating updrafts.")],
        15: [("What is the difference between ensemble mean and spread?",
              "The mean is the members' consensus forecast; the spread is how far apart they are - the honesty meter for confidence."),
             ("Which is more useful: 'the model shows snow' or '23 of 31 members show snow'?",
              "The member count - it is a real probability you can plan around, instead of one map's best guess."),
             ("When members split into two distinct storm tracks, where does truth usually land?",
              "Near one of the clusters, not in the mushy middle - and the odds follow the size of each cluster.")],
        16: [("What does a tight inbound/outbound velocity couplet at low levels mean?",
              "Rotation inside the storm - if it tightens while descending, a tornado may be forming or already on the ground."),
             ("What does LOW correlation coefficient near the hook echo signal?",
              "Debris: the beam is mixing non-weather targets, meaning a tornado has lofted material - the confirmed-landed (TDS) signature."),
             ("Which dual-pol field pinpoints the heaviest rain cores?",
              "KDP (specific differential phase) - it responds only to pure liquid and highlights heavy-rain cores for flash-flood work.")],
        17: [("What is the difference between a watch and a warning?",
              "A watch is county-scale, hours ahead: conditions are favorable, keep planning. A warning is storm-scale: imminent or occurring - act now."),
             ("What do the tags RADAR INDICATED and CONFIRMED mean in a tornado warning?",
              "RADAR INDICATED: rotation on radar without ground confirmation. CONFIRMED (or PDS): spotters or debris signatures verify - shelter immediately."),
             ("What are the official severe thunderstorm criteria?",
              "Hail 1 inch or larger and/or winds of 58+ mph.")],
    }

    def _quiz_html(title):
        n = _class_no(title)
        items = class_quizzes.get(n) or []
        if not items:
            return ""
        qhtml = "".join(
            f'<details class="met-q" data-quiz="{n}.{i}" style="margin:3px 0">'
            f'<summary style="cursor:pointer;margin-left:10px">{html.escape(q)}</summary>'
            f'<p style="margin:3px 0 2px 24px">\u2705 {a}</p></details>'
            for i, (q, a) in enumerate(items, 1))
        return (f'<p style="margin:8px 0 2px"><b>\U0001f9e0 Quiz yourself - think, then click to check:</b></p>{qhtml}')

    def _class_no(t):
        try:
            return int(t.split("\u00b7")[0].split()[-1])
        except (ValueError, IndexError):
            return 0

    classes_html = "".join(
        f'<details class="card met-class" id="class-{_class_no(t)}" data-class="{_class_no(t)}" style="margin:8px 0">'
        f'<summary style="cursor:pointer;font-weight:700">'
        f'<span class="met-done-box" data-n="{_class_no(t)}" title="Mark this class complete" '
        f'style="cursor:pointer;user-select:none">'
        f'<input type="checkbox" class="met-done-cb" style="cursor:pointer;vertical-align:middle;accent-color:#66bb6a"> '
        f'</span><span class="met-title">{html.escape(t)}</span></summary>'
        f'<p style="margin:8px 0 2px">{txt}</p>'
        f'<p style="margin:6px 0 2px"><b>📝 Homework:</b> <a class="src" href="{href}">{hw}</a></p>'
        f'{_quiz_html(t)}</details>'
        for t, txt, href, hw in classes)

    # Sticky class-index sidebar: quick-jump links for every class with live
    # progress (reads the same localStorage keys as the tracker below).
    def _short_title(t):
        s = t.split(':')[0]
        if len(s) > 34:
            s = s[:33].rstrip() + '\u2026'
        return s

    idx_rows = "".join(
        f'<a class="edx-row" href="#class-{_class_no(t)}" data-cls="{_class_no(t)}" title="{html.escape(t)}">'
        f'<span class="edx-num">{_class_no(t)}</span>'
        f'<span class="edx-name">{html.escape(_short_title(t))}</span>'
        f'<span class="edx-check">\u2713</span></a>'
        for t, _txt, _href, _hw in classes)

    sidebar_html = """
<style>
html { scroll-behavior:smooth; }
details.met-class, #edu-exam { scroll-margin-top:130px; }
#edx-side { position:sticky; top:118px; flex:0 0 224px; width:224px; max-height:calc(100vh - 134px);
            overflow-y:auto; z-index:10; margin:14px 0; }
.edx-row { display:flex; align-items:center; gap:8px; padding:5px 8px; border-radius:8px;
           color:#cdd7e4; font-size:12.5px; }
.edx-row:hover { background:#1d2432; color:#fff; }
.edx-row.active { background:#1d3557; color:#fff; }
.edx-num { flex:0 0 20px; height:20px; border-radius:50%; background:#1d2432; color:#8ef2a0;
           font-weight:700; font-size:11px; display:flex; align-items:center; justify-content:center; }
.edx-row.done .edx-num { background:#2e7d32; color:#fff; }
.edx-name { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.edx-check { margin-left:auto; font-size:11px; color:#66bb6a; opacity:0; }
.edx-row.done .edx-check { opacity:1; }
@media (max-width:900px){ #edx-side { display:none; } }
</style>
<aside id="edx-side" class="card" style="padding:10px 8px">
  <div style="font-weight:800;font-size:13px;padding:2px 8px 6px;color:#cdd7e4">\U0001f4d6 Class index</div>
  <div style="padding:0 8px 8px">
    <div style="background:#1d2432;height:8px;border-radius:4px;overflow:hidden">
      <div id="edx-bar" style="background:#8ef2a0;height:100%;width:0%;transition:width .3s"></div>
    </div>
    <span id="edx-count" style="font-size:11px;color:#9aa4b2">0 / """ + str(len(classes)) + """ done</span>
  </div>
""" + idx_rows + """
  <a class="edx-row" href="#edu-exam" title="Graduation exam - 20 questions, 80% to pass">
    <span class="edx-num" style="background:#3a2f00;color:#ffd54f">\U0001f393</span>
    <span class="edx-name">Graduation exam</span>
  </a>
</aside>
"""
    edx_js = """
(function () {
  var side = document.getElementById('edx-side');
  if (!side) return;
  var rows = Array.prototype.slice.call(side.querySelectorAll('.edx-row[data-cls]'));
  var cards = rows.map(function (r) { return document.getElementById('class-' + r.dataset.cls); });
  var DONE_KEY = 'tnwn.edu.done.v1';
  function load(k) { try { return JSON.parse(localStorage.getItem(k) || '{}'); } catch (e) { return {}; } }
  function paintDone() {
    var done = load(DONE_KEY), n = 0;
    rows.forEach(function (r) {
      var d = !!done[r.dataset.cls];
      r.classList.toggle('done', d);
      if (d) n++;
    });
    var cnt = document.getElementById('edx-count'), bar = document.getElementById('edx-bar');
    if (cnt) cnt.textContent = n + ' / ' + rows.length + ' done';
    if (bar) bar.style.width = Math.round(100 * n / Math.max(1, rows.length)) + '%';
  }
  rows.forEach(function (r, i) {
    r.addEventListener('click', function () { if (cards[i] && !cards[i].open) cards[i].open = true; });
  });
  var ticking = false;
  window.addEventListener('scroll', function () {
    if (ticking) return; ticking = true;
    requestAnimationFrame(function () {
      var act = -1;
      cards.forEach(function (c, i) {
        if (c && c.getBoundingClientRect().top <= 170) act = i;
      });
      rows.forEach(function (r, i) { r.classList.toggle('active', i === act); });
      ticking = false;
    });
  }, { passive: true });
  setInterval(paintDone, 1500);   /* cheap sync with the progress tracker */
  paintDone();
})();
"""

    # Graduation exam: 20 multiple-choice questions covering all 17 classes
    # (one each + 3 extra on radar/trust-rules/ratios). 80% (16/20) passes and
    # unlocks a printable certificate. Plain strings so JS braces survive.
    exam_qs = [
        ("Roughly how quickly does atmospheric pressure halve as you climb?",
         ["About every 5,000 ft", "About every 18,000 ft", "About every 50,000 ft", "Pressure never changes with height"], 1),
        ("Which measurement tells you the REAL moisture in the air?",
         ["Relative humidity", "Dew point", "Heat index", "Wind chill"], 1),
        ("Which cloud is often the first sign of an approaching warm front, 24-48 h out?",
         ["Cumulonimbus", "Mammatus", "Cirrus", "Stratus"], 2),
        ("What surface clue marks a front better than temperature does?",
         ["A 15\u00b0F dew-point jump", "A wind speed maximum", "A steady pressure reading", "The sun angle"], 0),
        ("On a pressure map, tightly packed isobars mean:",
         ["Calm winds", "Strong winds", "Rain is certain", "Nothing useful"], 1),
        ("A CAPE of 2500+ J/kg signals:",
         ["No storms possible", "Ordinary storms", "Severe-potential storms", "A certain tornado"], 2),
        ("Radar reflectivity of 55+ dBZ usually means:",
         ["Light rain", "Moderate rain", "Drizzle", "Likely hail"], 3),
        ("WIDE ensemble spread means:",
         ["High confidence", "The outcome is uncertain", "The model is broken", "A storm is certain"], 1),
        ("The deadliest tropical cyclone hazard is:",
         ["Wind", "Tornadoes in rainbands", "Water - surge and flooding", "Lightning"], 2),
        ("A warm nose (above-freezing layer) aloft gives:",
         ["Snow", "Sleet", "Freezing rain", "Rain"], 1),
        ("After rain ends, mountain rivers can keep rising for:",
         ["30 minutes", "2 hours", "12-24 hours", "Rivers never rise after rain ends"], 2),
        ("A 30-year climate 'normal' is:",
         ["A prediction for the next 30 years", "A 1991-2020 average baseline", "The all-time record", "A guarantee of the weather"], 1),
        ("Which satellite band works day AND night?",
         ["Visible 0.64 \u00b5m", "Infrared 10.3 \u00b5m", "Neither one", "Both, but only at noon"], 1),
        ("On a Skew-T, the CAPE area sits between:",
         ["Surface and LCL", "LCL and LFC", "LFC and EL", "EL and tropopause"], 2),
        ("The ensemble MEAN is:",
         ["The first member's forecast", "The members' consensus", "The highest member", "The forecast error"], 1),
        ("LOW correlation coefficient near a hook echo means:",
         ["Pure rain", "Certain hail", "Possible debris lofted by a tornado - the TDS signature", "Radar calibration error"], 2),
        ("The difference between a watch and a warning:",
         ["They are identical", "A watch means act now; a warning means stay alert",
          "A warning means act now - the hazard is imminent or occurring", "Watches cover storms, warnings cover counties"], 2),
        ("A tight inbound/outbound velocity couplet at low levels means:",
         ["Rotation inside the storm", "Certain hail", "Calm air", "A radar malfunction"], 0),
        ("For day 8+ forecasts, treat model output as:",
         ["Exact truth", "A pattern hint only", "Completely useless", "A guarantee"], 1),
        ("The classic 10:1 snow ratio is:",
         ["A law of physics", "A floor - Arctic air can push 15-20:1", "The maximum possible", "Only valid for rain"], 1),
    ]
    exam_qhtml = "".join(
        f'<div class="exam-q" data-correct="{ci}" style="margin:12px 0">'
        f'<b>{i}. {html.escape(q)}</b>'
        + "".join(
            f'<label style="display:block;margin:3px 0 3px 16px;cursor:pointer;padding:2px 6px;border-radius:4px">'
            f'<input type="radio" name="exq{i}" value="{j}" style="accent-color:#42a5f5;cursor:pointer"> {html.escape(o)}</label>'
            for j, o in enumerate(opts))
        + "</div>"
        for i, (q, opts, ci) in enumerate(exam_qs, 1))
    exam_html = ("""
<style>
@media print {
  body * { visibility: hidden; }
  #edu-cert, #edu-cert * { visibility: visible; }
  #edu-cert { position: absolute; top: 0; left: 0; width: 100%; }
  #edu-cert button, #edu-cert .cert-hint { display: none; }
}
</style>
<div class="card" id="edu-exam" style="border:2px solid #ffd54f">
  <h2>\U0001f393 Graduation exam - 20 questions, 80% to pass \u00b7 optional 15-minute timed mode</h2>
  <p style="margin:6px 0">One question from every class (plus three extras). Score <b>16 of 20 (80%)</b> to graduate\n  with your printable certificate. Wrong answers are marked green-correct/red-picked after grading so you can review.\n  Practice untimed as long as you like - or hit <b>Start timed attempt</b> for a real test run: 15 minutes on the clock,\n  answers cleared, and the exam auto-grades with whatever you finished when time expires.\n  <b style="display:block;margin:4px 0;color:#ffd54f">\U0001f512 The exam stays locked until all 17 classes are checked off and every quiz answer worked through.</b>\n  <b id="exam-best" style="display:block;margin:4px 0"></b>\n  <b id="exam-best-timed" style="display:block;margin:2px 0;color:#ffd54f"></b></p>
  <div id="exam-lock" style="display:none;margin:8px 0;padding:14px;border:1px dashed #ffd54f;border-radius:8px;text-align:center">
    <div style="font-size:24px">\U0001f512</div>
    <b style="display:block;margin:4px 0">Exam locked - finish the course first</b>
    <span id="lock-classes" style="display:block;font-size:14px;margin:2px 0"></span>
    <span id="lock-quizzes" style="display:block;font-size:14px;margin:2px 0"></span>
    <span style="display:block;font-size:12.5px;color:#9aa4b2;margin-top:4px">Check off every class \u2705 and work through every quiz answer, then this exam unlocks automatically.</span>
  </div>
  <div id="exam-body" style="display:none">
  <div id="timer-wrap" style="display:none;align-items:center;gap:10px;margin:10px 0;padding:8px 12px;border:1px solid #ffd54f;border-radius:8px">
    <span style="font-size:20px">\u23f1\ufe0f</span>
    <b id="exam-timer" style="font-family:Consolas,monospace;font-size:26px;min-width:74px;color:#8ef2a0">15:00</b>
    <span style="opacity:.85">on the clock - the exam auto-grades when time expires</span>
  </div>
  <button id="exam-timed-start" style="background:#e65100;color:#fff;border:none;border-radius:6px;padding:8px 16px;font-weight:700;cursor:pointer;margin:6px 0">\u23f1\ufe0f Start timed attempt (15:00)</button>
  <span style="opacity:.75;font-size:13px"> - clears your answers and starts the countdown. Untimed practice: just answer and grade below.</span>
""" + exam_qhtml + """
  <button id="exam-grade" style="background:#2e7d32;color:#fff;border:none;border-radius:6px;padding:8px 18px;font-weight:700;cursor:pointer;margin:6px 6px 0 0">Grade my exam</button>
  <button id="exam-retake" style="background:transparent;color:inherit;border:1px solid currentColor;border-radius:6px;padding:8px 14px;cursor:pointer;margin:6px 0 0">Retake (clears answers)</button>
  <p id="exam-result" style="margin:10px 0 2px;font-weight:700;font-size:1.05em"></p>
  <div id="edu-cert" style="display:none;margin-top:16px;background:#fffdf5;color:#1a1a1a;border:6px double #1d3557;border-radius:8px;padding:26px;text-align:center">
    <div style="font-size:13px;letter-spacing:3px;color:#1d3557">TENNESSEE WEATHER NETWORK \u00b7 WEATHER SCHOOL</div>
    <h2 style="margin:10px 0;color:#1d3557;font-size:30px">Certificate of Completion</h2>
    <p style="margin:6px 0">This certifies that</p>
    <input id="cert-name" placeholder="type your name here" style="border:none;border-bottom:2px dotted #555;background:transparent;font-size:24px;text-align:center;width:70%;color:#111;font-family:Georgia,serif">
    <p style="margin:12px 0">has completed the free meteorology course - all 17 classes -<br>with a graduation-exam score of <b id="cert-score"></b><span id="cert-timed"></span></p>
    <p style="margin:6px 0;font-size:13px;color:#444">Course covered: the atmosphere \u00b7 moisture \u00b7 clouds \u00b7 fronts \u00b7 wind \u00b7 instability \u00b7 radar \u00b7 models \u00b7\n    tropical cyclones \u00b7 winter weather \u00b7 flooding \u00b7 climate \u00b7 satellite bands \u00b7 Skew-T soundings \u00b7\n    ensembles \u00b7 velocity &amp; dual-pol \u00b7 NWS warnings \u00b7 <span id="cert-date"></span></p>
    <button id="cert-print" style="margin-top:12px;background:#1d3557;color:#fff;border:none;border-radius:6px;padding:8px 16px;cursor:pointer">\U0001f5a8\ufe0f Print certificate</button>
    <button id="cert-share" title="Opens Facebook with a link to the course and copies a ready-made graduation post to your clipboard" style="margin-top:12px;margin-left:8px;background:#1877f2;color:#fff;border:none;border-radius:6px;padding:8px 16px;cursor:pointer">\U0001f4d8 Post your graduation to Facebook</button>
    <div class="cert-hint" style="font-size:12px;color:#666;margin-top:8px">Sharing opens Facebook with the course link and copies a ready-made graduation post - just paste it into the composer.</div>
  </div>
  </div>
</div>
""")
    exam_js = """
(function () {
  var KEY = 'tnwn.edu.exam.v1', TKEY = 'tnwn.edu.exam.timed.v1';
  var LIMIT = 15 * 60;
  var qs = Array.prototype.slice.call(document.querySelectorAll('.exam-q'));
  function best() { try { return JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) { return null; } }
  function bestTimed() { try { return JSON.parse(localStorage.getItem(TKEY) || 'null'); } catch (e) { return null; } }
  function paintBest() {
    var b = best(), t = bestTimed();
    var el = document.getElementById('exam-best');
    if (el) el.textContent = b ? ('Practice best: ' + b.score + ' / ' + qs.length + ' (' + b.pct + '%)') : 'No attempts yet - practice anytime, untimed.';
    var el2 = document.getElementById('exam-best-timed');
    if (el2) el2.textContent = t ? ('Timed best (15 min): ' + t.score + ' / ' + qs.length + ' (' + t.pct + '%)' + (t.score >= 16 ? ' - passed under the clock' : '')) : '';
  }
  function clearMarks() {
    qs.forEach(function (q) {
      Array.prototype.forEach.call(q.querySelectorAll('label'), function (l) { l.style.background = ''; l.style.color = ''; });
    });
  }
  var gradeBtn = document.getElementById('exam-grade');
  var timeLeft = LIMIT, tickIv = null, timedMode = false, forceGrade = false;
  function fmt(s) { var m = Math.floor(s / 60), r = s % 60; return m + ':' + (r < 10 ? '0' : '') + r; }
  function paintTimer() {
    var tel = document.getElementById('exam-timer');
    if (!tel) return;
    tel.textContent = fmt(timeLeft);
    tel.style.color = timeLeft <= 30 ? '#ff5252' : timeLeft <= 120 ? '#ffb74d' : '#8ef2a0';
  }
  function stopClock() {
    if (tickIv) { clearInterval(tickIv); tickIv = null; }
    var sb = document.getElementById('exam-timed-start');
    if (sb) { sb.disabled = false; sb.style.opacity = ''; }
  }
  if (gradeBtn) gradeBtn.addEventListener('click', function () {
    if (timedMode) { stopClock(); }
    clearMarks();
    var score = 0, un = 0;
    qs.forEach(function (q) {
      var inputs = q.querySelectorAll('input');
      var pick = q.querySelector('input:checked');
      var labels = q.querySelectorAll('label');
      var ci = parseInt(q.dataset.correct, 10);
      if (!pick) { un++; return; }
      var idx = Array.prototype.indexOf.call(inputs, pick);
      if (idx === ci) { score++; }
      labels[ci].style.background = '#2e7d32'; labels[ci].style.color = '#fff';
      if (idx !== ci) { labels[idx].style.background = '#b71c1c'; labels[idx].style.color = '#fff'; }
    });
    var res = document.getElementById('exam-result');
    if (un > 0 && !forceGrade) { res.textContent = 'Answer all ' + qs.length + ' questions first - ' + un + ' left.'; res.style.color = '#ffb74d'; return; }
    forceGrade = false;
    var pct = Math.round(100 * score / qs.length), pass = score >= 16;
    var unNote = (un > 0 && timedMode) ? ' (' + un + ' unanswered when time ran out)' : '';
    res.textContent = pass
      ? ('\U0001f389 PASSED: ' + score + ' / ' + qs.length + ' (' + pct + '%)' + unNote + ' - Weather School graduate! Your certificate is below.')
      : (score + ' / ' + qs.length + ' (' + pct + '%)' + unNote + ' - not quite: 16 to pass. Review the green-marked answers and retake.');
    res.style.color = pass ? '#8ef2a0' : '#ffb74d';
    var b = timedMode ? bestTimed() : best();
    if (!b || score > b.score) { try { localStorage.setItem(timedMode ? TKEY : KEY, JSON.stringify({ score: score, pct: pct, date: new Date().toISOString().slice(0, 10) })); } catch (e) {} }
    paintBest();
    var cert = document.getElementById('edu-cert');
    if (pass) {
      cert.style.display = 'block';
      document.getElementById('cert-score').textContent = score + ' / ' + qs.length + ' (' + pct + '%)';
      document.getElementById('cert-timed').textContent = timedMode ? ' \u00b7 \u23f1\ufe0f passed in timed mode (15-minute limit)' : '';
      document.getElementById('cert-date').textContent = new Date().toLocaleDateString();
      cert.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } else { cert.style.display = 'none'; }
  });
  var retakeBtn = document.getElementById('exam-retake');
  if (retakeBtn) retakeBtn.addEventListener('click', function () {
    Array.prototype.forEach.call(document.querySelectorAll('.exam-q input'), function (i) { i.checked = false; });
    clearMarks();
    stopClock(); timedMode = false;
    document.getElementById('timer-wrap').style.display = 'none';
    timeLeft = LIMIT; paintTimer();
    document.getElementById('exam-result').textContent = '';
    document.getElementById('edu-cert').style.display = 'none';
  });
  var timedBtn = document.getElementById('exam-timed-start');
  if (timedBtn) timedBtn.addEventListener('click', function () {
    Array.prototype.forEach.call(document.querySelectorAll('.exam-q input'), function (i) { i.checked = false; });
    clearMarks();
    document.getElementById('exam-result').textContent = '';
    document.getElementById('edu-cert').style.display = 'none';
    document.getElementById('timer-wrap').style.display = 'flex';
    timedMode = true; timeLeft = LIMIT;
    timedBtn.disabled = true; timedBtn.style.opacity = '.55';
    paintTimer();
    if (tickIv) clearInterval(tickIv);
    tickIv = setInterval(function () {
      timeLeft--;
      if (timeLeft <= 0) { timeLeft = 0; paintTimer(); autoGrade(); return; }
      paintTimer();
    }, 1000);
  });
  function autoGrade() {
    stopClock();
    forceGrade = true;   /* expiry grades whatever was answered */
    document.getElementById('exam-grade').click();
    var res = document.getElementById('exam-result');
    if (res) res.textContent = '\u23f0 TIME UP - auto-graded with whatever was answered. ' + res.textContent;
    timedMode = false;   /* one-shot: later manual grades count as practice */
  }
  var printBtn = document.getElementById('cert-print');
  if (printBtn) printBtn.addEventListener('click', function () { window.print(); });
  /* Gate: exam unlocks only when every class is checked done AND every quiz
     answer worked through. Re-checked every second so unlocking feels instant. */
  function paintLock() {
    var done = {}, qz = {};
    try { done = JSON.parse(localStorage.getItem('tnwn.edu.done.v1') || '{}'); } catch (e) {}
    try { qz = JSON.parse(localStorage.getItem('tnwn.edu.quiz.v1') || '{}'); } catch (e) {}
    var cls = Array.prototype.slice.call(document.querySelectorAll('details.met-class'));
    var qzs = Array.prototype.slice.call(document.querySelectorAll('details.met-q'));
    var dc = 0, qc = 0;
    cls.forEach(function (c) { if (done[c.dataset.class]) dc++; });
    qzs.forEach(function (q) { if (qz[q.dataset.quiz]) qc++; });
    var unlocked = cls.length > 0 && dc === cls.length && qc === qzs.length;
    var lock = document.getElementById('exam-lock'), body = document.getElementById('exam-body');
    if (lock) {
      lock.style.display = unlocked ? 'none' : 'block';
      if (!unlocked) {
        var lc = document.getElementById('lock-classes'), lq = document.getElementById('lock-quizzes');
        if (lc) lc.textContent = '\u2705 Classes checked off: ' + dc + ' / ' + cls.length;
        if (lq) lq.textContent = '\U0001f9e0 Quiz answers worked: ' + qc + ' / ' + qzs.length;
      }
    }
    if (body) body.style.display = unlocked ? 'block' : 'none';
  }
  paintLock();
  setInterval(paintLock, 1000);
  var shareBtn = document.getElementById('cert-share');
  if (shareBtn) shareBtn.addEventListener('click', function () {
    var b = best();
    var scoreTxt = b ? (b.score + ' / ' + qs.length + ' (' + b.pct + '%)') : '';
    var pub = (window.TNWN_PUBLIC_URL || '').replace(/\\/+$/, '');
    var local = location.hostname === 'localhost' || location.hostname === '127.0.0.1';
    var target = (local && pub) ? pub + '/education.html' : location.origin + location.pathname;
    var msg = '\U0001f393 I just graduated from Tennessee Weather Network Weather School - 17 classes and a score of ' + scoreTxt + ' on the graduation exam! Take the free course here: ' + target;
    function openSharer() {
      window.open('https://www.facebook.com/sharer/sharer.php?u=' + encodeURIComponent(target) + '&quote=' + encodeURIComponent(msg), '_blank', 'noopener,width=620,height=540');
    }
    openSharer(); /* open right away - never block on clipboard permission */
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(msg).catch(function () {});
    }
  });
  paintBest();
})();
"""

    # Progress tracker: localStorage keeps each learner's check-offs across
    # visits. Plain string (not f-string) so the JS braces need no escaping.
    progress_banner = """
<div class="card" style="background:linear-gradient(135deg,#14532d,#2a6f97);color:#fff">
  <h2 style="color:#fff;margin:0 0 4px">📊 Your progress</h2>
  <p id="edu-done" style="margin:2px 0;font-weight:700">0 classes done</p>
  <p id="edu-quiz-done" style="margin:2px 0">0 quiz answers checked</p>
  <div style="background:rgba(255,255,255,.25);height:10px;border-radius:5px;overflow:hidden;margin:8px 0 6px">""" + """
    <div id="edu-bar" style="background:#8ef2a0;height:100%;width:0%;transition:width .3s"></div></div>
  <button id="edu-reset" style="background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.4);border-radius:6px;padding:4px 12px;cursor:pointer">Reset progress</button>
  <span style="opacity:.85"> · saved in your browser - your check-offs survive refreshes and visits</span>
</div>
"""
    progress_js = """
(function () {
  var DONE_KEY = 'tnwn.edu.done.v1', QZ_KEY = 'tnwn.edu.quiz.v1';
  function load(k) { try { return JSON.parse(localStorage.getItem(k) || '{}'); } catch (e) { return {}; } }
  function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  var done = load(DONE_KEY), qz = load(QZ_KEY);
  var classes = Array.prototype.slice.call(document.querySelectorAll('details.met-class'));
  var quizzes = Array.prototype.slice.call(document.querySelectorAll('details.met-q'));
  function paint() {
    quizzes.forEach(function (q) {
      var s = q.querySelector('summary'); if (!s) return;
      if (!s.dataset.base) s.dataset.base = s.textContent;
      s.textContent = (qz[q.dataset.quiz] ? '\u2713 ' : '') + s.dataset.base;
      q.style.opacity = qz[q.dataset.quiz] ? '.78' : '1';
    });
    classes.forEach(function (c) {
      var n = c.dataset.class, s = c.querySelector('summary'); if (!s) return;
      var cb = s.querySelector('.met-done-cb'); if (cb) cb.checked = !!done[n];
      var t = s.querySelector('.met-title'); if (!t) return;
      if (!t.dataset.base) t.dataset.base = t.textContent;
      t.textContent = (done[n] ? '\u2705 ' : '') + t.dataset.base;
      c.style.borderColor = done[n] ? '#2e7d32' : '';
    });
    var d = classes.filter(function (c) { return done[c.dataset.class]; }).length;
    var a = quizzes.filter(function (q) { return qz[q.dataset.quiz]; }).length;
    var dt = document.getElementById('edu-done'), qt = document.getElementById('edu-quiz-done');
    var bar = document.getElementById('edu-bar');
    if (dt) dt.textContent = d + ' / ' + classes.length + ' classes done';
    if (qt) qt.textContent = a + ' / ' + quizzes.length + ' quiz answers checked';
    if (bar) bar.style.width = (classes.length + quizzes.length ? Math.round(100 * (d + a) / (classes.length + quizzes.length)) : 0) + '%';
  }
  quizzes.forEach(function (q) {
    q.addEventListener('toggle', function () {
      if (q.open && !qz[q.dataset.quiz]) { qz[q.dataset.quiz] = true; save(QZ_KEY, qz); paint(); }
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll('.met-done-box'), function (box) {
    box.addEventListener('click', function (e) {
      e.preventDefault(); e.stopPropagation();
      var n = box.dataset.n; done[n] = !done[n]; save(DONE_KEY, done); paint();
    });
  });
  var rs = document.getElementById('edu-reset');
  if (rs) rs.addEventListener('click', function () {
    try { localStorage.removeItem(DONE_KEY); localStorage.removeItem(QZ_KEY); localStorage.removeItem('tnwn.edu.exam.v1'); } catch (e) {}
    location.reload();
  });
  paint();
})();
"""

    body = f"""
<header class="hero"><h1>📚 Education & Resources</h1>
<div class="sub">Learn to read weather data like a forecaster - official NOAA/NWS learning links,\nsafety guides, and how to use every tool on this site · free, always here</div></header>

<div class="card" style="background:linear-gradient(135deg,#1d3557,#2a6f97);color:#fff">
  <h2 style="color:#fff">Weather school, in order</h2>
  <p style="margin:6px 0">New to meteorology? Do these three: <b>1)</b> skim NOAA JetStream for the big picture,\n  <b>2)</b> read the radar &amp; model guides below while looking at this site's real data,\n  <b>3)</b> join SKYWARN for local, hands-on severe-weather training. That path takes most people\n  from \"just curious\" to reading soundings in a few weeks.</p>
</div>

<div class="card" style="background:linear-gradient(135deg,#14532d,#2a6f97);color:#fff">
  <h2 style="color:#fff">🎓 Free meteorology classes - 17 lessons, start anytime</h2>
  <p style="margin:6px 0">A self-paced mini-course built from the data this site already serves. Classes 1-12 are the\n  core course; 13-17 go deeper (satellite bands, Skew-T soundings, ensemble forecasting, radar velocity &amp;\n  dual-pol, how the NWS decides to warn). Take one class a day:\n  read the lesson, do the homework on the live maps, then take the 3-question self-quiz (think first, click to\n  reveal the answer). Click a class to expand it.</p>
</div>

<div style="display:flex;align-items:flex-start;gap:12px">
{sidebar_html}
<div style="flex:1;min-width:0">

{progress_banner}

{classes_html}

{exam_html}
</div>
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
<script>{progress_js}</script>
<script>{exam_js}</script>
<script>{edx_js}</script>
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

<div class="card"><h2>🎨 SPC color language</h2>
  <div class="legend" style="position:static;background:none;border:none;padding:0;flex-wrap:wrap;gap:6px 16px">
    <span><i style="background:#c1e9c1"></i> TSTM — unorganized; storms, if any, stay ordinary</span>
    <span><i style="background:#66cdaa"></i> MRGL — marginal: isolated severe possible</span>
    <span><i style="background:#ffff00"></i> SLGT — slight: scattered severe storms</span>
    <span><i style="background:#ff8c00"></i> ENH — enhanced: numerous severe storms</span>
    <span><i style="background:#ff0000"></i> MDT — moderate: widespread severe likely</span>
    <span><i style="background:#ff00ff"></i> HIGH — rare: long-track strong tornado / MCS outbreak</span>
  </div>
  <div class="src">The same six colors mean the same thing everywhere on this site: the outlook polygons on this page, the mesoanalysis composites, and the severe-parameter walls on the Models page (bulk shear, lapse rate, SCP, EHI, STP, MUCAPE, CAPE) all share SPC's categorical palette.</div>
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


def page_fieldguide(d):
    """Storm-chaser's field guide: reading SCP / STP / EHI + SPC colors together.

    A static teaching page (no live refresh risk): the six SPC categorical
    colors as the shared risk ladder, the three composite walls' formulas
    and thresholds, a chase-day drill that stacks the walls into a
    workflow, a cheat sheet, and safety. Deep links boot the Models page
    straight on each wall via its share-link (?pv=us&pvProd=...). The one
    live element is today's town ranking from the sevTowns payload.
    """
    st = d.get("sevTowns") or {}
    ranked = st.get("ranked") or []
    if ranked:
        live_html = (
            '<div class="alert" style="border-left-color:#ff8c00">'
            f'<b>Live right now ({html.escape(st.get("cycle", "")[:4]) + "Z HRRR"}):</b> '
            + " · ".join(
                f"{html.escape(r['town'])} {r['peakProd'].upper()} {r['peakVal']:.1f}"
                for r in ranked[:3])
            + ' — the <a href="models.html">Models page</a> ranks every East TN town hourly.</div>')
    else:
        live_html = ('<div class="alert ok">Right now no East TN town crosses SCP/STP/EHI ≥ 1 '
                     'in the next ~12 h (HRRR). The walls below still show where the ingredients are building.</div>')

    def _band(color, name, meaning):
        return (f'<span style="display:inline-flex;align-items:center;gap:6px;margin:2px 10px 2px 0">'
                f'<span style="width:14px;height:14px;background:{color};display:inline-block;'
                f'border:1px solid #555;border-radius:3px"></span>'
                f'<b>{name}</b> <span class="src">— {meaning}</span></span>')

    # --- Field quiz: ten "read the map" scenarios ---------------------------
    # Same grading style as the education page's graduation exam (radio MC,
    # green-correct / red-picked on grade, best score in localStorage), but
    # every question shows an SPC-colored value chip so the learner reads a
    # wall value, not just text. Chips use the exact band ramp from the
    # palette lesson above.
    fg_scenarios = [
        ("SCP", 1.6, "An SCP wall shows a broad 1.2-2.0 orange corridor ahead of the dryline. What is that corridor telling you?",
         ["Storms may form but stay ordinary", "Rotating supercells are possible — get in position",
          "Significant tornadoes are likely", "Nothing will fire today"], 1,
         "SCP ≥ 1 = supercell-favorable air; the orange band is your staging axis. Tornado wording needs STP, not SCP."),
        ("STP", 2.3, "A narrow STP maximum of 2-3 sits on the north edge of the SCP axis. What do you do with it?",
         ["Note hail as the main threat there", "Dismiss it as a model artifact",
          "Treat it as the significant-tornado corridor — the most respect on the map", "It marks where storms will die"], 2,
         "STP ≥ 2 flags a significant-tornado environment. The narrow nose on the axis's cool side is the classic tornadic end."),
        ("EHI", 1.1, "EHI crosses 1.0 two hours before SCP or STP show anything meaningful. What is this?",
         ["The other walls are broken", "A wet-bulb realm where storms can't form",
          "A hail signal", "The early heads-up: spin and fuel are stacking — watch for the others to follow"], 3,
         "EHI is the early composite — it often lights up first as CAPE and helicity come together. The guide's 'early check'."),
        ("Ladder", None, "An SCP wall shows deep green almost everywhere, a yellow batch, and one orange blob. What is the orange blob?",
         ["The day's target area — where parameters peak", "A rendering error",
          "Proof the whole map is High risk", "A less-dangerous area than the yellow"], 0,
         "Read the ladder: green unorganized → yellow slight-grade → orange enhanced/moderate-grade. The orange blob is the maximum."),
        ("SHIP", 2.2, "A SHIP wall paints 2.0-2.5 over your county while STP stays at 0.3. What kind of day is this?",
         ["A hail day, not a tornado day", "A quiet day", "A major tornado outbreak", "A snow day"], 0,
         "SHIP ≥ 1 = significant-hail environment; low STP says the tornado ingredient is missing. Read the family together."),
        ("Capped", None, "SCP reads 2.5 on the wall, but a capping inversion holds all day and nothing fires. What happened?",
         ["The model was garbage", "Walls guarantee storms", "The cap only matters over 3.0",
          "Ingredients ≠ storms — a cap can choke even strong parameters; watch erosion timing"], 3,
         "Walls describe the environment's potential, not a storm forecast. A stubborn cap is the classic forecast bust."),
        ("Night", None, "After dark, STP climbs to 2.5 along a squall line's leading edge. What does the field guide say?",
         ["Night chasing is safest — go punch the core", "Nocturnal STP always means nothing",
          "The hail threat ends at sunset", "Take it extra seriously — night chasing halves your options; reposition early, keep an out"], 3,
         "The safety section's night-discipline rule: fewer escape options in the dark — treat nocturnal STP axes with extra respect."),
        ("Outlier", None, "One model's SCP shows 4+ where three other models show 0.5-1.0. How do you handle the outlier?",
         ["Trust the extreme model — it's scarier", "Average everything to zero and ignore all",
          "Assume an outbreak is certain", "Lean toward the majority, hold a bigger safety margin, verify on radar/meso"], 3,
         "The wall's outlier badge exists for this: extremes are often resolution artifacts, so lean majority + bigger margin."),
        ("Quiet", None, "The whole wall is deep green with values under 0.5 everywhere. What does an all-green wall mean?",
         ["The map is broken — data is missing", "Green means High risk is out",
          "The green shading means hail", "A genuinely quiet pattern — all-green is information, not a bug"], 3,
         "Straight from the color-language lesson: the ladder's bottom rung is still useful information. No forcing today."),
        ("Bust", None, "You chase to the SCP 3 axis and storms are struggling — radar shows junky, disorganized cells. Now what?",
         ["Punch the core to force a look", "The models failed; give up entirely",
          "Drive into the hail to get closer", "Verify live on radar/meso, check the cap and observations — adjust rather than force it"], 3,
         "The chase-day drill's verify step: when reality lags the wall, interrogate the mesoanalysis and radar instead of forcing a core punch."),
    ]

    # --- Live wall thumbnails ------------------------------------------------
    # Each composite lesson embeds today's actual render next to the text:
    # the newest frame the payload ships for the product (page rebuilds every
    # cycle, so the picture advances with the models). Clicking opens the
    # live wall on the Models page. Graceful "no frame yet" when a product
    # has not rendered this cycle.
    _pv = (d.get("pivotUs") or [])
    _newest = {}   # product -> (url, fh, cycle) with the largest fh
    for _it in _pv:
        _p = _it.get("product")
        if _p not in ("scp", "stp", "ehi", "ship", "600_tmp"):
            continue
        for _f in (_it.get("frames") or []):
            _fh = _f.get("fh", -1)
            if _p not in _newest or _fh > _newest[_p][1]:
                _newest[_p] = (_f.get("url"), _fh, _it.get("cycle", ""))

    def _wall_thumb(prod, title):
        hit = _newest.get(prod)
        if not hit or not hit[0]:
            return ('<div class="src" style="margin:10px 0">🖼️ No ' + prod.upper()
                    + ' render in the current cycle yet — open the '
                    '<a href="models.html?pv=us&amp;pvProd=' + prod + '">live wall</a>.</div>')
        url, fh, cyc = hit
        try:
            valid = _tz.to_et(dt.datetime.strptime(cyc, "%Y%m%d%H") + dt.timedelta(hours=fh))
            stamp_txt = f"{valid.strftime('%a')} {_tz._hm(valid)} ET"
        except Exception:
            stamp_txt = f"f{fh:03d}"
        return (
            f'<a href="models.html?pv=us&amp;pvProd={prod}" title="Open the live {title} wall — zoomable, looping, all models">'
            f'<img src="{html.escape(url)}" alt="Current {title} composite map" '
            f'style="width:100%;max-width:640px;display:block;margin:10px auto 2px;border:1px solid #444;border-radius:6px"></a>'
            f'<div class="src" style="text-align:center;margin:0 0 6px">Today&#39;s {title} — newest frame (valid {html.escape(stamp_txt)}). '
            f'Click to open the live, zoomable wall.</div>')

    fg_thumb_scp = _wall_thumb("scp", "Supercell Composite")
    fg_thumb_stp = _wall_thumb("stp", "Significant Tornado Parameter")
    fg_thumb_ehi = _wall_thumb("ehi", "Energy Helicity Index")
    fg_thumb_ship = _wall_thumb("ship", "Significant Hail Parameter")
    fg_thumb_600 = _wall_thumb("600_tmp", "600 mb Temperature (melt layer)")

    def _fg_chip(prod, val):
        if val is None:
            return ""
        if val < 0.5: c, t = "#c1e9c1", "#1a1a1a"
        elif val < 1.0: c, t = "#66bb6a", "#1a1a1a"
        elif val < 2.0: c, t = "#fff176", "#1a1a1a"
        elif val < 3.0: c, t = "#ffa726", "#1a1a1a"
        elif val < 4.0: c, t = "#ef5350", "#ffffff"
        else: c, t = "#ff00ff", "#1a1a1a"
        return (f'<span style="display:inline-block;background:{c};color:{t};font-weight:700;'
                f'padding:2px 10px;border-radius:4px;border:1px solid #555;margin-right:6px">{prod} {val:.1f}</span>')

    fg_qhtml = "".join(
        f'<div class="fgq" data-correct="{ci}" data-why="{html.escape(why)}" style="margin:12px 0">'
        f'<b>{i}. {_fg_chip(prod, val)}{html.escape(scn)}</b>'
        + "".join(
            f'<label style="display:block;margin:3px 0 3px 16px;cursor:pointer;padding:2px 6px;border-radius:4px">'
            f'<input type="radio" name="fgq{i}" value="{j}" style="accent-color:#42a5f5;cursor:pointer"> {html.escape(o)}</label>'
            for j, o in enumerate(opts))
        + "</div>"
        for i, (prod, val, scn, opts, ci, why) in enumerate(fg_scenarios, 1))

    # --- Printable one-page chase card ---------------------------------------
    # Everything the guide teaches on one sheet: the SPC ladder, the four
    # composite thresholds, the drill and the safety rules. Screen view uses
    # the dark theme; @media print flips it to ink-friendly light while the
    # SPC swatches (inline backgrounds) keep their colors.
    _spc = [("#c1e9c1", "TSTM", "unorganized"), ("#66bb6a", "MRGL", "isolated severe"),
            ("#fff176", "SLGT", "scattered severe"), ("#ffa726", "ENH", "numerous severe"),
            ("#ef5350", "MDT", "widespread severe"), ("#ff00ff", "HIGH", "rare, long-track outbreak")]
    _sw = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:5px;margin:3px 14px 3px 0">'
        f'<span style="width:13px;height:13px;background:{c};display:inline-block;'
        f'border:1px solid #555;border-radius:3px"></span><b>{n}</b> '
        f'<span style="font-size:11.5px">{m}</span></span>'
        for c, n, m in _spc)
    _rows = [
        ("SCP", "supercells possible", "\u2265 4: significant supercells", "Where do rotating storms organize?"),
        ("STP", "tornado-favorable", "\u2265 2: significant tornado", "Which end is tornadic?"),
        ("EHI", "supercell-favorable \u2014 usually first", "cross-check STP", "Is spin-and-fuel stacking yet?"),
        ("SHIP", "significant hail", "very large hail", "How big might stones get?"),
    ]
    _trows = "".join(
        f'<tr style="border-bottom:1px solid #444"><td style="padding:4px 8px"><b>{w}</b></td>'
        f'<td style="padding:4px 8px">{a}</td><td style="padding:4px 8px">{b}</td>'
        f'<td style="padding:4px 8px;font-style:italic">{q}</td></tr>'
        for w, a, b, q in _rows)
    _safety = [
        ("#ef5350", "Plan the out", "know your escape route before storms fire \u2014 and a second one"),
        ("#ef5350", "No core punches", "approach hooks and hail cores from the south/southeast only"),
        ("#ffa726", "Respect night", "halve the ambition, double the margin \u2014 nocturnal STP axes deserve extra respect"),
        ("#ffa726", "Outlier humility", "one model screaming while others sleep \u2192 lean majority, hold bigger margin"),
        ("#ff0000", "Warnings win", "an NWS warning outranks every model wall \u2014 act first, argue later"),
    ]
    _srows = "".join(
        f'<div style="border-left:5px solid {c};padding:3px 10px;margin:5px 0">'
        f'<b>{t}</b> \u2014 {d}</div>'
        for c, t, d in _safety)
    # --- Hail extension: SHIP lesson + hail structure + the trade-off -------
    # Same style as the other composite lessons: the formula the wall actually
    # renders, SPC-colored thresholds, live wall thumbnail, and the field
    # judgment call the other lessons don't cover - when to abandon the
    # tornado target and let the hail core pay the day.
    FG_HAIL_HTML = f"""
<div class="card"><h2>🧊 SHIP — Significant Hail Parameter: <i>how big might the stones get?</i></h2>
  <div class="kpi"><span>Formula</span><b style="font-family:monospace;font-size:13px">min(MUCAPE/1000, 1.5) × min(LR75/5.6, 1.5) × min(mid-RH/50, 1.5) × min(EBWD/50, 1.5)</b></div>
  <p>SHIP stacks the four hail ingredients — instability, <b>steep 700–500 mb lapse rates</b> (the growth-zone chill), mid-level moisture, and deep-layer shear (which keeps stones recycling through the growth zone instead of falling out). Like the other composites, each term caps at 1.5 so one extreme can't fake the signal. SHIP says <i>nothing</i> about tornadoes — a SHIP 4 day can carry zero torsional spin.</p>
  {fg_thumb_ship}
  {fg_thumb_600}
  <div class="alert" style="border-left-color:#ffff00"><b>SHIP ≥ 1</b><span>significant-hail environment — severe hail is a credible scenario, quarter-to-golfball sized</span></div>
  <div class="alert" style="border-left-color:#ef5350"><b>SHIP ≥ 2</b><span>very large hail likely — tennis-ball-plus stones; car position and shelter planning stop being optional</span></div>
  <p class="src">Pair it with <a href=\"models.html?pv=us&amp;pvProd=lr75\">the 700–500 mb lapse-rate wall</a> (steepness of the growth zone) and <a href=\"models.html?pv=us&amp;pvProd=600_tmp\">the 600 mb temperature wall</a> (the melt layer — cold 600 mb air lets stones survive the fall). Both live on the same collage.</p>
</div>

<div class="card"><h2>📸 Hail structure — and when to trade the tornado target for the hail core</h2>
  <p><b>Reading hail structure:</b> on <a href=\"radar.html\">radar</a>, big hail announces itself — reflectivity cores <b>above 60 dBZ aloft</b>, a <b>three-body scatter spike</b> (the flaring \'hail spike\' downstream of the core), and an unusually deep, tight core pedestal. Visually: greenish-white glow inside the vault, mamma under the anvil, and stones that look like splattered rubber. High-based storms (elevated updraft bases) drop bigger stones at the ground — more fall distance to accelerate and less warm air to melt them.</p>
  <p><b>When to trade the tornado target:</b> tornado priority holds whenever <b>STP ≥ 1 shows anywhere in range</b> or a tornado watch is up — rotation beats photography, every time. Trade to the hail core when the walls split: <b>SHIP ≥ 2 with STP &lt; 0.5 and a fat cap</b> (huge fuel, no spin), high-based storms, or the SCP axis keeps dying while LR75 stays steep. The tell is in the pair, not one map — the <a href=\"models.html?pv=us&amp;pvProd=ship\">SHIP wall</a> next to <a href=\"models.html?pv=us&amp;pvProd=stp\">the STP wall</a> at the same hour makes the call obvious.</p>
  <div class="alert" style="border-left-color:#ffa726"><b>Hail intercept is a different geometry</b><span>never wait under the core — position on the storm\'s inflank, shoot wide, and keep the car pointed out with glass away from the wind. Hail kills chasers\' windshields far more often than tornadoes kill chasers.</span></div>
  <div class="alert"><b>The honest hybrid</b><span>when SHIP and STP overlap in space, ride the storm\'s rear flank where you can frame the structure and stay clear of both cores — the overlap corridor is where the day pays twice.</span></div>
  <p class="src">Hail photography prizes the same discipline as tornado work: a planned exit, distance, and letting the storm come to your camera. The <a href=\"#fg-chase-card\">field card\'s</a> safety rules all still apply.</p>
</div>
"""

    FG_CARD_HTML = f"""
<style>
@media print {{
  @page {{ margin: 10mm; }}
  body * {{ visibility: hidden; }}
  #fg-chase-card, #fg-chase-card * {{ visibility: visible; }}
  #fg-chase-card {{ position: absolute; top: 0; left: 0; width: 100%;
    background: #fff !important; border: 3px double #1d3557 !important;
    padding: 14px 18px !important; }}
  #fg-chase-card * {{ color: #111 !important; }}
  #fg-chase-card .src {{ color: #444 !important; }}
  #fg-chase-card .no-print {{ display: none !important; }}
}}
</style>
<div class="card" id="fg-chase-card" style="border:2px solid #1d3557">
  <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
    <h2 style="margin:0">🌪️ TNWN Storm Chaser's Field Card</h2>
    <span class="src">one page \u2014 colors \u00b7 thresholds \u00b7 the drill \u00b7 safety</span>
    <button id="fg-card-print" class="no-print" style="margin-left:auto;background:#1d3557;color:#fff;border:none;border-radius:6px;padding:7px 14px;cursor:pointer;font-weight:700">🖨️ Print chase card</button>
  </div>
  <p style="margin:8px 0 4px"><b>1 \u00b7 Read the colors</b> \u2014 one ladder on every map: outlooks, severe walls, mesoanalysis:</p>
  <div style="line-height:1.9">{_sw}</div>
  <p style="margin:8px 0 2px"><b>2 \u00b7 Composite thresholds</b> (green wall \u2260 bad data \u2014 it's information):</p>
  <table style="width:100%;border-collapse:collapse;font-size:12px">
    <tr style="text-align:left;border-bottom:2px solid #666"><th style="padding:4px 8px">Wall</th><th>\u2265 1 means</th><th>Stronger</th><th>Field question</th></tr>
    {_trows}
  </table>
  <p style="margin:8px 0 2px"><b>3 \u00b7 The drill</b>: Outlook colors frame the day \u2192 MUCAPE \u00d7 shear pair \u2192 <b>SCP axis</b> = staging \u2192 <b>STP corridor</b> = tornado target \u2192 verify live (radar \u00b7 meso \u00b7 town card).</p>
  <p style="margin:8px 0 2px"><b>4 \u00b7 Safety \u2014 non-negotiable</b>:</p>
  {_srows}
  <p class="src" style="margin:8px 0 0;font-size:11.5px">Educational guidance only \u00b7 official sources: weather.gov \u00b7 spc.noaa.gov \u00b7 your local NWS office \u00b7 Tennessee Weather Network field guide</p>
</div>"""

    FG_QUIZ_HTML = (
        '<div class="card" id="fg-quiz" style="border:2px solid #ffa726">'
        '<h2>🧠 Field quiz — read the walls, 8 of 10 to pass</h2>'
        '<p style="margin:6px 0">Ten map-reading scenarios straight off the severe walls. '
        'The colored chips use the SPC fills from the ladder above — judge each answer like you would on a chase day. '
        '<b>8 of 10 (80%)</b> passes. Grading marks green-correct / red-picked, explains the why behind each answer, '
        'and your best score is saved on this device.</p>'
        + fg_qhtml +
        '<button id="fg-grade" style="background:#2e7d32;color:#fff;border:none;border-radius:6px;padding:8px 18px;font-weight:700;cursor:pointer;margin:6px 6px 0 0">Grade my scenarios</button> '
        '<button id="fg-retake" style="background:transparent;color:inherit;border:1px solid currentColor;border-radius:6px;padding:8px 14px;cursor:pointer;margin:6px 0 0">Retake (clears answers)</button> '
        '<span id="fg-best" class="src" style="margin-left:8px"></span>'
        '<p id="fg-result" style="margin:10px 0 2px;font-weight:700;font-size:1.05em"></p>'
        '</div>')

    FG_QUIZ_JS = """
<script>
(function () {
  var KEY = 'tnwn.fg.quiz.v1';
  var qs = Array.prototype.slice.call(document.querySelectorAll('.fgq'));
  var PASS = 8;
  function best() { try { return JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) { return null; } }
  function paintBest() {
    var el = document.getElementById('fg-best'), b = best();
    if (el) el.textContent = b ? ('best: ' + b.score + ' / ' + qs.length + ' (' + b.pct + '%)' + (b.score >= PASS ? ' ✓' : '')) : 'no attempts yet';
  }
  function clearMarks() {
    qs.forEach(function (q) {
      Array.prototype.forEach.call(q.querySelectorAll('label'), function (l) { l.style.background = ''; l.style.color = ''; });
      var w = q.querySelector('.fgq-why'); if (w) w.remove();
    });
  }
  function grade() {
    clearMarks();
    var score = 0, un = 0;
    qs.forEach(function (q) {
      var inputs = q.querySelectorAll('input');
      var pick = q.querySelector('input:checked');
      var labels = q.querySelectorAll('label');
      var ci = parseInt(q.dataset.correct, 10);
      var w = document.createElement('div');
      w.className = 'fgq-why src';
      w.style.margin = '2px 0 2px 16px';
      w.textContent = '✓ ' + q.dataset.why;
      q.appendChild(w);
      if (!pick) { un++; labels[ci].style.background = '#2e7d32'; labels[ci].style.color = '#fff'; return; }
      var idx = Array.prototype.indexOf.call(inputs, pick);
      if (idx === ci) { score++; }
      labels[ci].style.background = '#2e7d32'; labels[ci].style.color = '#fff';
      if (idx !== ci) { labels[idx].style.background = '#b71c1c'; labels[idx].style.color = '#fff'; }
    });
    var res = document.getElementById('fg-result');
    if (un > 0) { res.textContent = 'Answer all ' + qs.length + ' scenarios first — ' + un + ' left.'; res.style.color = '#ffb74d'; return; }
    var pct = Math.round(100 * score / qs.length), pass = score >= PASS;
    res.textContent = pass
      ? ('🎉 ' + score + ' / ' + qs.length + ' (' + pct + '%) — you read the walls like a chaser. Field-ready.')
      : (score + ' / ' + qs.length + ' (' + pct + '%) — ' + PASS + ' to pass. Re-read the green-marked whys and retake.');
    res.style.color = pass ? '#8ef2a0' : '#ffb74d';
    var b = best();
    if (!b || score > b.score) { try { localStorage.setItem(KEY, JSON.stringify({ score: score, pct: pct, date: new Date().toISOString().slice(0, 10) })); } catch (e) {} }
    paintBest();
  }
  var g = document.getElementById('fg-grade');
  if (g) g.addEventListener('click', grade);
  var r = document.getElementById('fg-retake');
  if (r) r.addEventListener('click', function () {
    Array.prototype.forEach.call(document.querySelectorAll('.fgq input'), function (i) { i.checked = false; });
    clearMarks();
    var res = document.getElementById('fg-result'); if (res) res.textContent = '';
  });
  var pc = document.getElementById('fg-card-print');
  if (pc) pc.addEventListener('click', function () { window.print(); });
  paintBest();
})();
</script>
"""

    body = f"""
<div class="card"><h2>🌪️ Storm Chaser's Field Guide — reading the composites &amp; SPC colors together</h2>
  <p>This guide teaches one skill: walking onto the <a href="models.html">Models page</a>, reading the severe-parameter walls — <b>SCP</b>, <b>STP</b>, <b>EHI</b> (and SHIP for hail) — and knowing <i>instantly</i> what the colors are telling you, because every wall, the Severe page outlooks and the Mesoanalysis composites all speak the same six-color SPC language.</p>
  {live_html}
</div>

<div class="card"><h2>🎨 The SPC color language — one ladder, everywhere</h2>
  <p>SPC's categorical outlook colors double as the fill ramp on the severe walls. Memorize the ladder once and every map on this site reads the same way — a yellow blob means <i>slight-risk-grade</i> environment whether it's an outlook polygon, an SCP wall, or a mesoanalysis composite:</p>
  <div style="line-height:2.1">{_band('#c1e9c1', 'TSTM', 'unorganized — storms, if any, stay ordinary')}</div>
  <div style="line-height:2.1">{_band('#66cdaa', 'MRGL', 'marginal — isolated severe possible')}</div>
  <div style="line-height:2.1">{_band('#ffff00', 'SLGT', 'slight — scattered severe storms')}</div>
  <div style="line-height:2.1">{_band('#ff8c00', 'ENH', 'enhanced — numerous severe storms')}</div>
  <div style="line-height:2.1">{_band('#ff0000', 'MDT', 'moderate — widespread severe likely')}</div>
  <div style="line-height:2.1">{_band('#ff00ff', 'HIGH', 'rare — long-track strong tornado / MCS outbreak')}</div>
  <p class="src">On the walls the ramp is continuous (values blend between bands); the hex values above are the exact anchors shared with the Severe page and the mesoanalysis composites. A wall that is entirely green is simply saying "no organized-severe environment yet" — that is information, not a bug.</p>
</div>

<div class="card"><h2>🌪️ SCP — Supercell Composite: <i>can a rotating storm organize here?</i></h2>
  <div class="kpi"><span>Formula</span><b style="font-family:monospace;font-size:13px">min(MUCAPE/1000, 1.5) × min(ESRH/50, 1.5) × min(EBWD/20, 1.5)</b></div>
  <p>SCP multiplies the three supercell ingredients — instability, storm-relative helicity (spin), and deep-layer shear — so <b>all three must be present</b> for the value to climb. A wall goes yellow-orange only where fuel and spin overlap.</p>
  {fg_thumb_scp}
  <div class="alert" style="border-left-color:#66cdaa"><b>SCP &lt; 1</b><span>ordinary cells at best — chase for lightning photos, not structure</span></div>
  <div class="alert" style="border-left-color:#ffff00"><b>SCP 1–4</b><span>marginal supercells possible — watch storm mode closely</span></div>
  <div class="alert" style="border-left-color:#ff0000"><b>SCP ≥ 4</b><span>significant supercells likely — the classic target area</span></div>
  <p class="src">Read it on <a href="models.html?pv=us&amp;pvProd=scp">the Supercell Composite wall</a>. The strongest axis is where you stage <i>before</i> storms fire; the cap at 1.5 per term keeps one extreme ingredient (huge CAPE, dead air) from faking a signal.</p>
</div>

<div class="card"><h2>🎯 STP — Significant Tornado Parameter: <i>which end is tornadic?</i></h2>
  <div class="kpi"><span>Formula</span><b style="font-family:monospace;font-size:13px">SCP's terms at tornado weights × LCL term — low cloud bases raise it</b></div>
  <p>STP is SCP with tornado tuning: it weighs helicity and shear harder and adds a <b>cloud-base term</b> — low LCLs (humid surface air, small T/Td spread) are the tornadic ingredient. Where SCP says "supercells", STP says <i>which part of that area supports tornadoes</i>.</p>
  {fg_thumb_stp}
  <div class="alert" style="border-left-color:#ffff00"><b>STP ≥ 1</b><span>tornado-favorable — tornado is a credible scenario in that corridor</span></div>
  <div class="alert" style="border-left-color:#ff0000"><b>STP ≥ 2</b><span>significant-tornado environments — strong, long-track tornadoes possible</span></div>
  <p class="src">Read it on <a href="models.html?pv=us&amp;pvProd=stp">the Significant Tornado Parameter wall</a>, animated hour by hour. In the field: when a QLCS or supercell cluster moves along an STP axis, the embedded rotation threat is highest where the axis and the storm path intersect.</p>
</div>

<div class="card"><h2>⚡ EHI — Energy Helicity Index: <i>the early spin-and-fuel check</i></h2>
  <div class="kpi"><span>Formula</span><b style="font-family:monospace;font-size:13px">MUCAPE × ESRH / 160,000</b></div>
  <p>EHI is deliberately simple — just instability × spin. It ignores shear direction and cloud bases, which makes it fast and honest: when EHI climbs toward 1 while the shearing is still organizing, supercells are becoming possible even before SCP agrees. It is often the <b>first</b> of the three to light up as a warm front or dryline sets up.</p>
  {fg_thumb_ehi}
  <div class="alert" style="border-left-color:#ffff00"><b>EHI ≥ 1</b><span>supercell-favorable — cross-check SCP and STP for the full picture</span></div>
  <p class="src">Read it on <a href="models.html?pv=us&amp;pvProd=ehi">the EHI wall</a>. Hail hunters: pair it with <a href="models.html?pv=us&amp;pvProd=ship">the SHIP wall</a> — SHIP ≥ 1 marks significant-hail environments the same way.</p>
</div>

{FG_HAIL_HTML}
<div class="card"><h2>🧭 The chase-day drill — one workflow, five maps</h2>
  <p><b>1. Frame the day.</b> Open the <a href="severe.html">Severe page</a> — the SPC outlook colors set expectations. ENH+ anywhere in your range means today is a driving day.</p>
  <p><b>2. Check the ingredients.</b> On the <a href="models.html">collage</a>, load <a href="models.html?pv=us&amp;pvProd=mucape">MUCAPE</a> and <a href="models.html?pv=us&amp;pvProd=shear06">0–6 km shear</a> at the same hour: fuel ≥ ~2000 J/kg under ≥ ~30 kt shear is the raw supercell pairing.</p>
  <p><b>3. Find the organization zone.</b> Switch to <a href="models.html?pv=us&amp;pvProd=scp">SCP</a> — the yellow-orange axis is your staging area. Play the wall loop to see when it matures.</p>
  <p><b>4. Find the tornado end.</b> Switch to <a href="models.html?pv=us&amp;pvProd=stp">STP</a> — where it crosses 1 inside the SCP axis is your tornado corridor. Advance the hours: corridors migrate with the low-level jet.</p>
  <p><b>5. Verify live.</b> Watch the <a href="radar.html">radar</a> and the <a href="meso.html">mesoanalysis</a> as storms fire — the meso composites show what the storm is <i>actually</i> ingesting. The Models page town-ranking card tells you which communities sit in the parameter air.</p>
  <div class="alert"><b>When the models disagree</b><span>different models painting SCP differently = timing/setup uncertainty. The wall's outlier badge flags the odd one out; lean toward the majority and hold a bigger safety margin.</span></div>
</div>

<div class="card"><h2>📋 Cheat sheet</h2>
  <p style="margin:4px 0 8px"><b>SHIP — Significant Hail Parameter</b> rounds out the family; today's wall is below the table.</p>
  <table style="width:100%;border-collapse:collapse;font-size:13.5px">
    <tr style="text-align:left;border-bottom:2px solid #444"><th style="padding:6px">Wall</th><th>≥ 1 means</th><th>≥ 2 / 4 means</th><th>Field question it answers</th></tr>
    <tr style="border-bottom:1px solid #333"><td style="padding:6px"><b>SCP</b></td><td>supercells possible</td><td>≥ 4: significant supercells</td><td>Where do rotating storms organize?</td></tr>
    <tr style="border-bottom:1px solid #333"><td style="padding:6px"><b>STP</b></td><td>tornado-favorable</td><td>≥ 2: significant tornado</td><td>Which end is tornadic?</td></tr>
    <tr style="border-bottom:1px solid #333"><td style="padding:6px"><b>EHI</b></td><td>supercell-favorable</td><td>use with STP</td><td>Is spin-and-fuel stacking yet?</td></tr>
    <tr><td style="padding:6px"><b>SHIP</b></td><td>significant hail possible</td><td>very large hail</td><td>How big might the stones get?</td></tr>
  </table>
  <p class="src">All four walls share the SPC palette above — value bands climb green → teal → yellow → orange → red → magenta exactly like an outlook.</p>
  {fg_thumb_ship}
</div>

{FG_QUIZ_HTML}

<div class="card"><h2>⚠️ Safety — the part that actually matters</h2>
  <div class="alert" style="border-left-color:#ff0000"><b>The forecast gets you close. The radar, an escape route, and discipline keep you alive.</b>
  <span>Never core-punch a hook or hail core — approach from the south/southeast, keep an escape route planned, and know the road network before convection fires.</span></div>
  <div class="alert"><b>Chase with an out.</b><span>Always know where you would drive if the storm turns. Night chasing halves your options — treat nocturnal STP axes with extra respect.</span></div>
  <div class="alert"><b>Warnings win.</b><span>Model walls are guidance, not guarantees — when the NWS issues a warning for your location, act on it first and argue with the models later.</span></div>
  <p class="src">This guide is educational. It is not official forecasting guidance — the <a href="severe.html">Severe page's NWS/SPC products</a> and local warnings are the authoritative source.</p>
</div>

{FG_CARD_HTML}

{FG_QUIZ_JS}
"""
    return _page("Storm Chaser's Field Guide", "fieldguide.html", body)


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
<div class="sub">NWS Northwest River Prediction Center gauges - stage, flood category and forecasts · updated <span id="rvStamp">{d["generated"]}</span></div></header>

<div class="card">
  <div class="kpis" id="rvKpis">
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
let GAUGES = {gauges_js};
function table() {{
  const sel = document.getElementById("riverSel");
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
  sel.onchange = table;
  table();
}}
boot();
/* soft auto-refresh: stages and flood categories move with every NWS gauge
   sweep, so the KPIs, table and map markers re-render from each 90 s pull
   instead of waiting for a hard reload ("rivers frozen", 2026-09-21). The
   river filter <select> is rebuilt on refresh, so re-apply the user's pick. */
function onDataRefresh(d2) {{
  DATA = d2;
  const rv = d2.rivers || {{}};
  const st = document.getElementById("rvStamp");
  if (st && d2.generated) st.textContent = d2.generated;
  const gs = rv.gauges || [];
  const cnt = rv.counts || {{}};
  const fl = rv.floodCount || 0;
  const kp = document.getElementById("rvKpis");
  if (kp) {{
    const col = fl ? "#d32f2f" : "#43a047";
    const word = fl ? (fl + " river" + (fl !== 1 ? "s" : "") + " IN FLOOD right now")
                    : "No flooding on any monitored river";
    kp.innerHTML =
      `<div class="kpi"><span>Status</span><b style="color:${{col}}">${{word}}</b></div>`
      + `<div class="kpi"><span>Gauges reporting</span><b>${{gs.length}}</b></div>`
      + `<div class="kpi"><span>Categories</span><b>${{Object.entries(cnt)
          .sort((a, b) => b[1] - a[1]).slice(0, 4)
          .map(([k, v]) => v + " " + k.replace(/_/g, " ")).join(" \u00b7 ")}}</b></div>`;
  }}
  GAUGES = gs;
  const sel = document.getElementById("riverSel");
  if (sel) {{
    const keep = sel.value;
    const groups = {{}};
    gs.forEach(g => {{ if (g.lat != null) (groups[g.group] = groups[g.group] || []).push(g); }});
    sel.innerHTML = '<option value="">All rivers</option>' + Object.keys(groups).sort()
      .map(r => `<option value="${{r}}">${{r}} (${{groups[r].length}})</option>`).join("");
    if (keep && groups[keep]) sel.value = keep;   /* filter survives */
    sel.onchange = table;
  }}
  try {{ table(); }} catch (_e) {{}}
  try {{
    if (typeof map !== "undefined" && map) {{
      if (window._rvLayer) map.removeLayer(window._rvLayer);
      window._rvLayer = L.layerGroup(gs.filter(g => g.lat != null).map(g =>
        L.circleMarker([g.lat, g.lon], {{ radius: 6.5, color: "#1b2027", weight: 1.5,
          fillColor: g.catColor || "#9e9e9e", fillOpacity: .95 }})
          .bindPopup(`<b>${{g.name}}</b><br/>Stage: <b>${{g.stage ?? "?"}} ${{g.stageUnit || "ft"}}</b> - ${{g.catWord || ""}}<br/>`
            + (g.fcstStage != null ? `Forecast: ${{g.fcstStage}} ${{g.stageUnit || "ft"}}<br/>` : "")
            + `<a href="${{g.url}}" target="_blank" rel="noopener">Official hydrograph (NWPS)</a>`))).addTo(map);
    }}
  }} catch (_e) {{}}
}}
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


def page_traffic(d):
    """TDOT SmartWay traffic cameras: map + region/route picker + live snapshots."""
    rc = d.get("roadCams") or {}
    rc_js = json.dumps(rc, ensure_ascii=False, separators=(",", ":"))
    n_etn = rc.get("etnCount") or 0
    n_all = rc.get("total") or 0
    n_ev = len(rc.get("events") or [])
    routes = rc.get("etnRoutes") or {}
    route_opts = "".join(f'<option value="{html.escape(r)}">{html.escape(r)}</option>'
                         for r in routes)
    sev_col = {"high": "#e57373", "warn": "#ffb74d", "info": "#4fc3f7"}
    _sev_ord = {"high": 0, "warn": 1, "info": 2}
    ev_rows = "".join(
        f'<tr class="evRow" data-id="{html.escape(str(e.get("id")))}" style="cursor:pointer">'
        f'<td><span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
        f'background:{sev_col.get(e.get("sev"), "#4fc3f7")}"></span> '
        f'{html.escape(e.get("subtype") or "Weather")}</td>'
        f'<td>{html.escape(e.get("route") or "-")}</td>'
        f'<td>{html.escape((e.get("desc") or "")[:120])}{"..." if len(e.get("desc") or "") > 120 else ""}</td>'
        f'<td>{html.escape(e.get("county") or "")}</td>'
        f'<td>{html.escape(e.get("reported") or "")}</td>'
        f'<td>{("📷 " + str(len(e.get("cams") or []))) if e.get("cams") else "-"}</td></tr>'
        for e in sorted(rc.get("events") or [],
                        key=lambda x: _sev_ord.get(x.get("sev"), 3))[:14])
    events_tbl = (f'<table style="margin-top:8px"><tr><th>Type</th><th>Route</th><th>Event</th>'
                  f'<th>County</th><th>Reported</th><th>Cams</th></tr>{ev_rows}</table>'
                  if ev_rows else '<div class="src">No active road-weather events statewide - roads clear per TDOT.</div>')
    body = f"""
<header class="hero"><h1>🚦 TDOT Traffic Cameras</h1>
<div class="sub">SmartWay highway cameras statewide — {n_etn} across East Tennessee, {n_all} total. Snapshots refresh every ~60 s; tap a camera for the live view. TDOT open data · updated {html.escape(rc.get("fetched") or "-")}</div></header>

<div class="card">
  <h2>🚧 Road-weather events & road conditions <span class="src">(TDOT statewide feed - click a row to fly the map there)</span></h2>
  {events_tbl}
  <div class="src" style="margin-top:4px">Red = lanes blocked / storm damage · amber = ice, snow or frost risk · blue = advisory. The 📷 count links each event to nearby camera views - the ground truth for what the road surface looks like.</div>
</div>

<div class="card">
  <h2>🗺️ Camera map</h2>
  <div class="ctl" style="margin-bottom:8px">
    <select id="camRegion">
      <option value="etn">East Tennessee (Region 1)</option>
      <option value="1">Region 1 – Knoxville / Tri-Cities</option>
      <option value="2">Region 2 – Chattanooga</option>
      <option value="3">Region 3 – Nashville</option>
      <option value="4">Region 4 – Memphis</option>
      <option value="all">All regions</option>
    </select>
    <select id="camRoute"><option value="">All routes</option>{route_opts}</select>
    <label style="white-space:nowrap"><input type="checkbox" id="evNearCams"> cameras near events</label>
    <label style="white-space:nowrap"><input type="checkbox" id="radarToggle"> 🌧️ radar</label>
    <span class="src" id="camCount"></span>
  </div>
  <div id="map" class="map-dark" style="height:520px"></div>
  <div class="legend" style="margin-top:6px">
    <span><i style="background:#4fc3f7"></i>camera · click for snapshot + live stream</span>
    <span><i style="background:#e57373"></i>lanes blocked / damage</span>
    <span><i style="background:#ffb74d"></i>ice / snow risk</span>
    <span><i style="background:#7986cb"></i>advisory event</span>
  </div>
</div>

<div class="card">
  <h2>📷 Camera gallery <span class="src">(current snapshot, tap to open the live stream on SmartWay)</span></h2>
  <div id="gallery" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px"></div>
</div>

<div class="card"><span class="src">Data: TDOT SmartWay open-data API (official public feed powering smartway.tn.gov). Snapshots © TDOT; live HLS streams where TDOT publishes them. Times Eastern.</span></div>

<script>
const RC = {rc_js};
const RC_CAMS = RC.cams || [];
const RC_EVENTS = (RC.events || []).filter(e => e.lat && e.lng);
const EV_COL = {{ high: "#e57373", warn: "#ffb74d", info: "#7986cb" }};
let camMarkers = [], evMarkers = [];
let radarLayer = null;

/* ---- events: severity-colored markers + popup with camera ground truth ---- */
function evPopup(e) {{
  const cams = (e.cams || []).map(c =>
    `<div style="margin-top:5px"><img loading="lazy" src="${{c.thumb}}?t=${{Date.now()}}" `
    + `style="width:220px;border-radius:6px;display:block" alt="cam">`
    + `<span class="src">${{c.title}} · ${{c.miles}} mi</span></div>`).join("");
  return `<b>${{EV_COL[e.sev] ? "" : ""}}${{e.subtype}}</b> ${{e.status ? "· " + e.status : ""}}`
    + `${{e.route ? `<br><b>${{e.route}}</b>` : ""}}${{e.county ? ` · ${{e.county}} Co.` : ""}}`
    + `${{e.reported ? `<br><span class="src">reported ${{e.reported}}</span>` : ""}}`
    + `<div style="max-width:300px;margin-top:3px">${{e.desc || ""}}</div>`
    + `${{cams ? `<div class="src" style="margin-top:6px">📷 nearest cameras:</div>${{cams}}` : "<div class='src'>no camera within 6 mi</div>"}}`;
}}

function evDraw() {{
  evMarkers.forEach(m => map.removeLayer(m)); evMarkers = [];
  RC_EVENTS.forEach(e => {{
    const col = EV_COL[e.sev] || "#7986cb";
    const icon = L.divIcon({{ className: "", iconSize: [18, 18],
      html: `<div style="width:18px;height:18px;border-radius:50%;background:${{col}};`
        + `border:2.5px solid #fff;box-shadow:0 0 8px ${{col}}"></div>` }});
    const m = L.marker([e.lat, e.lng], {{ icon, zIndexOffset: 400 }}).addTo(map)
      .bindPopup(() => evPopup(e), {{ maxWidth: 340 }});
    m._ev = e;
    evMarkers.push(m);
  }});
}}

/* fly to an event when its table row is clicked */
function evWire() {{
  document.querySelectorAll("tr.evRow").forEach(tr => {{
    tr.onclick = () => {{
      const e = RC_EVENTS.find(x => String(x.id) === tr.dataset.id);
      if (!e) return;
      map.setView([e.lat, e.lng], 12, {{ animate: true }});
      const hit = evMarkers.find(mm => mm._ev && String(mm._ev.id) === tr.dataset.id);
      if (hit) setTimeout(() => hit.openPopup(), 350);
    }};
  }});
}}

/* radar overlay (same RainViewer tiles the radar page uses) */
async function radarToggle() {{
  const on = document.getElementById("radarToggle").checked;
  if (!on) {{ if (radarLayer) {{ map.removeLayer(radarLayer); radarLayer = null; }} return; }}
  if (radarLayer) return;
  try {{
    const j = await (await fetch("https://api.rainviewer.com/public/weather-maps.json", {{cache: "no-store"}})).json();
    const f = (j.radar && j.radar.past || []).pop();
    if (!f) return;
    radarLayer = L.tileLayer("https://tilecache.rainviewer.com" + f.path + "/256/{{z}}/{{x}}/{{y}}/2/1_1.png",
      {{ opacity: 0.55, maxNativeZoom: 10, maxZoom: 21 }}).addTo(map);
  }} catch (_e) {{ /* radar is sugar - ignore */ }}
}}

const nearEv = () => document.getElementById("evNearCams")
  && document.getElementById("evNearCams").checked;
function camFiltered() {{
  const reg = document.getElementById("camRegion").value;
  const route = document.getElementById("camRoute").value;
  /* cameras-near-events mode: regardless of region, show every camera
     within ~25 km of an active event (widened from 13: TDOT events often
     sit in rural dead zones, so the nearest corridor camera is the honest
     context). With no events, falls through to the region filter. */
  if (nearEv() && RC_EVENTS.length) {{
    const hit = new Set();
    RC_EVENTS.forEach(e => RC_CAMS.forEach(c => {{
      if (!c.active) return;
      const dx = (c.lat - e.lat) * 111.32, dy = (c.lng - e.lng) * 111.32 * Math.cos(e.lat * Math.PI / 180);
      if (Math.hypot(dx, dy) <= 25) hit.add(c.id);
    }}));
    return RC_CAMS.filter(c => hit.has(c.id));
  }}
  return RC_CAMS.filter(c => {{
    if (route && c.route !== route) return false;
    if (!c.active) return false;
    if (reg === "all") return true;
    const m = (c.region || "").match(/Region (\\d)/);
    const rn = m ? m[1] : null;
    if (reg === "etn") return rn === "1";
    return rn === reg;
  }});
}}

function camPopup(c) {{
  return `<b>${{c.title}}</b><br>${{c.route || ""}}${{c.mile ? " · mile " + c.mile : ""}}${{c.county ? " · " + c.county + " Co." : ""}}<br>` +
         `<img src="${{c.thumb}}?t=${{Date.now()}}" style="max-width:280px;border-radius:6px;margin:4px 0" alt="camera snapshot">` +
         (c.video ? `<a href="https://smartway.tn.gov/allcams/camera/${{c.id}}" target="_blank" rel="noopener">▶ Live stream on SmartWay ↗</a>` : "");
}}

function camDraw() {{
  const list = camFiltered();
  camMarkers.forEach(m => map.removeLayer(m)); camMarkers = [];
  evDraw();   /* events always visible - severity colors pop against cams */
  const gallery = document.getElementById("gallery");
  gallery.innerHTML = "";
  list.forEach(c => {{
    const m = L.circleMarker([c.lat, c.lng], {{ radius: 5, color: "#0d1117", weight: 1.5,
      fillColor: "#4fc3f7", fillOpacity: 0.95 }}).addTo(map)
      .bindPopup(() => camPopup(c), {{ maxWidth: 320 }});
    m._cam = c;
    camMarkers.push(m);
  }});
  document.getElementById("camCount").textContent =
    `${{list.length}} camera${{list.length === 1 ? "" : "s"}}`
    + (nearEv() && list.length ? ` near active event${{RC_EVENTS.length === 1 ? "" : "s"}}` : "");
  list.slice(0, 24).forEach(c => {{
    const d = document.createElement("div");
    d.style.cssText = "border:1px solid #263041;border-radius:8px;overflow:hidden;background:#111722;cursor:pointer";
    d.innerHTML = `<img loading="lazy" src="${{c.thumb}}?t=${{Date.now()}}" style="width:100%;display:block" alt="${{c.title}}">` +
      `<div style="padding:6px 8px;font-size:12px;color:#cdd7e4">${{c.title}}${{c.county ? " · " + c.county : ""}}</div>`;
    d.onclick = () => {{
      map.setView([c.lat, c.lng], 12);
      const hit = camMarkers.find(mm => mm._cam && mm._cam.id === c.id);
      if (hit) hit.openPopup();
    }};
    gallery.appendChild(d);
  }});
}}

function camRoutes() {{
  const reg = document.getElementById("camRegion").value;
  const counts = {{}};
  RC_CAMS.forEach(c => {{
    const m = (c.region || "").match(/Region (\\d)/); const rn = m ? m[1] : null;
    const inReg = reg === "all" ? true : reg === "etn" ? rn === "1" : rn === reg;
    if (inReg && c.route && c.active) counts[c.route] = (counts[c.route] || 0) + 1;
  }});
  document.getElementById("camRoute").innerHTML = '<option value="">All routes</option>' +
    Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([r, n]) => `<option value="${{r}}">${{r}} (${{n}})</option>`).join("");
}}

async function boot() {{
  try {{ DATA = await (await fetch(dataUrl(), {{cache: "no-store"}})).json(); }} catch (_e) {{ DATA = {{}}; }}
  document.title = DATA.pageName + " - Traffic";
  {_mapbox_token_js()}
  map = L.map("map", {{ zoomSnap: 0.5, maxZoom: 21 }}).setView([35.95, -83.6], 7);
  addMapControls(map, [35.95, -83.6], 7);
  camRoutes();
  camDraw();
  evWire();
  const rT = document.getElementById("radarToggle");
  if (rT) rT.onchange = radarToggle;
  const nE = document.getElementById("evNearCams");
  if (nE) nE.onchange = camDraw;
  document.getElementById("camRegion").onchange = () => {{ camRoutes(); camDraw(); }};
  document.getElementById("camRoute").onchange = camDraw;
  // snapshots refresh ~60 s like SmartWay's own viewer (only while visible)
  setInterval(() => {{ if (!document.hidden) camDraw(); }}, 60000);
}}
boot();
</script>
"""
    return _page("Traffic", "traffic.html", body)


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
  <div class="src">Images: NOAA/SPC Storm Prediction Center mesoscale analysis (public domain), updated hourly at :00. Field filled with SPC's official color palettes; overlays stack on top. The 6-hour animation steps through SPC's archived hourly frames. <b>EHI</b> comes from this site's own hourly HRRR render (SPC no longer analysis it) in the same SPC palette; it shows the latest model cycle rather than the hourly objective analysis.</div>
  <div class="src" id="spcKey" style="display:none;margin-top:6px;align-items:center;flex-wrap:wrap;gap:4px 12px">
    <b>SPC bands:</b>
    <span style="color:#c1e9c1">&#9632;</span> TSTM unorganized
    <span style="color:#66cdaa">&#9632;</span> MRGL marginal
    <span style="color:#ffff00">&#9632;</span> SLGT slight
    <span style="color:#ff8c00">&#9632;</span> ENH enhanced
    <span style="color:#ff0000">&#9632;</span> MDT moderate
    <span style="color:#ff00ff">&#9632;</span> HIGH high-end
    <span style="color:#8fa3bf">— shown when a composite index or outlook overlay is active; same colors as the Models-page severe walls</span>
  </div>
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
/* SPC band key: composite indices (stor/stpc/scp/sigh/ehi) read in the same
   categorical colors as the site's severe walls, and the outlook overlay
   literally paints those polygons - show the plain-English key for either.
   ehi comes from the site's own SPC-paletted HRRR wall (SPC dropped the
   field from its mesoanalysis lineup), same ladder, same key. */
const SPC_MESO_FIELDS = new Set(["stor", "stpc", "scp", "sigh", "ehi"]);
const spcKey = document.getElementById("spcKey");
function spcKeyUpdate() {{
  if (spcKey) spcKey.style.display =
    (SPC_MESO_FIELDS.has(curFld) || document.getElementById("ovOtlk").checked) ? "flex" : "none";
}}
document.getElementById("fld").onchange = e => {{ userPicked = true; curFld = e.target.value; if (!baseFor(curSec) && !(curFld === "ehi" && SEC[curSec])) curSec = "19"; document.getElementById("sec").value = curSec; tick(); spcKeyUpdate(); }};
document.getElementById("sec").onchange = e => {{ userPicked = true; curSec = e.target.value; tick(); }};
document.getElementById("ovOtlk").addEventListener("change", spcKeyUpdate);
for (const id of ["ovRadar", "ovWarns"]) document.getElementById(id).onchange = show;
spcKeyUpdate();
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
      /* The EHI field is the site's own wall render - its history lives in
         the model-map archive, not SPC's mesoarchive; label it as "now"
         rather than pretend an hourly ago-file exists. */
      if (curFld === "ehi") {{
        document.getElementById("fr").textContent = (MESO.analysis || "") + "  (latest wall render)";
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
      fillPickers();
      spcKeyUpdate();
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
<div class="sub">Live METARs for East Tennessee, Southwest VA & Western NC — plus the whole US · city board · RAP 13 km soundings at 8 locations · updated <span id="obsStamp">{d["generated"]}</span></div></header>

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
  <div style="overflow-x:auto"><table class="cells" id="cityTbl"></table></div>
  <div class="src" id="cityNote">Click a column header to sort. Arrows show the change since the station's previous observation.</div>
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
/* city board: one renderer used by boot AND every soft refresh, with
   clickable column sorts and prev-observation trend arrows. Sort state
   survives refreshes; arrows come from prevTempF computed at build time. */
const CITY_COLS = [
  {{key:"city", label:"City", num:false}},
  {{key:"tempF", label:"Temp", num:true}},
  {{key:"dewF", label:"Dew point", num:true}},
  {{key:"windMph", label:"Wind", num:true}},
  {{key:"gustMph", label:"Gust", num:true}},
  {{key:"rh", label:"Humidity", num:true}},
  {{key:"ageMin", label:"Obs", num:true}},
];
let citySort = {{k:"city", dir:1}};
function cityVal(c, k) {{
  const v = c[k];
  if (k === "windMph") return v == null ? Infinity : v;   /* calm -> bottom */
  return v == null ? (citySort.k === k ? (citySort.dir > 0 ? Infinity : -Infinity) : null) : v;
}}
function ageHtml(c) {{
  /* '9 min ago' style stamp - the METAR clock every station runs on its
     own cadence, shown so a fresh number never looks stuck */
  if (c.ageMin == null) return c.time ? esc2(c.time) : "-";
  const a = c.ageMin;
  const txt = a < 1 ? "just now" : a < 60 ? `${{a}} min ago` : `${{Math.round(a / 60)}} h ago`;
  const col = a <= 35 ? "#7cb47c" : a <= 75 ? "#d8b34a" : "#c46a6a";
  return `<span style="color:${{col}}" title="observed ${{esc2(c.time || "")}} Eastern">${{txt}}</span>`;
}}
function esc2(s) {{ return String(s == null ? "" : s).replace(/[&<>]/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;"}}[ch])); }}
function trendHtml(c) {{
  if (c.tempF == null || c.prevTempF == null) return "";
  const d = c.tempF - c.prevTempF;
  if (d > 0) return `<span class="trend up" title="was ${{c.prevTempF}}°F at the previous observation">▲${{d}}</span>`;
  if (d < 0) return `<span class="trend dn" title="was ${{c.prevTempF}}°F at the previous observation">▼${{-d}}</span>`;
  return `<span class="trend fl" title="unchanged from the previous observation">→</span>`;
}}
function renderCityTable(dd) {{
  const tbl = document.getElementById("cityTbl");
  if (!tbl) return;
  const cs = ((dd.obs || {{}}).cities || []).slice()
    .sort((a, b) => {{
      const ka = cityVal(a, citySort.k), kb = cityVal(b, citySort.k);
      const cmp = typeof ka === "string"
        ? String(ka).localeCompare(String(kb))
        : (ka === kb ? a.city.localeCompare(b.city) : (ka > kb ? 1 : -1));
      return cmp * citySort.dir;
    }});
  const esc = s => String(s == null ? "" : s).replace(/[&<>]/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;"}}[ch]));
  const cell = (v, suf) => v == null ? "-" : v + (suf || "");
  tbl.innerHTML = `<tr>${{CITY_COLS.map(col => {{
    const act = citySort.k === col.key;
    return `<th class="srt" data-k="${{col.key}}">${{col.label}}`
      + `<span class="dir">${{act ? (citySort.dir > 0 ? "▲" : "▼") : ""}}</span></th>`;
  }}).join("")}}<th>Station</th></tr>`
    + (cs.length ? cs.map(c => `<tr><td><b>${{esc(c.city)}}</b></td>`
        + `<td>${{cell(c.tempF, "°F")}}${{trendHtml(c)}}</td><td>${{cell(c.dewF, "°F")}}</td>`
        + `<td>${{c.windDir || "-"}} ${{cell(c.windMph)}}</td><td>${{cell(c.gustMph)}}</td>`
        + `<td>${{cell(c.rh, "%")}}</td>`
        + `<td>${{ageHtml(c)}}</td>`
        + `<td class="src">${{esc(c.station || "")}} · ${{esc(c.desc || "")}}</td></tr>`).join("")
      : `<tr><td colspan="7" class="src">Observations unavailable.</td></tr>`);
  tbl.querySelectorAll("th.srt").forEach(th => th.onclick = () => {{
    const k = th.dataset.k;
    if (citySort.k === k) citySort.dir *= -1; else citySort = {{k, dir: k === "city" ? 1 : -1}};
    renderCityTable(DATA);
  }});
}}
function tempFill(t) {{
  return t == null ? "#777" : (t >= 85 ? "#ff9f43" : t >= 65 ? "#ffd54f" : t >= 45 ? "#aed581" : "#4da3ff");
}}
function obsTip(s) {{
  const age = s.ageMin == null ? (s.time || "")
    : ` ${{s.ageMin < 1 ? "(just now)" : `(${{s.ageMin}} min ago)`}}`;
  return `<b>${{s.id}}</b>${{s.name && s.name !== s.id ? " " + s.name : ""}}<br/>${{s.tempF == null ? "-" : s.tempF + "°F"}} · dew ${{s.dewF == null ? "-" : s.dewF + "°F"}}<br/>${{s.windDir || ""}} ${{s.windMph == null ? "" : s.windMph + " mph"}} ${{s.desc || ""}} ${{s.time || ""}}${{age}}`;
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
  renderCityTable(DATA);   /* shared with the refresh hook: sortable + arrows */
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
/* soft auto-refresh: rebuild the city board, map markers and stamp from
   each 90 s data pull instead of leaving the build-time snapshot frozen
   on screen ("observations not updating", 2026-09-21). The sounding picker
   still resolves from the build snapshot; RAP hours change hourly, so a
   manual refresh covers it. */
function onDataRefresh(d2) {{
  DATA = d2;
  const st = document.getElementById("obsStamp");
  if (st && d2.generated) st.textContent = d2.generated;
  renderCityTable(d2);   /* re-sorts under the current sort, arrows recompute */
  try {{ drawObs(); }} catch (_e) {{}}   /* markers + hover times re-render */
}}
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
    "archive": 60_000_000,   # storm history gallery (per-advisory cone+summary)
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
            "storms.html": page_storms(d),
            "tropmodels.html": page_tropmodels(d),
            "climate.html": page_climate(d),
            "enso.html": page_enso(d),
            "severe.html": page_severe(d),
            "fieldguide.html": page_fieldguide(d),
            "winter.html": page_winter(d),
            "rivers.html": page_rivers(d),
            "dashboard.html": page_dashboard(d),
            "traffic.html": page_traffic(d),
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


# Priority order for the rotation: each model's FIRST product is its
# flagship (500-vort / mslp / reflectivity class), so "thinnest model
# first" naturally front-loads those. The old hand-maintained _SEED list
# (119 combos at hand-picked hours) went fully stale when the catalog
# switched to two representative hours per combo (2026-09-23) - every
# entry pointed at a non-existent hour and would have rendered nothing.
# The rotation's own sort (missing first, thinnest model first) now does
# the prioritizing; this constant only remains for back-compat imports.
_SEED = []


_SEED_LAST = [0.0]      # last seed attempt (monotonic-ish wall clock)
_SEED_STALE_S = 6 * 3600   # re-render a seed combo only after this age
_ROT_RUNNING = [False]  # a rotation pass is in progress - never stack more
_ROT_POS = [0]          # rotation cursor across the FULL combo space
_ROT_BATCH = 48         # combos per pass (round-robin, see below); keeps a
                        # pass under ~45 min even when it lands on heavy models
_ROT_GATE = 900.0       # min seconds between passes; passes then run
                        # back-to-back (~100-120 renders/h, full catalog
                        # swept in under a day - inside the 21 h retention)


def _all_model_combos():
    """Every (model, fh, product, region) the models page can offer.

    The catalog is the product x model x region space at TWO representative
    hours per combo: an early frame (~1/8 into the model's range, near-term
    weather) and its final hour (end of the forecast). The original design
    kept all <=7 loop hours per combo, which quietly exploded as walls were
    added (666 triples x 7 = 4,662 combos): the 30-min rotation could sweep
    that in ~65 h while the docs pruner deletes frames past 21 h, so the
    pruner won and the collage walls drained to 1-2 models per product
    (2026-09-23). Two hours per combo = 1,332 combos, a full sweep in well
    under a day, and ~350 MB of tiles - inside the docs disk cap. Full
    hour-by-hour loops still exist for whatever the rotation has on disk
    (the explorer's render index) and any requested hour renders on demand
    in the live app.
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
        # early = the FIRST loop hour (F001 for the 1-hour CAMs, F003 for
        # the 3-hour globals, F006 for the AI/ensemble sets): the near-term
        # wall then clusters on three shared grids so the collage's
        # same-hour default finds real cross-model coverage.
        early = hours[0]
        pick = (early, hours[-1]) if early != hours[-1] else (hours[-1],)
        for prod in prods:
            for region in ("etn", "us"):
                for fh in pick:
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
    walks the full combo space (every model x product x region at the
    catalog's representative hours), rendering a batch each pass:
    priority _SEED combos first WHEN STALE, then missing/stale combos
    before fresh ones (os.path.getmtime of the combo's newest frame).
    _SEED used to re-render all 130 seed combos every pass - after the
    catalog grew, that consumed ~130 of ~166 pass slots re-churning the
    same tiles while the rest of the catalog starved (2026-09-23). Seeds
    now only render when their newest frame is over _SEED_STALE_S old.
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

            rot = _all_model_combos()
            # ONE directory scan per pass: _age used to re-list MAP_DIR
            # (~1k PNGs) for every combo in the batch - 60+ listdirs of
            # stat churn per pass, on top of the renders (2026-09-19)
            import re as _re
            _mre = _re.compile(r"^(.+)_(f\d{3})_(\d{10})_([a-z0-9]+)\.png$")
            counts = {}
            newest_by_key = {}      # (model_prod, region) -> newest mtime
                                    # across ALL cycles: _age is staleness-
                                    # based, not current-cycle-based
            for fn in os.listdir(MAP_DIR):
                counts[fn.split("_", 1)[0]] = counts.get(fn.split("_", 1)[0], 0) + 1
                _m = _mre.match(fn)
                if not _m:
                    continue
                try:
                    _mt = _op.getmtime(_op.join(MAP_DIR, fn))
                except OSError:
                    continue
                _key = (_m.group(1), _m.group(4))
                if _mt > newest_by_key.get(_key, 0.0):
                    newest_by_key[_key] = _mt

            def _age(combo):
                # STALENESS semantics: age of the combo's newest frame from
                # ANY cycle. Keying on the current cycle (the old code) re-
                # zeroed a model's whole catalog slice at every rollover -
                # the hourly CAMs alone re-flooded the queue with "missing"
                # work faster than the rotation could render, the tree never
                # grew past ~370 tiles, and most products sat under the
                # page's 4-model visibility bar (2026-09-23). A 5 h old tile
                # from the previous cycle still shows the same pattern;
                # refresh tiles when they AGE, not when cycles tick.
                model, fh, prod, region = combo
                cyc = _cyc(model)
                if cyc is None:
                    # unfindable cycle -> sort to the BACK, not the front.
                    # Returning 0.0 here let a single stalled model (NAM,
                    # 68 combos) occupy the whole 60-slot pass every pass
                    # while 1,200+ renderable combos queued behind it
                    # ("0 rendered" passes, 2026-09-23).
                    return _t.time() + 1e6
                return newest_by_key.get((f"{model}_{prod}", region), 0.0)

            # _SEED is a priority list, not a per-pass chore: include a seed
            # combo only when it is missing (_age 0) or its newest frame for
            # the CURRENT cycle is over _SEED_STALE_S old. _age returns an
            # mtime, so "stale" = mtime before now-minus-window. Seeds whose
            # (model, hour, product, region) left the catalog are dropped -
            # the catalog itself now guarantees full product x model coverage.
            _rotset = set(rot)
            batch = [c for c in _SEED
                     if c in _rotset and _age(c) < _t.time() - _SEED_STALE_S]
            if rot:
                # rendered-map count per model: thin models must fill first so
                # every model visibly gains maps each pass (fair share)
                # missing combos (age 0) first; within them, thinnest models
                # first - otherwise the stable sort walks catalog order and a
                # late-catalog model (HREF) waits many passes (2026-09-14)
                rot.sort(key=lambda c: (_age(c), counts.get(c[0], 0)))
                # take the FRONT of the sorted list, but ROUND-ROBIN across
                # models: depth-first let one heavy model (SREF = 21-member
                # fetches, AI sets = NetCDF) own an entire pass for hours
                # while light models sat idle. Interleaving means every
                # model visibly gains tiles each pass - the wall fills in
                # breadth before depth (2026-09-23).
                _front = {}
                for c in rot[:_ROT_BATCH * 3]:
                    _front.setdefault(c[0], []).append(c)
                _order = list(_front)          # already thinnest-first
                batch = []
                while len(batch) < _ROT_BATCH and any(_front.values()):
                    for _m in _order:
                        if _front.get(_m) and len(batch) < _ROT_BATCH:
                            batch.append(_front[_m].pop(0))

            ok = fail = 0
            skipped = set()
            failed_models = set()
            # SERIAL renders on purpose: matplotlib's Agg backend is not
            # thread-safe - a 3-worker ThreadPool wedged at ~84 min with 3
            # tiles in the last quarter hour and never completed a pass
            # (2026-09-23). The netcdf/GRIB fetches would overlap nicely,
            # but the render step corrupts. Steady-state demand with the
            # staleness rotation (~64 renders/h) fits inside serial speed.
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

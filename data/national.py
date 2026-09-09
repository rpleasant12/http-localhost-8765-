"""National products: SPC outlooks (day 1-3), NHC active storms, WPC charts.

All keyless public NOAA feeds:
  SPC  - products/outlook GeoJSON (polygons for the map + point lookup)
  NHC  - CurrentStorms.json + the official GIS cone zip per storm
  WPC  - operational forecast graphics (QPF, snow, winter probabilities,
         days 3-7 fronts), scraped stable paths
"""
import datetime as dt
import io
import json
import os
import re
import time
import zipfile

import requests

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
NHC_STORMS = "https://www.nhc.noaa.gov/CurrentStorms.json"
# ---------------------------------------------------------------- NHC


def _kml_coords(text):
    """KML coordinate string -> [[lon, lat], ...]"""
    pts = []
    for trip in (text or '').split():
        parts = trip.split(',')[:2]
        if len(parts) == 2:
            try:
                pts.append([float(parts[0]), float(parts[1])])
            except ValueError:
                continue
    return pts


def _parse_cone_kmz(kmz_bytes):
    """NHC cone KMZ -> GeoJSON-style features (cone polygon, track, points).

    NHC ships both KML namespaces (2.1 Google and 2.2 OGC), so the ns is
    taken from the root tag rather than hardcoded.
    """
    import xml.etree.ElementTree as ET

    feats = []
    try:
        z = zipfile.ZipFile(io.BytesIO(kmz_bytes))
        kml_name = next(n for n in z.namelist() if n.endswith(".kml"))
        root = ET.fromstring(z.read(kml_name))
    except (zipfile.BadZipFile, StopIteration, ET.ParseError):
        return feats
    ns = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    q = lambda tag: f"{{{ns}}}{tag}" if ns else tag  # noqa: E731
    for pm in root.iter(q("Placemark")):
        name = pm.findtext(f".//{q('name')}") or ""
        desc = pm.findtext(f".//{q('description')}") or ""
        geometry = None
        poly = pm.find(f".//{q('Polygon')}")
        line = pm.find(f".//{q('LineString')}")
        point = pm.find(f".//{q('Point')}")
        if poly is not None:
            cs = poly.findtext(f".//{q('coordinates')}") or ""
            pts = _kml_coords(cs)
            if len(pts) > 2:
                geometry = {"type": "Polygon", "coordinates": [pts]}
        elif line is not None:
            pts = _kml_coords(line.findtext(f".//{q('coordinates')}") or "")
            if len(pts) > 1:
                geometry = {"type": "LineString", "coordinates": pts}
        elif point is not None:
            pts = _kml_coords(point.findtext(f".//{q('coordinates')}") or "")
            if pts:
                geometry = {"type": "Point", "coordinates": pts[0]}
        if geometry:
            plain = re.sub(r"<[^>]+>", " ", desc)
            plain = re.sub(r"\s+", " ", plain).strip()
            feats.append({"name": name, "desc": plain[:220], "geometry": geometry})
    return feats


def nhc_storms():
    """Active storms with metadata + official cone/track geometry (KMZ)."""
    try:
        r = requests.get(NHC_STORMS, headers=UA, timeout=20)
        r.raise_for_status()
        active = r.json().get("activeStorms") or []
    except (requests.RequestException, ValueError):
        return []
    storms = []
    for s in active:
        st = {
            "id": s.get("id"), "name": s.get("name"),
            "classification": s.get("classification"), "intensity": s.get("intensity"),
            "pressure": s.get("pressure"),
            "lat": s.get("latitudeNumeric"), "lon": s.get("longitudeNumeric"),
            "movement": f'{s.get("movementSpeed", "")} kt @ {s.get("movementDir", "")} deg',
            "lastUpdate": s.get("lastUpdate", "")[:16].replace("T", " ") + "Z",
            "advisoryUrl": (s.get("publicAdvisory") or {}).get("url"),
            "forecastGraphicsUrl": (s.get("forecastGraphics") or {}).get("url"),
            "windKmz": ((s.get("initialWindExtent") or {}).get("kmzFile")
                        or (s.get("forecastWindRadiiGIS") or {}).get("kmzFile")),
            "features": [],
        }
        # cone polygon + official forecast track/points come in two KMZs
        feats = []
        for key in ("trackCone", "forecastTrack"):
            kmz = (s.get(key) or {}).get("kmzFile")
            if not kmz:
                continue
            url = kmz if kmz.startswith("http") else \
                f"https://www.nhc.noaa.gov/gis/forecast/archive/{kmz}"
            try:
                kr = requests.get(url, headers=UA, timeout=30)
                if kr.status_code == 200:
                    feats.extend(_parse_cone_kmz(kr.content))
            except requests.RequestException:
                continue
        st["features"] = feats
        storms.append(st)
    return storms


# ---------------------------------------------------------------- WPC

def _kml_color_to_hex(abgr):
    """KML AABBGGRR -> #RRGGBB (alpha folded to a hex suffix for CSS 8-digit)."""
    try:
        a = int(abgr[0:2], 16)
        b, g, r = abgr[2:4], abgr[4:6], abgr[6:8]
        return f"#{r}{g}{b}{a:02x}"
    except (ValueError, IndexError):
        return "#4488cc"


def _parse_kml_features(xml_bytes, keep_styles=True):
    """KML/KMZ bytes -> [{'name','fill','color','geometry'}] (Polygon/LineString/Point).

    Namespace-agnostic; resolves styleUrl -> PolyStyle/LineStyle colors when
    `keep_styles` (WPC/QPF ship official category colors in the styles).
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    ns = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    q = lambda t: f"{{{ns}}}{t}" if ns else t  # noqa: E731

    styles = {}
    if keep_styles:
        for st in root.iter(q("Style")):
            sid = st.get("id")
            if not sid:
                continue
            poly = st.find(f".//{q('PolyStyle')}/{q('color')}")
            line = st.find(f".//{q('LineStyle')}/{q('color')}")
            styles["#" + sid] = {
                "poly": _kml_color_to_hex(poly.text) if poly is not None and poly.text else None,
                "line": _kml_color_to_hex(line.text) if line is not None and line.text else None,
            }

    feats = []
    for pm in root.iter(q("Placemark")):
        name = (pm.findtext(f".//{q('name')}") or "").strip()
        style_url = pm.findtext(f".//{q('styleUrl')}") or ""
        st = styles.get(style_url, {})
        geometry = None
        geom_el = pm.find(f".//{q('Polygon')}")
        kind = "poly"
        if geom_el is None:
            geom_el = pm.find(f".//{q('LineString')}")
            kind = "line"
        if geom_el is None:
            geom_el = pm.find(f".//{q('Point')}")
            kind = "point"
        if geom_el is None:
            continue
        cs = geom_el.findtext(f".//{q('coordinates')}") or ""
        pts = _kml_coords(cs)
        if kind == "poly" and len(pts) > 2:
            geometry = {"type": "Polygon", "coordinates": [pts]}
        elif kind == "line" and len(pts) > 1:
            geometry = {"type": "LineString", "coordinates": pts}
        elif kind == "point" and pts:
            geometry = {"type": "Point", "coordinates": pts[0]}
        if geometry:
            feats.append({
                "name": name,
                "fill": st.get("poly"),
                "color": st.get("line"),
                "geometry": geometry,
            })
    return feats


def wpc_sigwx():
    """WPC Day 1-3 National Significant Weather hazards for the interactive map.

    The KML ships GroundOverlay images (no vector placemarks), so the three
    day-overlays are returned as ({'name','href','bounds'}): the component can
    draw the official hazard graphics as image overlays.
    """
    import xml.etree.ElementTree as ET

    try:
        r = requests.get(SIGWX_URL, headers=UA, timeout=25)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError):
        return []
    ns = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    q = lambda t: f"{{{ns}}}{t}" if ns else t  # noqa: E731
    overlays = []
    base = SIGWX_URL.rsplit("/", 1)[0]
    for go in root.iter(q("GroundOverlay")):
        name = (go.findtext(f".//{q('name')}") or "WPC hazard").strip()
        href = go.findtext(f".//{q('Icon')}/{q('href')}") or ""
        if not href:
            continue
        try:
            north = float(go.findtext(f".//{q('LatLonBox')}/{q('north')}"))
            south = float(go.findtext(f".//{q('LatLonBox')}/{q('south')}"))
            east = float(go.findtext(f".//{q('LatLonBox')}/{q('east')}"))
            west = float(go.findtext(f".//{q('LatLonBox')}/{q('west')}"))
        except (TypeError, ValueError):
            continue
        url = href if href.startswith("http") else f"{base}/{href.lstrip('./')}"
        overlays.append({"name": name, "href": url, "bounds": [south, west, north, east]})
    return overlays


def wpc_qpf(day=1):
    """WPC 24-h QPF contour polygons for the interactive map (day 1-3)."""
    day = min(max(int(day), 1), 3)
    url = QPF_URL.format(day=day)
    try:
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code != 200:
            return {"features": [], "issued": ""}
        z = zipfile.ZipFile(io.BytesIO(r.content))
        kml_name = next(n for n in z.namelist() if n.endswith(".kml"))
        xml_bytes = z.read(kml_name)
        snippet = ""
    except (requests.RequestException, zipfile.BadZipFile, StopIteration):
        return {"features": [], "issued": ""}
    try:
        import xml.etree.ElementTree as ET

        root0 = ET.fromstring(xml_bytes)
        ns0 = root0.tag.split("}")[0].strip("{") if "}" in root0.tag else ""
        sn = root0.findtext(f".//{{{ns0}}}Snippet") if ns0 else root0.findtext(".//Snippet")
        snippet = " ".join((sn or "").split())
    except ET.ParseError:
        snippet = ""
    feats = [f for f in _parse_kml_features(xml_bytes) if f["geometry"]["type"] == "Polygon"]
    return {"features": feats, "issued": snippet}


def wpc_catalog():
    """Current WPC operational graphics (stable NOAA paths)."""
    return [
        {"key": "qpf_d1", "title": "QPF - Day 1 precipitation",
         "desc": "WPC quantitative precipitation forecast for the next 24 h.",
         "url": "https://www.wpc.ncep.noaa.gov/qpf/94ewbg.gif"},
        {"key": "qpf_d1_fill", "title": "QPF - Day 1 (filled)",
         "desc": "Filled day-1 precipitation amounts.",
         "url": "https://www.wpc.ncep.noaa.gov/qpf/fill_94qwbg.gif"},
        {"key": "snow_d1", "title": "Winter Storm - Day 1 snow",
         "desc": "Day-1 snowfall > 4 inches probabilities / outlines.",
         "url": "https://www.wpc.ncep.noaa.gov/wwd/day1_psnow_gt_04.gif"},
        {"key": "snow_d2", "title": "Winter Storm - Day 2 snow",
         "desc": "Day-2 snowfall > 4 inches probabilities / outlines.",
         "url": "https://www.wpc.ncep.noaa.gov/wwd/day2_psnow_gt_04.gif"},
        {"key": "winter_d4", "title": "Winter probabilities - Day 4",
         "desc": "Day 4 probability of exceeding winter-storm criteria (CONUS).",
         "url": "https://www.wpc.ncep.noaa.gov/wwd/pwpf_d47/gif/prbww_sn25_DAY4_conus.gif"},
        {"key": "winter_d5", "title": "Winter probabilities - Day 5",
         "desc": "Day 5 probability of exceeding winter-storm criteria (CONUS).",
         "url": "https://www.wpc.ncep.noaa.gov/wwd/pwpf_d47/gif/prbww_sn25_DAY5_conus.gif"},
        {"key": "medr_d3", "title": "Days 3-4 fronts & pressure",
         "desc": "WPC medium-range surface forecast, days 3-4.",
         "url": "https://www.wpc.ncep.noaa.gov/medr/9jhwbg_conus_sm.jpg"},
        {"key": "medr_d4", "title": "Days 4-5 fronts & pressure",
         "desc": "WPC medium-range surface forecast, days 4-5.",
         "url": "https://www.wpc.ncep.noaa.gov/medr/9khwbg_conus_sm.jpg"},
        {"key": "medr_d5", "title": "Days 5-6 fronts & pressure",
         "desc": "WPC medium-range surface forecast, days 5-6.",
         "url": "https://www.wpc.ncep.noaa.gov/medr/9lhwbg_conus_sm.jpg"},
    ]


# ---------------------------------------------------------------- US warnings
WWA_URL = ("https://mapservices.weather.noaa.gov/eventdriven/rest/services/"
           "WWA/watch_warn_adv/MapServer/1/query")

# Marine product families are dropped: they dominate the feed (hundreds of
# Small Craft Advisories / Gale records) and are meaningless on a CONUS map.
_MARINE_TYPES = ("Small Craft", "Gale", "Marine", "Rip Current", "High Surf",
                 "Beach Hazards", "Hazardous Seas", "Coastal Flood", "Brisk Wind",
                 "Small Craft", "Lake Effect Snow")
_WWA_WHERE = " AND ".join(
    f"prod_type NOT LIKE '%{t}%'" for t in sorted(set(_MARINE_TYPES))
)

_WWA_CACHE = {"t": 0.0, "features": None}
_TTL = 180  # 3 min - NWS spatial refreshes ~every minute

# NWS standard fill colors for the common products (severe first)
ALERT_COLORS = {
    "Tornado Warning": "#ff0000",
    "Severe Thunderstorm Warning": "#ffa500",
    "Flash Flood Warning": "#8b0000",
    "Flash Flood Watch": "#2e8b57",
    "Flood Warning": "#00ff00",
    "Flood Advisory": "#00ff7f",
    "Flood Watch": "#2e8b57",
    "Tornado Watch": "#ffff00",
    "Severe Thunderstorm Watch": "#db7093",
    "Special Marine Warning": "#ffa500",
    "Extreme Wind Warning": "#ff8c00",
    "Special Weather Statement": "#ffe4b5",
    "Winter Storm Warning": "#ff69b4",
    "Winter Weather Advisory": "#7b68ee",
    "Ice Storm Warning": "#8b008b",
    "High Wind Warning": "#daa520",
    "Wind Advisory": "#d2b48c",
    "Dense Fog Advisory": "#708090",
    "Heat Advisory": "#ff7f50",
    "Excessive Heat Warning": "#c71585",
    "Red Flag Warning": "#ff1493",
    "Freeze Warning": "#483d8b",
    "Frost Advisory": "#6495ed",
    "Air Quality Alert": "#808080",
}

# priority for sorting: tornado > severe > flash flood > other warnings > watches
def alert_rank(event):
    e = (event or "").lower()
    if "tornado warning" in e:
        return 0
    if "severe thunderstorm warning" in e or "extreme wind" in e:
        return 1
    if "flash flood warning" in e:
        return 2
    if e.endswith("warning") or "warning" in e:
        return 3
    if "watch" in e:
        return 5
    return 6


def _alert_index():
    """cap-urn -> alert properties from api.weather.gov's active index.

    The WWA spatial layer carries geometry but no area description, headline
    or severity; the api.weather.gov index has those. Joining on the CAP urn
    (WWA's cap_id = api.weather.gov id minus its prefix) gives real county
    names, tornado-detection tags and headlines - best effort, {} on failure.
    """
    idx = {}
    base = "https://api.weather.gov/alerts/active"
    # the default page caps ~400 and pagination headers are gone; the severe
    # events that matter are fetched explicitly so their county details land
    urls = [base] + [f"{base}?event={ev.replace(' ', '+')}" for ev in (
        "Tornado Warning", "Severe Thunderstorm Warning", "Flash Flood Warning",
        "Tornado Watch", "Severe Thunderstorm Watch", "Flash Flood Watch",
        "Extreme Wind Warning", "Special Marine Warning")]
    try:
        for url in urls:
            try:
                r = requests.get(url, headers={**UA, "Accept": "application/geo+json"}, timeout=60)
                r.raise_for_status()
                d = r.json()
            except (requests.RequestException, ValueError):
                continue
            for f in d.get("features", []):
                p = f.get("properties") or {}
                aid = p.get("id") or ""
                urn = aid.rsplit("/alerts/", 1)[-1]
                if urn:
                    idx[urn] = p
    except Exception:  # noqa: BLE001 - best-effort enrichment only
        pass
    return idx


def alert_color(event):
    """Official-ish NWS fill color for an event name."""
    if (event or "") in ALERT_COLORS:
        return ALERT_COLORS[event]
    e = (event or "").lower()
    if "tornado" in e:
        return "#ff0000"
    if "severe thunderstorm" in e:
        return "#ffa500"
    if "flash flood" in e:
        return "#8b0000"
    if "flood" in e:
        return "#00ff00"
    if e.endswith("warning"):
        return "#ff9f43"
    if "watch" in e:
        return "#ffd54f"
    return "#c0c0c0"  # advisory / other


def us_warnings():
    """All active US watches/warnings/advisories as GeoJSON features (no key).

    Official NWS Spatial (mapservices.weather.noaa.gov WWA/watch_warn_adv
    layer 1) with server-side simplification (maxAllowableOffset 2000, ~
    400 KB) so ~900 polygons render smoothly. Marine products are filtered
    out. Enriched from api.weather.gov's active index (joined on CAP urn):
    real areaDesc (counties), headline, severity, tornadoDetection.
    Features carry: event, code, kind, color, severity, areaDesc, headline,
    tor (radar/observed tag or ''), expires, url, geometry.
    Sorted worst-first. [] when both services are unreachable.
    """
    now = time.time()
    if _WWA_CACHE["features"] is not None and now - _WWA_CACHE["t"] < _TTL:
        return _WWA_CACHE["features"]

    params = {
        "f": "geojson",
        "where": _WWA_WHERE,
        "outFields": "prod_type,phenom,sig,url,expiration,onset,wfo,cap_id,event",
        "maxAllowableOffset": 2000,
    }
    feats = []
    try:
        r = requests.get(WWA_URL, params=params, headers=UA, timeout=90)
        r.raise_for_status()
        idx = _alert_index()
        for f in r.json().get("features", []):
            p = f.get("properties") or {}
            prod = p.get("prod_type") or "Alert"
            urn = (p.get("cap_id") or "").rsplit("/alerts/", 1)[-1]
            meta = idx.get(urn) or {}
            params_a = meta.get("parameters") or {}
            feats.append({
                "event": meta.get("event") or prod,
                "code": (p.get("phenom") or "").upper() + ("W" if prod.endswith("Warning") else "A" if prod.endswith("Advisory") else "Y"),
                "kind": ("warning" if prod.endswith("Warning") else
                         "watch" if prod.endswith("Watch") else "advisory"),
                "color": alert_color(meta.get("event") or prod),
                "severity": meta.get("severity") or ("Extreme" if "tornado" in prod.lower() else "Severe" if prod.endswith("Warning") else "Minor"),
                "areaDesc": meta.get("areaDesc") or (f"WFO {p.get('wfo') or '?'}" if (p.get("wfo") or "").strip() else ""),
                "headline": (meta.get("headline") or ""),
                "tor": (params_a.get("tornadoDetection") or [""])[0] if isinstance(params_a.get("tornadoDetection"), list) else (params_a.get("tornadoDetection") or ""),
                "expires": (p.get("expiration") or "")[:16].replace("T", " "),
                "url": p.get("url") or meta.get("@id") or "",
                "geometry": f.get("geometry"),
            })
        feats.sort(key=lambda x: alert_rank(x.get("event")))
    except (requests.RequestException, ValueError):
        feats = []
    _WWA_CACHE["t"] = now
    _WWA_CACHE["features"] = feats
    return feats


# ---------------------------------------------------------------- Upper air
OBSWX_URL = "https://www.spc.noaa.gov/obswx/maps/"

UA_LEVELS = {
    "sfc": "Surface analysis",
    "925": "925 mb - low levels",
    "850": "850 mb - ridges / LLJ",
    "700": "700 mb - moisture / upslope",
    "500": "500 mb - vorticity / shortwaves",
    "300": "300 mb - jet stream",
    "250": "250 mb - jet stream (deep)",
}

_OBSWX_CACHE = {"t": 0.0, "maps": None}


def upper_air_maps():
    """SPC observed upper-air analyses (00Z/12Z), latest first.

    Scrapes spc.noaa.gov/obswx/maps/ for the available level/time GIFs.
    Returns [{"level","levelLabel","time","url","title"}] sorted newest first,
    [] when the page is unreachable. No key, plain NOAA graphics.
    """
    now = time.time()
    if _OBSWX_CACHE["maps"] is not None and now - _OBSWX_CACHE["t"] < 900:
        return _OBSWX_CACHE["maps"]

    maps = []
    try:
        r = requests.get(OBSWX_URL, headers=UA, timeout=25)
        r.raise_for_status()
        seen = set()
        for level, ymd, hh in re.findall(
                r"/obswx/maps/(\w+)_(\d{6})_(\d{2})\.gif", r.text):
            key = (level, ymd, hh)
            if key in seen:
                continue
            seen.add(key)
            valid = dt.datetime.strptime("20" + ymd + hh, "%Y%m%d%H")
            maps.append({
                "level": level,
                "levelLabel": UA_LEVELS.get(level, level + " mb"),
                "time": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "url": f"{OBSWX_URL}{level}_{ymd}_{hh}.gif",
                "title": (f"SPC {UA_LEVELS.get(level, level)} analysis - "
                          f"{valid:%a %H:%M}Z"),
            })
    except (requests.RequestException, ValueError):
        maps = []
    maps.sort(key=lambda m: (m["time"], list(UA_LEVELS).index(m["level"])
                             if m["level"] in UA_LEVELS else 99), reverse=True)
    _OBSWX_CACHE["t"] = now
    _OBSWX_CACHE["maps"] = maps
    return maps

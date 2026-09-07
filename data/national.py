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
import zipfile

import requests

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
SPC_BASE = "https://www.spc.noaa.gov/products/outlook"
NHC_STORMS = "https://www.nhc.noaa.gov/CurrentStorms.json"
SIGWX_URL = "https://www.wpc.ncep.noaa.gov/kml/noaa_chart/WPC_Day1-3_SigWx_latest.kml"
QPF_URL = "https://www.wpc.ncep.noaa.gov/kml/qpf/QPF24hr_Day{day}_latest.kmz"

# ---------------------------------------------------------------- SPC

def spc_outlooks():
    """Day1/Day2/Day3 categorical outlook GeoJSONs -> {'day1': {...}, ...}.

    Each value: {'features': [ {'label','fill','geometry'} ], 'issue': str}
    """
    out = {}
    for day, prod in (("day1", "day1otlk_cat"), ("day2", "day2otlk_cat"), ("day3", "day3otlk_cat")):
        try:
            r = requests.get(f"{SPC_BASE}/{prod}.nolyr.geojson", headers=UA, timeout=15)
            if r.status_code != 200:
                continue
            gj = r.json()
            feats = []
            issue = ""
            for f in gj.get("features", []):
                props = f.get("properties", {}) or {}
                label = str(props.get("LABEL") or props.get("LABEL2") or "").upper()
                fill = props.get("fill")
                if f.get("geometry"):
                    feats.append({"label": label, "fill": fill, "geometry": f["geometry"]})
                if not issue:
                    issue = str(props.get("ISSUE") or "")
            out[day] = {"features": feats, "issue": issue}
        except (requests.RequestException, ValueError):
            continue
    return out


RISK_ORDER = ["TSTM", "MRGL", "SLGT", "ENH", "MDT", "HIGH"]
RISK_COLORS = {"TSTM": "#c1e9c1", "MRGL": "#66a366", "SLGT": "#ffe066", "ENH": "#e69138",
               "MDT": "#ff4747", "HIGH": "#cc00ff"}


def spc_risk_at(outlooks, lat, lon, day="day1"):
    """Categorical risk at a point from the outlook polygons ('' if none)."""
    data = (outlooks or {}).get(day)
    if not data:
        return ""
    try:
        from shapely.geometry import Point, shape

        pt = Point(lon, lat)
        best = ""
        for f in data["features"]:
            try:
                if shape(f["geometry"]).contains(pt):
                    lab = f["label"]
                    if lab in RISK_ORDER and (not best or
                                              RISK_ORDER.index(lab) > RISK_ORDER.index(best)):
                        best = lab
            except Exception:  # noqa: BLE001
                continue
        return best
    except ImportError:
        return ""


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

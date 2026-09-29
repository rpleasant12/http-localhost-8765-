"""Winter 2026-27 snowfall-chance map: derived from CPC seasonal shapefiles.

CPC publishes the long-lead outlooks as monthly shapefile zips - probability
contour polygons with fields Fcst_Date/Valid_Seas/Prob/Cat (cats Above /
Below / EC, probs 33/40/50/60/70, nested contours). This module turns them
into the winter page's graphic:

1. TILT LAYERS per winter window (NDJ / DJF / JFM): warm (temp-Above),
   cold (temp-Below) and wet (prcp-Above) contours as GeoJSON - the actual
   CPC forecast, not an interpretation.
2. SNOW-CHANCE features: where a cold tilt and a wet tilt overlap, the
   intersection is valued as chance = coldProb + wetProb - 33 (the
   both-ingredients probability, capped 85%) and classified on the site's
   snow ramp. In warm-tilt months this layer is legitimately EMPTY - the
   page says so out loud instead of inventing snow.
3. SCORECARDS: point-in-polygon samples at US / Tennessee / East-TN anchor
   points -> "warm 50-60% / wet near-normal" one-liners per window.
4. IMPACTS: an effects ladder (mostly rain -> frequent winter-storm
   impacts) driven by the same numbers.

Zips re-fetch at most daily; the derived bundle is cached 6 h. All
free/keyless: ftp.cpc.ncep.noaa.gov. Parsing: pyshp + shapely.
"""
import datetime as dt
import io
import json
import os
import threading
import time
import zipfile

import requests

OUT_DIR = "static/season"
BASE = "https://ftp.cpc.ncep.noaa.gov/GIS/us_tempprcpfcst"
UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}

WINDOWS = (
    ("lead2", "NDJ", "Nov-Dec-Jan"),
    ("lead3", "DJF", "Dec-Jan-Feb"),
    ("lead4", "JFM", "Jan-Feb-Mar"),
)

TILT_STYLE = {
    # (layer) -> {fill, word}: warm browns, cold blues, wet greens
    "warm":  {"fill": "#c96a3f", "word": " warmer than normal"},
    "cold":  {"fill": "#4f9be8", "word": " colder than normal"},
    "wet":   {"fill": "#3fae6a", "word": " wetter than normal"},
    "dry":   {"fill": "#d7b45a", "word": " drier than normal"},
}

# scorecard anchors: (label, lat, lon)
ANCHORS = (
    ("US Northeast", 42.5, -73.0),
    ("US Midwest", 41.0, -90.0),
    ("Tennessee", 35.85, -86.35),
    ("East Tennessee", 36.16, -82.83),
)

_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()
_TTL = 6 * 3600
_CACHE_FILE = os.path.join(OUT_DIR, "bundle.json")


# ------------------------------------------------------------ fetch + parse
def _fetch_zip(kind, ym):
    fn = f"{OUT_DIR}/{kind}_{ym}.zip"
    try:
        fresh = os.path.getsize(fn) > 1_000_000 and \
            time.time() - os.stat(fn).st_mtime < 86_400
    except OSError:
        fresh = False
    if not fresh:
        r = requests.get(f"{BASE}/{kind}_{ym}.zip", headers=UA, timeout=180)
        if not r.ok or len(r.content) < 1_000_000:
            return None
        with open(fn + ".part", "wb") as f:
            f.write(r.content)
        os.replace(fn + ".part", fn)
    return fn


def _contours(zf, lead, seas, var):
    """All non-EC probability contours of one shapefile.

    Returns {cat: [(prob, [ring, ...])]} with rings as [(lon, lat), ...].
    """
    import shapefile  # pyshp

    stem = f"{lead}_{seas}_{var}"
    try:
        r = shapefile.Reader(shp=io.BytesIO(zf.read(stem + ".shp")),
                             dbf=io.BytesIO(zf.read(stem + ".dbf")))
    except KeyError:
        return {}
    flds = [f[0] for f in r.fields[1:]]
    icat, iprob = flds.index("Cat"), flds.index("Prob")
    out = {}
    for i in range(r.numRecords):
        rec = r.record(i)
        cat, prob = str(rec[icat]), rec[iprob]
        if cat == "EC" or cat == "Normal" or prob is None or \
                float(prob) <= 33.0:
            continue
        shp = r.shape(i)
        pts = shp.points
        parts = list(shp.parts) + [len(pts)]
        rings = []
        for a, b in zip(parts[:-1], parts[1:]):
            ring = pts[a:b]
            if len(ring) >= 40:                    # drop specks
                rings.append([(round(x, 2), round(y, 2)) for x, y in ring])
        if rings:
            out.setdefault(cat, []).append((float(prob), rings))
    return out


def _issued(zf, lead, seas):
    try:
        import shapefile  # pyshp

        stem = f"{lead}_{seas}_temp"
        r = shapefile.Reader(shp=io.BytesIO(zf.read(stem + ".shp")),
                             dbf=io.BytesIO(zf.read(stem + ".dbf")))
        flds = [f[0] for f in r.fields[1:]]
        return str(r.record(0)[flds.index("Fcst_Date")])
    except Exception:                              # noqa: BLE001
        return None


# ------------------------------------------------------------ geometry
def _to_geom(contour_list):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    pairs = []
    for prob, rings in contour_list:
        try:
            g = unary_union([Polygon(r).buffer(0) for r in rings])
        except Exception:                          # noqa: BLE001
            continue
        if g is not None and not g.is_empty:
            pairs.append((prob, g))
    return pairs


def _area(g):
    import math

    try:
        return g.area * math.cos(math.radians(
            max(-80.0, min(80.0, g.centroid.y))))
    except Exception:                              # noqa: BLE001
        return 0.0


def _tilt_features(pairs, layer):
    """[(prob, geom)] -> GeoJSON features (simplified, payload-capped)."""
    from shapely.geometry import mapping

    feats = []
    for prob, g in sorted(pairs, key=lambda t: -t[0]):
        gg = g.simplify(0.08, preserve_topology=True)
        if gg.is_empty or _area(gg) < 1.0:
            continue
        st = TILT_STYLE[layer]
        feats.append({"type": "Feature",
                      "properties": {"prob": prob, "layer": layer,
                                     "fill": st["fill"],
                                     "word": st["word"]},
                      "geometry": mapping(gg)})
        if len(feats) >= 24:
            break
    return feats


def _snow_chances(cold_pairs, wet_pairs):
    """cold x wet intersections -> snow-chance features (may be empty)."""
    from shapely.geometry import MultiPolygon, Polygon, mapping

    feats = []
    for cprob, cg in sorted(cold_pairs, key=lambda t: -t[0]):
        for wprob, wg in sorted(wet_pairs, key=lambda t: -t[0]):
            try:
                inter = cg.intersection(wg)
            except Exception:                      # noqa: BLE001
                continue
            if inter.is_empty:
                continue
            polys = list(inter.geoms) if hasattr(inter, "geoms") \
                else [inter]
            chance = round(min(85.0, cprob + wprob - 33.0))
            for g in polys:
                if not isinstance(g, Polygon) or g.is_empty:
                    continue
                gg = g.simplify(0.08, preserve_topology=True)
                if gg.is_empty or _area(gg) < 2.0:
                    continue
                feats.append({"type": "Feature",
                              "properties": {
                                  "chance": chance,
                                  "coldPct": cprob, "wetPct": wprob,
                                  "fill": _chance_fill(chance)},
                              "geometry": mapping(gg)})
                if len(feats) >= 30:
                    return feats
    return feats


def _chance_fill(ch):
    if ch >= 60:
        return "#7b3fc9"
    if ch >= 50:
        return "#4f6ed8"
    if ch >= 42:
        return "#2f9be8"
    return "#7fc4f0"


def _impact(ch):
    if ch >= 60:
        return ("frequent", "Repeated winter-storm impacts: school "
                            "delays, treatment priorities, outage risk")
    if ch >= 50:
        return ("occasional", "Several plowable events; occasional "
                              "travel disruptions during storms")
    if ch >= 42:
        return ("episode", "A few wintry episodes; slick bridges and "
                           "pre-treat windows")
    return ("light", "Mostly rain or mix; standard cold-season caution")


def _score(points_by_layer, lat, lon):
    """Highest-prob tilt at a point -> {warm, cold, wet, dry} percents."""
    from shapely.geometry import Point

    pt = Point(lon, lat)
    out = {}
    for layer, plist in points_by_layer.items():
        best = 0.0
        for prob, g in plist:
            try:
                # distance test, NOT g.buffer(0.25): buffering a national
                # contour just to test one point rebuilds the geometry and
                # dominated the build time
                if g.contains(pt) or pt.distance(g) <= 0.25:
                    best = max(best, prob)
            except Exception:                      # noqa: BLE001
                continue
        out[layer] = best
    return out


# ------------------------------------------------------------ build
def _build():
    os.makedirs(OUT_DIR, exist_ok=True)
    ym = f"{dt.datetime.now(dt.timezone.utc):%Y%m}"
    try:
        tf = _fetch_zip("seastemp", ym)
        pf = _fetch_zip("seasprcp", ym)
    except requests.RequestException:
        return {"ok": False, "reason": "CPC unreachable"}
    if not tf or not pf:
        return {"ok": False, "reason": "CPC seasonal zips unavailable"}
    try:
        zt, zp = zipfile.ZipFile(tf), zipfile.ZipFile(pf)
    except zipfile.BadZipFile:
        return {"ok": False, "reason": "bad CPC zip"}

    windows, issued = [], None
    for lead, seas, label in WINDOWS:
        temp = _contours(zt, lead, seas, "temp")
        prcp = _contours(zp, lead, seas, "prcp")
        if not temp and not prcp:
            continue
        issued = issued or _issued(zt, lead, seas)
        cold = temp.get("Below", [])
        warm = temp.get("Above", [])
        wet = prcp.get("Above", [])
        dry = prcp.get("Below", [])

        # build each contour geometry ONCE: tilt layers, snow chances and
        # scorecards all consume the same geoms (the polygon fix-up unions on
        # national CPC rings are the expensive step - recomputing them per
        # consumer made the first build take 8+ minutes)
        geoms = {k: _to_geom(v) for k, v in
                 (("cold", cold), ("warm", warm), ("wet", wet), ("dry", dry))}
        snow = _snow_chances(geoms["cold"], geoms["wet"]) \
            if geoms["cold"] and geoms["wet"] else []

        # scorecards sample the same unsimplified geoms
        samples = geoms
        cards = []
        for name, lat, lon in ANCHORS:
            sc = _score(samples, lat, lon)
            top = max(sc.items(), key=lambda kv: kv[1])
            if top[1] <= 33.0:
                word = "Near-normal temperatures and precipitation " \
                       "(equal chances)"
            else:
                word = (f"{int(top[1])}% chance of a "
                        f"{'colder' if top[0] == 'cold' else 'warmer' if top[0] == 'warm' else 'wetter' if top[0] == 'wet' else 'drier'}-than-normal window")
            cards.append({"region": name, "scores": sc, "summary": word})

        top_chance = max((f["properties"]["chance"] for f in snow),
                         default=0)
        impact_word, impact_text = _impact(top_chance) if top_chance >= 42 \
            else ("light", "Warm-tilt winter: snowfall chances suppressed; "
                           "watch for cold snaps in the 6-10 day outlooks")
        windows.append({
            "window": seas, "label": label, "issued": issued,
            "layers": {"warm": _tilt_features(geoms["warm"], "warm"),
                       "cold": _tilt_features(geoms["cold"], "cold"),
                       "wet": _tilt_features(geoms["wet"], "wet"),
                       "dry": _tilt_features(geoms["dry"], "dry")},
            "snow": snow, "cards": cards,
            "topSnow": top_chance,
            "impactWord": impact_word, "impactText": impact_text,
        })
    if not windows:
        return {"ok": False, "reason": "no winter windows in CPC zips"}
    return {"ok": True, "issued": issued, "windows": windows,
            "generated": time.strftime("%Y-%m-%d %H:%M")}


def _load_cache(max_age):
    """Fresh-enough disk copy of the derived bundle, or None."""
    try:
        if time.time() - os.stat(_CACHE_FILE).st_mtime < max_age:
            with open(_CACHE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("ok"):
                return d
    except Exception:                              # noqa: BLE001
        pass
    return None


def _save_cache(data):
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(_CACHE_FILE + ".part", "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(_CACHE_FILE + ".part", _CACHE_FILE)
    except Exception:                              # noqa: BLE001
        pass


def season_bundle():
    """Derived winter outlook bundle.

    In-memory 6 h cache, then a shared on-disk cache (static/season/
    bundle.json): the contour geometry takes minutes to derive and the
    updater / regen / app all need the SAME bundle - whoever builds it
    first writes it for the rest. A stale disk copy (30 days) still beats
    an error page if CPC becomes unreachable.
    """
    now = time.time()
    if _cache["data"] and now - _cache["at"] < _TTL:
        return _cache["data"]
    with _lock:
        if _cache["data"] and time.time() - _cache["at"] < _TTL:
            return _cache["data"]
        disk = _load_cache(_TTL)
        if disk is not None:
            _cache["at"], _cache["data"] = time.time(), disk
            return disk
        try:
            data = _build()
        except Exception as exc:                   # noqa: BLE001
            data = {"ok": False, "reason": str(exc)[:120]}
        if not data.get("ok"):
            if (_cache["data"] or {}).get("ok"):
                data = _cache["data"]              # keep the last good build
            else:
                stale = _load_cache(30 * 86_400)
                if stale is not None:
                    data = stale                   # stale beats an error page
        if data.get("ok"):
            _save_cache(data)
        _cache["at"], _cache["data"] = time.time(), data
        return data


if __name__ == "__main__":
    import json
    b = season_bundle()
    print("ok:", b.get("ok"), b.get("reason", ""))
    for w in b.get("windows", []):
        print(f"{w['label']}: warm={len(w['layers']['warm'])} "
              f"cold={len(w['layers']['cold'])} wet={len(w['layers']['wet'])} "
              f"dry={len(w['layers']['dry'])} snow={len(w['snow'])} "
              f"topSnow={w['topSnow']}")
        for c in w["cards"]:
            print("   ", c["region"], "->", c["summary"])

"""SPC Mesoscale Analysis (SFCOA) — hourly objective analyses, no key.

SPC runs a surface objective analysis (SFCOA) at the top of every hour
(40 km RAP first guess + merged surface observations, post-processed with
NSHARP soundings) and publishes each diagnostic field as a transparent
GIF per region "sector". Layout (verified against the live viewer):

    /exper/mesoanalysis/s{NN}/{field}/{field}[_sf].gif   field image
    /exper/mesoanalysis/s{NN}/{overlay}/{overlay}.gif    radar/warns/otlk...
    /exper/mesoanalysis/s{NN}/sfctime.txt                analysis hour

Fields with the plain name carry contour labels; the `_sf` variant is the
"filled" (color-filled) version. We fetch filled variants for a curated
field set, every sector, each refresh, into static/meso/ and expose a
payload the page stacks in layers (field + optional radar/warnings/
outlook overlays), exactly like SPC's own viewer.
"""
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

UA = {"User-Agent": "Mozilla/5.0 tnwx/1.0"}
BASE = "https://www.spc.noaa.gov/exper/mesoanalysis"
OUT_DIR = os.path.join("static", "meso")

# Sectors 11-22 (from the viewer's sector map); 19 = full CONUS national
SECTORS = {
    "19": "National (CONUS)",
    "15": "Southern Plains",
    "18": "Southeast",
    "17": "East Coast",
    "21": "Great Lakes",
    "20": "Midwest",
    "14": "Central Plains",
    "13": "Northern Plains",
    "16": "Northeast",
    "22": "Great Basin",
    "12": "Southwest",
    "11": "Pacific NW",
}
# sectors most relevant to East Tennessee, first
SECTOR_ORDER = ["19", "15", "18", "17", "21", "20", "14", "13", "16", "22", "12", "11"]
HOME_SECTORS = ("19", "15", "18")       # always refreshed at full depth

# Curated diagnostic fields (code -> label). Codes are SPC's own.
FIELD_GROUPS = [
    ("Thermodynamics / Instability", [
        ("sbcp", "SBCAPE"),
        ("mlcp", "MLCAPE"),
        ("mucp", "MUCAPE"),
        ("muli", "MU Lifted Index"),
        ("lasi", "LASI"),
        ("effh", "Effective Inflow Base"),
        ("lclh", "LCL Height"),
        ("lfch", "LFC Height"),
        ("lr3c", "0-3 km Lapse Rate"),
        ("lllr", "Low-Level Lapse Rate"),
        # SPC's own viewer labels 'laps' MID-LEVEL Lapse Rates (850-500 mb);
        # it was wrongly labeled "700-500 mb" here, hiding the field users
        # asked for (2026-09-18). maxlr = Max 2-6 km AGL Lapse Rate.
        ("laps", "Mid-Level Lapse Rate (850-500 mb)"),
        # maxlr publishes ONLY the contour-labeled variant (no _sf fill) -
        # keep it OUT of FILLED or its fetch 404s (verified 2026-09-18)
        ("maxlr", "Max 2-6 km Lapse Rate"),
        ("ttd", "Sfc Temp / Dewpoint"),
        ("pwtr", "Precipitable Water"),
        ("fzlv", "Freezing Level"),
    ]),
    ("Shear / Storm Motion", [
        ("eshr", "Effective Shear"),
        ("cpsh", "0-6 km Shear Vector"),
        ("srh1", "0-1 km Storm-Relative Helicity"),
        ("dvvr", "0-6 km Bulk Vector Difference"),
        ("brn", "Bulk Richardson Number"),
        ("prop", "Supercell Motion / Propagation"),
    ]),
    ("Composite Indices", [
        ("stor", "Supercell Composite (SCP)"),
        ("stpc", "Significant Tornado (STP)"),
        ("scp", "SCP (classic)"),
        ("sigh", "Significant Hail Parameter"),
        # SPC dropped EHI from the mesoanalysis lineup (verified 2026-09-24);
        # served from the site's own SPC-paletted HRRR EHI wall so the
        # composite family stays complete and the palette stays shared.
        ("ehi", "Energy Helicity Index (HRRR wall)"),
        ("thea", "Theta-E Advection"),
        ("mcon", "Moisture Convergence"),
        ("qlcs1", "QLCS Tornado Prob (0-1 km)"),
        ("qlcs2", "QLCS QLCS-Mode Composite"),
        ("peff", "Precipitation Efficiency"),
    ]),
    ("Pressure Layers", [
        ("925mb", "925 mb Temp / Heights"),
        ("850mb", "850 mb Temp / Heights"),
        ("700mb", "700 mb Temp / Heights"),
        ("500mb", "500 mb Temp / Heights"),
        ("300mb", "300 mb Winds / Heights"),
        ("comp", "Composite Layer (sfc-6km)"),
        ("tran", "Orographic/Transport Flow"),
    ]),
]
FIELDS = {code: label for _, fields in FIELD_GROUPS for code, label in fields}
# fields that exist as "filled" (use _sf variant)
FILLED = {"sbcp", "mlcp", "mucp", "muli", "lasi", "effh", "lclh", "lfch", "lr3c",
          "lllr", "laps", "ttd", "pwtr", "fzlv", "eshr", "cpsh", "srh1", "dvvr",
          "brn", "prop", "stor", "stpc", "scp", "sigh", "thea", "mcon", "qlcs1",
          "qlcs2", "peff", "925mb", "850mb", "700mb", "500mb", "300mb", "comp",
          "tran"}
# transparent overlay layers (stack on top of any field)
OVERLAYS = {
    "radar": ("rgnlrad", "Regional radar"),
    "warns": ("warns", "Warnings"),
    "otlk": ("otlk", "SPC outlook"),
}
# extra standalone "fields" that are just images
EXTRA = {"bigsfc": "Surface analysis (winds/pressure)"}

# ---------------------------------------------------------------------------
# East Tennessee zoom (virtual sector "ET")
#
# SPC only publishes fixed regions - none centered on East Tennessee - so we
# magnify the native sector image to a Greeneville-centered box. The crop is
# computed with SPC's own Lambert Conformal math (their carto.js: cone
# constants, secant latitudes 35/45 or 35/50, R=6371) so geography lines up
# exactly, and the crop is LANCZOS-upscaled back to 1000x750 PNG.
# ---------------------------------------------------------------------------
ET_CODE = "ET"
ET_NAME = "East Tennessee (zoom)"
# Context box around East TN (S, N, W, E), Greeneville-centered. Aspect is
# corrected to 4:3 in code so the 1000x750 output has no distortion.
# (south, north, west, east) — centered on East Tennessee itself:
# East TN spans ~34.98-36.7N, -84.3..-81.65W; the box keeps Chattanooga /
# Cookeville context on the SW edge and Asheville on the E edge, with
# Greeneville near the frame center (validated: markers land mid-frame).
EAST_TN_BOX = (34.3, 37.3, -85.8, -80.2)
IMG_W, IMG_H = 1000, 750
_RAD = 0.01745            # SPC carto.js uses this exact constant
_RRR = 6371.0
# initmap params per sector: (clat, clon, slat1, slat2, slon, zoom) - carto.js setMap()
SECTOR_CART = {
    "17": (35.20, -85.10, 35, 50, -97.0, 23.1),
    "18": (28.45, -89.20, 35, 50, -97.0, 19.2),
    "19": (32.60, -103.20, 35, 45, -98.0, 8.3),   # CONUS - projection-validated
}
# Zoom source: s19 only. Verified against SPC's own overlays: active NWS
# warning polygons land within ~5 px of the black outlines on s19/warns.gif.
# (s17/s18 frames do not match carto.js parameters - measured misalignment -
# and s18's frame also tops out at ~36.2N, cutting off the box.)
ET_SOURCE_SECTORS = ("19",)
# Reference cities drawn onto every zoomed image (label, lat, lon, star=home)
ET_CITIES = [
    ("Greeneville", 36.163, -82.830, True),
    ("Knoxville", 35.960, -83.920, False),
    ("Chattanooga", 35.046, -85.310, False),
    ("Kingsport", 36.405, -82.545, False),
    ("Asheville", 35.595, -82.551, False),
    ("Cookeville", 36.163, -85.504, False),
]


def _d2r(v):
    return v * _RAD


def _cart(sector):
    """Projection state for one sector (equivalent of carto.js initmap)."""
    clat, clon, slat1, slat2, slon, zoom = SECTOR_CART[sector]
    t1 = math.log(math.cos(_d2r(slat1)) / math.cos(_d2r(slat2)))
    t2 = math.log(math.tan(_d2r(45.0 - slat1 / 2.0)) / math.tan(_d2r(45.0 - slat2 / 2.0)))
    cone = 1.0 if t2 == 0 else t1 / t2
    psi = (_RRR * math.cos(_d2r(slat1))) / (cone * math.tan(_d2r(45.0 - slat1 / 2.0)) ** cone)
    theta = _d2r(clon - slon) * cone
    rho_c = psi * math.tan(_d2r(45.0 - clat / 2.0)) ** cone
    return {"cone": cone, "reflon": slon, "reflat1": slat1, "psi": psi, "grid": 40.0,
            "zoom": zoom, "xxl": rho_c * math.sin(theta) / 40.0,
            "yyl": (psi * math.tan(_d2r(45.0 - slat1 / 2.0)) ** cone - rho_c * math.cos(theta)) / 40.0}


def _lalo_pix(c, lat, lon):
    """lat/lon -> pixel on the sector's 1000x750 image (SPC carto.js math)."""
    theta = _d2r(lon - c["reflon"]) * c["cone"]
    rho = c["psi"] * math.tan(_d2r(45.0 - lat / 2.0)) ** c["cone"]
    rho1 = c["psi"] * math.tan(_d2r(45.0 - c["reflat1"] / 2.0)) ** c["cone"]
    x = rho * math.sin(theta) / c["grid"]
    y = (rho1 - rho * math.cos(theta)) / c["grid"]
    return (x - c["xxl"]) * c["zoom"] + IMG_W / 2, (c["yyl"] - y) * c["zoom"] + IMG_H / 2


def _et_source():
    """Projection-validated sector that fully contains the East TN box."""
    s, n, w, e = EAST_TN_BOX
    for sec in ET_SOURCE_SECTORS:
        c = _cart(sec)
        pts = [_lalo_pix(c, s, w), _lalo_pix(c, s, e), _lalo_pix(c, n, w), _lalo_pix(c, n, e)]
        if all(20 <= p[0] <= IMG_W - 20 and 20 <= p[1] <= IMG_H - 20 for p in pts):
            return sec
    return "19"


def _et_rect(src_sector):
    """Pixel bounding rect of the East TN box on the source image (+4 px margin)."""
    c = _cart(src_sector)
    s, n, w, e = EAST_TN_BOX
    pts = [_lalo_pix(c, s, w), _lalo_pix(c, s, e), _lalo_pix(c, n, w), _lalo_pix(c, n, e)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = max(0, math.floor(min(xs)) - 4), min(IMG_W, math.ceil(max(xs)) + 4)
    y0, y1 = max(0, math.floor(min(ys)) - 4), min(IMG_H, math.ceil(max(ys)) + 4)
    return x0, y0, x1, y1


def _label_font(size):
    try:
        from matplotlib import font_manager
        from PIL import ImageFont
        return ImageFont.truetype(font_manager.findfont("DejaVu Sans"), size)
    except Exception:  # noqa: BLE001
        from PIL import ImageFont
        try:
            return ImageFont.truetype("arial.ttf", size)
        except Exception:  # noqa: BLE001
            return ImageFont.load_default()


_STATE_CACHE = {}
_STATE_GEOJSON = os.path.join(OUT_DIR, "_states.geojson")
_STATE_TOPO = os.path.join(OUT_DIR, "_states_topo.json")
_STATE_TOPO_URLS = (
    "https://cdn.jsdelivr.net/npm/us-atlas@3/states-10m.json",
    "https://unpkg.com/us-atlas@3/states-10m.json",
)
_STATE_URLS = (
    "https://eric.clst.org/assets/wiki/uploads/Stuff/gz_2010_us_040_00_20m.json",
    "https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json",
)
_STATE_NAMES = {"Tennessee", "Kentucky", "Virginia", "North Carolina", "Georgia",
                "Alabama", "West Virginia", "Mississippi", "Missouri"}
_TN_FIPS = "47"
_FIPS_TO_NAME = {"47": "Tennessee", "21": "Kentucky", "51": "Virginia", "37": "North Carolina",
                 "13": "Georgia", "01": "Alabama", "54": "West Virginia", "28": "Mississippi",
                 "39": "Ohio", "45": "South Carolina"}


def _load_states_geojson():
    """US state polygons: high-res us-atlas TopoJSON preferred, GeoJSON
    (Census 20m) fallback. Cached on disk (refresh weekly)."""
    import json
    try:
        if time.time() - os.path.getmtime(_STATE_TOPO) < 7 * 86400:
            with open(_STATE_TOPO, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    for url in _STATE_TOPO_URLS:
        try:
            r = requests.get(url, headers=UA, timeout=60)
            data = json.loads(r.content)
            if data.get("objects", {}).get("states", {}).get("geometries"):
                tmp = _STATE_TOPO + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, _STATE_TOPO)
                return data
        except Exception:  # noqa: BLE001
            continue
    try:
        if time.time() - os.path.getmtime(_STATE_GEOJSON) < 7 * 86400:
            with open(_STATE_GEOJSON, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    for url in _STATE_URLS:
        try:
            r = requests.get(url, headers=UA, timeout=60)
            data = json.loads(r.content)
            if data.get("features"):
                tmp = _STATE_GEOJSON + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, _STATE_GEOJSON)
                return data
        except Exception:  # noqa: BLE001
            continue
    return {"features": []}


def _topo_to_geojson(topo):
    """Decode us-atlas TopoJSON states layer into GeoJSON features (pure
    Python: delta-decode arcs, stitch rings, keep exterior rings only)."""
    import json

    def _dec_arc(arc):
        out, x, y = [], 0, 0
        for dx, dy in arc:
            x += dx
            y += dy
            out.append((x, y))
        return out

    tr = topo.get("transform") or {}
    sx, sy = tr.get("scale", [1.0, 1.0])
    tx, ty = tr.get("translate", [0.0, 0.0])
    # Per TopoJSON spec, delta encoding resets at the start of every arc.
    arcs = [_dec_arc(a) for a in topo.get("arcs", [])]

    def _ring(arc_idx):
        pts = []
        for i in arc_idx:
            seg = arcs[i] if i >= 0 else list(reversed(arcs[~i]))
            if pts and pts[-1] == seg[0]:
                seg = seg[1:]
            pts.extend(seg)
        return [(p[0] * sx + tx, p[1] * sy + ty) for p in pts]

    feats = []
    states = topo.get("objects", {}).get("states", {}).get("geometries", [])
    for g in states:
        fips = str(g.get("id") or "")
        name = (g.get("properties") or {}).get("name") or _FIPS_TO_NAME.get(fips, fips)
        polys = []
        gt = g.get("type")
        if gt == "Polygon":
            polys = [_ring(r) for r in g.get("arcs", [])]
        elif gt == "MultiPolygon":
            polys = [_ring(r) for poly in g.get("arcs", []) for r in poly]
        if polys:
            feats.append({"properties": {"name": name},
                          "geometry": {"type": "MultiPolygon",
                                       "coordinates": [[p] for p in polys]}})
    return {"features": feats}


def _rings(coords):
    """Yield coordinate rings from any GeoJSON geometry nesting."""
    if not isinstance(coords, (list, tuple)) or not coords:
        return
    first = coords[0]
    if isinstance(first, (int, float)):
        return  # a bare position; rings are yielded by callers above
    if isinstance(first, (list, tuple)) and first and isinstance(first[0], (int, float)):
        yield coords
        return
    for item in coords:
        yield from _rings(item)


def _state_borders(c, x0, y0, crop_w, crop_h):
    """Real state border polylines (us-atlas 10m, cached) in zoomed-image
    pixel coords. Ground-truth geography, drawn on every frame."""
    key = (c["reflon"], x0, y0, crop_w, crop_h)
    if key in _STATE_CACHE:
        return _STATE_CACHE[key]
    data = _load_states_geojson()
    if data.get("objects"):          # TopoJSON -> decode to GeoJSON
        data = _topo_to_geojson(data)
    sx, sy = IMG_W / crop_w, IMG_H / crop_h
    lines = []
    for feat in data.get("features", []):
        props = feat.get("properties") or {}
        name = props.get("name") or props.get("NAME") or ""
        if name not in _STATE_NAMES:
            continue
        g = feat.get("geometry") or {}
        for ring in _rings(g.get("coordinates")):
            pts = []
            for lon, lat in ring:
                px, py = _lalo_pix(c, float(lat), float(lon))
                pts.append(((px - x0) * sx, (py - y0) * sy))
            lines.append((pts, name == "Tennessee"))
    _STATE_CACHE[key] = lines
    return lines


def _draw_states(im, x0, y0, crop_w, crop_h):
    """Draw state borders; Tennessee emphasized in white, others thin gray."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(im)

    def _stroke(pts, color, width):
        for i in range(1, len(pts)):
            x1p, y1p = pts[i - 1]
            x2p, y2p = pts[i]
            if abs(x1p - x2p) > 200 or abs(y1p - y2p) > 200:  # ring seam
                continue
            if -60 <= x1p <= IMG_W + 60 and -60 <= y1p <= IMG_H + 60:
                d.line([x1p, y1p, x2p, y2p], fill=color, width=width)

    borders = _state_borders(_cart("19"), x0, y0, crop_w, crop_h)
    for pts, is_tn in borders:
        if not is_tn:
            _stroke(pts, (150, 150, 155, 190), 2)
    for pts, is_tn in borders:
        if is_tn:
            _stroke(pts, (255, 255, 255, 235), 4)
    return im


def _draw_cities(im, x0, y0, crop_w, crop_h):
    """Draw labeled city markers onto a zoomed image (geographic anchors)."""
    from PIL import ImageDraw
    c = _cart("19")
    sx, sy = IMG_W / crop_w, IMG_H / crop_h
    d = ImageDraw.Draw(im)
    font = _label_font(30)
    for name, lat, lon, star in ET_CITIES:
        px, py = _lalo_pix(c, lat, lon)
        x = (px - x0) * sx
        y = (py - y0) * sy
        if not (30 <= x < IMG_W - 30 and 40 <= y < IMG_H - 10):
            continue
        r = 9 if star else 6
        d.ellipse([x - r - 2, y - r - 2, x + r + 2, y + r + 2], outline=(0, 0, 0, 220), width=3)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 255) if star else (255, 210, 60, 255),
                  outline=(0, 0, 0, 255), width=2)
        d.text((x + 14, y - 32), name, font=font, fill=(255, 255, 255, 255),
               stroke_width=3, stroke_fill=(0, 0, 0, 230))
    return im


def zoom_east_tn(src_sector=None, force=False):
    """Magnify every GIF in the source sector into the East TN box (PNG out).

    Returns number of images written, or -1 when source images are unchanged
    since the last zoom (nothing to do). Overlays and hourly archive frames
    are zoomed alongside the live fields so the page needs no special cases.
    """
    from PIL import Image
    if src_sector is None:
        src_sector = _et_source()
    sdir = os.path.join(OUT_DIR, f"s{src_sector}")
    if not os.path.isdir(sdir):
        return 0
    try:
        x0, y0, x1, y1 = _et_rect(src_sector)
    except Exception:  # noqa: BLE001
        return 0
    if x1 - x0 < 60 or y1 - y0 < 60:
        return 0
    # pad the crop to the output aspect ratio so the zoom has no distortion
    target = IMG_W / IMG_H
    w, h = x1 - x0, y1 - y0
    if w / h > target:
        nh = int(round(w / target))
        cy = (y0 + y1) // 2
        y0 = max(0, min(IMG_H - nh, cy - nh // 2))
        y1 = min(IMG_H, y0 + nh)
    else:
        nw = int(round(h * target))
        cx = (x0 + x1) // 2
        x0 = max(0, min(IMG_W - nw, cx - nw // 2))
        x1 = min(IMG_W, x0 + nw)
    odir = os.path.join(OUT_DIR, f"s{ET_CODE}")
    os.makedirs(odir, exist_ok=True)
    names = [f for f in os.listdir(sdir) if f.endswith(".gif")]
    if not names:
        return 0
    newest = max(os.path.getmtime(os.path.join(sdir, f)) for f in names)
    stamp_path = os.path.join(odir, ".src_stamp")
    if not force and os.path.exists(stamp_path):
        try:
            if abs(float(open(stamp_path).read().strip() or 0) - newest) < 1:
                return -1
        except (ValueError, OSError):
            pass

    def _do(name):
        try:
            im = Image.open(os.path.join(sdir, name)).convert("RGB")
            crop = im.crop((x0, y0, x1, y1))
            if crop.width < 8 or crop.height < 8:
                return False
            out = crop.resize((IMG_W, IMG_H), Image.LANCZOS)
            _draw_states(out, x0, y0, x1 - x0, y1 - y0)
            _draw_cities(out, x0, y0, x1 - x0, y1 - y0)
            # Palette-quantize: SPC fields have <128 distinct colors, and the
            # RGBA PNGs this replaced ran ~550 KB each - 37 live + 216 archive
            # files blew docs/ past the 950 MB publish cap (2026-09-20 23:52,
            # "build too large - skipping"). Measured 27% of the RGBA size,
            # visually identical at map scale.
            out = out.quantize(colors=128, method=Image.MEDIANCUT,
                               dither=Image.FLOYDSTEINBERG)
            # quantize carries an RGB-tuple transparency into P-mode where
            # PNG save compares tuple<int and TypeErrors (measured) - these
            # renders are opaque, so drop the key entirely.
            t = out.info.get("transparency")
            if not isinstance(t, int):
                out.info.pop("transparency", None)
            dst = os.path.join(odir, name[:-4] + ".png")
            tmp = dst + ".tmp"
            out.save(tmp, "PNG", optimize=True)
            os.replace(tmp, dst)
            return True
        except Exception:  # noqa: BLE001
            return False

    with ThreadPoolExecutor(max_workers=8) as ex:
        done = sum(1 for r in ex.map(_do, names) if r)
    try:
        with open(stamp_path, "w") as f:
            f.write(str(newest))
    except OSError:
        pass
    return done

_META = {"t": 0.0, "data": None}
_TTL = 100          # SFCOA runs hourly; refresh each site cycle (~2 min)
_DL_TIMEOUT = 20


def _fetch_one(url, out_path):
    """Download one GIF; returns True when fresh bytes were saved."""
    try:
        r = requests.get(url, headers=UA, timeout=_DL_TIMEOUT)
        if r.ok and r.headers.get("content-type", "").startswith("image") and len(r.content) > 1500:
            tmp = out_path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(r.content)
            os.replace(tmp, out_path)
            return True
    except requests.RequestException:
        pass
    return False


def _archive_url(sector, code, dt_utc):
    """Hourly-archive URL: s{sec}/{field}/{field}_{yymmddhh}.gif (no _sf)."""
    stamp = dt_utc.strftime("%y%m%d%H")
    return f"{BASE}/s{sector}/{code}/{code}_{stamp}.gif"


def refresh_history(sector, fields, hours=4):
    """Download the past `hours` hourly archive frames for these fields."""
    import datetime as dt
    sdir = os.path.join(OUT_DIR, f"s{sector}")
    os.makedirs(sdir, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    jobs = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for back in range(1, hours + 1):          # skip the current hour (already have 'live')
            stamp = (now - dt.timedelta(hours=back)).strftime("%y%m%d%H")
            for code in fields:
                dst = os.path.join(sdir, f"{code}_{stamp}.gif")
                if os.path.exists(dst) and os.path.getsize(dst) > 1500:
                    continue
                jobs.append(ex.submit(_fetch_one, _archive_url(sector, code, now - dt.timedelta(hours=back)), dst))
        ok = sum(1 for j in as_completed(jobs) if j.result())
    return ok


def _prune_archive(keep=4):
    """Delete archive frames older than the newest `keep` hourly stamps.

    refresh_history skips files it already has, so old stamps accumulated
    forever - docs/meso/ alone hit 275 MB and pushed the build past the
    publish size cap (2026-09-20 23:52). Runs at the end of each meso_bundle
    render; a field whose live+4 archive frames the page animates is untouched.
    """
    import re as _re
    removed = 0
    try:
        for sec in os.listdir(OUT_DIR):
            sdir = os.path.join(OUT_DIR, sec)
            if not os.path.isdir(sdir) or not sec.startswith("s"):
                continue
            by_field = {}
            for f in os.listdir(sdir):
                m = _re.match(r"^(.+)_(\d{8})\.(?:gif|png)$", f)
                if m:
                    by_field.setdefault(m.group(1), []).append((m.group(2), f))
            for field, frames in by_field.items():
                frames.sort(reverse=True)
                for _stamp, f in frames[keep:]:
                    try:
                        os.remove(os.path.join(sdir, f))
                        removed += 1
                    except OSError:
                        continue
    except Exception:                          # noqa: BLE001 - never break render
        return removed
    return removed


def _analysis_hour_et(raw):
    """SPC sfctime.txt ('09/10/26 23 UTC') -> '09/10/26 7 PM ET' (raw on any surprise)."""
    try:
        from zoneinfo import ZoneInfo
        import datetime as _dt
        from data._tz import to_et
        utc = _dt.datetime.strptime(raw.strip(), "%m/%d/%y %H UTC").replace(
            tzinfo=_dt.timezone.utc)
        e = to_et(utc)
        hr, ampm = ((e.hour % 12) or 12), ("AM" if e.hour < 12 else "PM")
        return f"{e.month:02d}/{e.day:02d}/{e.year % 100:02d} {hr} {ampm} ET"
    except (ValueError, AttributeError):
        return raw


def _analysis_hour(sector):
    try:
        r = requests.get(f"{BASE}/s{sector}/sfctime.txt", headers=UA, timeout=10)
        if r.ok:
            return r.text.strip()
    except requests.RequestException:
        pass
    return ""


def _fetch_field(sector, code, out_dir):
    """Download one field's filled variant, falling back to the labeled one.

    SPC historically published `{code}_sf.gif` (color-filled) for most
    fields, but has been removing `_sf` for some (scp, sigh, eshr, srh1
    verified 2026-09-24) while the labeled `{code}.gif` stays up. Trying
    the fill first and falling back keeps every field alive whatever SPC
    does next; the labeled variant reads fine stacked under overlays.
    """
    filled = code in FILLED or code in EXTRA
    out_path = os.path.join(out_dir, f"{code}.gif")
    if filled and code not in EXTRA:
        if _fetch_one(f"{BASE}/s{sector}/{code}/{code}_sf.gif", out_path):
            return True
    return _fetch_one(f"{BASE}/s{sector}/{code}/{code}.gif", out_path)


def refresh(sector, fields):
    """Download the given field set for one sector into static/meso/s{NN}/."""
    sdir = os.path.join(OUT_DIR, f"s{sector}")
    os.makedirs(sdir, exist_ok=True)
    jobs = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for code in fields:
            jobs.append(ex.submit(_fetch_field, sector, code, sdir))
        for oname, (ocode, _) in OVERLAYS.items():
            jobs.append(ex.submit(_fetch_one, f"{BASE}/s{sector}/{ocode}/{ocode}.gif",
                                  os.path.join(sdir, f"{oname}.gif")))
        ok = sum(1 for j in as_completed(jobs) if j.result())
    return ok


def _inject_wall_ehi(sectors, et_ok=False):
    """Add the site's HRRR EHI wall render as an `ehi` field in every sector.

    SPC removed EHI from the mesoanalysis lineup, so the composite family
    would be incomplete on the viewer. The Models-page EHI wall is already
    rendered hourly in the shared SPC palette - the native sectors show the
    US tile, the ET zoom shows the East-Tennessee tile. Newest cycle wins;
    if no tile exists yet the field is simply absent (page never breaks).
    """
    mdir = os.path.join("static", "model_maps")
    try:
        names = os.listdir(mdir)
    except OSError:
        return

    # Prefer HRRR (CAM, matches the hourly-analysis feel); any other
    # SPC-paletted EHI wall works as fallback while the render rotation
    # backfills the catalog (ehi tiles may not exist for every model yet).
    def newest(model, region):
        rx = re.compile(rf"^{model}_ehi_f(\d+)_(\d{{10}})_{region}\.png$")
        best = None
        for n in names:
            m = rx.match(n)
            if m:
                key = (m.group(2), int(m.group(1)))
                if best is None or key > best[0]:
                    best = (key, n)
        return best[1] if best else None

    us_png = et_png = None
    for model in ("HRRR", "SREF", "HREF", "REFS", "GFS"):
        us_png = us_png or newest(model, "us")
        et_png = et_png or (newest(model, "etn") if et_ok else None)
        if us_png and (et_png or not et_ok):
            break
    entry = {"label": "Energy Helicity Index (HRRR wall)",
             "url": f"../model_maps/{us_png}"} if us_png else None
    for sec, info in sectors.items():
        if not isinstance(info, dict) or "fields" not in info:
            continue
        if sec == ET_CODE:
            if et_png:
                info["fields"]["ehi"] = {"label": "Energy Helicity Index (HRRR wall)",
                                         "url": f"../model_maps/{et_png}"}
        elif entry:
            info["fields"]["ehi"] = dict(entry)


def meso_bundle(force=False):
    """Payload for the mesoanalysis page (cached ~2 min).

    Downloads anything missing or stale, then returns:
    {"analysis": str, "generated": str, "sectors": {code: {"name", "fields":
    {code: {"url", "label"}}, "overlays": {key: {"url", "label"}}}}}
    """
    now = time.time()
    if not force and _META["data"] is not None and now - _META["t"] < _TTL:
        return _META["data"]

    fields_all = dict(FIELDS)
    fields_all.update(EXTRA)
    sectors = {}
    now = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {}
        for sec in SECTOR_ORDER:
            wanted = list(fields_all) if sec in HOME_SECTORS else [
                c for c in ("sbcp", "mlcp", "mucp", "eshr", "srh1", "stor",
                            "stpc", "sigh", "effh", "pwtr", "bigsfc")]
            futs[ex.submit(refresh, sec, wanted)] = (sec, True)
            futs[ex.submit(refresh_history, sec, wanted, 4)] = (sec, False)
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception:  # noqa: BLE001 - one sector must not kill all
                continue
    # East Tennessee zoom: derive from whichever sector carries the box
    et_src = _et_source()
    try:
        if zoom_east_tn(et_src) == 0:
            et_src = None
    except Exception:  # noqa: BLE001 - zoom must never break the page
        et_src = None
    hour = _analysis_hour(SECTOR_ORDER[0])
    for sec in SECTOR_ORDER:
        sdir = os.path.join(OUT_DIR, f"s{sec}")
        fields_out = {}
        for code, label in fields_all.items():
            p = os.path.join(sdir, f"{code}.gif")
            if os.path.exists(p) and os.path.getsize(p) > 1500:
                fields_out[code] = {"label": label, "url": f"../meso/s{sec}/{code}.gif"}
        if fields_out:
            sectors[sec] = {
                "name": SECTORS[sec],
                "fields": fields_out,
                "overlays": {k: {"label": lbl, "url": f"../meso/s{sec}/{k}.gif"}
                             for k, (oc, lbl) in OVERLAYS.items()
                             if os.path.exists(os.path.join(sdir, f"{k}.gif"))},
            }
    # virtual ET sector from the zoomed crops
    order = list(SECTOR_ORDER)
    if et_src:
        edir = os.path.join(OUT_DIR, f"s{ET_CODE}")
        et_fields = {}
        for code, label in fields_all.items():
            p = os.path.join(edir, f"{code}.png")
            if os.path.exists(p) and os.path.getsize(p) > 800:
                et_fields[code] = {"label": label, "url": f"../meso/s{ET_CODE}/{code}.png"}
        et_over = {k: {"label": lbl, "url": f"../meso/s{ET_CODE}/{k}.png"}
                   for k, (oc, lbl) in OVERLAYS.items()
                   if os.path.exists(os.path.join(edir, f"{k}.png"))}
        if et_fields:
            sectors[ET_CODE] = {"name": ET_NAME, "fields": et_fields, "overlays": et_over,
                                "zoom": True, "source": f"s{et_src}"}
            order = [ET_CODE] + order
    _inject_wall_ehi(sectors, et_ok=bool(et_src))
    _prune_archive()
    data = {
        "analysis": _analysis_hour_et(hour),
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "sectorOrder": order,
        "sectorNames": {**SECTORS, ET_CODE: ET_NAME} if et_src else SECTORS,
        "fieldGroups": [[name, fields] for name, fields in FIELD_GROUPS],
        "historyHours": 4,
        "sectors": sectors,
    }
    _META["t"] = now
    _META["data"] = data
    return data


if __name__ == "__main__":
    import sys
    sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    b = meso_bundle(force=True)
    print("analysis:", b["analysis"], "| generated:", b["generated"])
    for sec, info in b["sectors"].items():
        print(f"  s{sec} {info['name']}: {len(info['fields'])} fields, {len(info['overlays'])} overlays")

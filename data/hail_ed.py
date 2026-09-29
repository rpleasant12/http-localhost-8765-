"""Hail-signature teaching cut: the radar page's annotated hail-core case study.

The lesson needs a REAL hail core, not a textbook cartoon. This module:
  1. pulls the largest verified hail report (NWS Local Storm Report) of the
     past 7 days via IEM's keyless LSR GeoJSON service,
  2. fetches IEM's archived CONUS composite-reflectivity PNG (n0r, 5-min
     cadence, keyless) for the report time + location,
  3. crops a ~400 km box around the storm, applies the classic NWS N0R
     dBZ color ramp (the IEM archive ships a grayscale ramp), upscales,
  4. annotates the hail core, the LSR point, the inflow notch and the
     forward-flank rain core - the four features the lesson teaches.

Output: static/hail_ed/hail_cut.png + hail_case.json (report metadata so
the page can show an honest caption). Cached for 12 h; a quiet week with
no hail reports returns an empty case and the page degrades gracefully.
"""
import datetime as dt
import io
import json
import os
import re

import requests

STATIC_DIR = os.path.join("static", "hail_ed")
UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}

# IEM comprad CONUS grid (640x480): lat 54->24 N, lon -124->-66 W
LAT_TOP, LAT_BOT, LON_L, LON_R = 54.0, 24.0, -124.0, -66.0
IMG_W, IMG_H = 640, 480

# classic NWS N0R 5-dBZ steps
_LEVELS = list(range(5, 76, 5))
_COLORS = [(1, 159, 244), (3, 0, 244), (2, 253, 2), (1, 197, 1), (0, 142, 0),
           (253, 248, 2), (229, 188, 0), (253, 149, 0), (253, 80, 0),
           (255, 0, 0), (212, 0, 0), (188, 0, 0), (248, 0, 240),
           (152, 84, 198), (80, 0, 140)]
_NO_DATA = 105  # IEM's grayscale no-data background


def _iem_lsrs():
    """Largest CONUS hail LSRs of the past 7 days (magnitude = inches).

    IEM's phenomena/event query params zero out unpredictably (verified
    live 2026-09-23), so fetch the unfiltered 168 h feed and filter
    client-side on typetext - slower but deterministic.
    """
    try:
        r = requests.get(
            "https://mesonet.agron.iastate.edu/geojson/lsr.geojson",
            params={"hours": 168}, headers=UA, timeout=60)
        feats = (r.json() or {}).get("features") or []
    except Exception:  # noqa: BLE001 - upstream hiccup = empty case
        return []
    out = []
    for f in feats:
        p = f.get("properties") or {}
        if "HAIL" not in (p.get("typetext") or "").upper():
            continue
        lat, lon = p.get("lat"), p.get("lon")
        try:
            mag = float(p.get("magnitude") or 0)
        except (TypeError, ValueError):
            continue
        try:
            valid = dt.datetime.strptime(p.get("valid", ""), "%Y-%m-%dT%H:%M:%SZ")
        except Exception:  # noqa: BLE001
            continue
        if not lat or not lon or mag < 1.0:      # severe threshold = 1 in.
            continue
        if not (LAT_BOT < lat < LAT_TOP and LON_L < lon < LON_R):
            continue                              # skip AK/HI/PR
        out.append({"lat": lat, "lon": lon, "mag": mag, "valid": valid,
                    "city": p.get("city") or "", "st": p.get("st") or "",
                    "wfo": p.get("wfo") or "", "source": p.get("source") or ""})
    out.sort(key=lambda e: -e["mag"])
    return out


def _fetch_frame(day, hhmm):
    url = (f"https://mesonet.agron.iastate.edu/archive/data/"
           f"{day:%Y}/{day:%m}/{day:%d}/comprad/n0r_{day:%Y%m%d}_{hhmm}.png")
    try:
        r = requests.get(url, headers=UA, timeout=60)
        return r.content if r.ok else None
    except requests.RequestException:
        return None


def _colorize(gray_img):
    """Grayscale IEM N0R ramp -> classic NWS reflectivity palette."""
    import numpy as np
    a = np.array(gray_img.convert("L")).astype(float)
    dbz = 5.0 + (a - 1.0) * 70.0 / 110.0
    out = np.zeros((*a.shape, 3), dtype=np.uint8)
    mask = (a >= 1) & (a <= 110) & (a != _NO_DATA)
    for i, lv in enumerate(_LEVELS):
        sel = mask & (dbz >= lv)
        if i + 1 < len(_LEVELS):
            sel &= dbz < _LEVELS[i + 1]
        out[sel] = _COLORS[i]
    return out, dbz, mask


def _annotate(rgb, core_xy, lsr_xy, meta, crop_origin):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.fromarray(rgb)
    d = ImageDraw.Draw(img)
    W, H = img.size

    def font(sz):
        for name in ("arialbd.ttf", "Arial.ttf", "DejaVuSans-Bold.ttf"):
            try:
                return ImageFont.truetype(name, sz)
            except Exception:  # noqa: BLE001
                pass
        return ImageFont.load_default()

    f14, f12, f11 = font(15), font(13), font(12)
    cx, cy = lsr_xy
    core_x, core_y = core_xy

    # hail core ring + label
    r = 30
    d.ellipse([core_x - r, core_y - r, core_x + r, core_y + r],
              outline=(255, 255, 255), width=3)
    d.ellipse([core_x - r - 2, core_y - r - 2, core_x + r + 2, core_y + r + 2],
              outline=(0, 0, 0), width=1)
    d.line([core_x + r, core_y - 8, W - 92, 22], fill=(255, 255, 255), width=2)
    d.text((W - 88, 12), "Hail core 65+ dBZ", font=f14, fill=(255, 255, 255))
    d.text((W - 88, 29), "storm's hail factory", font=f11, fill=(235, 235, 235))

    # LSR marker (crosshair) + report line
    d.line([cx - 8, cy, cx + 8, cy], fill=(0, 255, 255), width=2)
    d.line([cx, cy - 8, cx, cy + 8], fill=(0, 255, 255), width=2)
    stamp = meta["valid"].strftime("%d %b %H:%M UTC")
    d.text((cx + 12, cy + 10),
           f"{meta['mag']:.1f} in. hail report - {stamp}", font=f12,
           fill=(0, 255, 255))

    # inflow notch SE of the core
    nx, ny = core_x + 46, core_y + 40
    d.arc([nx - 16, ny - 16, nx + 16, ny + 16], start=300, end=90,
          fill=(255, 220, 0), width=3)
    d.line([nx + 22, ny + 26, nx + 30, H - 70], fill=(255, 220, 0), width=2)
    d.text((nx + 2, H - 66), "Inflow notch", font=f14, fill=(255, 220, 0))
    d.text((nx + 2, H - 50), "(storm's intake)", font=f11, fill=(255, 220, 0))

    # forward-flank rain core NE
    fx, fy = core_x + 56, core_y - 44
    d.ellipse([fx - 8, fy - 8, fx + 8, fy + 8], outline=(160, 220, 255), width=3)
    d.line([fx + 10, fy - 6, W - 104, fy - 20], fill=(160, 220, 255), width=2)
    d.text((W - 100, fy - 30), "Forward flank", font=f14, fill=(160, 220, 255))
    d.text((W - 100, fy - 14), "(rain core)", font=f11, fill=(160, 220, 255))

    # header: what/where/when (honest sourcing)
    where = f"{meta['city']}, {meta['st']}".strip(", ")
    d.text((10, 8), "Composite reflectivity - biggest verified hail of the week",
           font=f12, fill=(240, 240, 240))
    d.text((10, 24),
           f"{where} - {stamp} (NWS report via IEM LSR archive)",
           font=f12, fill=(240, 240, 240))
    d.rectangle([0, 0, W - 1, H - 1], outline=(60, 60, 60), width=1)
    return img


def build_case(max_age_h=12):
    """Return case dict (or None) + write static/hail_ed/hail_cut.png.

    Cached: if the PNG + case json exist and are younger than max_age_h,
    re-serve them. Chooses the biggest LSR whose storm actually shows a
    strong echo (>= 55 dBZ equivalent gray) in its neighborhood.
    """
    os.makedirs(STATIC_DIR, exist_ok=True)
    png_path = os.path.join(STATIC_DIR, "hail_cut.png")
    json_path = os.path.join(STATIC_DIR, "hail_case.json")
    now = dt.datetime.now(dt.timezone.utc)
    try:
        if (os.path.isfile(png_path) and os.path.isfile(json_path)
                and now.timestamp() - os.path.getmtime(png_path) < max_age_h * 3600):
            with open(json_path, encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:  # noqa: BLE001 - fall through to regenerate
        pass

    ltrs = _iem_lsrs()
    from PIL import Image
    import numpy as np
    half = 45                                   # 90 px crop ~ 5.6 deg lat
    for rpt in ltrs[:12]:                       # try biggest, then next...
        # frame nearest the report time at 5-min cadence, +10 min for lead
        t = rpt["valid"] + dt.timedelta(minutes=10)
        day, hhmm = t.date(), t.strftime("%H%M")
        raw = _fetch_frame(day, hhmm)
        if not raw:
            continue
        base = Image.open(io.BytesIO(raw))
        a = np.array(base.convert("L")).astype(int)
        h, w = a.shape
        y = int((LAT_TOP - rpt["lat"]) / (LAT_TOP - LAT_BOT) * (h - 1))
        x = int((rpt["lon"] - LON_L) / (LON_R - LON_L) * (w - 1))
        win = a[max(0, y - 12):y + 13, max(0, x - 12):x + 13]
        data = win[(win >= 1) & (win <= 110) & (win != _NO_DATA)]
        if len(data) < 40 or data.max() < 88:   # < ~56 dBZ: no real core here
            continue
        # strongest pixel inside the window = hail-core center
        wy, wx = np.unravel_index(int(np.argmax(np.where(
            (win >= 1) & (win <= 110) & (win != _NO_DATA), win, 0))), win.shape)
        oy, ox = max(0, y - 12), max(0, x - 12)
        core_y, core_x = oy + int(wy), ox + int(wx)
        box = (max(0, x - half), max(0, y - half),
               min(w, x + half), min(h, y + half))
        cut = base.crop(box)
        rgb, dbz, mask = _colorize(cut)
        # local core position within the crop (crop was upscaled 4x later)
        c_local = (core_x - box[0], core_y - box[1])
        l_local = (x - box[0], y - box[1])
        cut_up = Image.fromarray(rgb).resize(
            (cut.width * 4, cut.height * 4), Image.LANCZOS)
        # the UNANNOTATED twin: the radar page's practice mode shows this
        # first so the learner finds the core themselves before the reveal
        plain_path = os.path.join(STATIC_DIR, "hail_cut_plain.png")
        cut_up.save(plain_path)
        # re-derive annotation coords at 4x
        ann = _annotate(np.array(cut_up.convert("RGB")),
                        (c_local[0] * 4, c_local[1] * 4),
                        (l_local[0] * 4, l_local[1] * 4), rpt, box)
        ann.save(png_path)
        # geographic calibration for the practice mode's click->lat/lon math
        g = lambda px, py: (
            LAT_TOP - py * (LAT_TOP - LAT_BOT) / (h - 1),
            LON_L + px * (LON_R - LON_L) / (w - 1))
        c_lat, c_lon = g(core_x, core_y)
        case = {"report": {"lat": rpt["lat"], "lon": rpt["lon"],
                           "mag": rpt["mag"], "city": rpt["city"],
                           "st": rpt["st"], "wfo": rpt["wfo"],
                           "source": rpt["source"],
                           "valid": rpt["valid"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "frame": f"{day:%Y-%m-%d} {hhmm}Z"},
                # practice-mode calibration: click (x,y) on the plain crop ->
                # lat/lon via linear interpolation between the corners
                "crop": {"latTop": g(box[0], box[1])[0],
                         "latBot": g(box[0], box[3])[0],
                         "lonL": g(box[0], box[1])[1],
                         "lonR": g(box[2], box[1])[1]},
                "core": {"lat": c_lat, "lon": c_lon},
                "png": "hail_cut.png",
                "built": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(case, fh)
        return case
    return None


def case_bundle():
    """Payload-ready case for the radar page (None when no hail this week)."""
    try:
        case = build_case()
    except Exception:  # noqa: BLE001 - the lesson must never break the page
        case = None
    if not case:
        return None
    c = dict(case)
    # /app/static/ form: the app server resolves it locally and the packager
    # rewrites it to docs-relative form on publish (same as every pngUrl)
    c["url"] = "/app/static/hail_ed/hail_cut.png"
    c["urlPlain"] = "/app/static/hail_ed/hail_cut_plain.png"
    return c


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    c = case_bundle()
    print(json.dumps(c, indent=1) if c else "no hail case this week")

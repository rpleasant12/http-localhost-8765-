"""MPAS + FV3 (SHiELD) forecast maps from pre-rendered public archives (no key).

These two models don't publish GRIB2 that's practical to decode per-request, but
both publish complete pre-rendered map graphics for every forecast hour:

MPAS-A   NCAR/MMM real-time global 3.75-km forecasts (GFS-initialized).
         Archive: project.mmm.ucar.edu/real-time-forecasts/2025/fall/
         Endpoint: get-files.php POST {d: init, v: var, o: domain, m: mpas}
         -> JSON list of frame URLs (hourly to F120 on conus, 3-hourly global).
         NOTE: the demonstration ended Oct 2025, so this is an archived run -
         find_cycle() returns the newest init that still has frames.

SHiELD   GFDL's FV3-core real-time models (T-SHiELD 2026 Atlantic nests,
         C-SHiELD 2025 CONUS + nests). Runs 4x daily, live.
         Endpoints: model-ajax.php (init list), ymdh-json-ajax.php (regions),
         region-json-ajax.php (fields), get-images-ajax.php (frame URLs).

Both serve plain JPGs; we download to static/ and serve them like the other
image-frame layers (satellite/MRMS pattern).
"""
import datetime as dt
import json
import os
import re
import threading
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0"}

MPAS_BASE = "https://project.mmm.ucar.edu/real-time-forecasts/2025/fall/"
MPAS_GET = MPAS_BASE + "get-files.php"
SHIELD_BASE = "https://shield.gfdl.noaa.gov/"

STATIC_DIR = os.path.join("static", "aimodels")
os.makedirs(STATIC_DIR, exist_ok=True)

# NCAR MPAS: var -> (label, product key). conus domains run hourly to F120.
MPAS_PRODUCTS = {
    "wspd":  {"label": "10 m Wind Speed", "key": "mpas_wind"},
    "t2m":   {"label": "2 m Temperature", "key": "mpas_t2m"},
    "cape":  {"label": "Surface-Based CAPE", "key": "mpas_cape"},
}
MPAS_DOMAINS = {"atl": "Atlantic", "epac": "E Pacific", "wpac": "W Pacific",
                "conus": "CONUS", "global": "Global"}

# GFDL SHiELD (FV3): field key -> (label, product key). C-SHiELD_2025 has CONUS.
SHIELD_MODEL = "C-SHiELD_2025"
SHIELD_PRODUCTS = {
    "max_reflectivity_wind": {"label": "Composite Radar Reflectivity + Wind", "key": "shield_refl"},
    "base_reflectivity_wind": {"label": "Base Radar Reflectivity + Wind", "key": "shield_bref"},
    "TMP2m":                 {"label": "2 m Temperature", "key": "shield_t2m"},
    "CAPE":                  {"label": "Surface-Based CAPE", "key": "shield_cape"},
    "precip_accum":          {"label": "Total Accumulated Precipitation", "key": "shield_precip"},
    "simulatedir":           {"label": "Simulated IR Satellite", "key": "shield_simir"},
    "pwat_slp_wind850":      {"label": "PWAT + MSLP + 850 mb Wind", "key": "shield_pwat"},
    "h500_slp":              {"label": "500 mb Height + MSLP", "key": "shield_h500"},
    "vort500_hgt_wind":      {"label": "500 mb Vorticity + Heights + Wind", "key": "shield_vort500"},
    "uh25max_swath":         {"label": "Max Updraft Helicity 2-5 km", "key": "shield_uh"},
    "shr06":                 {"label": "0-6 km Bulk Shear", "key": "shield_shear"},
    "maxwind10m":            {"label": "Maximum 10 m Wind", "key": "shield_gust"},
}
SHIELD_REGIONS = {"CONUS": "Continental US", "nestNE": "Northeastern US",
                  "nestMA": "Mid-Atlantic US", "nestSE": "Southeastern US",
                  "nestSW": "Southwestern US", "nestNC": "N Central US",
                  "us_sc": "S Central US", "nestNW": "Northwestern US",
                  "tcNATL": "Atlantic"}


# ----------------------------------------------------------------- caching
_CACHE = {}
_LOCK = threading.Lock()
_TTL = 900  # 15 min for endpoint listings


def _cached(key, fn):
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    val = fn()
    with _LOCK:
        _CACHE[key] = (time.time(), val)
    return val


def _get(url, params=None, timeout=25):
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def _post(url, data, timeout=25):
    r = requests.post(url, data=data, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


# ----------------------------------------------------------------- MPAS
def _mpas_latest_init():
    """Newest archived MPAS init that still returns frames (demo ended 2025-10)."""
    def _probe():
        # the fall-2025 archive ran Sept-Oct 2025; walk back from Oct 14
        for i in range(20):
            d = dt.datetime(2025, 10, 14) - dt.timedelta(days=i)
            stamp = d.strftime("%Y%m%d") + "00"
            try:
                r = _post(MPAS_GET, {"d": stamp, "v": "wspd", "o": "conus", "m": "mpas"})
                txt = r.text.strip()
                if txt and txt != '"BLANK"':
                    pics = json.loads(txt)
                    if isinstance(pics, list) and pics:
                        return stamp
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        return None
    return _cached("mpas_init", _probe)


def mpas_frames(var, domain="conus"):
    """Frame list for one MPAS var/domain -> [{'hour', 'url', 'label'}]."""
    def _fetch():
        init = _mpas_latest_init()
        if not init:
            return []
        try:
            r = _post(MPAS_GET, {"d": init, "v": var, "o": domain, "m": "mpas"})
            txt = r.text.strip()
            if not txt or txt == '"BLANK"':
                return []
            pics = json.loads(txt)
        except Exception:  # noqa: BLE001
            return []
        out = []
        for p in pics:
            m = re.search(r"fcst(\d+)hr", p)
            if not m:
                continue
            fh = int(m.group(1))
            # the API returns site-absolute paths; only prefix the host
            url = p if p.startswith("http") else "https://project.mmm.ucar.edu" + p
            out.append({
                "hour": fh,
                "url": url,
                "label": f"F{fh:03d}",
                "init": init,
            })
        out.sort(key=lambda f: f["hour"])
        return out
    return _cached(f"mpas_{var}_{domain}", _fetch)


def _download(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 5_000:
        return dest
    r = _get(url, timeout=40)
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, dest)
    return dest


def mpas_product(var, domain="conus", max_frames=25):
    """Download (cached) the newest frames for one MPAS product.

    Returns {'product', 'label', 'domain', 'init', 'frames': [{'hour','file','label'}]}
    or None. Frames are pre-sampled: latest `max_frames` hours to keep the loop snappy.
    """
    frames = mpas_frames(var, domain)
    if not frames:
        return None
    init = frames[0]["init"]
    sub = frames[-max_frames:]
    out = []
    for f in sub:
        fname = f"mpas_{var}_{domain}_f{f['hour']:03d}_{init}.jpg"
        dest = os.path.join(STATIC_DIR, fname)
        try:
            _download(f["url"], dest)
        except Exception:  # noqa: BLE001 - skip failed frame, keep the loop
            continue
        valid = dt.datetime.strptime(init, "%Y%m%d%H") + dt.timedelta(hours=f["hour"])
        out.append({
            "hour": f["hour"],
            "file": f"/app/static/aimodels/{fname}",
            "label": f"F{f['hour']:03d} \u00b7 {valid:%a %HZ}",
        })
    if not out:
        return None
    return {
        "product": MPAS_PRODUCTS[var]["key"],
        "label": f"MPAS {MPAS_PRODUCTS[var]['label']} ({MPAS_DOMAINS.get(domain, domain)})",
        "init": init,
        "frames": out,
    }


# ----------------------------------------------------------------- SHiELD
def _shield_inits():
    def _fetch():
        try:
            r = _get(SHIELD_BASE + "model-ajax.php", params={"model": SHIELD_MODEL})
            return [x for x in r.text.split(",") if x.strip()]
        except Exception:  # noqa: BLE001
            return []
    return _cached("shield_inits", _fetch)


def shield_latest_init():
    inits = _shield_inits()
    return inits[0] if inits else None


def shield_frames(field, region="CONUS", ymdh=None):
    """Frame URL list for one SHiELD field/region at one init."""
    def _fetch():
        init = shield_latest_init()
        if not init:
            return []
        try:
            r = _get(SHIELD_BASE + "get-images-ajax.php",
                     params={"model": SHIELD_MODEL, "ymdh": init, "region": region, "field": field})
            frames = [f for f in r.text.split(",") if f.strip()]
        except Exception:  # noqa: BLE001
            return []
        out = []
        for f in frames:
            m = re.search(r"-(\d{3})\.jpg$", f)
            if not m:
                continue
            fh = int(m.group(1))
            out.append({"hour": fh, "url": SHIELD_BASE + f, "label": f"F{fh:03d}", "init": init})
        out.sort(key=lambda x: x["hour"])
        return out
    return _cached(f"shield_{field}_{region}_latest", _fetch)


def shield_product(field, region="CONUS", max_frames=25):
    """Download (cached) newest frames for one SHiELD product."""
    frames = shield_frames(field, region)
    if not frames:
        return None
    init = frames[0]["init"]
    sub = frames[-max_frames:]
    out = []
    for f in sub:
        fname = f"shield_{field}_{region}_f{f['hour']:03d}_{init}.jpg"
        dest = os.path.join(STATIC_DIR, fname)
        try:
            _download(f["url"], dest)
        except Exception:  # noqa: BLE001
            continue
        valid = dt.datetime.strptime(init, "%Y%m%d%H") + dt.timedelta(hours=f["hour"])
        out.append({
            "hour": f["hour"],
            "file": f"/app/static/aimodels/{fname}",
            "label": f"F{f['hour']:03d} \u00b7 {valid:%a %HZ}",
        })
    if not out:
        return None
    return {
        "product": SHIELD_PRODUCTS[field]["key"],
        "label": f"SHiELD-FV3 {SHIELD_PRODUCTS[field]['label']} ({SHIELD_REGIONS.get(region, region)})",
        "init": init,
        "frames": out,
    }


def shield_fields(region="CONUS"):
    """Field catalog for one region at the latest init -> {key: label}."""
    def _fetch():
        ymdh = shield_latest_init()
        if not ymdh:
            return {}
        try:
            r = _get(SHIELD_BASE + "region-json-ajax.php",
                     params={"model": SHIELD_MODEL, "ymdh": ymdh, "region": region})
            return json.loads(r.text)
        except Exception:  # noqa: BLE001
            return {}
    return _cached(f"shield_fields_{region}", _fetch)

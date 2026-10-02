"""CIRA / NOAA-GSL AI Weather Prediction (AIWP): GraphCast, Pangu-Weather and
FourCastNet v2 initialized from NOAA GFS, published keylessly on the
noaa-oar-mlwp-data bucket (https://registry.opendata.aws/aiwp).

Layout: one ~5 GB NetCDF per initialization holding every 6-hourly step
f000-f240, so files are opened IN PLACE over HTTP (fsspec -> h5py ->
h5netcdf) and each map only pulls the few 4 MB latitude/longitude planes
it needs. Updated twice daily (00Z/12Z), published a few hours after init.
"""
import datetime as dt
import io
import re
import threading
import time

import numpy as np
import requests

UA = {"User-Agent": "Mozilla/5.0"}
BUCKET = "https://noaa-oar-mlwp-data.s3.amazonaws.com"

# map-model key -> bucket directory code
AIWP_CODES = {
    "AI-GraphCast": "GRAP_v100_GFS",
    "AI-Pangu": "PANG_v100_GFS",
    "AI-FourCastNet": "FOUR_v200_GFS",
    "AI-Aurora": "AURO_v100_GFS",
}

_LOCK = threading.Lock()
_OPEN_FILES = {}      # url -> h5netcdf File (keep the two most recent)
_CYCLE_CACHE = {}     # model -> (monotonic ts, cycle or None)


def _file_url(model, cycle):
    code = AIWP_CODES[model]
    return (f"{BUCKET}/{code}/{cycle:%Y}/{cycle:%m%d}/"
            f"{code}_{cycle:%Y%m%d%H}_f000_f240_06.nc")


def find_cycle(model, max_back_days=10):
    """Latest AIWP init (00Z/12Z, published ~4-6 h after) with a live file."""
    code = AIWP_CODES[model]
    now = time.monotonic()
    hit = _CYCLE_CACHE.get(model)
    if hit and now - hit[0] < 900:
        return hit[1]
    cycle = None
    today = dt.datetime.now(dt.timezone.utc)
    for back in range(max_back_days):
        day = today - dt.timedelta(days=back)
        day_dir = f"{code}/{day:%Y/%m%d}/"
        try:
            r = requests.get(f"{BUCKET}/?list-type=2&prefix={day_dir}&max-keys=20",
                             headers=UA, timeout=20)
            if not r.ok:
                continue
            keys = [k.rsplit("/", 1)[-1] for k in
                    _keys_from_listing(r.text) if k.endswith(".nc")]
        except requests.RequestException:
            continue
        for hh in (12, 0):
            for k in keys:
                if k.endswith(f"_{day:%Y%m%d}{hh:02d}_f000_f240_06.nc"):
                    # published a few hours after init; skip too-fresh inits
                    if dt.datetime.now(dt.timezone.utc) >= \
                            day.replace(hour=hh, minute=0, second=0, microsecond=0,
                                        tzinfo=dt.timezone.utc) + dt.timedelta(hours=4):
                        cycle = day.replace(hour=hh, minute=0, second=0, microsecond=0,
                                            tzinfo=dt.timezone.utc)
                        break
            if cycle is not None:
                break
        if cycle is not None:
            break
    _CYCLE_CACHE[model] = (time.monotonic(), cycle)
    return cycle


def _dewpoint_from_e(e):
    """Dew point (K) from vapor pressure (Pa) - inverted Magnus/Tetens."""
    e = np.clip(np.asarray(e, dtype=float), 1.0, None)
    return 243.04 * np.log(e / 611.2) / (17.67 - np.log(e / 611.2)) + 273.15


def _keys_from_listing(xml_text):
    import re
    return re.findall(r"<Key>(.*?)</Key>", xml_text)


def _open(url):
    """Open one AIWP NetCDF in place; keeps the two most recent handles."""
    with _LOCK:
        hit = _OPEN_FILES.get(url)
        if hit is not None:
            return hit
    import fsspec
    import h5netcdf
    import h5py
    fobj = fsspec.open(url, "rb", block_size=8 * 1024 * 1024).open()
    hf = h5netcdf.File(h5py.File(fobj, "r"), "r")
    with _LOCK:
        while len(_OPEN_FILES) >= 2:
            oldest = next(iter(_OPEN_FILES))
            try:
                _OPEN_FILES.pop(oldest).close()
            except Exception:  # noqa: BLE001 - a dead handle must not block
                _OPEN_FILES.pop(oldest, None)
        _OPEN_FILES[url] = hf
    return hf


# pressure levels the AIWP files carry (verified in the live NetCDF headers
# 2026-09-22): products named <level>_<thing> map their level straight onto
# the u/v/t/z/q planes - no per-product branches needed for new levels.
_LEVELS = {925, 850, 700, 600, 500, 300, 250, 200}


def fetch_fields(model, cycle, fh, product):
    """Read one forecast plane for a product; returns model_maps-style dict.

    Field planes are contiguous in the file, so each is a single ~4 MB read.
    """
    hf = _open(_file_url(model, cycle))
    idx = fh // 6
    lv = np.array(hf.variables["level"][:])

    def plane(var_name, level=None, t_index=idx):
        var = hf.variables[var_name]
        if level is None:
            return np.array(var[t_index, :, :], dtype=float)
        li = int(np.abs(lv - level).argmin())
        return np.array(var[t_index, li, :, :], dtype=float)

    fields = {}
    m = re.match(r"^(\d{3})_", product or "")
    level = int(m.group(1)) if m and int(m.group(1)) in _LEVELS else None

    if level is not None:
        # every <level>_* chart: temps/vorticity at 500/600/700/850/925,
        # jet stream at 300/250/200 - same four planes, different level
        fields["HGT"] = plane("z", level) / 9.80665   # geopotential -> height (m)
        fields["TMP"] = plane("t", level)
        fields["UGRD"] = plane("u", level)
        fields["VGRD"] = plane("v", level)
        if product == "700_w":
            # omega must ride THIS branch: the old bare `elif product ==
            # "700_w"` below was dead code, shadowed by the level branch
            # (700 is in _LEVELS), so every AI 700_w shipped a temp chart
            # and the render raised 'no omega field decoded' (2026-10-02)
            if "w" not in hf.variables:
                return None      # Pangu/Aurora ship no omega at all
            fields["VVEL"] = plane("w", 700)
        if product in ("700_rh", "600_rh"):
            # FourCastNet ships RH directly ('r', 0-1); the others carry
            # specific humidity 'q' (kg/kg) - convert via vapor pressure
            vname = "r" if "r" in hf.variables else ("q" if "q" in hf.variables else None)
            if vname is None:
                return None
            vals = plane(vname, level)
            if vname == "r":
                fields["RH"] = np.clip(vals * (100.0 if np.nanmax(vals) <= 1.5 else 1.0), 0, 100)
            else:
                p_pa = level * 100.0
                # exact from mixing ratio w = q/(1-q): e = w*p/(0.622+w)
                e = vals * p_pa / (0.622 + 0.378 * vals)
                es = 611.2 * np.exp(17.67 * (fields["TMP"] - 273.15) / (fields["TMP"] - 29.65))
                fields["RH"] = np.clip(100.0 * e / es, 0, 100)
    elif product == "shear06":
        # 0-6 km bulk shear proxy: |V500 - V10m| (kt)
        fields["UGRD@10 m above ground"] = plane("u10")
        fields["VGRD@10 m above ground"] = plane("v10")
        fields["UGRD@500 mb"] = plane("u", 500)
        fields["VGRD@500 mb"] = plane("v", 500)
        fields["PRMSL"] = plane("msl")
    elif product == "lr75":
        # 700-500 mb lapse rate inputs: temps + heights at both levels
        fields["TMP"] = plane("t", 700)
        fields["TMP@500 mb"] = plane("t", 500)
        fields["HGT"] = plane("z", 700) / 9.80665
        fields["HGT@500 mb"] = plane("z", 500) / 9.80665
    elif product == "850_vort":
        # tropical low-level spin: 850 heights/winds (vorticity is derived
        # in the shared render branch)
        fields["HGT"] = plane("z", 850) / 9.80665
        fields["UGRD"] = plane("u", 850)
        fields["VGRD"] = plane("v", 850)
    elif product == "200_div":
        # tropical upper outflow: 200 heights/winds (divergence derived in
        # the shared render branch)
        fields["HGT"] = plane("z", 200) / 9.80665
        fields["UGRD"] = plane("u", 200)
        fields["VGRD"] = plane("v", 200)
    elif product == "3var_fronts":
        # surface analysis composite: isobars + thickness + 2 m temps
        fields["PRMSL"] = plane("msl")
        fields["HGT"] = plane("z", 500) / 9.80665
        fields["HGT@1000 mb"] = plane("z", 1000) / 9.80665
        fields["UGRD"] = plane("u10")
        fields["VGRD"] = plane("v10")
        fields["TMP"] = plane("t2")
    elif product == "thickness":
        # 1000-500 mb thickness for the rain/snow line - same suffixed-key
        # convention model_maps uses so the render branch is shared
        fields["HGT"] = plane("z", 500) / 9.80665
        fields["HGT@1000 mb"] = plane("z", 1000) / 9.80665
    elif product == "sfc_dew":
        # AI files carry no 2 m dew point - derive the vapor pressure from
        # whichever moisture variable the file ships (q most models, r on
        # FourCastNet) at 1000 hPa as the near-surface proxy, with 2 m temp
        t2 = plane("t2")
        if "q" in hf.variables:
            q = plane("q", 1000)
            e = q * 100000.0 / (0.622 + 0.378 * q)      # vapor pressure (Pa)
        else:
            rh = plane("r", 1000)
            if np.nanmax(rh) <= 1.5:
                rh = rh * 100.0                          # fraction -> percent
            es = 611.2 * np.exp(17.67 * (t2 - 273.15) / (t2 - 29.65))
            e = np.clip(rh, 1.0, 100.0) / 100.0 * es
        fields["DPT"] = _dewpoint_from_e(e)
        fields["PRMSL"] = plane("msl")
        fields["UGRD"] = plane("u10")
        fields["VGRD"] = plane("v10")
    elif product == "sfc_mslp":
        fields["PRMSL"] = plane("msl")
        fields["UGRD"] = plane("u10")
        fields["VGRD"] = plane("v10")
        fields["TMP"] = plane("t2")
    elif product == "pwat":
        if "tcwv" in hf.variables:          # FourCastNet
            fields["PWAT"] = plane("tcwv")
            # synoptic overlay: labeled MSLP isobars + 10 m wind barbs
            if "msl" in hf.variables:
                fields["PRMSL"] = plane("msl")
            if "u10" in hf.variables and "v10" in hf.variables:
                fields["UGRD"], fields["VGRD"] = plane("u10"), plane("v10")
        elif "q" in hf.variables:
            # GraphCast (and Pangu/Aurora) ships specific humidity at 13
            # pressure levels instead of tcwv - integrate q over pressure
            # (trapezoid, surface -> 300 hPa) for precipitable water:
            # PWAT = (1/g) SUM q*dp in kg/m^2 = mm. This used to return
            # None (no tcwv) and wall-fail every GraphCast pwat render
            # with 'No decodable data' (2026-10-02).
            ps = (1000, 925, 850, 700, 600, 500, 400, 300)
            pw = np.zeros(plane("q", ps[0]).shape, dtype=float)
            for k in range(len(ps) - 1):
                q1, q2 = plane("q", ps[k]), plane("q", ps[k + 1])
                pw = pw + 0.5 * (q1 + q2) * ((ps[k] - ps[k + 1]) * 100.0) / 9.80665
            fields["PWAT"] = pw
            if "msl" in hf.variables:
                fields["PRMSL"] = plane("msl")
            if "u10" in hf.variables and "v10" in hf.variables:
                fields["UGRD"], fields["VGRD"] = plane("u10"), plane("v10")
        else:
            return None
    elif product == "ai_precip":
        if "apcp" not in hf.variables:      # GraphCast only
            return None
        fields["APCP"] = plane("apcp")
        if "msl" in hf.variables:
            fields["PRMSL"] = plane("msl")
        if "u10" in hf.variables and "v10" in hf.variables:
            fields["UGRD"], fields["VGRD"] = plane("u10"), plane("v10")
    else:
        return None

    lat1 = np.array(hf.variables["latitude"][:], dtype=float)
    lon1 = np.array(hf.variables["longitude"][:], dtype=float)
    # pressure levels below terrain carry the NetCDF _FillValue (9.97e36) -
    # contourf turns those into degenerate geometries and cartopy throws
    # 'getX called on empty Point' (AI-Aurora 600 mb, 2026-09-22)
    fields = {k: np.where(np.abs(v) > 1e10, np.nan, v) for k, v in fields.items()}
    if lat1[0] > lat1[-1]:                  # flip to ascending for MetPy deltas
        lat1 = lat1[::-1]
        fields = {k: v[::-1, :] for k, v in fields.items()}
    lon2, lat2 = np.meshgrid(lon1, lat1)
    fields = {k: v[::2, ::2] for k, v in fields.items()}
    return {"fields": fields, "lat": lat2[::2, ::2], "lon": lon2[::2, ::2]}

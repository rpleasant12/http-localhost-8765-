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
    if product in ("500_vort", "500_tmp"):
        level = 500
    elif product == "850_tmp":
        level = 850
    else:
        level = None

    if product in ("500_vort", "500_tmp", "850_tmp"):
        fields["HGT"] = plane("z", level) / 9.80665   # geopotential -> height (m)
        fields["TMP"] = plane("t", level)
        fields["UGRD"] = plane("u", level)
        fields["VGRD"] = plane("v", level)
    elif product == "sfc_mslp":
        fields["PRMSL"] = plane("msl")
        fields["UGRD"] = plane("u10")
        fields["VGRD"] = plane("v10")
        fields["TMP"] = plane("t2")
    elif product == "pwat":
        if "tcwv" in hf.variables:          # FourCastNet
            fields["PWAT"] = plane("tcwv")
        else:
            return None
    elif product == "ai_precip":
        if "apcp" not in hf.variables:      # GraphCast only
            return None
        fields["APCP"] = plane("apcp")
    else:
        return None

    lat1 = np.array(hf.variables["latitude"][:], dtype=float)
    lon1 = np.array(hf.variables["longitude"][:], dtype=float)
    if lat1[0] > lat1[-1]:                  # flip to ascending for MetPy deltas
        lat1 = lat1[::-1]
        fields = {k: v[::-1, :] for k, v in fields.items()}
    lon2, lat2 = np.meshgrid(lon1, lat1)
    fields = {k: v[::2, ::2] for k, v in fields.items()}
    return {"fields": fields, "lat": lat2[::2, ::2], "lon": lon2[::2, ::2]}

"""Forecast-model point series - GFS, NAM, RAP, HRRR, ECMWF, GEFS, CFS, NBM.

All keyless, read straight from NOAA/ECMWF AWS open-data buckets via byte-range
fetches (idx trick) or COG range reads, decoded locally with cfgrib/rasterio
and interpolated to a point. Disk-cached per (model, variable, cycle, location).

Styles:
  ncep  - classic .idx byte-range GRIB2 (GFS, NAM, RAP, HRRR, GEFS)
  ecmwf - per-step GRIB2 + JSON-lines index (ecmwf-forecasts bucket)
  cfs   - NCEP .idx but file names carry the VALID time (flxfYYYYMMDDHH...)
  nbm   - NBM blend v5.0 hourly Cloud-Optimized GeoTIFFs (rasterio /vsicurl)
"""
import datetime as dt
import hashlib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import time

import requests

from data.herbie_client import HERBIE_MODELS as _HERBIE_MODELS

# cfgrib logs every mismatched-message blob at ERROR with full tracebacks; keep logs clean
logging.getLogger("cfgrib").setLevel(logging.CRITICAL)

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}

CACHE_DIR = os.path.join("static", "model_cache")

# ---------------------------------------------------------------- model defs
# var spec: {"msgs": [(idx shortName, level substring), ...]  (2 msgs + combine="speed" for wind),
#            "label", "unit", "convert": k2f|direct_f|ms2mph|pa2inhg|dam|geo2dam|none,
#            "decode": expected cfgrib shortName (optional, fallback = first var)}
MODELS = {
    "HRRR": {
        "label": "HRRR (3 km, out 18 h, hourly)",
        "family": "CAM",
        "style": "ncep",
        "base": "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{c:%Y%m%d}/conus/hrrr.t{c:%H}z.wrfsfcf",
        "step_fmt": "{fh:02d}",
        "suffix": ".grib2",
        "cycles_hours": [0, 1, 2, 3, 4, 5],
        "step_minutes": 60,
        "max_hours": 18,
        "vars": {
            "temp": {"msgs": [("TMP", "surface")], "label": "Temperature (2 m)", "unit": "\u00b0F", "convert": "k2f"},
            "wind": {"msgs": [("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
                     "label": "Wind speed (10 m)", "unit": "mph", "convert": "ms2mph", "combine": "speed"},
            "gust": {"msgs": [("GUST", "surface")], "label": "Wind gusts", "unit": "mph", "convert": "ms2mph"},
            "cape": {"msgs": [("CAPE", "surface")], "label": "CAPE (storm energy)", "unit": "J/kg", "convert": "none"},
            "mslp": {"msgs": [("MSLMA", "mean sea level")], "label": "Mean sea-level pressure", "unit": "inHg", "convert": "pa2inhg"},
        },
    },
    "RAP": {
        "label": "RAP (13 km, out 18 h, hourly)",
        "family": "CAM",
        "style": "ncep",
        "base": "https://noaa-rap-pds.s3.amazonaws.com/rap.{c:%Y%m%d}/rap.t{c:%H}z.awip32f",
        "step_fmt": "{fh:02d}",
        "suffix": ".grib2",
        "cycles_hours": [0, 1],
        "step_minutes": 60,
        "max_hours": 18,
        "vars": {
            "temp": {"msgs": [("TMP", "2 m above ground")], "label": "Temperature (2 m)", "unit": "\u00b0F", "convert": "k2f"},
            "wind": {"msgs": [("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
                     "label": "Wind speed (10 m)", "unit": "mph", "convert": "ms2mph", "combine": "speed"},
            "gust": {"msgs": [("GUST", "surface")], "label": "Wind gusts", "unit": "mph", "convert": "ms2mph"},
            "cape": {"msgs": [("CAPE", "surface")], "label": "CAPE (storm energy)", "unit": "J/kg", "convert": "none"},
            "mslp": {"msgs": [("PRMSL", "mean sea level"), ("MSLMA", "mean sea level")],
                     "label": "Mean sea-level pressure", "unit": "inHg", "convert": "pa2inhg"},
        },
    },
    "NAM": {
        "label": "NAM (12 km, out 36 h, hourly)",
        "family": "Regional",
        "style": "ncep",
        "base": "https://noaa-nam-pds.s3.amazonaws.com/nam.{c:%Y%m%d}/nam.t{c:%H}z.awphys",
        "step_fmt": "{fh:02d}",
        "suffix": ".tm00.grib2",
        "cycles_hours": [0, 1, 2, 3, 4, 5, 6, 7, 8],
        "step_minutes": 60,
        "max_hours": 36,
        "vars": {
            "temp": {"msgs": [("TMP", "2 m above ground")], "label": "Temperature (2 m)", "unit": "\u00b0F", "convert": "k2f"},
            "wind": {"msgs": [("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
                     "label": "Wind speed (10 m)", "unit": "mph", "convert": "ms2mph", "combine": "speed"},
            "gust": {"msgs": [("GUST", "surface")], "label": "Wind gusts", "unit": "mph", "convert": "ms2mph"},
            "cape": {"msgs": [("CAPE", "surface")], "label": "CAPE (storm energy)", "unit": "J/kg", "convert": "none"},
            "mslp": {"msgs": [("PRMSL", "mean sea level"), ("MSLMA", "mean sea level")],
                     "label": "Mean sea-level pressure", "unit": "inHg", "convert": "pa2inhg"},
        },
    },
    "GFS": {
        "label": "GFS (0.25\u00b0, out 72 h, hourly)",
        "family": "Global",
        "style": "ncep",
        "base": "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{c:%Y%m%d}/{c:%H}/atmos/gfs.t{c:%H}z.pgrb2.0p25.f",
        "step_fmt": "{fh:03d}",
        "suffix": "",
        "cycles_hours": [0, 1, 2, 3, 4, 5, 6, 7, 8],
        "step_minutes": 60,
        "max_hours": 72,
        "vars": {
            "temp": {"msgs": [("TMP", "2 m above ground")], "label": "Temperature (2 m)", "unit": "\u00b0F", "convert": "k2f"},
            "wind": {"msgs": [("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
                     "label": "Wind speed (10 m)", "unit": "mph", "convert": "ms2mph", "combine": "speed"},
            "gust": {"msgs": [("GUST", "surface")], "label": "Wind gusts", "unit": "mph", "convert": "ms2mph"},
            "cape": {"msgs": [("CAPE", "surface")], "label": "CAPE (storm energy)", "unit": "J/kg", "convert": "none"},
            "mslp": {"msgs": [("PRMSL", "mean sea level"), ("MSLMA", "mean sea level")],
                     "label": "Mean sea-level pressure", "unit": "inHg", "convert": "pa2inhg"},
        },
    },
    "HREF": {
        "label": "HREF ensemble mean (3 km CAM ensemble, 3-hourly)",
        "family": "CAM",
        "style": "ncep",
        # HREF CONUS mean files (00z/12z cycles). Some hosts 403 this feed;
        # the app degrades to 'unavailable' until it answers.
        "base": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/href/prod/href.{c:%Y%m%d}/{c:%H}/href.t{c:%H}z.mean.3km.f",
        "step_fmt": "{fh:03d}",
        "suffix": ".conus.grib2",
        "cycles_hours": [12, 13, 14, 15, 16, 17],
        "probe_fh": 24,
        "step_minutes": 180,
        "max_hours": 48,
        "vars": {
            "temp": {"msgs": [("TMP", "2 m above ground")], "label": "Temperature (2 m) - ens mean", "unit": "\u00b0F", "convert": "k2f"},
            "cape": {"msgs": [("CAPE", "surface")], "label": "CAPE (storm energy) - ens mean", "unit": "J/kg", "convert": "none"},
            "mslp": {"msgs": [("MSLMA", "mean sea level"), ("PRMSL", "mean sea level")], "label": "Mean sea-level pressure - ens mean", "unit": "inHg", "convert": "pa2inhg"},
        },
    },
    "GEFS-Mean": {
        "label": "GEFS ensemble mean (0.5\u00b0, 3-hourly)",
        "family": "Ensemble",
        "style": "ncep",
        "base": "https://noaa-gefs-pds.s3.amazonaws.com/gefs.{c:%Y%m%d}/{c:%H}/atmos/pgrb2ap5/geavg.t{c:%H}z.pgrb2a.0p50.f",
        "step_fmt": "{fh:03d}",
        "suffix": "",
        "cycles_hours": [0, 1, 2, 3, 4, 5, 6, 7, 8],
        "probe_fh": 3,   # GEFS output is 3-hourly
        "step_minutes": 180,
        "max_hours": 120,
        "vars": {
            "temp": {"msgs": [("TMP", "2 m above ground")], "label": "Temperature (2 m) - ens mean", "unit": "\u00b0F", "convert": "k2f"},
            "wind": {"msgs": [("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
                     "label": "Wind speed (10 m) - ens mean", "unit": "mph", "convert": "ms2mph", "combine": "speed"},
            "mslp": {"msgs": [("PRMSL", "mean sea level"), ("MSLMA", "mean sea level")],
                     "label": "Mean sea-level pressure - ens mean", "unit": "inHg", "convert": "pa2inhg"},
            "h500": {"msgs": [("HGT", "500 mb")], "label": "500 mb height - ens mean", "unit": "dam", "convert": "dam"},
        },
    },
    "GEFS-Spread": {
        "label": "GEFS ensemble spread (forecast uncertainty)",
        "family": "Ensemble",
        "style": "ncep",
        "base": "https://noaa-gefs-pds.s3.amazonaws.com/gefs.{c:%Y%m%d}/{c:%H}/atmos/pgrb2ap5/gespr.t{c:%H}z.pgrb2a.0p50.f",
        "step_fmt": "{fh:03d}",
        "suffix": "",
        "cycles_hours": [0, 1, 2, 3, 4, 5, 6, 7, 8],
        "probe_fh": 3,   # GEFS output is 3-hourly
        "step_minutes": 180,
        "max_hours": 120,
        "vars": {
            "temp": {"msgs": [("TMP", "2 m above ground")], "label": "Temperature spread (2 m)", "unit": "\u00b0F", "convert": "kdelta2f"},
            "h500": {"msgs": [("HGT", "500 mb")], "label": "500 mb height spread", "unit": "dam", "convert": "dam"},
            "mslp": {"msgs": [("PRMSL", "mean sea level"), ("MSLMA", "mean sea level")],
                     "label": "MSLP spread", "unit": "inHg", "convert": "pa2inhg"},
        },
    },
    "ECMWF": {
        "label": "ECMWF IFS - Euro (0.25\u00b0, 3-hourly to 144 h)",
        "family": "Global",
        "style": "ecmwf",
        "base": "https://ecmwf-forecasts.s3.amazonaws.com/{c:%Y%m%d}/{c:%H}z/ifs/0p25/oper",
        "cycles_hours": list(range(7, 34)),   # published ~7 h after cycle time
        "step_minutes": 180,
        "max_hours": 144,
        "vars": {
            "temp": {"msgs": [("2t", "sfc")], "label": "Temperature (2 m)", "unit": "\u00b0F", "convert": "k2f"},
            "wind": {"msgs": [("10u", "sfc"), ("10v", "sfc")],
                     "label": "Wind speed (10 m)", "unit": "mph", "convert": "ms2mph", "combine": "speed"},
            "mslp": {"msgs": [("msl", "sfc")], "label": "Mean sea-level pressure", "unit": "inHg", "convert": "pa2inhg"},
            "h500": {"msgs": [("z", "pl:500")], "label": "500 mb geopotential height", "unit": "dam", "convert": "geo2dam"},
        },
    },
    "RRFS": {
        "label": "RRFS (3 km, out 18 h, hourly :45 samples)",
        "family": "CAM",
        "style": "ncep",
        # Live runs live on NOMADS (the noaa-rrfs-pds bucket only has retro data).
        # Sub-hourly files: fNNN holds the (NNN-1)h+15min step of the CONUS 3 km grid.
        "base": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/rrfs/v1.0/rrfs.{c:%Y%m%d}/{c:%H}/rrfs.t{c:%H}z.2dfld.3km.subh.f",
        "step_fmt": "{fh:03d}",
        "suffix": ".conus.grib2",
        "cycles_hours": [0, 1, 2, 3, 4],
        "step_minutes": 60,
        "step_base": 1,          # valid = cycle + (fh-1) h + valid_offset
        "valid_offset_min": 15,
        "max_hours": 18,
        "vars": {
            "temp": {"msgs": [("TMP", "2 m above ground")], "label": "Temperature (2 m)", "unit": "\u00b0F", "convert": "k2f"},
            "dewpoint": {"msgs": [("DPT", "2 m above ground")], "label": "Dewpoint (2 m)", "unit": "\u00b0F", "convert": "k2f"},
            "wind": {"msgs": [("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
                     "label": "Wind speed (10 m)", "unit": "mph", "convert": "ms2mph", "combine": "speed"},
            "gust": {"msgs": [("GUST", "surface")], "label": "Wind gusts", "unit": "mph", "convert": "ms2mph"},
        },
    },
    "CFS": {
        "label": "CFS v2 (0.5-1\u00b0, 6-hourly fluxes)",
        "family": "Global",
        "style": "cfs",
        "base": "https://noaa-cfs-pds.s3.amazonaws.com/cfs.{c:%Y%m%d}/{c:%H}/6hrly_grib_01/flxf",
        "cycles_hours": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
        "probe_fh": 0,   # flux files are named by valid time; f000 exists at cycle time
        "step_minutes": 360,
        "max_hours": 120,
        "vars": {
            "temp": {"msgs": [("TMP", "2 m above ground")], "label": "Temperature (2 m)", "unit": "\u00b0F", "convert": "k2f"},
            "wind": {"msgs": [("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
                     "label": "Wind speed (10 m)", "unit": "mph", "convert": "ms2mph", "combine": "speed"},
            "gust": {"msgs": [("GUST", "surface")], "label": "Wind gusts", "unit": "mph", "convert": "ms2mph"},
        },
    },
    "NBM": {
        "label": "NBM blend v5.0 (NWS National Blend of Models, hourly)",
        "family": "Blend",
        "style": "nbm",
        "base": "https://noaa-nbm-pds.s3.amazonaws.com/blendv5.0/conus",
        "max_hours": 36,
        "vars": {
            "temp": {"msgs": [("temp", None)], "label": "Temperature - blend of all models", "unit": "\u00b0F", "convert": "nbmtemp"},
            "dewpoint": {"msgs": [("dewpoint", None)], "label": "Dewpoint - blend of all models", "unit": "\u00b0F", "convert": "nbmtemp"},
            "sbcape": {"msgs": [("sbcape", None)], "label": "SBCAPE (storm energy)", "unit": "J/kg", "convert": "none"},
            "sky": {"msgs": [("sky", None)], "label": "Sky cover (%)", "unit": "%", "convert": "none"},
        },
    },
}

# Models compared in the ensemble fan chart (var: temp)
FAN_MODELS = ["HRRR", "RRFS", "RAP", "NAM", "GFS", "GEFS-Mean", "ECMWF", "NBM"]
FAN_SPREAD_MODEL = "GEFS-Spread"

# Menu grouping: families shown in this order, models alphabetical within
MODEL_FAMILIES = [
    ("CAM - convection-allowing, high-res", ["HRRR", "RRFS", "HREF", "RAP"]),
    ("Regional", ["NAM"]),
    ("Global", ["GFS", "ECMWF", "CFS"]),
    ("Ensemble", ["GEFS-Mean", "GEFS-Spread"]),
    ("Blend", ["NBM"]),
]


def grouped_model_list():
    """[(family_label, [model, ...])] for grouped selectboxes, live models only."""
    out = []
    for fam, names in MODEL_FAMILIES:
        live = [n for n in names if n in MODELS]
        if live:
            out.append((fam, live))
    return out


def model_info(model):
    return MODELS[model]


def _ls_prefixes(bucket, prefix):
    url = f"https://{bucket}/?list-type=2&prefix={prefix}&max-keys=50&delimiter=/"
    r = requests.get(url, headers=UA, timeout=15)
    return re.findall(r"<CommonPrefixes><Prefix>([^<]+)</Prefix>", r.text)


_CYCLE_CACHE = {}  # model -> (found_cycle_or_None, wallclock)


def find_cycle(model):
    """Most recent cycle datetime for which this model has data (cached 10 min)."""
    cached = _CYCLE_CACHE.get(model)
    if cached and (dt.datetime.now(dt.timezone.utc) - cached[1]).total_seconds() < 600:
        return cached[0]
    cycle = _find_cycle_uncached(model)
    _CYCLE_CACHE[model] = (cycle, dt.datetime.now(dt.timezone.utc))
    return cycle


def _find_cycle_uncached(model):
    """Most recent cycle datetime for which this model has data."""
    m = MODELS[model]
    now = dt.datetime.now(dt.timezone.utc)
    style = m.get("style", "ncep")

    if style == "nbm":
        for back in range(0, 3):
            day = (now - dt.timedelta(days=back))
            prefixes = _ls_prefixes("noaa-nbm-pds.s3.amazonaws.com",
                                    f"blendv5.0/conus/{day:%Y/%m/%d}/")
            inits = []
            for p in prefixes:
                hhmm = p.split("/")[-2]
                try:
                    t = dt.datetime.strptime(f"{day:%Y-%m-%d} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=dt.timezone.utc)
                except ValueError:
                    continue
                inits.append(t)
            inits = [t for t in inits if t <= now - dt.timedelta(minutes=100)]
            if inits:
                return max(inits)
        return None

    if style == "ecmwf":
        for back in m["cycles_hours"]:
            c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
            day, hh = c.strftime("%Y%m%d"), c.strftime("%H")
            idx = f"{m['base'].format(c=c)}/{day}{hh}0000-3h-oper-fc.index"
            try:
                if requests.head(idx, headers=UA, timeout=12).status_code == 200:
                    return c
            except requests.RequestException:
                continue
        return None

    probe = m.get("probe_fh", 1)
    for back in m["cycles_hours"]:
        c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        try:
            if requests.get(_idx_url(model, c, probe), headers=UA, timeout=15).ok:
                return c
        except requests.RequestException:
            continue
    return None


# ---------------------------------------------------------------- NCEP (.idx) helpers
def _file_url(model, cycle, fh):
    m = MODELS[model]
    if m.get("style") == "cfs":
        valid = cycle + dt.timedelta(hours=fh)
        return f"{m['base'].format(c=cycle)}{valid:%Y%m%d%H}.01.{cycle:%Y%m%d%H}.grb2"
    return f"{m['base'].format(c=cycle)}{m['step_fmt'].format(fh=fh)}{m['suffix']}"


def _idx_url(model, cycle, fh):
    return _file_url(model, cycle, fh) + ".idx"


def _find_range(idx_text, spec_msgs):
    """Byte ranges for each (shortName, level) spec.

    CFS .idx files use fractional start offsets (e.g. '36.1'); parse as float.
    """
    lines = idx_text.splitlines()
    out = []
    for short, level in spec_msgs:
        if level is None:
            out.append(None)
            continue
        lv = level.lower()
        rng = None
        for i, line in enumerate(lines):
            fields = line.split(":")
            if len(fields) > 4 and fields[3] == short and lv in ":".join(fields[4:]).lower():
                try:
                    start = int(float(fields[1]))
                except ValueError:
                    break
                end = start + 3_000_000
                if i + 1 < len(lines):
                    try:
                        nxt = int(float(lines[i + 1].split(":")[1]))
                        if nxt > start:
                            end = nxt
                    except ValueError:
                        pass
                rng = (start, end)
                break
        out.append(rng)
    return out


def _fetch_range(url, start, end):
    r = requests.get(url, headers={**UA, "Range": f"bytes={start}-{end - 1}"}, timeout=60)
    r.raise_for_status()
    return r.content


def _sample_point(glat, glon, values, lat, lon):
    from scipy.interpolate import griddata

    if glat.ndim == 1 and glon.ndim == 1:
        glon, glat = np.meshgrid(glon, glat)
    if values.ndim != 2 or glat.shape != values.shape:
        return None
    if np.nanmax(glon) > 180:
        glon = np.where(glon > 180, glon - 360, glon)
        lon_query = lon if lon < 180 else lon - 360
    else:
        lon_query = lon
    pts = np.column_stack([glat.ravel(), glon.ravel()])
    val = griddata(pts, np.asarray(values, dtype=float).ravel(), [[lat, lon_query]], method="nearest")[0]
    return float(val) if np.isfinite(val) else None


def _level_type(level_sub):
    """Map an idx level substring to the cfgrib typeOfLevel filter."""
    lv = (level_sub or "").lower()
    if lv.endswith("mb"):
        return "isobaricInhPa"
    if "mean sea level" in lv:
        return "meanSea"
    if "above ground" in lv:
        return "heightAboveGround"
    if lv == "surface":
        return "surface"
    return None


def _target_level(level_sub):
    lv = (level_sub or "").lower()
    if lv.endswith("mb"):
        return int(lv.replace("mb", ""))
    if "above ground" in lv:
        return int(lv.split()[0])
    return None


def _decode_blob(blob, want_short=None, level_sub=None):
    """cfgrib-decode one GRIB message blob -> (values2d, lat, lon).

    Blobs often contain sibling messages at other heights (e.g. CFS packs 2 m
    and 10 m fields together); filter by typeOfLevel and reduce 3-D fields to
    the level nearest the requested one.
    """
    import tempfile
    import xarray as xr

    tmp = os.path.join(tempfile.gettempdir(), f"tnwx_ser_{abs(hash(blob[:64])) % 999999}.grib2")
    try:
        with open(tmp, "wb") as f:
            f.write(blob)
        backend = {"indexpath": ""}
        tol = _level_type(level_sub)
        if tol:
            backend["filter_by_keys"] = {"typeOfLevel": tol}
        ds = xr.open_dataset(tmp, engine="cfgrib", backend_kwargs=backend)
        # eccodes names fields differently from idx shortNames (UGRD -> u10/u)
        aliases = {"UGRD": ("u10", "u"), "VGRD": ("v10", "v"), "TMP": ("t", "t2m", "2t"),
                   "HGT": ("gh", "z"), "RH": ("r",), "PRMSL": ("msl", "prmsl"),
                   "MSLMA": ("msl", "prmsl"), "PWAT": ("tcw",), "TCDC": ("tcc",),
                   "DPT": ("dpt", "2d"), "WEASD": ("sdwe", "snod")}
        cands = [want_short] + (list(aliases.get(want_short, ())) if want_short else [])
        var = next((c for c in cands if c and c in ds.data_vars), None)
        if var is None:
            var = next((v for v in ds.data_vars
                        if np.asarray(ds[v].values).ndim >= 2 and v not in ("latitude", "longitude")), None)
        arr = np.asarray(ds[var].values, dtype=float)
        if arr.ndim == 3:
            coord = next((c for c in ("isobaricInhPa", "heightAboveGround")
                          if c in ds[var].coords or c in ds.coords), None)
            if coord is not None:
                levels = np.asarray(ds[coord].values, dtype=float)
                arr = arr[int(np.argmin(np.abs(levels - (_target_level(level_sub) or levels[0]))))]
            else:
                arr = arr[0]
        values = np.squeeze(arr)
        glat = np.asarray(ds["latitude"].values, dtype=float)
        glon = np.asarray(ds["longitude"].values, dtype=float)
        del ds
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return values, glat, glon


# ---------------------------------------------------------------- step fetchers
def _herbie_series_payload(model, cycle, fh, var):
    """Herbie-first point-series fetch -> [blob | None] aligned with var msgs.

    Herbie searches partner archives (AWS/NOMADS/Google/ECMWF mirror), so a
    single-bucket hiccup no longer drops steps. Returns None when Herbie
    can't serve the file - callers fall back to the legacy byte-range path.
    """
    try:
        from data.herbie_client import fetch_subset_blobs, herbie_kwargs
        if herbie_kwargs(model, cycle) is None:
            return None
        searches = []
        for short, level in var["msgs"]:
            if ":" in level:      # ECMWF-style "pl:500" -> ":t:500"
                searches.append(f":{short}:{level.split(':')[-1]}")
            else:                 # NCEP-style "10 m above ground"
                searches.append(f":{re.escape(short)}[^:]*:{re.escape(level)}")
        return fetch_subset_blobs(model, cycle, fh, searches, surface=True)
    except Exception:  # noqa: BLE001 - Herbie failures always fall back
        return None


def _fetch_step_ncep(model, cycle, fh, var):
    """[blob, ...] for each msg of var from one NCEP step file, or None."""
    try:
        it = requests.get(_idx_url(model, cycle, fh), headers=UA, timeout=20)
        if not it.ok:
            return None
        ranges = _find_range(it.text, var["msgs"])
        if not any(ranges):
            return None
        url = _file_url(model, cycle, fh)
        blobs = []
        for rng in ranges:
            if rng is None:
                blobs.append(None)
                continue
            blobs.append(_fetch_range(url, rng[0], rng[1]))
        return blobs
    except (requests.RequestException, OSError):
        return None


_ECMWF_IDX_CACHE = {}


_ECMWF_IDX_HOSTS = (
    "https://ecmwf-forecasts.s3.amazonaws.com",
    "https://data.ecmwf.int/forecasts",   # ECMWF mirror of the same tree
)


def _ecmwf_index(cycle, fh, model_dir="ifs/0p25/oper", tag="oper-fc"):
    """JSON-lines index for ONE step - ECMWF ships a separate index per step file.

    model_dir swaps the product directory: "ifs/0p25/oper" (physical IFS),
    "aifs-single/0p25/oper" (ECMWF's AI model) or "aifs-ens/0p25/enfo"
    (AI ensemble control) - all with identical file naming. tag is the
    filename product tag: "oper-fc" or "enfo-cf". The S3 bucket rate-limits
    the tiny index files (SlowDown 503s), so each host is retried before
    falling back to ECMWF's own mirror."""
    day, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    key = f"{day}{hh}-{fh}-{model_dir}"
    if key not in _ECMWF_IDX_CACHE:
        text = None
        for host in _ECMWF_IDX_HOSTS:
            url = (f"{host}/{day}/{hh}z/{model_dir}/"
                   f"{day}{hh}0000-{fh}h-{tag}.index")
            for attempt in range(4 if "s3" in host else 2):
                try:
                    r = requests.get(url, headers=UA, timeout=20)
                    if r.ok:
                        text = r.text
                        break
                except requests.RequestException:
                    pass
                time.sleep(1.5 + attempt)
            if text is not None:
                break
        _ECMWF_IDX_CACHE[key] = text
    return _ECMWF_IDX_CACHE[key]


def _ecmwf_entry(idx_text, param, levtype, levelist=None):
    for line in idx_text.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("param") == param and d.get("levtype") == levtype:
            if levelist is not None and str(d.get("levelist")) != str(levelist):
                continue
            return d
    return None


def _ecmwf_file_url(cycle, fh, model_dir="ifs/0p25/oper", tag="oper-fc"):
    day, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    return f"https://ecmwf-forecasts.s3.amazonaws.com/{day}/{hh}z/{model_dir}/{day}{hh}0000-{fh}h-{tag}.grib2"


def _fetch_step_ecmwf(cycle, fh, var):
    try:
        idx = _ecmwf_index(cycle, fh)
        if idx is None:
            return None
        url = _ecmwf_file_url(cycle, fh)
        blobs = []
        for short, level in var["msgs"]:
            levtype, _, levelist = (level.partition(":") + ("",))[:3]
            entry = _ecmwf_entry(idx, short, levtype or "sfc", levelist or None)
            if entry is None:
                blobs.append(None)
                continue
            o, ln = entry["_offset"], entry["_length"]
            blobs.append(_fetch_range(url, o, o + ln))
        return blobs
    except (requests.RequestException, KeyError):
        return None


# ---------------------------------------------------------------- NBM (COG) helpers
def _nbm_file(init, elem, valid_hour):
    v = init + dt.timedelta(hours=valid_hour)
    return (f"{MODELS['NBM']['base']}/{init:%Y/%m/%d}/{init:%H%M}/{elem}/"
            f"blendv5.0_conus_{elem}_{init:%Y-%m-%dT%H:%M}_{v:%Y-%m-%dT%H:%M}.tif")


# ---------------------------------------------------------------- unit conversion
def _convert(val, convert):
    if val is None or convert in (None, "none"):
        return val
    if convert == "k2f":
        return (val - 273.15) * 9 / 5 + 32
    if convert == "nbmtemp":
        return val  # NBM temp is already Fahrenheit (verified against the COG)
    if convert == "ms2mph":
        return val * 2.23694
    if convert == "kdelta2f":
        return val * 9 / 5  # a spread/delta in K, not an absolute temperature
    if convert == "pa2inhg":
        return val * 0.000295299
    if convert == "dam":
        return val / 10.0
    if convert == "geo2dam":
        return val / 9.80665 / 10.0
    return val


def _decode_series_step(model, cycle, fh, var_key, lat, lon, payload=None):
    """Decode one step -> (valid_datetime, value) or None."""
    m = MODELS[model]
    var = m["vars"][var_key]
    style = m.get("style", "ncep")

    if style == "nbm":
        return _decode_nbm_step(m, cycle, fh, var_key, lat, lon)

    if payload is None:
        return None
    blobs = payload
    vals = []
    want = var.get("decode")
    msg_levels = [m[1] for m in var["msgs"]]
    for i, blob in enumerate(blobs):
        if blob is None:
            vals.append(None)
            continue
        try:
            # packed messages (AWIPS "UGRD/VGRD") decode both siblings -
            # pin each slot to its requested short so U/V don't collide
            want_i = want or (var["msgs"][i][0] if i < len(var["msgs"]) else None)
            values, glat, glon = _decode_blob(blob, want_short=want_i,
                                              level_sub=msg_levels[i] if i < len(msg_levels) else None)
            v = _sample_point(glat, glon, values, lat, lon)
        except Exception:  # noqa: BLE001 - one bad step must not kill the series
            v = None
        vals.append(v)
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    if var.get("combine") == "speed":
        if len(vals) < 2:
            return None  # a lone u-component is meaningless as a speed
        val = float(np.hypot(vals[0], vals[1]))
    else:
        val = vals[0]
    val = _convert(val, var.get("convert"))
    if val is None:
        return None
    valid_min = (fh - m.get("step_base", 0)) * m["step_minutes"] + m.get("valid_offset_min", 0)
    return cycle + dt.timedelta(minutes=valid_min), val


def _decode_nbm_step(m, cycle, fh, var_key, lat, lon):
    import rasterio
    from pyproj import Transformer

    var = m["vars"][var_key]
    elem = var["msgs"][0][0]
    url = _nbm_file(cycle, elem, fh)
    try:
        with rasterio.open(f"/vsicurl/{url}") as src:
            tr = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            x, y = tr.transform(lon, lat)
            row, col = src.index(x, y)
            if not (0 <= row < src.height and 0 <= col < src.width):
                return None
            window_vals = src.read(1)
            val = float(window_vals[row, col])
            nodata = src.nodata
    except Exception:  # noqa: BLE001
        return None
    if nodata is not None and abs(val - nodata) < 1e-6:
        return None
    if not np.isfinite(val):
        return None
    val = _convert(val, var.get("convert"))
    return cycle + dt.timedelta(hours=fh), val


# ---------------------------------------------------------------- public API
def _steps_for(model, cycle, max_hours):
    m = MODELS[model]
    if m.get("style") == "ecmwf":
        return [fh for fh in range(0, 145, 3) if fh <= max_hours]
    if m.get("style") == "nbm":
        return list(range(1, min(max_hours, m["max_hours"]) + 1))
    return list(range(0, max_hours + 1, m["step_minutes"] // 60))


def get_series(model, var_key, lat, lon, max_hours=None, fresh=False, stride=1):
    """Point time series for one model + variable, disk-cached per cycle.

    Returns {'model','variable','unit','cycle','points': [{'t','v'}], 'error'}
    """
    m = MODELS[model]
    if var_key not in m["vars"]:
        return {"error": f"Unknown variable {var_key}"}
    var = m["vars"][var_key]
    max_hours = min(max_hours or m["max_hours"], m["max_hours"])
    cycle = find_cycle(model)
    if cycle is None:
        return {"error": f"{model} data unavailable (bucket unreachable or run not published yet)"}

    cache_key = hashlib.sha1(
        f"{model}|{var_key}|{cycle:%Y%m%d%H}|{lat:.2f}|{lon:.2f}|{max_hours}|{stride}".encode()
    ).hexdigest()[:16]
    cache_path = os.path.join(CACHE_DIR, cache_key + ".json")
    if not fresh:
        try:
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass

    steps = _steps_for(model, cycle, max_hours)
    if stride > 1:
        steps = steps[::stride]
        if steps and steps[0] != 0:
            steps.insert(0, 0)

    style = m.get("style", "ncep")

    def fetch(fh):
        try:
            if style == "ecmwf":
                payload = _herbie_series_payload(model, cycle, fh, var)
                if payload is None:
                    payload = _fetch_step_ecmwf(cycle, fh, var)
                return fh, payload
            if style == "nbm":
                return fh, "nbm"
            if model in _HERBIE_MODELS:
                payload = _herbie_series_payload(model, cycle, fh, var)
                if payload is not None:
                    return fh, payload
            return fh, _fetch_step_ncep(model, cycle, fh, var)
        except Exception:  # noqa: BLE001 - one bad step must not kill the series
            return fh, None

    with ThreadPoolExecutor(max_workers=6) as ex:
        fetched = list(ex.map(fetch, steps))

    points = []
    for fh, payload in fetched:
        r = _decode_series_step(model, cycle, fh, var_key, lat, lon, payload=payload)
        if r is None:
            continue
        t, v = r
        points.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "v": round(v, 2 if var_key == "mslp" else 1)})
    if not points:
        return {"error": f"No decodable {var['label']} steps for {model} (run may still be uploading)."}

    out = {
        "model": m["label"],
        "model_key": model,
        "variable": var["label"],
        "var_key": var_key,
        "unit": var["unit"],
        "cycle": cycle.strftime("%Y-%m-%d %H:%M UTC"),
        "points": points,
    }
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = cache_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f)
    os.replace(tmp, cache_path)
    return out


def get_ensemble_fan(lat, lon, var_key="temp", max_hours=48, fresh=False):
    """Multi-model comparison at a point: one series per FAN_MODELS model.

    Returns {'models': [series dicts], 'spread': GEFS spread series or None,
             'unit', 'errors': {model: msg}}
    """
    unit = MODELS["GFS"]["vars"]["temp"]["unit"] if var_key == "temp" else ""
    models_out, errors = [], {}
    for mk in FAN_MODELS:
        if var_key not in MODELS[mk]["vars"]:
            continue
        stride = 6 if MODELS[mk].get("step_minutes", 60) >= 180 else (3 if mk == "NBM" else 4)
        s = get_series(mk, var_key, lat, lon, max_hours=max_hours, fresh=fresh, stride=stride)
        if "error" in s:
            errors[mk] = s["error"]
        else:
            models_out.append(s)
    spread = get_series(FAN_SPREAD_MODEL, var_key, lat, lon, max_hours=max_hours, fresh=fresh, stride=6)
    if "error" in spread:
        errors[FAN_SPREAD_MODEL] = spread["error"]
        spread = None
    return {"models": models_out, "spread": spread, "unit": unit, "errors": errors}


def clear_model_cache():
    """Drop cached model series (e.g. when Refresh data is pressed)."""
    try:
        for name in os.listdir(CACHE_DIR):
            if name.endswith(".json"):
                os.remove(os.path.join(CACHE_DIR, name))
    except OSError:
        pass

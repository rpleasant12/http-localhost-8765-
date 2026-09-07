"""NWS forecast-model MAPS: classic weather maps (500 mb, 850 mb, jet, surface).

Reads GFS / NAM / RAP GRIB2 directly from NOAA's AWS open-data buckets via
.idx byte-range fetches, computes meteorological fields with MetPy
(vorticity, absolute vorticity), and renders synoptic-style maps with
matplotlib + Cartopy. No API keys anywhere.

Products: 500 mb heights+vorticity, 850 mb heights+temperature,
700 mb RH, 300 mb jet isotachs, surface MSLP+wind, CAPE.
"""
import datetime as dt
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
}
MAP_DIR = os.path.join("static", "model_maps")

# ---------------------------------------------------------------- models
from data.aiwp import AIWP_CODES  # noqa: E402 - needed by find_cycle/fetch below

MAP_MODELS = {
    "GFS": {
        "label": "GFS (0.25\u00b0 global)",
        "base": "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{c:%Y%m%d}/{c:%H}/atmos/gfs.t{c:%H}z.pgrb2.0p25.f",
        "step_fmt": "{fh:03d}",
        "suffix": "",
        "cycles": [0, 2, 4, 6, 8, 10],  # hours back; hours snap to 00/06/12/18
        "synoptic": True,
        "max_hour": 72,
        "hour_step": 3 if 73 else 1,  # hourly to f72
    },
    "NAM": {
        "label": "NAM (12 km CONUS)",
        "base": "https://noaa-nam-pds.s3.amazonaws.com/nam.{c:%Y%m%d}/nam.t{c:%H}z.awphys",
        "step_fmt": "{fh:02d}",
        "suffix": ".tm00.grib2",
        "cycles": [0, 2, 4, 6, 8, 10],  # hours back; hours snap to 00/06/12/18
        "synoptic": True,
        "max_hour": 36,
        "hour_step": 1,
    },
    "RAP": {
        "label": "RAP (13 km CONUS)",
        "base": "https://noaa-rap-pds.s3.amazonaws.com/rap.{c:%Y%m%d}/rap.t{c:%H}z.awip32f",
        "step_fmt": "{fh:02d}",
        "suffix": ".grib2",
        "cycles": [0, 1],
        "max_hour": 18,
        "hour_step": 1,
    },
    "GEFS": {
        "label": "GEFS ensemble mean (0.5\u00b0 global)",
        "base": "https://noaa-gefs-pds.s3.amazonaws.com/gefs.{c:%Y%m%d}/{c:%H}/atmos/pgrb2ap5/geavg.t{c:%H}z.pgrb2a.0p50.f",
        "step_fmt": "{fh:03d}",
        "suffix": "",
        "cycles": [0, 2, 4, 6, 8, 10],  # hours back; hours snap to 00/06/12/18
        "synoptic": True,
        "probe_fh": 3,          # GEFS output is 3-hourly
        "max_hour": 120,
        "hour_step": 3,
    },
    "GEFS-Spread": {
        "label": "GEFS ensemble spread (uncertainty)",
        "base": "https://noaa-gefs-pds.s3.amazonaws.com/gefs.{c:%Y%m%d}/{c:%H}/atmos/pgrb2ap5/gespr.t{c:%H}z.pgrb2a.0p50.f",
        "step_fmt": "{fh:03d}",
        "suffix": "",
        "cycles": [0, 2, 4, 6, 8, 10],  # hours back; hours snap to 00/06/12/18
        "synoptic": True,
        "probe_fh": 3,
        "max_hour": 120,
        "hour_step": 3,
    },
    "ECMWF": {
        "label": "ECMWF IFS - Euro (0.25\u00b0 global)",
        "base": "https://ecmwf-forecasts.s3.amazonaws.com/{c:%Y%m%d}/{c:%H}z/ifs/0p25/oper",
        "max_hour": 144,
        "hour_step": 3,
    },
    "RRFS": {
        "label": "RRFS (3 km CONUS CAM, hourly)",
        "base": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/rrfs/v1.0/rrfs.{c:%Y%m%d}/{c:%H}/rrfs.t{c:%H}z.prslev.3km.f",
        "step_fmt": "{fh:03d}",
        "suffix": ".conus.grib2",
        "cycles": [0, 1, 2, 3, 4],
        "max_hour": 18,
        "hour_step": 1,
    },
    "HRRR": {
        "label": "HRRR (3 km CONUS CAM, hourly)",
        "base": "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{c:%Y%m%d}/conus/hrrr.t{c:%H}z.wrfprsf",
        "step_fmt": "{fh:02d}",
        "suffix": ".grib2",
        "cycles": [0, 1, 2, 3],
        "max_hour": 18,
        "hour_step": 1,
    },
    "NBM": {
        "label": "NBM - National Blend of Models (COG mosaic)",
        "style": "nbm",
        "base": "https://noaa-nbm-pds.s3.amazonaws.com/blendv5.0/conus",
        "cycles": [0, 1, 2, 3, 4],
        "max_hour": 36,
        "hour_step": 3,
    },
    "CFS": {
        "label": "CFS v2 (0.5-1\u00b0 global, 6-hourly)",
        "style": "cfs",
        "base": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/cfs/prod/cfs.{c:%Y%m%d}/{c:%H}/6hrly_grib_01/",
        "cycles": [0, 2, 4, 6, 8, 10],  # hours back; hours snap to 00/06/12/18
        "probe_fh": 0,           # pgbf files are named by valid time; f000 = analysis
        "synoptic": True,
        "max_hour": 120,
        "hour_step": 6,
    },
    "AIFS": {
        "label": "ECMWF AIFS - AI model (0.25\u00b0 global)",
        "style": "aifs",
        "max_hour": 240,
        "hour_step": 6,          # 6-hourly out to +240 h
    },
    "AIFS-ENS": {
        "label": "ECMWF AIFS-ENS - AI ensemble control (0.25\u00b0 global)",
        "style": "aifs",
        "max_hour": 240,
        "hour_step": 6,
    },
    "AI-GraphCast": {
        "label": "GraphCast AI - GFS init (DeepMind, 0.25\u00b0 global)",
        "style": "aiwp",
        "max_hour": 240,
        "hour_step": 6,
    },
    "AI-Pangu": {
        "label": "Pangu-Weather AI - GFS init (Huawei, 0.25\u00b0 global)",
        "style": "aiwp",
        "max_hour": 240,
        "hour_step": 6,
    },
    "AI-FourCastNet": {
        "label": "FourCastNet v2 AI - GFS init (NVIDIA, 0.25\u00b0 global)",
        "style": "aiwp",
        "max_hour": 240,
        "hour_step": 6,
    },
    "HREF": {
        "label": "HREF CAM ensemble mean (3 km CONUS)",
        "base": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/href/prod/href.{c:%Y%m%d}/ensprod/href.t{c:%H}z.conus.mean.f",
        "step_fmt": "{fh:02d}",
        "suffix": ".grib2",
        "cycles": [0, 2, 4, 6, 8, 10],  # hours back; hours snap to 00/06/12/18
        "synoptic": True,
        "probe_fh": 1,
        "max_hour": 48,
        "hour_step": 1,
    },
    "REFS": {
        "label": "REFS CAM ensemble mean (3 km CONUS)",
        "base": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/refs/v1.0/refs.{c:%Y%m%d}/{c:%H}/ensprod/refs.t{c:%H}z.mean.f",
        "step_fmt": "{fh:02d}",
        "suffix": ".conus.grib2",
        "cycles": [0, 2, 4, 6, 8, 10],  # hours back; hours snap to 00/06/12/18
        "synoptic": True,
        "probe_fh": 1,
        "max_hour": 48,
        "hour_step": 1,
    },
}

# Which products each model supports (verified against each feed's .idx;
# REFC lives in HRRR's *surface* files, not its pressure-level files)
PRODUCTS_BY_MODEL = {
    "GFS": ["refc", "500_vort", "500_tmp", "850_tmp", "925_tmp", "700_rh", "300_jet", "250_jet",
            "200_jet", "sfc_mslp", "cape_wind", "mucape", "pwat", "sfc_gust", "tcdc", "vis",
            "snow", "prate"],
    "NAM": ["refc", "500_vort", "500_tmp", "850_tmp", "925_tmp", "700_rh", "300_jet", "250_jet",
            "200_jet", "sfc_mslp", "cape_wind", "pwat", "sfc_gust", "tcdc", "vis", "snow"],
    "RAP": ["refc", "500_vort", "500_tmp", "850_tmp", "925_tmp", "300_jet", "250_jet", "200_jet",
            "sfc_mslp", "cape_wind", "mucape", "pwat", "tcdc", "vis"],
    "GEFS": ["500_vort", "500_tmp", "850_tmp", "925_tmp", "700_rh", "300_jet", "250_jet",
             "200_jet", "sfc_mslp", "pwat", "tcdc"],
    "GEFS-Spread": ["h500_spread"],
    "ECMWF": ["500_vort", "500_tmp", "850_tmp", "300_jet", "250_jet", "sfc_mslp", "pwat", "tcdc"],
    "RRFS": ["500_vort", "500_tmp", "850_tmp", "925_tmp", "300_jet", "250_jet", "200_jet", "pwat"],
    "HRRR": ["refc", "500_vort", "500_tmp", "850_tmp", "925_tmp", "700_rh", "300_jet", "250_jet",
             "200_jet", "sfc_mslp", "cape_wind", "mucape", "pwat", "sfc_gust", "tcdc", "vis",
             "snow", "prate"],
    "NBM": ["nbm_temp", "nbm_gust", "nbm_cape", "nbm_refc"],
    "CFS": ["500_vort", "500_tmp", "850_tmp", "925_tmp", "700_rh", "300_jet", "250_jet", "200_jet"],
    "AIFS": ["500_vort", "500_tmp", "850_tmp", "300_jet", "sfc_mslp", "pwat", "tcdc"],
    "AIFS-ENS": ["500_vort", "500_tmp", "850_tmp", "300_jet", "250_jet", "sfc_mslp", "pwat", "tcdc"],
    "AI-GraphCast": ["500_vort", "500_tmp", "850_tmp", "sfc_mslp", "ai_precip"],
    "AI-Pangu": ["500_vort", "500_tmp", "850_tmp", "sfc_mslp"],
    "AI-FourCastNet": ["500_vort", "500_tmp", "850_tmp", "sfc_mslp", "pwat"],
    "HREF": ["cam_pmmn", "cam_mean_500", "cam_mean_srh", "cam_prob_uphl", "cam_prob_ltng",
             "mucape"],
    "REFS": ["cam_pmmn", "cam_mean_500", "cam_mean_srh", "cam_prob_uphl", "cam_prob_ltng",
             "cam_h500_spread", "mucape"],
}

# NBM COG element per product: (element_name, kind, cmap, units, levels_fn)
NBM_ELEMENTS = {
    "nbm_temp":  ("temp", "c", "RdBu_r", "Temperature (\u00b0F)", None),
    "nbm_gust":  ("windgust", "c", "YlOrRd", "Wind gust (mph)", None),
    "nbm_cape":  ("sbcape", "c", "YlOrRd", "SBCAPE (J/kg)", None),
    "nbm_refc":  ("maxref", "c", "turbo", "Max simulated reflectivity (dBZ)", None),
}


# ECMWF open-data params per product: (param, "pl:<hPa>" or "sfc")
ECMWF_PARAMS = {
    "500_vort": [("z", "pl:500"), ("u", "pl:500"), ("v", "pl:500")],
    "850_tmp": [("z", "pl:850"), ("t", "pl:850"), ("u", "pl:850"), ("v", "pl:850")],
    "500_tmp": [("z", "pl:500"), ("t", "pl:500"), ("u", "pl:500"), ("v", "pl:500")],
    "300_jet": [("z", "pl:300"), ("u", "pl:300"), ("v", "pl:300")],
    "250_jet": [("z", "pl:250"), ("u", "pl:250"), ("v", "pl:250")],
    "sfc_mslp": [("msl", "sfc"), ("10u", "sfc"), ("10v", "sfc"), ("2t", "sfc")],
    "pwat": [("tcw", "sfc")],
    "tcdc": [("tcc", "sfc")],
}

# ---------------------------------------------------------------- products
# Each variable: (idx shortName, idx level substring)
PRODUCTS = {
    "500_vort": {
        "label": "500 mb - Heights + Vorticity + Wind",
        "desc": "Classic upper-air chart: geopotential heights (dam), absolute vorticity shaded, winds",
        "vars": [("HGT", "500 mb"), ("UGRD", "500 mb"), ("VGRD", "500 mb")],
    },
    "850_tmp": {
        "label": "850 mb - Heights + Temperature + Wind",
        "desc": "Low-level thermal field: moisture axis & temperature advection handily visible",
        "vars": [("HGT", "850 mb"), ("TMP", "850 mb"), ("UGRD", "850 mb"), ("VGRD", "850 mb")],
    },
    "700_rh": {
        "label": "700 mb - Relative Humidity + Heights",
        "desc": "Dry slots and moisture plumes aloft; useful for fire weather & precip forecasts",
        "vars": [("HGT", "700 mb"), ("RH", "700 mb")],
    },
    "300_jet": {
        "label": "300 mb - Jet Stream (isotachs + heights)",
        "desc": "Upper jet cores, ridges and troughs driving surface systems",
        "vars": [("HGT", "300 mb"), ("UGRD", "300 mb"), ("VGRD", "300 mb")],
    },
    "sfc_mslp": {
        "label": "Surface - MSLP + Fronts-level winds + Temperature",
        "desc": "Mean sea-level pressure, 10 m winds and 2 m temperature",
        "vars": [("PRMSL", "mean sea level"), ("UGRD", "10 m above ground"),
                 ("VGRD", "10 m above ground"), ("TMP", "2 m above ground")],
    },    "cape_wind": {
        "label": "Surface - CAPE + Winds (instability)",
        "desc": "Thunderstorm fuel (J/kg) with surface winds",
        "vars": [("CAPE", "surface"), ("UGRD", "10 m above ground"),
                 ("VGRD", "10 m above ground")],
    },
    "refc": {
        "label": "Composite radar - simulated reflectivity",
        "desc": "Simulated composite reflectivity (dBZ) - what radar echoes the model expects",
        "vars": [("REFC", "entire atmosphere")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},  # REFC lives in HRRR surface files
    },
    "250_jet": {
        "label": "250 mb - Heights + Jet Winds",
        "desc": "250 mb jet-level chart: geopotential heights (dam), isotachs, wind barbs",
        "vars": [("HGT", "250 mb"), ("UGRD", "250 mb"), ("VGRD", "250 mb")],
    },
    "200_jet": {
        "label": "200 mb - Heights + Jet Winds",
        "desc": "200 mb jet-level chart: geopotential heights (dam), isotachs, wind barbs",
        "vars": [("HGT", "200 mb"), ("UGRD", "200 mb"), ("VGRD", "200 mb")],
    },
    "925_tmp": {
        "label": "925 mb - Heights + Temperature + Wind",
        "desc": "925 mb low-level chart: temperatures, heights, wind barbs",
        "vars": [("HGT", "925 mb"), ("TMP", "925 mb"), ("UGRD", "925 mb"), ("VGRD", "925 mb")],
    },
    "500_tmp": {
        "label": "500 mb - Heights + Temperature + Wind",
        "desc": "Mid-level temperature chart: cold cores, shortwave troughs",
        "vars": [("HGT", "500 mb"), ("TMP", "500 mb"), ("UGRD", "500 mb"), ("VGRD", "500 mb")],
    },
    "pwat": {
        "label": "Precipitable Water (PWAT)",
        "desc": "Total atmospheric moisture (mm) - rain-heavy air masses show up instantly",
        "vars": [("PWAT", "entire atmosphere")],
        "vars_by_model": {"RRFS": [("PWAT", "30-0 mb above ground")]},  # RRFS labels the layer oddly
    },
    "mucape": {
        "label": "MUCAPE - Most-Unstable CAPE",
        "desc": "Instability for the most unstable parcel (J/kg) - storm fuel",
        "vars": [("CAPE", "90-0 mb above ground")],
    },
    "sfc_gust": {
        "label": "Surface Wind Gusts + Winds",
        "desc": "Forecast gusts (mph) with 10 m wind barbs",
        "vars": [("GUST", "surface"), ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},  # gusts live in HRRR surface files
    },
    "tcdc": {
        "label": "Total Cloud Cover",
        "desc": "Cloud-cover fraction (%), whole atmospheric column",
        "vars": [("TCDC", "entire atmosphere")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},
    },
    "vis": {
        "label": "Surface Visibility",
        "desc": "Horizontal visibility (miles) - fog and haze at a glance",
        "vars": [("VIS", "surface")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},
    },
    "snow": {
        "label": "Snow Depth (accumulated)",
        "desc": "Snow depth on the ground (inches)",
        "vars": [("WEASD", "surface")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},
    },
    "prate": {
        "label": "Precipitation Rate",
        "desc": "Instant rain rate (in/hr) - where it's pouring right now",
        "vars": [("PRATE", "surface")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},
    },
    "h500_spread": {
        "label": "500 mb Height SPREAD (ensemble uncertainty)",
        "desc": "GEFS ensemble spread of 500 mb heights - large values = low forecast confidence",
        "vars": [("HGT", "500 mb")],
    },
    # --- CAM-ensemble (HREF / REFS) products; per-model file-kind swap via file_type ---
    "cam_pmmn": {
        "label": "PMMN Composite Reflectivity (ensemble)",
        "desc": "Probability-matched mean reflectivity - sharper than a plain mean, keeps storm structure",
        "vars": [("REFC", "entire atmosphere")],
        "file_type": {"HREF": ("mean", "pmmn"), "REFS": ("mean", "pmmn")},
    },
    "cam_mean_500": {
        "label": "Ensemble-mean 500 mb - Heights + Vorticity + Wind",
        "desc": "Synoptic pattern from the CAM ensemble average",
        "vars": [("HGT", "500 mb"), ("UGRD", "500 mb"), ("VGRD", "500 mb")],
    },
    "cam_mean_srh": {
        "label": "Ensemble-mean 0-3 km Storm-Relative Helicity",
        "desc": "Mean SRH (m\u00b2/s\u00b2) - rotation available for supercells",
        "vars": [("HLCY", "3000-0 m")],
    },
    "cam_prob_uphl": {
        "label": "Probability Updraft Helicity > 75 (rotating storms)",
        "desc": "Ensemble probability (%) of 2-5 km updraft helicity exceeding 75 m\u00b2/s\u00b2 - severe proxy",
        "vars": [("MXUPHL", "prob >75")],
        "file_type": {"HREF": ("mean", "prob"), "REFS": ("mean", "prob")},
    },
    "cam_prob_ltng": {
        "label": "Probability of Lightning (total lightning)",
        "desc": "Ensemble probability (%) of lightning in the hour - storm coverage at a glance",
        "vars": [("LTNG", "prob >0")],   # HREF threshold .2, REFS .08 - both start 'prob >0'
        "file_type": {"HREF": ("mean", "prob"), "REFS": ("mean", "prob")},
    },
    "ai_precip": {
        "label": "6-hour Precipitation Accumulation (AI)",
        "desc": "Precipitation accumulated over the previous 6 h (inches) from the AI model",
        "vars": [("APCP", "surface")],
    },
    "cam_h500_spread": {
        "label": "500 mb Height SPREAD (ensemble uncertainty)",
        "desc": "CAM-ensemble spread of 500 mb heights - big values = low confidence",
        "vars": [("HGT", "500 mb")],
        "file_type": {"REFS": ("mean", "sprd")},
    },
}


def find_cycle(model):
    """Most recent cycle datetime for which this model has an .idx."""
    m = MAP_MODELS[model]
    now = dt.datetime.now(dt.timezone.utc)
    if model == "NBM":
        return _find_nbm_cycle(model)
    if model in AIWP_CODES:
        from data.aiwp import find_cycle as _aiwp_cycle
        return _aiwp_cycle(model)
    if model == "ECMWF":
        return _find_ecmwf_cycle(model, "ifs/0p25/oper", "{day}{hh}0000", 3, "oper-fc")
    if model == "AIFS":
        return _find_ecmwf_cycle(model, "aifs-single/0p25/oper", "{day}{hh}0000", 6, "oper-fc")
    if model == "AIFS-ENS":
        return _find_ecmwf_cycle(model, "aifs-ens/0p25/enfo", "{day}{hh}0000", 6, "enfo-cf")
    probe = m.get("probe_fh", 1)
    for back in m["cycles"]:
        c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        if m.get("synoptic"):
            c = c.replace(hour=(c.hour // 6) * 6)
        try:
            r = requests.get(_idx_url(model, c, probe), headers=UA, timeout=15)
            if r.ok:
                return c
        except requests.RequestException:
            continue
    return None


def _find_ecmwf_cycle(model, model_dir, stamp_fmt, probe_step=3, tag="oper-fc"):
    """Most recent ECMWF-bucket cycle (IFS/AIFS/AIFS-ENS) with a live index.
    Published ~4-8 h after cycle time; all products share the file naming."""
    now = dt.datetime.now(dt.timezone.utc)
    for back in range(8, 34):
        c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        day, hh = c.strftime("%Y%m%d"), c.strftime("%H")
        stamp = stamp_fmt.format(day=day, hh=hh)
        url = (f"https://ecmwf-forecasts.s3.amazonaws.com/{day}/{hh}z/{model_dir}/"
               f"{stamp}-{probe_step}h-{tag}.index")
        for attempt in range(3):   # S3 rate-limits these tiny files (SlowDown)
            try:
                r = requests.get(url, headers=UA, timeout=15)
                if r.ok:
                    return c
            except requests.RequestException:
                pass
            time.sleep(1.5 + attempt)
    return None


def _idx_url(model, cycle, fh, product=None):
    m = MAP_MODELS[model]
    if m.get("style") == "cfs":
        valid = cycle + dt.timedelta(hours=fh)
        return (f"{m['base'].format(c=cycle)}pgbf{valid:%Y%m%d%H}.01.{cycle:%Y%m%d%H}.grb2.idx")
    base = m["base"].format(c=cycle)
    # per-product file-type swap (e.g. HRRR REFC/CAPE/MSLMA live in wrfsfcf files)
    swap = (PRODUCTS.get(product) or {}).get("file_type", {}).get(model)
    if swap:
        base = base.replace(swap[0], swap[1])
    return f"{base}{m['step_fmt'].format(fh=fh)}{m['suffix']}.idx"


def _find_range(idx_text, short_name, level_sub):
    """Byte range (start, end) for one idx entry.

    When U/V (or multi-level fields) share a message, the idx lists several
    entries at the same start; the true end is the next entry with a strictly
    larger start byte.
    """
    lines = idx_text.splitlines()
    lv = level_sub.lower()
    for i, line in enumerate(lines):
        fields = line.split(":")
        if len(fields) > 4 and fields[3] == short_name and lv in ":".join(fields[4:]).lower():
            start = int(fields[1])
            end = start
            for j in range(i + 1, len(lines)):
                nxt = int(lines[j].split(":")[1])
                if nxt > start:
                    end = nxt
                    break
            if end <= start:
                end = start + 3_000_000
            return start, end
    return None


def _fetch_range(url, start, end, attempts=3):
    """Byte-range GET with retry - S3/NOMADS intermittently 503/429 small reads."""
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers={**UA, "Range": f"bytes={start}-{end - 1}"}, timeout=90)
            if r.status_code in (429, 503) and i < attempts - 1:
                time.sleep(2 + 2 * i)
                continue
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:
            last = exc
            time.sleep(1 + i)
    raise last if last is not None else requests.RequestException("range fetch failed")


def _level_type(level_sub):
    """Map an idx level substring to the cfgrib typeOfLevel filter.

    Ranged above-ground layers (MUCAPE's "90-0 mb", SRH's "3000-0 m")
    encode as various layer types (pressureFromGroundLayer, ...) - decoding
    unfiltered is safest since each byte range holds a single message.
    """
    lv = level_sub.lower()
    if "above ground" in lv:
        first = lv.split()[0]
        if "-" in first:            # ranged layer -> unknown typeOfLevel
            return None
        return "heightAboveGround"  # single level like "10 m above ground"
    if lv.endswith("mb"):
        return "isobaricInhPa"
    if "mean sea level" in lv:
        return "meanSea"
    if lv == "surface":
        return "surface"
    return None


def _decode_grib_bytes(blob, type_of_level=None, target_level=None):
    """Decode GRIB bytes -> ({shortName: values2d}, lat2d, lon2d).

    NAM/RAP pack U+V (and all levels of a variable) into single messages, so
    one decode can yield several fields; 3-D arrays are reduced to the level
    nearest `target_level` (e.g. 500 for mb, 10 for '10 m above ground').
    """
    import os
    import tempfile
    import xarray as xr

    tmp = os.path.join(tempfile.gettempdir(), f"tnwx_map_{abs(hash(blob[:64])) % 99999}.grib2")
    try:
        with open(tmp, "wb") as f:
            f.write(blob)
        backend = {"indexpath": ""}
        if type_of_level:
            backend["filter_by_keys"] = {"typeOfLevel": type_of_level}
        ds = xr.open_dataset(tmp, engine="cfgrib", backend_kwargs=backend)
        lat = np.asarray(ds["latitude"].values, dtype=float)
        lon = np.asarray(ds["longitude"].values, dtype=float)
        out = {}
        for var in ds.data_vars:
            arr = np.asarray(ds[var].values, dtype=float)
            if arr.ndim == 3 and target_level is not None:
                coord = next((c for c in ("isobaricInhPa", "heightAboveGround")
                              if c in ds[var].coords or c in ds.coords), None)
                if coord is not None:
                    levels = np.asarray(ds[coord].values, dtype=float)
                    arr = arr[int(np.argmin(np.abs(levels - target_level)))]
                else:
                    arr = arr[0]
            sn = str(ds[var].attrs.get("GRIB_shortName", var)).upper()
            out[sn] = np.squeeze(arr)
        del ds
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if lat.ndim == 1 and lon.ndim == 1:
        lon, lat = np.meshgrid(lon, lat)
    return out, lat, lon


# Catalog entries for the NBM COG products (vars unused; fetched via _fetch_nbm_fields)
PRODUCTS["nbm_temp"] = {
    "label": "NBM Temperature \u00b7 Blend of Models",
    "desc": "NBM CONUS temperature mosaic (pre-blended guidance, 2.5 km)",
    "vars": [],
}
PRODUCTS["nbm_gust"] = {
    "label": "NBM Wind Gust \u00b7 Blend of Models",
    "desc": "NBM CONUS wind-gust mosaic (pre-blended guidance, 2.5 km)",
    "vars": [],
}
PRODUCTS["nbm_cape"] = {
    "label": "NBM SBCAPE \u00b7 Blend of Models",
    "desc": "NBM CONUS surface-based CAPE mosaic (pre-blended guidance, 2.5 km)",
    "vars": [],
}
PRODUCTS["nbm_refc"] = {
    "label": "NBM Max Simulated Reflectivity \u00b7 Blend of Models",
    "desc": "NBM CONUS max simulated radar reflectivity mosaic (pre-blended guidance, 2.5 km)",
    "vars": [],
}

# GRIB shortName -> the idx shortName our products use
_CANON = {"U": "UGRD", "V": "VGRD", "GH": "HGT", "T": "TMP", "R": "RH",
          "U10": "UGRD", "V10": "VGRD", "10U": "UGRD", "10V": "VGRD",
          "T2M": "TMP", "2T": "TMP", "DPT2M": "DPT", "Z": "HGT", "MSL": "PRMSL",
          "CAPE": "CAPE", "PRMSL": "PRMSL", "MSLMA": "PRMSL", "TCW": "PWAT", "TCC": "TCDC",
          "SDWE": "WEASD", "SNOD": "WEASD"}
# eccodes shortNames are often lowercase (cape, tcw, sdwe...) - alias them too
for _k, _v in list(_CANON.items()):
    _CANON.setdefault(_k.lower(), _v)
# Some models use different parameter names for the same field
MODEL_VAR_ALIAS = {
    "RRFS": {
        # NOMADS RRFS idx names these HGT/TMP (like most NCEP models), but the
        # GRIB2 shortName inside the file is "HPBL"-style canon — keep explicit:
        "TMP": "TMP", "HGT": "HGT", "UGRD": "UGRD", "VGRD": "VGRD", "ABSV": "ABSV",
    },
    "NBM": {},
    "CFS": {},
    "HRRR": {"PRMSL": "MSLMA"},  # HRRR surface files name mean-sea pressure MSLMA
    "RAP": {"PRMSL": "MSLMA"},
}


def _target_level(level_sub):
    lv = level_sub.lower()
    if lv.endswith("mb"):
        return int(lv.replace("mb", "").strip())
    if "above ground" in lv:
        # layer strings like "30-0 mb" or "5000-2000 m" - first standalone int wins
        m = re.match(r"(\d+)(?:-(\d+))?\s", lv)
        if m:
            return int(m.group(1))
    return None


_IDX_TEXT_CACHE = {}


def _get_idx_text(model, cycle, fh, product=None):
    """Fetch (and cache) the .idx listing for one model file."""
    key = (model, cycle.strftime("%Y%m%d%H"), fh, product)
    if key not in _IDX_TEXT_CACHE:
        it = requests.get(_idx_url(model, cycle, fh, product), headers=UA, timeout=30)
        _IDX_TEXT_CACHE[key] = it.text if it.ok else None
    return _IDX_TEXT_CACHE[key]


def _find_nbm_cycle(model):
    """Most recent NBM init (COG dirs are named HHMM, published ~75 min after)."""
    m = MAP_MODELS[model]
    now = dt.datetime.now(dt.timezone.utc)
    for back in m["cycles"]:
        c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        prefix = f"blendv5.0/conus/{c:%Y/%m/%d}/{c:%H%M}/temp/"
        url = "https://noaa-nbm-pds.s3.amazonaws.com/?list-type=2" + f"&prefix={prefix}&max-keys=2"
        try:
            r = requests.get(url, headers=UA, timeout=15)
            if "<Key>" in r.text:
                return c
        except requests.RequestException:
            continue
    return None


def _nbm_cog_url(cycle, elem, fh):
    """NBM COG path for one element/valid time (files start at init+1 h)."""
    v = cycle + dt.timedelta(hours=max(1, fh))
    return (f"https://noaa-nbm-pds.s3.amazonaws.com/blendv5.0/conus/{cycle:%Y/%m/%d}/{cycle:%H%M}/"
            f"{elem}/blendv5.0_conus_{elem}_{cycle:%Y-%m-%dT%H:%M}_{v:%Y-%m-%dT%H:%M}.tif")


def _fetch_nbm_fields(cycle, fh, product):
    """Read one NBM COG tile -> {'DATA': values, 'lat', 'lon'} via rasterio.

    The COGs are small (~1 MB), so a straight download + local open beats
    /vsicurl's many small range requests on this bucket.
    """
    import rasterio
    import tempfile
    from pyproj import Transformer

    elem, _kind, _cmap, _units, _lv = NBM_ELEMENTS[product]
    url = _nbm_cog_url(cycle, elem, fh)
    tmp = None
    try:
        r = requests.get(url, headers=UA, timeout=120)
        if r.status_code != 200 or len(r.content) < 1000:
            return None
        fd, tmp = tempfile.mkstemp(suffix=".tif")
        with os.fdopen(fd, "wb") as f:
            f.write(r.content)
        with rasterio.open(tmp) as src:
            step = max(1, int(np.ceil(max(src.height, src.width) / 900)))
            band = src.read(1, out_shape=(src.height // step, src.width // step)).astype(float)
            nodata = src.nodata
            crs = src.crs
            tr0 = src.transform
    except Exception:  # noqa: BLE001 - missing tile/network
        return None
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    if nodata is not None:
        band[band == nodata] = np.nan
    rows, cols = band.shape
    # derive lat/lon per cell centre from the source affine transform
    r_idx = (np.arange(rows) + 0.5) * step
    c_idx = (np.arange(cols) + 0.5) * step
    xs = tr0.c + c_idx * tr0.a
    ys = tr0.f + r_idx * tr0.e
    X, Y = np.meshgrid(xs, ys)
    if crs and crs.to_epsg() != 4326:
        tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        lon, lat = tr.transform(X, Y)
    else:
        lon, lat = X, Y
    return {"DATA": band, "lat": lat, "lon": lon}


def _fetch_fields_herbie(model, cycle, fh, product):
    """Herbie-first fetch for the GRIB2 models (multi-partner fallback).

    Subset-fetches each product message via Herbie (AWS/NOMADS/Google/ECMWF
    mirror) and decodes with the same cfgrib path as the legacy fetchers.
    Returns None so the caller falls back when Herbie can't serve a file.
    """
    ecmwf_family = model in ("ECMWF", "AIFS", "AIFS-ENS")
    swap = None
    if ecmwf_family:
        params = ECMWF_PARAMS.get(product)
        if params is None:
            return None
        pairs = []
        for param, level in params:
            levtype, _, levelist = level.partition(":")
            pairs.append((f":{param}:{levelist or levtype}", None))
    else:
        vars_needed = (PRODUCTS[product].get("vars_by_model", {}).get(model)
                       or PRODUCTS[product]["vars"])
        alias = MODEL_VAR_ALIAS.get(model, {})
        swap = (PRODUCTS.get(product) or {}).get("file_type", {}).get(model)
        pairs = [(f":{re.escape(alias.get(s, s))}[^:]*:{re.escape(lv)}", lv) for s, lv in vars_needed]
    from data.herbie_client import fetch_subset_blobs
    blobs = fetch_subset_blobs(model, cycle, fh, [p[0] for p in pairs], swap=swap)
    if blobs is None or not any(b is not None for b in blobs):
        return None
    fields = {}
    lat = lon = None
    dec_step = 2 if model in ("GFS", "RRFS", "HRRR", "CFS", "HREF", "REFS") else 1
    for (search, lv), blob in zip(pairs, blobs):
        if blob is None:
            continue
        try:
            decoded, lat2, lon2 = _decode_grib_bytes(
                blob,
                type_of_level=_level_type(lv) if lv else None,
                target_level=_target_level(lv) if lv else None,
            )
        except Exception:  # noqa: BLE001 - one bad message must not kill the map
            continue
        for sn, values in decoded.items():
            fields[_CANON.get(sn, sn)] = values[::dec_step, ::dec_step]
        lat, lon = lat2[::dec_step, ::dec_step], lon2[::dec_step, ::dec_step]
    if lat is None or not fields:
        return None
    if ecmwf_family and "HGT" in fields:
        fields["HGT"] = fields["HGT"] / 9.80665   # ECMWF 'z' is geopotential, not height
    return {"fields": fields, "lat": lat, "lon": lon}


def fetch_product_fields(model, cycle, fh, product):
    """Fetch+decode all variables for a product. Returns {shortName: values} + lat/lon.

    Herbie (multi-partner archive search) is tried first for the GRIB2
    models; the original single-bucket fetchers remain as fallback.
    """
    if model == "NBM":
        data = _fetch_nbm_fields(cycle, fh, product)
        if data is None:
            return None
        return {"fields": {"DATA": data["DATA"]}, "lat": data["lat"], "lon": data["lon"]}
    if model in AIWP_CODES:
        from data.aiwp import fetch_fields as _aiwp_fetch
        return _aiwp_fetch(model, cycle, fh, product)
    from data.herbie_client import HERBIE_MODELS
    if model in HERBIE_MODELS:
        try:
            res = _fetch_fields_herbie(model, cycle, fh, product)
        except Exception:  # noqa: BLE001 - Herbie failures always fall back
            res = None
        if res is not None:
            return res
    if model == "ECMWF":
        return _fetch_ecmwf_fields(cycle, fh, product)
    if model == "AIFS":
        return _fetch_ecmwf_fields(cycle, fh, product, model_dir="aifs-single/0p25/oper")
    if model == "AIFS-ENS":
        return _fetch_ecmwf_fields(cycle, fh, product, model_dir="aifs-ens/0p25/enfo", tag="enfo-cf")
    m = MAP_MODELS[model]
    vars_needed = (PRODUCTS[product].get("vars_by_model", {}).get(model)
                   or PRODUCTS[product]["vars"])
    idx_text = _get_idx_text(model, cycle, fh, product)
    if idx_text is None:
        return None
    alias = MODEL_VAR_ALIAS.get(model, {})

    # Resolve byte ranges; U/V (and multi-level fields) may share one message,
    # so group requests by message start byte.
    groups = {}
    for short, level in vars_needed:
        actual = alias.get(short, short)
        rng = _find_range(idx_text, actual, level)
        if rng is None:
            continue
        start, end = rng
        g = groups.setdefault(start, {"end": end, "level": level, "shorts": []})
        g["end"] = max(g["end"], end)
        g["shorts"].append(short)
    if not groups:
        return None
    url = _idx_url(model, cycle, fh, product).replace(".idx", "")

    def work(g_start, g_end):
        try:
            return _fetch_range(url, g_start, g_end)
        except requests.RequestException:
            return None

    blobs = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for g_start, g in groups.items():
            blobs.append((g, ex.submit(work, g_start, g["end"])))

    fields = {}
    lat = lon = None
    dec_step = 2 if model in ("GFS", "RRFS", "HRRR", "CFS", "HREF", "REFS") else 1  # downsample huge grids
    for g, fut in blobs:
        blob = fut.result()
        if blob is None:
            continue
        decoded, lat2, lon2 = _decode_grib_bytes(
            blob, type_of_level=_level_type(g["level"]), target_level=_target_level(g["level"])
        )
        # processed ensemble products (e.g. REFS pmmn REFC) decode with an
        # UNDEFINED shortName - restore the expected name from the idx request
        if set(decoded.keys()) == {"UNKNOWN"} and g["shorts"]:
            decoded = {g["shorts"][0]: next(iter(decoded.values()))}
        for sn, values in decoded.items():
            canonical = _CANON.get(sn, sn)
            fields[canonical] = values[::dec_step, ::dec_step]
        lat, lon = lat2[::dec_step, ::dec_step], lon2[::dec_step, ::dec_step]
    if lat is None or len(fields) < 1:
        return None
    if product.startswith("cam_prob"):
        # normalize the probability message: single field -> PROB, fraction -> percent
        vals = next(iter(fields.values()))
        if np.nanmax(vals) <= 1.0:
            vals = vals * 100.0
        fields = {"PROB": vals}
    return {"fields": fields, "lat": lat, "lon": lon}


def _fetch_ecmwf_fields(cycle, fh, product, model_dir="ifs/0p25/oper", tag="oper-fc"):
    """ECMWF IFS/AIFS fields via its per-step JSON index (helpers shared with data.models)."""
    from data.models import _ecmwf_entry, _ecmwf_file_url, _ecmwf_index

    idx = _ecmwf_index(cycle, fh, model_dir=model_dir, tag=tag)
    if idx is None:
        return None
    params = ECMWF_PARAMS.get(product)
    if params is None:
        return None
    url = _ecmwf_file_url(cycle, fh, model_dir=model_dir, tag=tag)
    # the S3 bucket throttles hot range reads - ECMWF's mirror serves the same
    # tree without rate limits, so try it first and fall back to S3
    url_mirror = url.replace("https://ecmwf-forecasts.s3.amazonaws.com", "https://data.ecmwf.int/forecasts")

    def _grab(off, ln):
        for u in (url_mirror, url):
            try:
                return _fetch_range(u, off, off + ln)
            except requests.RequestException:
                continue
        return None

    fields = {}
    lat = lon = None
    for param, level in params:
        levtype, _, levelist = (level.partition(":") + ("",))[:3]
        entry = _ecmwf_entry(idx, param, levtype or "sfc", levelist or None)
        if entry is None:
            continue
        try:
            blob = _grab(entry["_offset"], entry["_length"])
            if blob is None:
                continue
            decoded, lat2, lon2 = _decode_grib_bytes(blob)
        except Exception:  # noqa: BLE001 - one missing field must not kill the map
            continue
        for sn, values in decoded.items():
            fields[_CANON.get(sn, sn)] = values
        lat, lon = lat2, lon2
    if lat is None or not fields:
        return None
    if "HGT" in fields:
        fields["HGT"] = fields["HGT"] / 9.80665   # ECMWF 'z' is geopotential, not height
    # display downsample: 0.25 deg global is ~1M points, too heavy for contours
    fields = {k: v[::2, ::2] for k, v in fields.items()}
    lat, lon = lat2[::2, ::2], lon2[::2, ::2]
    return {"fields": fields, "lat": lat, "lon": lon}


# ---------------------------------------------------------------- plotting
def render_product_map(model, cycle, fh, product, out_dir=MAP_DIR):
    """Render one map; returns (png_path, meta). Cached on disk per cycle/fh."""
    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, f"{model}_{product}_f{fh:03d}_{cycle:%Y%m%d%H}.png")
    if os.path.exists(png) and os.path.getsize(png) > 10_000:
        valid = cycle + dt.timedelta(hours=fh)
        return png, {"cycle": cycle.strftime("%Y-%m-%d %H:%M UTC"), "fh": fh,
                     "valid": valid.strftime("%Y-%m-%d %H:%M UTC"), "cached": True}
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import metpy.calc as mpcalc
    from metpy.units import units
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    data = fetch_product_fields(model, cycle, fh, product)
    if data is None:
        raise RuntimeError("No decodable data for this model/hour (NOAA bucket issue?)")
    fields, lat, lon = data["fields"], data["lat"], data["lon"]
    f = fields.get

    m = MAP_MODELS[model]
    valid = cycle + dt.timedelta(hours=fh)

    proj = ccrs.LambertConformal(central_longitude=-96, central_latitude=39)
    trans = ccrs.PlateCarree()
    fig = plt.figure(figsize=(13, 8), dpi=110)
    ax = plt.axes(projection=proj)
    ax.set_extent([-125, -66, 23, 51], crs=trans)
    ax.add_feature(cfeature.STATES.with_scale("50m"), linewidth=0.5, edgecolor="#666666")
    ax.add_feature(cfeature.COASTLINE.with_scale("50m"), linewidth=0.6)
    ax.add_feature(cfeature.BORDERS.with_scale("50m"), linewidth=0.8)

    # subsample stride for barbs based on grid density
    stride = max(1, int(max(lat.shape) / 45))
    if product.startswith("nbm_"):
        stride = 10**9  # no winds in NBM COG products

    def heights(ax, h, level_mb, interval=60):
        hd = h / 10.0  # meters -> decameters
        cs = ax.contour(lon, lat, hd, levels=np.arange(480, 612, interval / 10),
                        colors="black", linewidths=1.0, transform=trans)
        ax.clabel(cs, fmt="%d", fontsize=7, inline=True)
        return hd

    wind = None
    if product in ("500_vort", "cam_mean_500"):
        u, v, h = f("UGRD"), f("VGRD"), f("HGT")
        dx, dy = mpcalc.lat_lon_grid_deltas(lon, lat)
        relv = mpcalc.vorticity(u * units("m/s"), v * units("m/s"), dx=dx, dy=dy).to("1/s")
        coriolis = 2 * 7.2921e-5 * np.sin(np.deg2rad(lat))
        absv = np.asarray(relv) + coriolis
        hd = heights(ax, h, 500, interval=60)
        cf = ax.contourf(lon, lat, absv * 1e5, levels=np.arange(8, 34, 2), cmap="YlGnBu",
                         transform=trans, alpha=0.75)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Absolute vorticity (10\u207b\u2075 s\u207b\u00b9)")
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product in ("850_tmp", "925_tmp"):
        u, v, h, t = f("UGRD"), f("VGRD"), f("HGT"), f("TMP")
        tc = t - 273.15
        hd = heights(ax, h, int(product[:3]), interval=30)
        cf = ax.contourf(lon, lat, tc, levels=np.arange(-30, 42, 2), cmap="RdBu_r",
                         transform=trans, alpha=0.7)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Temperature (\u00b0C)")
        cs = ax.contour(lon, lat, tc, levels=np.arange(-30, 42, 5), colors="darkred",
                        linewidths=0.7, transform=trans)
        ax.clabel(cs, fmt="%d\u00b0C", fontsize=6)
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product == "700_rh":
        rh, h = f("RH"), f("HGT")
        hd = heights(ax, h, 700, interval=30)
        cf = ax.contourf(lon, lat, rh, levels=np.arange(10, 105, 10), cmap="Greens",
                         transform=trans, alpha=0.8)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Relative humidity (%)")
    elif product == "500_tmp":
        u, v, h, t = f("UGRD"), f("VGRD"), f("HGT"), f("TMP")
        tc = t - 273.15
        hd = heights(ax, h, 500, interval=60)
        cf = ax.contourf(lon, lat, tc, levels=np.arange(-48, 5, 3), cmap="coolwarm",
                         transform=trans, alpha=0.7)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Temperature (\u00b0C)")
        cs = ax.contour(lon, lat, tc, levels=np.arange(-48, 1, 5), colors="darkblue",
                        linewidths=0.7, transform=trans)
        ax.clabel(cs, fmt="%d\u00b0C", fontsize=6)
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product == "pwat":
        pw = f("PWAT")
        cf = ax.contourf(lon, lat, pw, levels=np.arange(5, 80, 5), cmap="PuBuGn",
                         transform=trans, alpha=0.85, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Precipitable water (mm)")
        cs = ax.contour(lon, lat, pw, levels=(25, 40, 55), colors="darkcyan",
                        linewidths=0.8, transform=trans)
        ax.clabel(cs, fmt="%dmm", fontsize=6)
    elif product == "mucape":
        cape = f("CAPE")
        cf = ax.contourf(lon, lat, cape, levels=np.arange(100, 5001, 250), cmap="YlOrRd",
                         transform=trans, alpha=0.8, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Most-unstable CAPE (J/kg)")
        cs = ax.contour(lon, lat, cape, levels=(1000, 2500, 4000), colors="black",
                        linewidths=0.7, transform=trans)
        ax.clabel(cs, fmt="%d", fontsize=6)
    elif product == "sfc_gust":
        g, u, v = f("GUST"), f("UGRD"), f("VGRD")
        gmph = g * 2.23694
        cf = ax.contourf(lon, lat, gmph, levels=np.arange(10, 85, 5), cmap="YlOrRd",
                         transform=trans, alpha=0.8, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Wind gust (mph)")
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product == "tcdc":
        c = f("TCDC")
        if np.nanmax(c) <= 1.0:
            c = c * 100.0   # ECMWF encodes 0-1
        cf = ax.contourf(lon, lat, c, levels=np.arange(0, 101, 10), cmap="gist_gray_r",
                         transform=trans, alpha=0.75)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Cloud cover (%)")
    elif product == "vis":
        vis_mi = f("VIS") / 1609.34
        cf = ax.contourf(lon, lat, vis_mi, levels=np.arange(0, 10.5, 0.5), cmap="RdYlGn",
                         transform=trans, alpha=0.8, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Visibility (mi)")
    elif product == "snow":
        s = f("WEASD") * 39.37   # meters -> inches
        fill = np.where(s < 0.1, np.nan, s)
        cf = ax.contourf(lon, lat, fill, levels=[0.1, 1, 2, 4, 6, 9, 12, 18, 24],
                         cmap="cool", transform=trans, alpha=0.85, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Snow depth (in)")
    elif product == "ai_precip":
        ap = f("APCP") * 39.37   # meters -> inches over 6 h
        fill = np.where(ap < 0.01, np.nan, ap)
        cf = ax.contourf(lon, lat, fill, levels=[0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0],
                         cmap="YlGnBu", transform=trans, alpha=0.85, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="6-h precipitation (in)")
    elif product == "prate":
        pr = f("PRATE") * 141.732   # kg m-2 s-1 -> in/hr
        fill = np.where(pr < 0.01, np.nan, pr)
        cf = ax.contourf(lon, lat, fill, levels=[0.01, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0],
                         cmap="turbo", transform=trans, alpha=0.85, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Precip rate (in/hr)")
    elif product in ("300_jet", "250_jet", "200_jet"):
        u, v, h = f("UGRD"), f("VGRD"), f("HGT")
        spd_kt = np.hypot(u, v) * 1.94384
        hd = heights(ax, h, int(product[:3]), interval=120)
        cf = ax.contourf(lon, lat, spd_kt, levels=np.arange(40, 181, 10), cmap="turbo",
                         transform=trans, alpha=0.75)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Wind speed (kt)")
        cs = ax.contour(lon, lat, spd_kt, levels=np.arange(70, 181, 30), colors="white",
                        linewidths=0.8, transform=trans)
        ax.clabel(cs, fmt="%dkt", fontsize=6)
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=5, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product == "sfc_mslp":
        p, u, v, t = f("PRMSL"), f("UGRD"), f("VGRD"), f("TMP")
        hpa = p / 100.0
        cs = ax.contour(lon, lat, hpa, levels=np.arange(960, 1050, 4), colors="black",
                        linewidths=1.0, transform=trans)
        ax.clabel(cs, fmt="%d", fontsize=7, inline=True)
        tf = t - 273.15
        cf = ax.contourf(lon, lat, tf, levels=np.arange(-30, 46, 3), cmap="coolwarm",
                         transform=trans, alpha=0.45)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="2 m temperature (\u00b0C)")
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product == "cape_wind":
        cape, u, v = f("CAPE"), f("UGRD"), f("VGRD")
        cf = ax.contourf(lon, lat, cape, levels=np.arange(250, 5001, 250), cmap="YlOrRd",
                         transform=trans, alpha=0.8)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="CAPE (J/kg)")
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product == "h500_spread" or product == "cam_h500_spread":
        h = f("HGT")
        hd = h / 10.0
        cf = ax.contourf(lon, lat, hd, levels=np.arange(0, 4.01, 0.25), cmap="viridis",
                         transform=trans, alpha=0.85)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="500 mb height spread (dam)")
    elif product == "refc" or product == "cam_pmmn":
        # simulated composite reflectivity - what radar echoes the model expects
        refl = f("REFC")
        fill = np.where(refl < 5, np.nan, refl)
        cf = ax.contourf(lon, lat, fill, levels=np.arange(5, 70, 5), cmap="turbo",
                         transform=trans, alpha=0.8, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Simulated composite reflectivity (dBZ)")
        cs = ax.contour(lon, lat, fill, levels=(35, 50), colors="black",
                        linewidths=0.8, transform=trans)
        ax.clabel(cs, fmt="%ddBZ", fontsize=6)
    elif product == "cam_mean_srh":
        # ensemble-mean 0-3 km storm-relative helicity
        srh = f("HLCY")
        cf = ax.contourf(lon, lat, srh, levels=np.arange(0, 501, 25), cmap="Spectral_r",
                         transform=trans, alpha=0.8, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="0-3 km SRH (m\u00b2/s\u00b2)")
        cs = ax.contour(lon, lat, srh, levels=(150, 250, 400), colors="black",
                        linewidths=0.8, transform=trans)
        ax.clabel(cs, fmt="%d", fontsize=6)
    elif product in ("cam_prob_uphl", "cam_prob_ltng"):
        # ensemble probability (%) field
        prob = f("PROB")
        fill = np.where(prob < 2, np.nan, prob)
        cmap = "YlOrRd" if product == "cam_prob_uphl" else "YlGnBu"
        cf = ax.contourf(lon, lat, fill, levels=np.arange(5, 100, 5), cmap=cmap,
                         transform=trans, alpha=0.8, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Probability (%)")
        cs = ax.contour(lon, lat, fill, levels=(25, 50, 75), colors="black",
                        linewidths=0.8, transform=trans)
        ax.clabel(cs, fmt="%d%%", fontsize=6)
    elif product in NBM_ELEMENTS:
        # NBM COG mosaic: one pre-blended field per product
        _elem, _kind, cmap, units_lbl, _lv = NBM_ELEMENTS[product]
        data_arr = f("DATA")
        fill = data_arr if product != "nbm_refc" else np.where(data_arr < 5, np.nan, data_arr)
        levels = {
            "nbm_temp": np.arange(-10, 111, 2),
            "nbm_gust": np.arange(5, 85, 5),
            "nbm_cape": np.arange(100, 5001, 250),
            "nbm_refc": np.arange(5, 70, 5),
        }[product]
        cf = ax.contourf(lon, lat, fill, levels=levels, cmap=cmap,
                         transform=trans, alpha=0.8, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label=units_lbl)
        if product in ("nbm_temp",):
            cs = ax.contour(lon, lat, data_arr, levels=np.arange(20, 110, 10), colors="black",
                            linewidths=0.6, transform=trans)
            ax.clabel(cs, fmt="%d\u00b0F", fontsize=6)
    else:
        raise ValueError(product)

    prod_label = PRODUCTS[product]["label"] if product in PRODUCTS \
        else {"nbm_temp": "NBM Temperature (pre-blended)",
              "nbm_gust": "NBM Wind Gust (pre-blended)",
              "nbm_cape": "NBM SBCAPE (pre-blended)",
              "nbm_refc": "NBM Max Simulated Reflectivity"}.get(product, product)
    ax.set_title(
        f"{prod_label}\n"
        f"{m['label']} \u00b7 init {cycle:%Y-%m-%d %H}Z \u00b7 F{fh:03d} \u00b7 valid {valid:%Y-%m-%d %H}Z",
        fontsize=11, loc="left",
    )
    ax.text(0.99, 0.01, "Tennessee Weather Network \u00b7 data: NOAA/NCEP \u00b7 MetPy",
            transform=ax.transAxes, ha="right", fontsize=7, color="#444444")

    fig.tight_layout()
    fig.savefig(png, bbox_inches="tight")
    plt.close(fig)
    valid = cycle + dt.timedelta(hours=fh)
    return png, {"cycle": cycle.strftime("%Y-%m-%d %H:%M UTC"), "fh": fh,
                 "valid": valid.strftime("%Y-%m-%d %H:%M UTC")}


def clear_map_cache():
    try:
        for name in os.listdir(MAP_DIR):
            if name.endswith(".png"):
                os.remove(os.path.join(MAP_DIR, name))
    except OSError:
        pass

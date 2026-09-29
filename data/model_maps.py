"""NWS forecast-model MAPS: classic weather maps (500 mb, 850 mb, jet, surface).

Reads GFS / NAM / RAP GRIB2 directly from NOAA's AWS open-data buckets via
.idx byte-range fetches, computes meteorological fields with MetPy
(vorticity, absolute vorticity), and renders synoptic-style maps with
matplotlib + Cartopy. No API keys anywhere.

Products: 500 mb heights+vorticity, 850 mb heights+temperature,
700 mb RH, 300 mb jet isotachs, surface MSLP+wind, CAPE.
"""
import datetime as dt
import json
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
        "max_hour": 240,        # snowfall (WEASD) reaches day 10 - winter page (09-20)
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
        "max_hour": 240,        # files ship to f840; 10-day loop reach (09-18)
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
        "max_hour": 240,        # match GEFS mean 10-day reach (09-18)
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
        # 2dfld file carries the surface fields (CAPE/MSLMA/GUST) the prslev file lacks
        "file_type_base": {"2dfld": ("wrfprsf", "wrfsfcf")},
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
        "label": "NBM - National Blend of Models (NOMADS core)",
        "style": "nbm",
        # NOMADS core GRIB2 (the noaa-nbm-pds COG mirror stopped publishing
        # 2026-09-14 and starved every NBM map - see _find_nbm_cycle).
        "base": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod",
        "cycles": [0, 1, 2, 3, 4, 5, 6],
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
    "AI-Aurora": {
        "label": "Aurora AI - GFS init (Microsoft, 0.25\u00b0 global)",
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
    "SREF": {
        "label": "SREF - Short-Range Ensemble Mean (NCEP, 3-hourly to f87)",
        "style": "sref",
        # One per-cycle ensemble-MEAN file (ensprod/sref.tHHz.pgrb132.mean_3hrly)
        # packs EVERY forecast hour, so base carries no {fh} - the .idx URL
        # comes from the style override in _idx_url and lead times are matched
        # inside the idx by _sref_range (2026-09-20).
        "base": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/sref/prod",
        # render fallback walk-back: 6h -> previous SREF cycle (03/09/15/21Z)
        "cycles": [6, 12],
        "max_hour": 87,
        "hour_step": 3,
    },
    "EPS-Weekly": {
        "label": "ECMWF EPS Weekly - 50-member ensemble to day 15",
        "style": "ecmwf-ens",
        # ECMWF IFS ensemble (ifs/0p25/enfo, enfo-ef files): 3-hourly to f144,
        # 6-hourly to f360 = 15 days - the open-data "EPS weeklies" (the
        # official week-3..6 weekly-mean anomaly products need an ECMWF
        # license). No precomputed mean ships on the open bucket, so fields
        # are fetched for the first few perturbed members and AVERAGED (see
        # _fetch_eps_fields). Cycles 00/12Z, published ~8 h after init.
        "max_hour": 360,
        "hour_step": 6,
        # render fallback walk-back: 12h -> previous EPS cycle (00Z <-> 12Z)
        "cycles": [12, 24],
        "synoptic": True,
    },
}

# Which products each model supports (verified against each feed's .idx;
# REFC lives in HRRR's *surface* files, not its pressure-level files)
PRODUCTS_BY_MODEL = {
    "GFS": ["refc", "500_vort", "500_tmp", "600_tmp", "850_tmp", "925_tmp", "700_rh", "300_jet", "250_jet",
            "200_jet", "sfc_mslp", "cape_wind", "mucape", "pwat", "sfc_gust", "tcdc", "vis",
            "snow", "prate", "thickness", "700_w", "sfc_dew", "shear06", "lr75", "scp", "ehi", "stp", "ship",
            "850_vort", "200_div", "3var_fronts", "frz_lvl"],
    "NAM": ["refc", "500_vort", "500_tmp", "600_tmp", "850_tmp", "925_tmp", "700_rh", "300_jet", "250_jet",
            "200_jet", "sfc_mslp", "cape_wind", "pwat", "sfc_gust", "tcdc", "vis", "snow",
            "850_vort", "200_div", "3var_fronts", "frz_lvl"],
    "RAP": ["refc", "500_vort", "500_tmp", "850_tmp", "925_tmp", "300_jet", "250_jet", "200_jet",
            "sfc_mslp", "cape_wind", "mucape", "pwat", "tcdc", "vis", "sfc_gust"],
    "GEFS": ["500_vort", "500_tmp", "850_tmp", "925_tmp", "700_rh", "300_jet", "250_jet",
             "200_jet", "sfc_mslp", "cape_wind", "pwat", "tcdc", "snow", "thickness", "shear06", "lr75", "ship",
             # no scp/stp/ehi: GEFS mean files ship no HLCY (verified in the
             # 2026-09-23 18Z geavg idx - only CAPE 180-0/CIN/PWAT), and the
             # member-mean SRH they'd need does not exist either
             "850_vort", "200_div", "3var_fronts"],
    "GEFS-Spread": ["h500_spread", "sp_850_tmp", "sp_925_tmp", "sp_700_rh",
                    "sp_300_jet", "sp_250_jet", "sp_200_jet", "sp_sfc_mslp",
                    "sp_cape_wind", "sp_pwat", "sp_tcdc", "sp_snow",
                    "sp_apcp"],
    # ECMWF/AIFS/AI families expanded 2026-09-22 'more levels': every level
    # verified present in each source's live index before cataloging (a
    # level the source never publishes would render forever-empty and
    # starve the rotation queue)
    "ECMWF": ["500_vort", "500_tmp", "600_tmp", "600_rh", "850_tmp", "925_tmp", "700_rh",
              "300_jet", "250_jet", "200_jet", "sfc_mslp", "pwat", "tcdc", "sfc_gust", "prate",
              "thickness", "700_w", "sfc_dew", "shear06", "lr75",
              "850_vort", "200_div", "3var_fronts"],
    # sfc_gust + refc + hail + uphl: 2dfld staging file carries GUST, REFC,
    # HAIL and MXUPHL (verified 2026-09-23 / 2026-09-25). scp/stp/ehi/ship:
    # the 2dfld file ships every SCP/STP/EHI/SHIP input (CAPE, HLCY 3-0 km,
    # 2 m T/Td, 10 m + 500 mb winds) - verified live 2026-09-25.
    "RRFS": ["refc", "cape_wind", "mucape", "hail", "uphl", "scp", "stp", "ehi", "ship",
             "shear06", "shear01", "500_vort", "500_tmp", "600_tmp", "850_tmp", "925_tmp",
             "700_rh", "300_jet", "250_jet", "200_jet", "pwat", "lr75", "sfc_gust",
             "850_vort", "200_div", "3var_fronts"],
    "HRRR": ["refc", "hail", "uphl", "shear01", "500_vort", "500_tmp", "600_tmp", "850_tmp", "925_tmp", "700_rh", "300_jet", "250_jet",
             "200_jet", "sfc_mslp", "cape_wind", "mucape", "pwat", "sfc_gust", "tcdc", "vis",
             "snow", "prate", "thickness", "700_w", "sfc_dew",
             "850_vort", "200_div", "3var_fronts", "frz_lvl",
             # SPC composite family (2026-09-23): wrfprsf ships every input
             # (CAPE 90-0, HLCY 3000-0, 2 m T/Td, 10 m + 500 mb winds) - puts
             # a 4th model on the SCP/STP/EHI walls so they clear the page's
             # 4-model dropdown bar like SHIP already does
             "scp", "stp", "ehi", "ship", "shear06"],
    "NBM": ["nbm_temp", "nbm_gust", "nbm_cape", "nbm_refc", "nbm_dew",
            "nbm_qpf", "nbm_pwat", "nbm_snow06", "nbm_tstm", "nbm_vis",
            "nbm_cloud", "nbm_wbgt"],
    "CFS": ["500_vort", "500_tmp", "850_tmp", "925_tmp", "700_rh", "300_jet", "250_jet", "200_jet",
            "lr75"],
    "AIFS": ["500_vort", "500_tmp", "600_tmp", "600_rh", "850_tmp", "925_tmp", "700_rh",
             "300_jet", "250_jet", "200_jet", "sfc_mslp", "pwat", "tcdc",
             "thickness", "700_w", "sfc_dew", "shear06", "lr75",
             "850_vort", "200_div", "3var_fronts"],
    "AIFS-ENS": ["500_vort", "500_tmp", "600_tmp", "600_rh", "850_tmp", "925_tmp", "700_rh",
                 "300_jet", "250_jet", "200_jet", "sfc_mslp", "pwat", "tcdc",
                 "thickness", "700_w", "sfc_dew", "shear06", "lr75",
                 "850_vort", "200_div", "3var_fronts"],
    # AIWP NetCDFs carry u/v/t/z at all 13 levels 50-1000 hPa (verified in
    # the live file headers 2026-09-22) - the full synoptic level suite
    "AI-GraphCast": ["500_vort", "500_tmp", "600_tmp", "600_rh", "850_tmp", "925_tmp", "700_rh",
                     "300_jet", "250_jet", "200_jet", "sfc_mslp", "pwat", "ai_precip",
                     "thickness", "700_w", "sfc_dew", "shear06", "lr75",
                     "850_vort", "200_div", "3var_fronts"],
    "AI-Pangu": ["500_vort", "500_tmp", "600_tmp", "600_rh", "850_tmp", "925_tmp", "700_rh",
                 "300_jet", "250_jet", "200_jet", "sfc_mslp", "thickness", "700_w", "sfc_dew",
                 "shear06", "lr75", "850_vort", "200_div", "3var_fronts"],
    "AI-FourCastNet": ["500_vort", "500_tmp", "600_tmp", "600_rh", "850_tmp", "925_tmp", "700_rh",
                       "300_jet", "250_jet", "200_jet", "sfc_mslp", "pwat",
                       "thickness", "sfc_dew", "shear06", "lr75",
                       "850_vort", "200_div", "3var_fronts"],
    "AI-Aurora": ["500_vort", "500_tmp", "600_tmp", "600_rh", "850_tmp", "925_tmp", "700_rh",
                  "300_jet", "250_jet", "200_jet", "sfc_mslp", "thickness", "700_w", "sfc_dew",
                  "shear06", "lr75", "850_vort", "200_div", "3var_fronts"],
    "HREF": ["ship", "cam_pmmn", "cam_mean_500", "cam_mean_srh", "cam_prob_uphl", "cam_prob_ltng",
             "mucape", "500_vort", "500_tmp", "850_tmp", "925_tmp", "700_rh", "250_jet",
             "vis", "snow", "scp", "ehi", "shear06", "stp"],
    "REFS": ["ship", "cam_pmmn", "cam_mean_500", "cam_mean_srh", "cam_prob_uphl", "cam_prob_ltng",
             "cam_h500_spread", "mucape", "500_vort", "500_tmp", "850_tmp", "925_tmp",
             "700_rh", "250_jet", "tcdc", "vis", "snow", "scp", "ehi", "stp"],
    # SREF ens-mean carries ABSV natively (no vorticity math needed) plus the
    # full synoptic suite and precip-type probabilities - verified in the
    # 2026-09-20 09Z mean-file idx (2026-09-20)
    "SREF": ["ship", "sref_500_vort", "500_tmp", "850_tmp", "600_rh", "700_rh", "300_jet", "sfc_mslp",
             "cape_wind", "pwat", "sref_csnow", "sref_cfrzr", "sref_cicep", "snow", "tcdc",
             "thickness", "sfc_dew", "shear06", "lr75", "scp", "ehi", "stp",
             # no 200_div: SREF tops out at 300 mb (250/200 mb winds absent
             # from its mean file catalog)
             "850_vort", "3var_fronts"],
    # EPS-Weekly: IFS ensemble MEAN charts on the extended day-4..15 range -
    # the "will the pattern support a storm in week 2" view (2026-09-20)
    "EPS-Weekly": ["500_vort", "sfc_mslp", "850_tmp", "pwat", "snow", "850_vort", "200_div", "3var_fronts"],
}

# NBM COG element per product: (element_name, kind, cmap, units, levels_fn)
# (2026-09-14) expanded from 4 to 10 products - every element here was verified
# present in the live blendv5.0/conus bucket listing.
NBM_ELEMENTS = {
    # (grib2 shortName, grib2 level string, cmap, legend label, fill levels)
    # Source: NOMADS blend core GRIB2 (blend.tHHz.core.fFFF.co.grib2). Values
    # arrive in SI (K, m, J/kg...) and are converted in _fetch_nbm_fields.
    "nbm_temp":  ("TMP", "2 m above ground", "RdBu_r", "Temperature (°F)",
                  np.arange(-10, 111, 2)),
    "nbm_dew":   ("DPT", "2 m above ground", "RdYlBu_r", "Dew point (°F)",
                  np.arange(10, 86, 2)),
    "nbm_gust":  ("GUST", "10 m above ground", "YlOrRd", "Wind gust (mph)",
                  np.arange(5, 85, 5)),
    "nbm_cape":  ("CAPE", "surface", "spc", "SBCAPE (J/kg)",
                  np.arange(100, 5001, 250)),
    "nbm_qpf":   ("APCP", "surface", "YlGnBu", "6-h QPF (in)",
                  [0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]),
    "nbm_pwat":  ("PWAT", "entire atmosphere (considered as a single layer)",
                  "PuBuGn", "Precipitable water (in)",
                  np.arange(0.2, 2.85, 0.15)),
    "nbm_snow06": ("ASNOW", "surface", "cool", "6-h snow accumulation (in)",
                   [0.01, 0.1, 0.5, 1, 2, 4, 6, 9, 12, 18]),
    "nbm_tstm":  ("TSTM", "surface", "YlOrRd", "1-h thunderstorm probability (%)",
                  np.arange(5, 65, 5)),
    "nbm_vis":   ("VIS", "surface", "YlOrBr_r", "Visibility (mi)",
                  [0, 0.25, 0.5, 1, 2, 3, 5, 8, 10]),
    "nbm_cloud": ("TCDC", "surface", "Greys", "Total cloud cover (%)",
                  np.arange(10, 105, 10)),
    "nbm_wbgt":  ("WETGLBT", "surface", "YlOrRd", "WBGT (°F)",
                  np.arange(70, 106, 2)),
    # The blend ships no max-reflectivity field (REFC); VIL (vertically
    # integrated liquid) is the storm-strength proxy it does publish.
    "nbm_refc":  ("VIL", "entire atmosphere", "turbo", "Storm strength - VIL (kg/m²)",
                  np.arange(0.5, 30.5, 1.5)),
}
# Human labels for the pre-blended NBM products (map titles)
NBM_LABELS = {
    "nbm_temp": "NBM Temperature (pre-blended)",
    "nbm_gust": "NBM Wind Gust (pre-blended)",
    "nbm_cape": "NBM SBCAPE (pre-blended)",
    "nbm_refc": "NBM Storm Strength (VIL)",
    "nbm_dew": "NBM Dew Point (pre-blended)",
    "nbm_qpf": "NBM 6-h QPF (pre-blended)",
    "nbm_pwat": "NBM Precipitable Water",
    "nbm_snow06": "NBM 6-h Snow Accumulation",
    "nbm_tstm": "NBM 1-h Thunderstorm Probability",
    "nbm_vis": "NBM Visibility",
    "nbm_cloud": "NBM Total Cloud Cover",
    "nbm_wbgt": "NBM Wet-Bulb Globe Temperature",
}


# ECMWF open-data params per product: (param, "pl:<hPa>" or "sfc")
ECMWF_PARAMS_BASE = {
    "500_vort": [("z", "pl:500"), ("u", "pl:500"), ("v", "pl:500")],
    "850_tmp": [("z", "pl:850"), ("t", "pl:850"), ("u", "pl:850"), ("v", "pl:850")],
    "500_tmp": [("z", "pl:500"), ("t", "pl:500"), ("u", "pl:500"), ("v", "pl:500")],
    "300_jet": [("z", "pl:300"), ("u", "pl:300"), ("v", "pl:300")],
    "250_jet": [("z", "pl:250"), ("u", "pl:250"), ("v", "pl:250")],
    "200_jet": [("z", "pl:200"), ("u", "pl:200"), ("v", "pl:200")],
    # tropical pair (2026-09-23 'more panels'): 850 mb spin and 200 mb
    # outflow - the two levels tropical forecasters live on
    "850_vort": [("z", "pl:850"), ("u", "pl:850"), ("v", "pl:850")],
    "200_div": [("z", "pl:200"), ("u", "pl:200"), ("v", "pl:200")],
    # 600 mb thermal chart (added 2026-09-22 'more levels'): the melt-layer
    # level - sit between the 850 low-level warmth and the 500 cold pool to
    # read the warm-nose / dendritic-growth-zone sandwich on winter events
    "600_tmp": [("z", "pl:600"), ("t", "pl:600"), ("u", "pl:600"), ("v", "pl:600")],
    # SREF's ens-mean ships RH only up to 500 mb - its 600/700/925 requests
    # never match an idx line, so the strict all-fields gate kills every
    # map. vars_by_model: 700 RH with winds; 600 RH height-only (verified
    # in the 2026-09-22 09Z mean idx: RH at 300/500/600/700, UGRD at 700)
    "700_rh": [("z", "pl:700"), ("r", "pl:700"), ("u", "pl:700"), ("v", "pl:700")],
    "600_rh": [("z", "pl:600"), ("r", "pl:600")],
    "925_tmp": [("z", "pl:925"), ("t", "pl:925"), ("u", "pl:925"), ("v", "pl:925")],
    # 1000-500 mb thickness + the 540 dam (5400 m) rain/snow line - the
    # single most-asked-for winter chart (2026-09-22 'more products').
    # Level-suffixed 'z' so both heights survive the field dict.
    "thickness": [("z", "pl:500"), ("z@1000 mb", "pl:1000"),
                  ("msl", "sfc"), ("10u", "sfc"), ("10v", "sfc")],
    # 700 mb omega (pressure velocity, Pa/s): where the atmosphere is rising
    "700_w": [("w", "pl:700"), ("z", "pl:700"), ("u", "pl:700"), ("v", "pl:700")],
    # 2 m dew point: the moisture/fog/latent-heat surface chart
    "sfc_dew": [("2d", "sfc"), ("msl", "sfc"), ("10u", "sfc"), ("10v", "sfc")],
    # severe walls (2026-09-22): bulk shear from 10 m vs 500 mb winds, the
    # supercell discriminator; mid-level lapse rate from 700/500 temps.
    # Both wind levels carry suffixed params (10u and u both canonicalize
    # to UGRD) so the field dict holds all four components under the
    # renderer's names.
    "shear06": [("10u@10 m above ground", "sfc"), ("10v@10 m above ground", "sfc"),
                ("u@500 mb", "pl:500"), ("v@500 mb", "pl:500"), ("msl", "sfc")],
    "lr75": [("t", "pl:700"), ("t@500 mb", "pl:500"), ("gh", "pl:700"), ("gh@500 mb", "pl:500")],
    "sfc_mslp": [("msl", "sfc"), ("10u", "sfc"), ("10v", "sfc"), ("2t", "sfc")],
    # 10fg = max 10 m gust; msl/10u/10v feed the isobar overlay + barbs
    "sfc_gust": [("10fg", "sfc"), ("msl", "sfc"), ("10u", "sfc"), ("10v", "sfc")],
    "prate": [("tprate", "sfc"), ("msl", "sfc"), ("10u", "sfc"), ("10v", "sfc")],
    "pwat": [("tcw", "sfc")],
    "tcdc": [("tcc", "sfc"), ("msl", "sfc"), ("10u", "sfc"), ("10v", "sfc")],
    "mucape": [("cape", "sfc"), ("msl", "sfc"), ("10u", "sfc"), ("10v", "sfc")],
    "cape_wind": [("cape", "sfc"), ("10u", "sfc"), ("10v", "sfc"), ("msl", "sfc")],
}
# ECMWF products by MODEL: oper/AIFS files key off 'z', the IFS ensemble key
# off 'gh' for the same field - so 500_vort/850_tmp swap the geopotential
# shortName for EPS-Weekly (verified in the enfo-ef index, 2026-09-20)
ECMWF_PARAMS = ECMWF_PARAMS_BASE.copy()
ECMWF_PARAMS["snow"] = [("sd", "sfc")]   # ensemble ships snow DEPTH (m w.e.)
ECMWF_PARAMS["pwat"] = ECMWF_PARAMS_BASE["pwat"]
# the surface-analysis composite: ECMWF 'gh' heights (ens files) swap inside
# _ecmwf_params; msl/2t/10u/10v are sfc params shared with existing products
ECMWF_PARAMS["3var_fronts"] = [
    ("msl", "sfc"),
    ("z@500 mb", "pl:500"), ("z@1000 mb", "pl:1000"),
    ("10u", "sfc"), ("10v", "sfc"), ("2t", "sfc"),
]


def _ecmwf_params(model, product):
    """ECMWF param list for one model+product ('gh' vs 'z' ensemble split)."""
    if model == "EPS-Weekly" and product in ECMWF_PARAMS_BASE:
        base = []
        for param, level in ECMWF_PARAMS_BASE[product]:
            # 'z' only exists in the single-level files; the ensemble idx
            # carries the same field as 'gh'
            base.append(("gh" if param == "z" else param, level))
        return base
    return ECMWF_PARAMS.get(product)

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
        "vars": [("HGT", "700 mb"), ("RH", "700 mb"), ("UGRD", "700 mb"), ("VGRD", "700 mb")],
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
        "vars": [("CAPE", "surface"), ("PRMSL", "mean sea level"), ("UGRD", "10 m above ground"),
                 ("VGRD", "10 m above ground")],
        # GEFS mean files carry CAPE only as a mixed-layer 180-0 mb quantity
        "vars_by_model": {"GEFS": [("CAPE", "180-0 mb above ground"),
                                   ("UGRD", "10 m above ground"),
                                   ("VGRD", "10 m above ground")],
                          "GEFS-Spread": [("CAPE", "180-0 mb above ground"),
                                          ("UGRD", "10 m above ground"),
                                          ("VGRD", "10 m above ground")]},
    },
    "refc": {
        "label": "Composite radar - simulated reflectivity",
        "desc": "Simulated composite reflectivity (dBZ) - what radar echoes the model expects",
        "vars": [("REFC", "entire atmosphere")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},  # REFC lives in HRRR surface files
        # RRFS: handled in _idx_url - REFC ships in its 2dfld staging file
        # (verified in the 2026-09-25 12Z 2dfld idx), not the prslev base
    },
    "hail": {
        "label": "Hail diameter - max hourly (mm)",
        "desc": "Model forecast of maximum hail diameter (mm) in the hour - the hail-shaft map",
        "vars": [("HAIL", "entire atmosphere")],
        "vars_by_model": {
            # RRFS 2dfld idx carries HAIL at 'surface' (verified 2026-09-25
            # 12Z idx); HRRR names the layer 'entire atmosphere'
            "RRFS": [("HAIL", "surface")]},
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},
    },
    "uphl": {
        "label": "Updraft Helicity max 2-5 km (rotation)",
        "desc": "Max updraft helicity (m\u00b2/s\u00b2) - the rotating-updraft / mesocyclone map",
        "vars": [("MXUPHL", "2000-0 m above ground")],
        "vars_by_model": {
            # RRFS 2dfld publishes MXUPHL as 3000-0 m and 5000-2000 m (no
            # 2000-0 layer; verified 2026-09-25 12Z idx) - 3000-0 is the
            # closest rotation proxy
            "RRFS": [("MXUPHL", "3000-0 m above ground")]},
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},
    },
    "shear01": {
        "label": "0-1 km Bulk Shear (surface-925 mb winds)",
        "desc": "Low-level shear magnitude (kt): storm inflow, tornadic potential, spin-up risk",
        "vars": [("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"),
                 ("UGRD@925 mb", "925 mb"), ("VGRD@925 mb", "925 mb"),
                 ("PRMSL", "mean sea level")],
        "vars_by_model": {
            # RRFS prslev carries no 10 m winds and no MSLP (verified in the
            # 2026-09-25 12Z idx) - 925 mb anchors both legs; the render
            # branch treats the 10 m leg as optional, 925 mb as required
            "RRFS": [("UGRD@925 mb", "925 mb"), ("VGRD@925 mb", "925 mb")]},
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
    "600_tmp": {
        "label": "600 mb - Heights + Temperature + Wind",
        "desc": "The melt-layer level: between 850 warmth and 500 cold - warm nose vs DGLZ on winter events",
        "vars": [("HGT", "600 mb"), ("TMP", "600 mb"), ("UGRD", "600 mb"), ("VGRD", "600 mb")],
    },
    "600_rh": {
        "label": "600 mb - Heights + Relative Humidity",
        "desc": "Mid-level moisture near the dendritic growth zone - dry slots inside precip shields",
        "vars": [("HGT", "600 mb"), ("RH", "600 mb")],
    },
    "thickness": {
        "label": "1000-500 mb Thickness + 540 dam line",
        "desc": "The classic rain/snow chart: 540 dam line marks the rain-snow transition",
        "vars": [("HGT", "500 mb"), ("HGT@1000 mb", "1000 mb"), ("PRMSL", "mean sea level"),
                 ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
        # RRFS splits surface fields into a separate 2dfld file the pressure
        # idx can't reach - heights-only thickness for it (render branch
        # draws the isobar overlay and barbs only when the fields exist)
        "vars_by_model": {"RRFS": [("HGT", "500 mb"), ("HGT@1000 mb", "1000 mb")]},
    },
    "700_w": {
        "label": "700 mb - Omega (vertical motion)",
        "desc": "Pressure velocity (Pa/s): where the atmosphere is rising (blue) or sinking",
        "vars": [("VVEL", "700 mb"), ("HGT", "700 mb"), ("UGRD", "700 mb"), ("VGRD", "700 mb")],
    },
    "sfc_dew": {
        "label": "Surface - 2 m Dew Point + MSLP",
        "desc": "Low-level moisture: dewpoints, isobars and wind - fog, storms, Gulf returns",
        "vars": [("DPT", "2 m above ground"), ("PRMSL", "mean sea level"),
                 ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
    },
    "shear06": {
        "label": "0-6 km Bulk Shear (surface-500 mb winds)",
        "desc": "Deep-layer shear magnitude (kt): the supercell vs ordinary-cell discriminator",
        "vars": [("UGRD@10 m above ground", "10 m above ground"), ("VGRD@10 m above ground", "10 m above ground"),
                 ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb"), ("PRMSL", "mean sea level")],
        # HREF mean files carry no 10 m winds and no MSLP (verified 2026-09-22)
        # - 925 mb (~750 m AGL) stands in as the low-level shear anchor, and
        # the MSLP overlay is dropped (the renderer falls back to 925 mb too)
        "vars_by_model": {"HREF": [("UGRD@925 mb", "925 mb"), ("VGRD@925 mb", "925 mb"),
                                   ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb")]},
    },
    "850_vort": {
        "label": "850 mb - Vorticity + Winds (tropical)",
        "desc": "Low-level spin: the tropical wave/depression tracker - vorticity maxima mark circulation centers",
        "vars": [("HGT", "850 mb"), ("UGRD", "850 mb"), ("VGRD", "850 mb")],
    },
    "200_div": {
        "label": "200 mb - Divergence + Winds (outflow)",
        "desc": "Upper outflow: divergence aloft over a tropical cyclone signals intensification; jet-side exits too",
        "vars": [("HGT", "200 mb"), ("UGRD", "200 mb"), ("VGRD", "200 mb")],
    },
    "frz_lvl": {
        "label": "0C Isotherm Height (freezing level)",
        "desc": "Height of the 0C layer: winter's rain/snow/zr split - near-ground = snow, mid-levels = sleet, deep warm nose = zr",
        "vars": [("HGT", "0C isotherm"), ("PRMSL", "mean sea level")],
    },
    "3var_fronts": {
        "label": "Surface - Fronts Analysis (MSLP + 540 line + temps)",
        "desc": "The classic surface analysis: isobars, 540 dam rain/snow line, 2 m temperature field and winds",
        "vars": [("PRMSL", "mean sea level"), ("HGT", "500 mb"), ("HGT@1000 mb", "1000 mb"),
                 ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"),
                 ("TMP", "2 m above ground")],
        # RRFS splits surface fields into a separate 2dfld file the pressure
        # idx can't reach - heights-only fronts view for it (render branch
        # draws isobars/temps/barbs only when the fields exist)
        "vars_by_model": {"RRFS": [("HGT", "500 mb"), ("HGT@1000 mb", "1000 mb")]},
    },
    "lr75": {
        "label": "700-500 mb Lapse Rate (mid-level instability)",
        "desc": "Mid-level steepness (C/km): steep = hail, cold pools, downburst potential",
        "vars": [("TMP", "700 mb"), ("TMP@500 mb", "500 mb"), ("HGT", "700 mb"), ("HGT@500 mb", "500 mb")],
    },
    # Supercell Composite Parameter (2026-09-22 'severe parameters'): the
    # SPC composite (MUCAPE/1000) x (ESRH/50) x (EBWD/20), each term capped
    # at 1.5. EBWD is approximated with the 0-6 km bulk shear (10 m vs
    # 500 mb, the shear06 convention) - cataloged ONLY for models whose
    # files ship both a CAPE layer and HLCY 0-3 km (verified in each live
    # index 2026-09-22: GFS/HREF/REFS/SREF; ECMWF has mucape but no HLCY,
    # AIWP NetCDFs carry neither, HRRR/NAM/RAP/RRFS lack HLCY).
    "scp": {
        "label": "Supercell Composite Parameter (CAPE x shear x helicity)",
        "desc": "SPC-style SCP: instability x helicity x deep shear - where rotating storms organize",
        "vars": [("CAPE", "90-0 mb above ground"), ("HLCY", "3000-0 m"),
                 ("PRMSL", "mean sea level"), ("UGRD", "10 m above ground"),
                 ("VGRD", "10 m above ground"), ("UGRD@500 mb", "500 mb"),
                 ("VGRD@500 mb", "500 mb")],
        "vars_by_model": {
            # SREF mean ships only surface-based CAPE - a fine SCP proxy
            "SREF": [("CAPE", "surface"), ("HLCY", "3000-0 m"),
                     ("PRMSL", "mean sea level"), ("UGRD", "10 m above ground"),
                     ("VGRD", "10 m above ground"), ("UGRD@500 mb", "500 mb"),
                     ("VGRD@500 mb", "500 mb")],
            # HREF mean files ship NO mean-sea-level pressure and NO 10 m
            # winds (verified in the 20260922 12Z idx - winds start at 925
            # mb), so 925 mb (~750 m AGL) stands in as the low-level shear
            # anchor and the MSLP overlay is dropped
            "HREF": [("CAPE", "90-0 mb above ground"), ("HLCY", "3000-0 m"),
                     ("UGRD@925 mb", "925 mb"), ("VGRD@925 mb", "925 mb"),
                     ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb")],
            # REFS mean files also carry no MSLP (but do ship 10 m winds)
            "REFS": [("CAPE", "90-0 mb above ground"), ("HLCY", "3000-0 m"),
                     ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"),
                     ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb")],
        },
    },
    # Energy Helicity Index (2026-09-22, 'severe parameters' follow-up): the
    # SPC two-term composite (CAPE x ESRH / 160000). Same helicity + CAPE
    # fields the SCP wall uses, so the same models carry it - no winds or
    # MSLP needed, which also makes these the fastest severe renders.
    "ehi": {
        "label": "Energy Helicity Index (CAPE x helicity)",
        "desc": "EHI: instability x storm-relative helicity - >= 1 marks supercell-favorable air",
        "vars": [("CAPE", "90-0 mb above ground"), ("HLCY", "3000-0 m")],
        "vars_by_model": {
            "SREF": [("CAPE", "surface"), ("HLCY", "3000-0 m")],
        },
    },
    # Significant Tornado Parameter (2026-09-22, SPC-palette family): the
    # CIN-only effective-layer term - (mucape/1500) x (esrh/150) x
    # (ebwd/22.5) x (lcl_lfcape/2000), lcl term = max((2000 - LCLm)/1000, 0)
    # with LCLm from the 2 m T/Td Espy split. Cataloged ONLY where all four
    # inputs exist (verified in each live index 2026-09-22: GFS, HREF, REFS,
    # SREF carry CAPE + HLCY + 2 m temps + 2 m dewpoints; the AI NetCDFs and
    # ECMWF open-data lack the CAPE/HLCY pair).
    "stp": {
        "label": "Significant Tornado Parameter (CIN-only)",
        "desc": "SPC-style STP: CAPE x helicity x shear x low LCL - >= 1 marks tornado-favorable air",
        "vars": [("CAPE", "90-0 mb above ground"), ("HLCY", "3000-0 m"),
                 ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"),
                 ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb"),
                 ("TMP", "2 m above ground"), ("DPT", "2 m above ground")],
        "vars_by_model": {
            "SREF": [("CAPE", "surface"), ("HLCY", "3000-0 m"),
                     ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"),
                     ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb"),
                     ("TMP", "2 m above ground"), ("DPT", "2 m above ground")],
            # HREF mean files ship no 10 m winds (verified 2026-09-22) -
            # 925 mb stands in as the low-level shear anchor, as on scp
            "HREF": [("CAPE", "90-0 mb above ground"), ("HLCY", "3000-0 m"),
                     ("UGRD@925 mb", "925 mb"), ("VGRD@925 mb", "925 mb"),
                     ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb"),
                     ("TMP", "2 m above ground"), ("DPT", "2 m above ground")],
        },
    },
    # Significant Hail Parameter (2026-09-22 'SPC composite family'): SHIP
    # blends the four ingredients SPC's parameter combines - instability,
    # 700-500 mb lapse rate, mid-level moisture (850-700 mb mean RH) and
    # deep-layer shear - as (mucape/1000) x (mllr/5.6) x (rhmid/50) x
    # (ebwd/50), each term capped at 1.5 (the site's SCP/STP convention;
    # SPC's exact constants are unpublished). SHIP >= 1 marks
    # significant-hail environments. Cataloged ONLY for models whose files
    # carry every input (verified in each live index 2026-09-22: GFS, GEFS,
    # HREF, REFS, SREF - the scp family; ECMWF's open-data GRIB carries NO
    # cape param at all (40k-entry index checked) and the AI NetCDFs carry
    # neither CAPE nor RH planes; HREF mean files lack 850 mb RH and
    # 10 m winds -> 700 mb RH + 925 mb fallbacks, REFS lacks only 850 mb RH).
    "ship": {
        "label": "Significant Hail Parameter (CAPE x lapse x moisture x shear)",
        "desc": "SPC-style SHIP: four hail ingredients in one index - >= 1 marks significant-hail air",
        "vars": [("CAPE", "90-0 mb above ground"), ("TMP", "700 mb"), ("TMP@500 mb", "500 mb"),
                 ("RH", "850 mb"), ("RH@700 mb", "700 mb"),
                 ("UGRD@10 m above ground", "10 m above ground"), ("VGRD@10 m above ground", "10 m above ground"),
                 ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb")],
        "vars_by_model": {
            "SREF": [("CAPE", "surface"), ("TMP", "700 mb"), ("TMP@500 mb", "500 mb"),
                     ("RH", "850 mb"), ("RH@700 mb", "700 mb"),
                     ("UGRD@10 m above ground", "10 m above ground"), ("VGRD@10 m above ground", "10 m above ground"),
                     ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb")],
            # GEFS mean files key CAPE as the 180-0 mb mixed-layer quantity
            "GEFS": [("CAPE", "180-0 mb above ground"), ("TMP", "700 mb"), ("TMP@500 mb", "500 mb"),
                     ("RH", "850 mb"), ("RH@700 mb", "700 mb"),
                     ("UGRD@10 m above ground", "10 m above ground"), ("VGRD@10 m above ground", "10 m above ground"),
                     ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb")],
            # HREF mean files ship no 850 mb RH and no 10 m winds (verified
            # 2026-09-22) - 700 mb RH and 925 mb winds stand in, as on scp/stp
            "HREF": [("CAPE", "90-0 mb above ground"), ("TMP", "700 mb"), ("TMP@500 mb", "500 mb"),
                     ("RH", "700 mb"),
                     ("UGRD@925 mb", "925 mb"), ("VGRD@925 mb", "925 mb"),
                     ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb")],
            # REFS mean files ship no 850 mb RH - 700 mb stands in
            "REFS": [("CAPE", "90-0 mb above ground"), ("TMP", "700 mb"), ("TMP@500 mb", "500 mb"),
                     ("RH", "700 mb"),
                     ("UGRD@10 m above ground", "10 m above ground"), ("VGRD@10 m above ground", "10 m above ground"),
                     ("UGRD@500 mb", "500 mb"), ("VGRD@500 mb", "500 mb")],
        },
    },
    "pwat": {
        "label": "Precipitable Water (PWAT)",
        "desc": "Total atmospheric moisture (mm) - rain-heavy air masses show up instantly",
        "vars": [("PWAT", "entire atmosphere"), ("PRMSL", "mean sea level"),
                 ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
        "vars_by_model": {"RRFS": [("PWAT", "30-0 mb above ground")]},  # RRFS labels the layer oddly
    },
    "mucape": {
        "label": "MUCAPE - Most-Unstable CAPE",
        "desc": "Instability for the most unstable parcel (J/kg) - storm fuel",
        "vars": [("CAPE", "90-0 mb above ground"), ("PRMSL", "mean sea level"),
                 ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},  # HRRR CAPE lives in surface files
    },
    "sfc_gust": {
        "label": "Surface Wind Gusts + Winds",
        "desc": "Forecast gusts (mph) with 10 m wind barbs",
        "vars": [("GUST", "surface"), ("PRMSL", "mean sea level"), ("UGRD", "10 m above ground"),
                 ("VGRD", "10 m above ground")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},  # gusts live in HRRR surface files
    },
    "tcdc": {
        "label": "Total Cloud Cover",
        "desc": "Cloud-cover fraction (%), whole atmospheric column",
        "vars": [("TCDC", "entire atmosphere"), ("PRMSL", "mean sea level"),
                 ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},
    },
    "vis": {
        "label": "Surface Visibility",
        "desc": "Horizontal visibility (miles) - fog and haze at a glance",
        "vars": [("VIS", "surface"), ("PRMSL", "mean sea level"),
                 ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},
    },
    "snow": {
        "label": "Snow Depth (accumulated)",
        "desc": "Snow depth on the ground (inches)",
        "vars": [("WEASD", "surface"), ("PRMSL", "mean sea level"),
                 ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},
    },
    "prate": {
        "label": "Precipitation Rate",
        "desc": "Instant rain rate (in/hr) - where it's pouring right now",
        "vars": [("PRATE", "surface"), ("PRMSL", "mean sea level"),
                 ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
        "file_type": {"HRRR": ("wrfprsf", "wrfsfcf")},
    },
    "h500_spread": {
        "label": "500 mb Height SPREAD (ensemble uncertainty)",
        "desc": "GEFS ensemble spread of 500 mb heights - large values = low forecast confidence",
        "vars": [("HGT", "500 mb")],
    },
    # --- GEFS-Spread family (sp_*): ensemble STANDARD-DEVIATION versions of
    # the mean charts, rendered from the gespr.* files. Each reuses the base
    # product's var list + render branch (see _SPREAD_OF below), so "where is
    # the forecast uncertain" is readable in the same style as the forecast
    # itself. gespr files carry every field the geavg files do, including
    # PRMSL and 2 m temps (verified in the 2026-09-18 f003 idx). ---
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
    # --- SREF ensemble-mean products (2026-09-20) ---
    # SREF ships ABSV (absolute vorticity) natively, so its 500 mb chart
    # shades the analyzed field instead of deriving relative vorticity with
    # MetPy (the generic 500_vort path). Levels verified in the 09Z mean idx.
    "sref_500_vort": {
        "label": "SREF 500 mb - Heights + Abs. Vorticity + Wind",
        "desc": "Ensemble-mean upper-air chart: heights (dam), absolute vorticity shaded, winds",
        "vars": [("ABSV", "500 mb"), ("HGT", "500 mb"), ("UGRD", "500 mb"), ("VGRD", "500 mb")],
    },
    "sref_csnow": {
        "label": "SREF Snow Probability",
        "desc": "Ensemble probability (%) that falling precipitation is snow - winter storm signal",
        "vars": [("CSNOW", "surface"), ("PRMSL", "mean sea level")],
    },
    "sref_cfrzr": {
        "label": "SREF Freezing Rain Probability",
        "desc": "Ensemble probability (%) of freezing rain - ice-storm signal",
        "vars": [("CFRZR", "surface"), ("PRMSL", "mean sea level")],
    },
    "sref_cicep": {
        "label": "SREF Ice Pellets Probability",
        "desc": "Ensemble probability (%) of sleet/ice pellets",
        "vars": [("CICEP", "surface"), ("PRMSL", "mean sea level")],
    },
}

# spread-of mapping: sp_* product -> base product whose render branch and
# var list it reuses (the spread file stores the same variables as the
# mean file - values are ensemble std-dev instead of mean)
_SPREAD_OF = {
    "sp_850_tmp": "850_tmp",
    "sp_925_tmp": "925_tmp",
    "sp_700_rh": "700_rh",
    "sp_300_jet": "300_jet",
    "sp_250_jet": "250_jet",
    "sp_200_jet": "200_jet",
    "sp_sfc_mslp": "sfc_mslp",
    "sp_cape_wind": "cape_wind",
    "sp_pwat": "pwat",
    "sp_tcdc": "tcdc",
    "sp_snow": "snow",
    "sp_apcp": "ai_precip",   # only existing APCP render branch (units match)
}
for _sp, _base in _SPREAD_OF.items():
    PRODUCTS[_sp] = {
        "label": PRODUCTS[_base]["label"] + " - SPREAD",
        "desc": "GEFS ensemble standard deviation - where the ensemble "
                "disagrees; big values = low forecast confidence",
        "vars": PRODUCTS[_base]["vars"],
    }
PRODUCTS["sp_apcp"] = {
    "label": "Precipitation SPREAD (3-h accumulation)",
    "desc": "Ensemble disagreement on 3-hour rainfall - flood-forecast "
            "uncertainty at a glance",
    "vars": [("APCP", "surface")],
}


def find_cycle(model, product=None):
    """Most recent cycle datetime for which this model has an .idx.

    product-aware probing: the newest cycle is only useful if the file this
    PRODUCT lives in exists (e.g. HRRR uploads the pressure file before the
    surface file, so MUCAPE needs to fall back to an older cycle while
    500-vorticity can use the newest). Falls back to the generic probe when
    no product is given or the product has no file-type swap.
    """
    m = MAP_MODELS[model]
    now = dt.datetime.now(dt.timezone.utc)
    if model == "NBM":
        return _find_nbm_cycle(model, product)
    if model in AIWP_CODES:
        from data.aiwp import find_cycle as _aiwp_cycle
        return _aiwp_cycle(model)
    if model == "ECMWF":
        return _find_ecmwf_cycle(model, "ifs/0p25/oper", "{day}{hh}0000", 3, "oper-fc")
    if model == "AIFS":
        return _find_ecmwf_cycle(model, "aifs-single/0p25/oper", "{day}{hh}0000", 6, "oper-fc")
    if model == "AIFS-ENS":
        return _find_ecmwf_cycle(model, "aifs-ens/0p25/enfo", "{day}{hh}0000", 6, "enfo-cf")
    if model == "EPS-Weekly":
        # IFS 50-member ensemble: 00/12Z cycles, probe the f006 ensemble file
        return _find_ecmwf_cycle(model, "ifs/0p25/enfo", "{day}{hh}0000", 6, "enfo-ef")
    if model == "SREF":
        # 03/09/15/21Z cycles; the ensprod mean-file idx for any hour serves
        # as the publish probe (whole run lands with the file)
        now = dt.datetime.now(dt.timezone.utc)
        for back in range(4, 30):
            c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
            c = c.replace(hour=(c.hour // 6) * 6 + 3)   # snap to 03/09/15/21Z
            try:
                r = requests.get(_idx_url("SREF", c, 3), headers=UA, timeout=15)
                if r.ok and ":HGT:500 mb:" in r.text:
                    return c
            except requests.RequestException:
                continue
        return None
    probe = m.get("probe_fh", 1)
    for back in m["cycles"]:
        c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        if m.get("synoptic"):
            c = c.replace(hour=(c.hour // 6) * 6)
        try:
            if product:
                r = requests.get(_idx_url(model, c, probe, product), headers=UA, timeout=15)
            else:
                r = requests.get(_idx_url(model, c, probe), headers=UA, timeout=15)
            if r.ok:
                return c
        except requests.RequestException:
            continue
    return None


def _find_ecmwf_cycle(model, model_dir, stamp_fmt, probe_step=3, tag="oper-fc"):
    """Most recent ECMWF-bucket cycle (IFS/AIFS/AIFS-ENS) with a live file.
    Published ~4-8 h after cycle time; all products share the file naming.
    Probes the GRIB itself (not the .index): ECMWF publishes per-step index
    files BEFORE their GRIBs, so an index 200 does not mean the step's data
    is readable (EPS-Weekly cycles starved on 2026-09-20 because of this)."""
    now = dt.datetime.now(dt.timezone.utc)
    for back in range(8, 34):
        c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        day, hh = c.strftime("%Y%m%d"), c.strftime("%H")
        stamp = stamp_fmt.format(day=day, hh=hh)
        # S3 rate-limits these files (SlowDown 503 storms - one probe saw 503
        # on EVERY file for 20 min, 2026-09-20) - ECMWF's mirror serves the
        # same tree without the throttle, so probe it first, S3 second
        for host in ("https://data.ecmwf.int/forecasts",
                     "https://ecmwf-forecasts.s3.amazonaws.com"):
            url = f"{host}/{day}/{hh}z/{model_dir}/{stamp}-{probe_step}h-{tag}.grib2"
            try:
                r = requests.get(url, headers={**UA, "Range": "bytes=0-99"}, timeout=15)
                if r.status_code == 206:
                    return c
                if r.status_code == 404:
                    break          # file not published - S3 cannot have it either
            except requests.RequestException:
                pass
    return None


def _idx_url(model, cycle, fh, product=None):
    m = MAP_MODELS[model]
    if m.get("style") == "sref":
        # SREF packs every lead time in ONE per-cycle ensprod mean file
        return (f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/sref/prod/"
                f"sref.{cycle:%Y%m%d}/{cycle:%H}/ensprod/"
                f"sref.t{cycle:%H}z.pgrb132.mean_3hrly.grib2.idx")
    if m.get("style") == "cfs":
        valid = cycle + dt.timedelta(hours=fh)
        return (f"{m['base'].format(c=cycle)}pgbf{valid:%Y%m%d%H}.01.{cycle:%Y%m%d%H}.grb2.idx")
    base = m["base"].format(c=cycle)
    # per-product file-type swap (e.g. HRRR REFC/CAPE/MSLMA live in wrfsfcf files)
    swap = (PRODUCTS.get(product) or {}).get("file_type", {}).get(model)
    if swap:
        base = base.replace(swap[0], swap[1])
    elif model == "RRFS" and product in ("refc", "mucape", "cape_wind",
                                          "sfc_gust", "sfc_mslp",
                                          "hail", "uphl", "scp", "stp",
                                          "ehi", "ship"):
        # RRFS splits files: surface fields (incl. REFC simulated radar,
        # HAIL, MXUPHL, and the SCP/STP/EHI/SHIP inputs) live in the 2dfld
        # staging file. shear01 stays on prslev - it needs UGRD/VGRD@925 mb,
        # which only the pressure-level file carries (verified 2026-09-25).
        base = base.replace("prslev.3km", "2dfld.3km")
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


def _find_range_plain(idx_text, short_name, level_sub):
    """_find_range, but only the DETERMINISTIC message.

    Blend (and some ensemble) idx listings interleave statistical variants -
    'prob >0.254', 'ens std dev', '50% level' - around the plain forecast
    field, sometimes BEFORE it (APCP's probability message precedes the
    deterministic accumulation). Those render as garbage under the plain
    product's colour scale, so skip them; falls back to any match.
    """
    lines = idx_text.splitlines()
    lv = level_sub.lower()

    def hit(line):
        f = line.split(":")
        return len(f) > 4 and f[3] == short_name and lv in ":".join(f[4:]).lower()

    def bounds(i):
        start = int(lines[i].split(":")[1])
        end = start
        for j in range(i + 1, len(lines)):
            nxt = int(lines[j].split(":")[1])
            if nxt > start:
                end = nxt
                break
        return (start, end if end > start else start + 3_000_000)

    for i, line in enumerate(lines):
        low = line.lower()
        if hit(line) and not ("prob" in low or "std dev" in low or "% level" in low):
            return bounds(i)
    return _find_range(idx_text, short_name, level_sub)


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

    from data import griblock

    tmp = os.path.join(tempfile.gettempdir(), f"tnwx_map_{abs(hash(blob[:64])) % 99999}.grib2")
    try:
        with open(tmp, "wb") as f:
            f.write(blob)
        backend = {"indexpath": ""}
        if type_of_level:
            backend["filter_by_keys"] = {"typeOfLevel": type_of_level}
        with griblock.open_dataset(tmp, backend) as ds:
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


# Catalog entries for the NBM products (fetched via _fetch_nbm_fields from
# the NOMADS blend core GRIB2; vars unused here)
PRODUCTS["nbm_temp"] = {
    "label": "NBM Temperature \u00b7 Blend of Models",
    "desc": "NBM CONUS temperature blend (2.5 km guidance, hourly cycles)",
    "vars": [],
}
PRODUCTS["nbm_gust"] = {
    "label": "NBM Wind Gust \u00b7 Blend of Models",
    "desc": "NBM CONUS wind-gust blend (2.5 km guidance, hourly cycles)",
    "vars": [],
}
PRODUCTS["nbm_cape"] = {
    "label": "NBM SBCAPE \u00b7 Blend of Models",
    "desc": "NBM CONUS surface-based CAPE blend (2.5 km guidance)",
    "vars": [],
}
PRODUCTS["nbm_refc"] = {
    "label": "NBM Storm Strength (VIL) \u00b7 Blend of Models",
    "desc": "Vertically integrated liquid - the blend's storm-intensity proxy (no REFC published)",
    "vars": [],
}

# GRIB shortName -> the idx shortName our products use
# Products whose chart is a single field (cloud, vis, snow, precip, PWAT,
# CAPE, gusts, AI precip) get a synoptic overlay so they read like real
# weather maps: labeled MSLP isobars + 10 m wind barbs (barbs skipped for
# the three products that already draw their own).
_OVERLAY_MSLP = frozenset(("tcdc", "vis", "snow", "prate", "pwat", "mucape",
                           "cape_wind", "sfc_gust", "ai_precip", "sfc_dew", "700_w",
                           "3var_fronts", "frz_lvl", "hail", "uphl",
                           "sp_tcdc", "sp_snow", "sp_pwat", "sp_cape_wind"))
_OVERLAY_HAS_BARBS = frozenset(("mucape", "cape_wind", "sfc_gust", "sp_cape_wind"))
# RRFS's 2dfld staging file ships CAPE/HLCY/2 m T-Td/10 m winds (the SPC
# composite core) but NOT the 500-mb winds/T or isobaric RH the composite
# formulas also use - those live in the prslev file. The render branches
# substitute: max-wind winds for the 500 mb leg, a fixed 6.0 C/km lapse
# default when 700/500 mb temps are absent. Without these exemptions the
# strict all-vars check rejects every RRFS composite (2026-09-25).
_RRFS_COMPOSITE_SUBS = {
    "scp": [("CAPE", "90-0 mb above ground"), ("HLCY", "3000-0 m"),
            ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
    "stp": [("CAPE", "90-0 mb above ground"), ("HLCY", "3000-0 m"),
            ("TMP", "2 m above ground"), ("DPT", "2 m above ground"),
            ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
    "ehi": [("CAPE", "90-0 mb above ground"), ("HLCY", "3000-0 m")],
    "ship": [("CAPE", "90-0 mb above ground"),
             ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
             # RRFS 2dfld has no isobaric RH/T; lapse + RH terms fall back
             # to their neutral defaults inside the render branch
}

# SPC outlook palette (2026-09-22 'match the severe page'): the severe-wall
# fills now use the SAME categorical colors the site's severe page and SPC
# itself use - TSTM green through HIGH magenta (data/severe.SPC_COLORS).
# One shared colormap so a user can eyeball any wall and map fill to the
# risk language they already know.
_SPC_STOPS = ((0.00, "#c1e9c1"),   # TSTM pale green
              (0.20, "#66cdaa"),   # MRGL turquoise
              (0.40, "#ffff00"),   # SLGT yellow
              (0.60, "#ff8c00"),   # ENH orange
              (0.80, "#ff0000"),   # MDT red
              (1.00, "#ff00ff"))   # HIGH magenta

def _spc_cmap():
    """Shared LinearSegmentedColormap over the SPC categorical stops."""
    import matplotlib.colors as mcolors
    return mcolors.LinearSegmentedColormap.from_list("spc", _SPC_STOPS)

# level-suffix convention (2026-09-22 'more products'): products carrying the
# SAME variable at TWO levels (1000-500 mb thickness) would collide in the
# {shortName: values} field dict - a vars entry 'HGT@1000 mb' stores/reads
# fields['HGT@1000 mb']. SREF additionally derives the missing PRMSL overlay
# from the 1000 mb height it already fetched (bucket rule for surface pressure).
def _base_short(short):
    """'HGT@1000 mb' -> 'HGT' (idx lookups use the bare shortName)."""
    return short.split("@", 1)[0]

_CANON = {"U": "UGRD", "V": "VGRD", "GH": "HGT", "T": "TMP", "R": "RH",
          "U10": "UGRD", "V10": "VGRD", "10U": "UGRD", "10V": "VGRD",
          "T2M": "TMP", "2T": "TMP", "DPT2M": "DPT", "Z": "HGT", "MSL": "PRMSL",
          "CAPE": "CAPE", "PRMSL": "PRMSL", "MSLMA": "PRMSL", "TCW": "PWAT", "TCC": "TCDC",
          # ECMWF IFS ENSEMBLE idx uses 'gh' for pressure-level geopotential
          # while the single-level oper files use 'z' (verified in the
          # enfo-ef index, 2026-09-20) - same field, different shortName
          "GH": "HGT",
          "SDWE": "WEASD", "SNOD": "WEASD", "SD": "WEASD",
          # ECMWF IFS ensemble shortNames (EPS-Weekly) -> NCEP canonical names
          # (z/t/u/v/msl verified in the enfo-ef JSON index; ABSV stays ABSV)
          "ABSV": "ABSV",
          # cfgrib decodes accumulated-precip messages as TP (total precip)
          # in GEFS spread/mean files - same field, different shortName
          "TP": "APCP", "tp": "APCP", "Total": "APCP",
          # GRIB2 eccodes shortNames differ from the NCEP idx names for omega
          # (idx VVEL -> in-file 'W') and 2 m dew point (idx DPT -> '2d')
          # (verified against the 2026-09-22 12Z GFS pgrb2 decode)
          "W": "VVEL", "w": "VVEL", "2D": "DPT", "2d": "DPT",
          # ECMWF IFS open data: 10fg = maximum 10 m wind gust (m/s, verified
          # in the 2026-09-23 00Z oper JSON index) - feeds the sfc_gust wall
          "10fg": "GUST", "10FG": "GUST",
          # tprate = instantaneous precipitation rate (kg m-2 s-1, verified
          # same index) - feeds the prate wall from IFS/AIFS
          "tprate": "PRATE", "TPRATE": "PRATE"}
# eccodes shortNames are often lowercase (cape, tcw, sdwe...) - alias them too
for _k, _v in list(_CANON.items()):
    _CANON.setdefault(_k.lower(), _v)
# Some models use different parameter names for the same field
MODEL_VAR_ALIAS = {
    "RRFS": {
        # NOMADS RRFS idx names these HGT/TMP (like most NCEP models), but the
        # GRIB2 shortName inside the file is "HPBL"-style canon — keep explicit:
        "TMP": "TMP", "HGT": "HGT", "UGRD": "UGRD", "VGRD": "VGRD", "ABSV": "ABSV",
        "PRMSL": "MSLMA",   # RRFS surface files name mean-sea pressure MSLMA
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


def _find_nbm_cycle(model, product=None):
    """Most recent NBM init whose core file for THIS product is live.

    Source moved to NOMADS blend core GRIB2 (2026-09-17): the noaa-nbm-pds
    COG mirror stopped publishing 2026-09-14 (days 15+ empty), which starved
    every NBM map. Files are hourly: blend.t{HH}z.core.f{FFF}.co.grib2
    (+.idx). Probing the tiny .idx for the product's own element at f012 is
    exact, cheap and never a false positive; walk back a full day because
    hourly cycles go stale fast relative to publication.
    """
    elem, level_sub = "TMP", "2 m above ground"
    if product and product in NBM_ELEMENTS:
        elem, level_sub = NBM_ELEMENTS[product][0], NBM_ELEMENTS[product][1]
    now = dt.datetime.now(dt.timezone.utc)
    for back in range(1, 25):
        c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        try:
            r = requests.get(_nbm_core_url(c, 12) + ".idx", headers=UA, timeout=15)
            if r.ok and _find_range_plain(r.text, elem, level_sub):
                return c
        except requests.RequestException:
            continue
    return None


def _nbm_core_url(cycle, fh):
    """NOMADS blend core GRIB2 path for one cycle/valid hour (CONUS grid)."""
    return (f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod/"
            f"blend.{cycle:%Y%m%d}/{cycle:%H}/core/"
            f"blend.t{cycle:%H}z.core.f{fh:03d}.co.grib2")


def _fetch_nbm_fields(cycle, fh, product):
    """Read one NBM blend-core message -> {'DATA': values, 'lat', 'lon'}.

    Uses the .idx byte-range fetch + GRIB2 decode shared with the other
    NCEP models, then converts SI -> display units (K->°F, m->mi) to match
    the old COG contract where values arrived pre-converted.
    """
    elem, level_sub, _cmap, _units, _lv = NBM_ELEMENTS[product]
    try:
        idx_text = requests.get(_nbm_core_url(cycle, fh) + ".idx",
                                headers=UA, timeout=30).text
        rng = _find_range_plain(idx_text, elem, level_sub)
        if rng is None:
            return None
        blob = _fetch_range(_nbm_core_url(cycle, fh), rng[0], rng[1])
        if blob is None:
            return None
        decoded, lat, lon = _decode_grib_bytes(blob)
        if not decoded or lat is None:
            return None
        vals = np.asarray(next(iter(decoded.values())), dtype=float)
    except Exception:  # noqa: BLE001 - missing file/network/decode
        return None
    # SI -> display units (the old COG tiles arrived pre-converted)
    if product in ("nbm_temp", "nbm_dew", "nbm_wbgt"):
        vals = vals * 9.0 / 5.0 + 32.0                    # K -> °F
    elif product == "nbm_vis":
        vals = vals / 1609.344                            # m -> miles
    elif product in ("nbm_qpf", "nbm_snow06", "nbm_pwat"):
        vals = vals * 0.0393700787                        # mm w.e. -> inches
    return {"DATA": vals, "lat": lat, "lon": lon}


def _fetch_fields_herbie(model, cycle, fh, product):
    """Herbie-first fetch for the GRIB2 models (multi-partner fallback).

    Subset-fetches each product message via Herbie (AWS/NOMADS/Google/ECMWF
    mirror) and decodes with the same cfgrib path as the legacy fetchers.
    Returns None so the caller falls back when Herbie can't serve a file.
    """
    ecmwf_family = model in ("ECMWF", "AIFS", "AIFS-ENS")
    # sp_* GEFS-Spread products reuse the base product's variable list (the
    # spread FILE is already chosen by model member=spr) - 2026-09-18
    product = _SPREAD_OF.get(product, product)
    swap = None
    triples = []            # (idx search regex, decode level, original param)
    if ecmwf_family:
        params = ECMWF_PARAMS.get(product)
        if params is None:
            return None
        for param, level in params:
            levtype, _, levelist = level.partition(":")
            triples.append((f":{_base_short(param)}:{levelist or levtype}",
                            None, param))
    else:
        vars_needed = (PRODUCTS[product].get("vars_by_model", {}).get(model)
                       or (model == "RRFS" and _RRFS_COMPOSITE_SUBS.get(product))
                       or PRODUCTS[product]["vars"])
        alias = MODEL_VAR_ALIAS.get(model, {})
        swap = (PRODUCTS.get(product) or {}).get("file_type", {}).get(model)
        triples = [(f":{re.escape(alias.get(_base_short(s), _base_short(s)))}[^:]*:{re.escape(lv)}",
                    lv, s) for s, lv in vars_needed]
    from data.herbie_client import fetch_subset_blobs
    blobs = fetch_subset_blobs(model, cycle, fh, [t[0] for t in triples], swap=swap)
    if blobs is None or not any(b is not None for b in blobs):
        return None
    fields = {}
    lat = lon = None
    dec_step = 2 if model in ("GFS", "RRFS", "HRRR", "CFS", "HREF", "REFS") else 1
    for (_search, lv, _s), blob in zip(triples, blobs):
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
            values = np.asarray(values)
            if values.ndim > 2:      # multi-level message: take the nearest level
                values = values.reshape(-1, *values.shape[-2:])[0]
            elif values.ndim != 2:
                continue
            canonical = _CANON.get(sn, sn)
            vals = values[::dec_step, ::dec_step]
            _s_base = _CANON.get(_base_short(_s), _base_short(_s))
            if "@" in _s and _s_base == canonical:
                # suffixed var: store under CANONICAL@level ('z@1000 mb' ->
                # 'HGT@1000 mb') so the renderer reads one name everywhere
                fields[canonical + "@" + _s.split("@", 1)[1]] = vals
            else:
                fields[canonical] = vals
        lat, lon = lat2[::dec_step, ::dec_step], lon2[::dec_step, ::dec_step]
    if lat is None or not fields:
        return None
    # require every requested variable - partial Herbie decodes (NAM drops
    # some fields on some cycles) would crash the renderer or ship a broken
    # map; returning None lets the caller fall back to an older cycle
    if not ecmwf_family:
        # canonical names (bare - suffixed entries copy at runtime) - see the
        # matching comment in fetch_product_fields
        wanted = {_CANON.get(alias.get(_base_short(s), _base_short(s)),
                             _base_short(s)) for s, _ in vars_needed}
        missing = wanted - fields.keys()
        if product in _OVERLAY_MSLP:
            # overlay fields (PRMSL/UGRD/VGRD) are cosmetic - render without
            # them rather than discarding the base product's data
            base_missing = missing - {"PRMSL", "UGRD", "VGRD"}
            if base_missing:
                return None
        elif missing:
            return None
    if ecmwf_family:
        # ECMWF open-data 'z' is geopotential (m2/s2), not geopotential height.
        # NCEP files already carry geopotential HEIGHT (gpm) - dividing those
        # corrupted every Herbie-path height contour, which is why maps lost
        # their height/isobar lines. Suffixed height keys (HGT@1000 mb from
        # the thickness/fronts composites) need the same conversion.
        for _hk in [k for k in fields if k == "HGT" or k.startswith("HGT@")]:
            fields[_hk] = fields[_hk] / 9.80665
    return {"fields": fields, "lat": lat, "lon": lon}


def fetch_product_fields(model, cycle, fh, product):
    """Fetch+decode all variables for a product. Returns {shortName: values} + lat/lon.

    Herbie (multi-partner archive search) is tried first for the GRIB2
    models; the original single-bucket fetchers remain as fallback.
    """
    # sp_* GEFS-Spread products reuse the base product's variable list; the
    # spread FILE is already selected by the model (member=spr) - only the
    # product lookup needs mapping (2026-09-18 spread-family addition).
    product = _SPREAD_OF.get(product, product)
    if model == "NBM":
        data = _fetch_nbm_fields(cycle, fh, product)
        if data is None:
            return None
        return {"fields": {"DATA": data["DATA"]}, "lat": data["lat"], "lon": data["lon"]}
    if model in AIWP_CODES:
        from data.aiwp import fetch_fields as _aiwp_fetch
        return _aiwp_fetch(model, cycle, fh, product)
    from data.herbie_client import HERBIE_MODELS
    if model in HERBIE_MODELS and model not in ("SREF", "EPS-Weekly"):
        try:
            res = _fetch_fields_herbie(model, cycle, fh, product)
        except Exception:  # noqa: BLE001 - Herbie failures always fall back
            res = None
        if res is not None:
            return res
    if model == "SREF":
        return _fetch_sref_fields(cycle, fh, product)
    if model == "EPS-Weekly":
        return _fetch_eps_fields(cycle, fh, product)
    if model == "ECMWF":
        return _fetch_ecmwf_fields(cycle, fh, product)
    if model == "AIFS":
        return _fetch_ecmwf_fields(cycle, fh, product, model_dir="aifs-single/0p25/oper")
    if model == "AIFS-ENS":
        return _fetch_ecmwf_fields(cycle, fh, product, model_dir="aifs-ens/0p25/enfo", tag="enfo-cf")
    if model == "RRFS" and product in _RRFS_COMPOSITE_SUBS:
        # RRFS stays on its dedicated NOMADS fetcher anyway (not a Herbie
        # model), but the composite products need the reduced var list here
        # BEFORE the generic path recomputes vars_needed - same substitution
        # as the fetch below
        pass
    m = MAP_MODELS[model]
    vars_needed = (PRODUCTS[product].get("vars_by_model", {}).get(model)
                   or (model == "RRFS" and _RRFS_COMPOSITE_SUBS.get(product))
                   or PRODUCTS[product]["vars"])
    idx_text = _get_idx_text(model, cycle, fh, product)
    if idx_text is None:
        return None
    alias = MODEL_VAR_ALIAS.get(model, {})

    # Resolve byte ranges; U/V (and multi-level fields) may share one message,
    # so group requests by message start byte.
    groups = {}
    for short, level in vars_needed:
        actual = alias.get(_base_short(short), _base_short(short))
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
            vals = values[::dec_step, ::dec_step]
            suffixed = [s for s in g["shorts"] if "@" in s and _base_short(s) == canonical]
            if suffixed:
                # level-suffixed entries (thickness' HGT@1000 mb) share the
                # bare field's byte range - write ONLY the suffixed key, or
                # this group's level would overwrite the bare key the other
                # level's group wrote (GFS thickness rendered h1000-h1000=0,
                # an invisible fill, 2026-09-22)
                for _s in suffixed:
                    fields[_s] = vals
            else:
                fields[canonical] = vals
        lat, lon = lat2[::dec_step, ::dec_step], lon2[::dec_step, ::dec_step]
    if lat is None or len(fields) < 1:
        return None
    # require EVERY requested variable to have decoded - a partial decode
    # (e.g. NAM via Herbie occasionally drops some fields) would otherwise
    # render without contours/barbs or crash; falling back to an older cycle
    # with a complete file is better than shipping a broken map.
    # Compare against the CANONICAL names (decode keys are canonicalized via
    # _CANON above) - comparing alias names like MSLMA against canonical
    # PRMSL keys made every HRRR/RAP pressure product fail outright.
    wanted = {_CANON.get(alias.get(s, s), alias.get(s, s)) for s, _ in vars_needed}
    if product in _OVERLAY_MSLP:
        # overlay fields (PRMSL/UGRD/VGRD) are cosmetic - render without
        # them rather than discarding the base product's data
        wanted -= {"PRMSL", "UGRD", "VGRD"}
    if model == "RRFS" and product in _RRFS_COMPOSITE_SUBS:
        # the composite formulas tolerate their substitution fields being
        # absent (max-wind winds, fixed lapse) - only the core must decode
        wanted &= {_CANON.get(alias.get(_base_short(s), _base_short(s)),
                              alias.get(_base_short(s), _base_short(s)))
                   for s, _ in _RRFS_COMPOSITE_SUBS[product]}
    if not wanted.issubset(fields.keys()):
        return None
    if product.startswith("cam_prob"):
        # normalize the probability message: single field -> PROB, fraction -> percent
        vals = next(iter(fields.values()))
        if np.nanmax(vals) <= 1.0:
            vals = vals * 100.0
        fields = {"PROB": vals}
    return {"fields": fields, "lat": lat, "lon": lon}


# ---------------------------------------------------------------- SREF

def _fetch_sref_fields(cycle, fh, product):
    """SREF ensemble-MEAN fields from the packed per-cycle mean file.

    Unlike every other NCEP model, SREF (ensprod/sref.tHHz.pgrb132.mean_3hrly)
    ships ALL forecast hours in ONE GRIB per cycle, so the .idx is scanned
    for the exact lead time ('N hour fcst' for instants, 'A-B hour acc fcst'
    matched on B for accumulations) and the byte ranges fetched directly.
    """
    product = _SPREAD_OF.get(product, product)
    vars_needed = (PRODUCTS[product].get("vars_by_model", {}).get("SREF")
                   or PRODUCTS[product]["vars"])
    try:
        idx_text = requests.get(_idx_url("SREF", cycle, fh), headers=UA, timeout=30).text
    except requests.RequestException:
        return None
    if not idx_text:
        return None
    lines = idx_text.splitlines()
    by_hour = {}
    for i, line in enumerate(lines):
        fl = line.split(":")
        if len(fl) < 5:
            continue
        trailing = ":".join(fl[4:]).lower()
        # trailing looks like '500 mb:3 hour fcst:wt ens mean' or
        # 'surface:0-6 hour acc fcst:wt ens mean' - anchor on the colons so
        # the level text can never be mistaken for a lead time
        m = re.search(r"(?:^|:)(\d+) hour fcst(?::|$)", trailing)
        if m:
            by_hour.setdefault(int(m.group(1)), []).append(i)
            continue
        m = re.search(r"(?:^|:)(\d+)-(\d+) hour acc fcst(?::|$)", trailing)
        if m:
            by_hour.setdefault(int(m.group(2)), []).append(i)

    def _range(i):
        start = int(lines[i].split(":")[1])
        end = start
        for j in range(i + 1, len(lines)):
            nxt = int(lines[j].split(":")[1])
            if nxt > start:
                end = nxt
                break
        return start, (end if end > start else start + 3_000_000)

    wanted = []
    for short, level in vars_needed:
        short_bare = _base_short(short)
        hit = None
        for i in by_hour.get(fh, []):
            fl = lines[i].split(":")
            if len(fl) > 4 and fl[3] == short_bare and level.lower() in ":".join(fl[4:]).lower():
                hit = i
                break
        if hit is None:
            return None          # a missing field = broken map; fall back a cycle
        wanted.append(_range(hit))

    url = _idx_url("SREF", cycle, fh).replace(".idx", "")
    fields = {}
    lat = lon = None
    for (short, level), (start, end) in zip(vars_needed, wanted):
        try:
            blob = _fetch_range(url, start, end)
            decoded, lat2, lon2 = _decode_grib_bytes(
                blob, type_of_level=_level_type(level), target_level=_target_level(level))
        except Exception:  # noqa: BLE001 - one bad message must not kill the map
            continue
        for sn, values in decoded.items():
            values = np.asarray(values)
            if values.ndim > 2:      # multi-level message: take the nearest level
                values = values.reshape(-1, *values.shape[-2:])[0]
            elif values.ndim != 2:
                continue
            canonical = _CANON.get(sn, sn)
            if "@" in short and _base_short(short) == canonical:
                fields[short] = values[::2, ::2]   # suffixed: keep bare intact
            else:
                fields[canonical] = values[::2, ::2]
        lat, lon = lat2[::2, ::2], lon2[::2, ::2]
    if lat is None or not fields:
        return None
    # SREF's derived MSLP overlay: the ens-mean ships no PRMSL, but 1000 mb
    # height converts via the bucket rule p0 ≈ 100000·(h0/28.96)^5.257
    # (h in gpm) - good enough for isobars
    if product == "sfc_dew" and "PRMSL" not in fields and "HGT@1000 mb" in fields:
        fields["PRMSL"] = 100000.0 * np.power(fields["HGT@1000 mb"] / 28.96, 5.257)
    return {"fields": fields, "lat": lat, "lon": lon}


# ------------------------------------------------------------ EPS weekly

_EPS_MEMBERS = 4          # perturbed members averaged into a mean chart
_EPS_IDX_CACHE = {}       # (cycle, fh) -> enfo-ef index text | None


def _eps_idx_url(cycle, fh):
    day, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    return (f"https://ecmwf-forecasts.s3.amazonaws.com/{day}/{hh}z/ifs/0p25/enfo/"
            f"{day}{hh}0000-{fh}h-enfo-ef.index")


def _fetch_eps_fields(cycle, fh, product):
    """ECMWF IFS 50-member ensemble fields, averaged into a mean chart.

    The open-data bucket ships only the 50 perturbed members (no precomputed
    mean, no control at 0.25deg), so this grabs the first _EPS_MEMBERS
    members of each requested parameter and averages them. The result is the
    ensemble-mean chart for the extended week-2 range (out to day 15).
    """
    params = _ecmwf_params("EPS-Weekly", product)
    if params is None:
        return None
    idx_text = None
    for attempt in range(3):    # both hosts throw transient 503s on the index
        try:
            r = requests.get(_eps_idx_url(cycle, fh), headers=UA, timeout=30)
            if r.ok:
                idx_text = r.text
                break
        except requests.RequestException:
            pass
        time.sleep(2 + 2 * attempt)
    if not idx_text:
        return None
    entries = []
    for line in idx_text.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "pf":
            entries.append(d)

    def _grab(off, ln):
        # Both hosts serve the identical tree. The mirror is not throttled
        # but intermittently 503s too, and S3 throws SlowDown 503 storms, so
        # each attempt alternates hosts with a small backoff (2026-09-20).
        url = _eps_idx_url(cycle, fh).replace(".index", ".grib2")
        mirror = url.replace("https://ecmwf-forecasts.s3.amazonaws.com",
                             "https://data.ecmwf.int/forecasts")
        for attempt in range(4):
            for u in (mirror, url):
                try:
                    r = requests.get(url=u, headers={**UA, "Range": f"bytes={off}-{off + ln - 1}"},
                                     timeout=45)
                    if r.status_code in (200, 206):
                        return r.content
                    if r.status_code == 404:
                        return None     # not published - no twin can have it
                except requests.RequestException:
                    pass
            time.sleep(2 + attempt)
        return None

    fields = {}
    lat = lon = None
    for param, level in params:
        levtype, _, levelist = (level.partition(":") + ("",))[:3]
        blobs = []
        seen = set()
        for d in entries:                      # idx is already member-ordered
            if d.get("param") != param or d.get("levtype") != levtype:
                continue
            if levelist and str(d.get("levelist")) != str(levelist):
                continue
            num = str(d.get("number"))
            if num in seen:
                continue
            seen.add(num)
            blob = _grab(d["_offset"], d["_length"])
            if blob is not None:
                blobs.append(blob)
            if len(blobs) >= _EPS_MEMBERS:
                break
        if len(blobs) < max(2, _EPS_MEMBERS // 2):
            return None          # too few members: mean would lie; fall back
        vals = None
        lat2 = lon2 = None
        n = 0
        for blob in blobs:
            try:
                decoded, la, lo = _decode_grib_bytes(blob)
            except Exception:  # noqa: BLE001 - one bad member is averaged out
                continue
            for sn, arr in decoded.items():
                arr = np.asarray(arr, dtype=float)
                if arr.ndim != 2:
                    continue
                if vals is None:
                    vals = arr.copy()
                    lat2, lon2 = la, lo
                elif arr.shape == vals.shape:
                    vals += arr
                else:
                    continue
                n += 1
                break
        if vals is None or n == 0:
            continue
        if "@" in param:
            _canon_p = _CANON.get(_base_short(param), _base_short(param))
            _key = _canon_p + "@" + param.split("@", 1)[1]
        else:
            _key = _CANON.get(_base_short(param), _base_short(param))
        fields[_key] = (vals / n)[::2, ::2]
        lat, lon = lat2[::2, ::2], lon2[::2, ::2]
    if lat is None or not fields:
        return None
    if "HGT" in fields:
        # ECMWF 'z' (oper) is geopotential (m2/s2), while the ensemble 'gh'
        # already decodes as geopotential HEIGHT via cfgrib - only rescale
        # when the values are clearly geopotential (2026-09-20)
        if float(np.nanmax(fields["HGT"])) > 100_000:
            fields["HGT"] = fields["HGT"] / 9.80665
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
        # suffixed params (z@1000 mb) look up the idx under their BARE name
        entry = _ecmwf_entry(idx, _base_short(param), levtype or "sfc", levelist or None)
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
            if "@" not in param:
                fields[_CANON.get(sn, sn)] = values
        # suffixed entry (z@1000 mb) rides the same message as bare z -
        # writes ONLY its own key (canonicalized to HGT@1000 mb) so the
        # 500 mb bare HGT survives for the thickness subtraction
        if "@" in param:
            _canon_p = _CANON.get(_base_short(param), _base_short(param))
            fields[_canon_p + "@" + param.split("@", 1)[1]] = next(iter(decoded.values()), None)
        lat, lon = lat2, lon2
    if lat is None or not fields:
        return None
    if "HGT" in fields:
        fields["HGT"] = fields["HGT"] / 9.80665   # ECMWF 'z' is geopotential, not height
    if "HGT@1000 mb" in fields:
        fields["HGT@1000 mb"] = fields["HGT@1000 mb"] / 9.80665
    # display downsample: 0.25 deg global is ~1M points, too heavy for contours
    fields = {k: v[::2, ::2] for k, v in fields.items()}
    lat, lon = lat2[::2, ::2], lon2[::2, ::2]
    return {"fields": fields, "lat": lat, "lon": lon}


# ---------------------------------------------------------------- plotting
# Map regions: 'us' = full CONUS (default), 'etn' = zoomed Eastern Tennessee
# and the surrounding southern Appalachians. Extents are lon/lat in PlateCarree.
MAP_REGIONS = {
    "us": {"label": "US (CONUS)", "extent": [-125, -66, 23, 51]},
    "etn": {"label": "East Tennessee", "extent": [-92, -74, 30, 42]},
}
DEFAULT_REGION = "etn"



def _clabel(ax, cs, **kw):
    """Contour labels are cosmetic - a degenerate contour (NaN-heavy
    grids like NBM tiles over ocean produce empty GEOS points) must not
    kill the whole map render."""
    try:
        ax.clabel(cs, **kw)
    except Exception:                          # noqa: BLE001
        pass


def _mslp_overlay(ax, lon, lat, fields, trans, stride=1, barbs=True):
    """Best-effort synoptic overlay for single-field charts: labeled MSLP
    isobars (4 hPa) + 10 m wind barbs. Fields may be absent (some models
    don't publish them) - skip quietly; a map must never fail for decoration."""
    try:
        p = fields.get("PRMSL")
        u, v = fields.get("UGRD"), fields.get("VGRD")
        if p is not None:
            hpa = np.asarray(p) / 100.0
            cs = ax.contour(lon, lat, hpa, levels=np.arange(960, 1051, 4),
                            colors="black", linewidths=1.0, transform=trans)
            _clabel(ax, cs, fmt="%d", fontsize=7, inline=True)
        if barbs and u is not None and v is not None:
            ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                     (np.asarray(u) * 1.94384)[::stride, ::stride],
                     (np.asarray(v) * 1.94384)[::stride, ::stride],
                     length=6, transform=trans, linewidth=0.4)
    except Exception:                          # noqa: BLE001 - decoration only
        pass


def render_product_map(
model, cycle, fh, product, out_dir=MAP_DIR, region=DEFAULT_REGION):
    """Render one map; returns (png_path, meta). Cached on disk per cycle/fh.

    Cycle fallback: if this product's file doesn't exist for the given cycle
    at this hour (NOAA publishes HRRR surface files progressively), walk back
    to the previous cycle where the whole run is available so the requested
    valid time still renders instead of erroring.
    """
    # sp_* GEFS-Spread products reuse their base product's fetch + render
    # branch (identical variables; values are ensemble std-dev instead of
    # mean). The render branch dispatches on the base name; the label and
    # filename keep the sp_* identity.
    render_as = _SPREAD_OF.get(product, product)
    os.makedirs(out_dir, exist_ok=True)
    reg = MAP_REGIONS.get(region, MAP_REGIONS[DEFAULT_REGION])
    png = os.path.join(out_dir, f"{model}_{product}_f{fh:03d}_{cycle:%Y%m%d%H}_{region}.png")
    if os.path.exists(png) and os.path.getsize(png) > 10_000:
        valid = cycle + dt.timedelta(hours=fh)
        from data._tz import full
        return png, {"cycle": full(cycle), "fh": fh,
                     "valid": full(valid), "cached": True}
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import metpy.calc as mpcalc
    from metpy.units import units
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    data = fetch_product_fields(model, cycle, fh, render_as)
    if data is None:
        # progressive publication: this cycle's file for this product isn't up
        # yet - fall back to older cycles (same valid time, older init)
        m0 = MAP_MODELS[model]
        for back in m0.get("cycles", [])[1:5]:
            alt = (cycle - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
            if m0.get("synoptic"):
                alt = alt.replace(hour=(alt.hour // 6) * 6)
            if alt < cycle - dt.timedelta(hours=12):
                break
            data = fetch_product_fields(model, alt, fh, render_as)
            if data is not None:
                cycle = alt
                break
    if data is None:
        raise RuntimeError("No decodable data for this model/hour (NOAA bucket issue?)")
    fields, lat, lon = data["fields"], data["lat"], data["lon"]
    f = fields.get
    # the whole render dispatch runs under the BASE product name for sp_*
    # (variables and branches are identical; only label/legend differ)
    product = _SPREAD_OF.get(product, product)

    m = MAP_MODELS[model]
    valid = cycle + dt.timedelta(hours=fh)

    proj = ccrs.LambertConformal(central_longitude=-96, central_latitude=39)
    trans = ccrs.PlateCarree()
    # zoomed regions get a tighter projection center so the panel stays square
    if region == "etn":
        proj = ccrs.LambertConformal(central_longitude=-85, central_latitude=36)
    fig = plt.figure(figsize=(13, 8), dpi=90)
    ax = plt.axes(projection=proj)
    ax.set_extent(reg["extent"], crs=trans)
    coast = "10m" if region == "etn" else "50m"  # finer detail when zoomed
    ax.add_feature(cfeature.STATES.with_scale(coast), linewidth=0.5, edgecolor="#666666")
    ax.add_feature(cfeature.COASTLINE.with_scale(coast), linewidth=0.6)
    ax.add_feature(cfeature.BORDERS.with_scale(coast), linewidth=0.8)

    # subsample stride for barbs based on grid density
    stride = max(1, int(max(lat.shape) / 45))
    if product.startswith("nbm_"):
        stride = 10**9  # no winds in NBM COG products
    elif product.startswith("sp_"):
        stride = 10**9  # spread charts are pure fill fields - no barbs

    def heights(ax, h, level_mb, interval=60):
        if h is None:
            # some model/cycle combos (e.g. NAM via Herbie) omit HGT for this
            # level - skip the contours rather than failing the whole render
            return None
        hd = h / 10.0  # meters -> decameters
        cs = ax.contour(lon, lat, hd, levels=np.arange(480, 612, interval / 10),
                        colors="black", linewidths=1.0, transform=trans)
        _clabel(ax, cs, fmt="%d", fontsize=7, inline=True)
        return hd

    # sp_* spread products dispatch on their BASE product's branch; the
    # var fetch already ran under the base name too (2026-09-18)
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
        _clabel(ax, cs, fmt="%d\u00b0C", fontsize=6)
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product == "600_tmp":
        # 600 mb thermal chart - same styling as 500_tmp but with the tighter
        # 850-family contour interval (2026-09-22 'more levels')
        u, v, h, t = f("UGRD"), f("VGRD"), f("HGT"), f("TMP")
        tc = t - 273.15
        hd = heights(ax, h, 600, interval=30)
        cf = ax.contourf(lon, lat, tc, levels=np.arange(-30, 42, 2), cmap="coolwarm",
                         transform=trans, alpha=0.7)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Temperature (\u00b0C)")
        cs = ax.contour(lon, lat, tc, levels=np.arange(-30, 42, 5), colors="darkblue",
                        linewidths=0.7, transform=trans)
        _clabel(ax, cs, fmt="%d\u00b0C", fontsize=6)
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product == "600_rh":
        # 600 mb moisture - height contours only (SREF ships no 600 winds,
        # ECMWF/AI models join when they carry the level)
        rh, h = f("RH"), f("HGT")
        hd = heights(ax, h, 600, interval=30)
        cf = ax.contourf(lon, lat, rh, levels=np.arange(10, 105, 10), cmap="Greens",
                         transform=trans, alpha=0.8)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Relative humidity (%)")
    elif product == "thickness":
        # 1000-500 mb thickness + the 540 dam rain/snow line. HGT is the 500
        # mb field; HGT@1000 mb rides alongside (level-suffix convention).
        h5, h1 = f("HGT"), f("HGT@1000 mb")
        if h1 is None:
            h1 = f("HGT@1000")
        if h5 is None or h1 is None:
            raise RuntimeError("thickness: no 1000 mb heights decoded")
        else:
            th = (h5 - h1) / 10.0           # m -> dam
            p, u, v = f("PRMSL"), f("UGRD"), f("VGRD")
            if p is not None:
                hpa = p / 100.0
                cs = ax.contour(lon, lat, hpa, levels=np.arange(960, 1050, 4), colors="black",
                                linewidths=0.8, transform=trans)
                _clabel(ax, cs, fmt="%d", fontsize=6)
            cf = ax.contourf(lon, lat, th, levels=np.arange(480, 600, 6), cmap="PuOr_r",
                             transform=trans, alpha=0.65)
            plt.colorbar(cf, ax=ax, shrink=0.8, label="1000-500 mb thickness (dam)")
            cs540 = ax.contour(lon, lat, th, levels=[540], colors="blue", linewidths=2.2,
                               transform=trans)
            try:
                ax.clabel(cs540, fmt="540 dam", fontsize=7)
            except Exception:                  # noqa: BLE001
                pass
            cs = ax.contour(lon, lat, th, levels=np.arange(486, 598, 12), colors="darkorange",
                            linewidths=0.7, transform=trans)
            _clabel(ax, cs, fmt="%d", fontsize=6)
            if u is not None and v is not None:
                ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                         (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                         length=6, transform=trans, linewidth=0.4)
                wind = (u, v)
    elif product == "700_w":
        # 700 mb omega: negative = rising (Pa/s). Annotate 500 heights so the
        # ascent band can be read against the shortwave.
        w, h = f("VVEL"), f("HGT")
        if w is None:
            raise RuntimeError("700_w: no omega field decoded")
        else:
            wpa = w * 1.0                   # already Pa/s
            hd = heights(ax, h, 700, interval=30)
            cf = ax.contourf(lon, lat, wpa, levels=np.arange(-40, 42, 4), cmap="BrBG",
                             transform=trans, alpha=0.8)
            plt.colorbar(cf, ax=ax, shrink=0.8,
                         label="Omega - rising Pa/s (blue = ascent)")
            u7, v7 = f("UGRD"), f("VGRD")
            if u7 is not None and v7 is not None:
                ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                         (u7 * 1.94384)[::stride, ::stride], (v7 * 1.94384)[::stride, ::stride],
                         length=6, transform=trans, linewidth=0.4)
                wind = (u7, v7)
    elif product == "sfc_dew":
        d, p = f("DPT"), f("PRMSL")
        if d is None:
            raise RuntimeError("sfc_dew: no dew point decoded")
        else:
            df = d - 273.15
            df = df * 9 / 5 + 32            # chart reads in F for TN audience
            if p is not None:
                hpa = p / 100.0
                cs = ax.contour(lon, lat, hpa, levels=np.arange(960, 1050, 4), colors="black",
                                linewidths=0.9, transform=trans)
                _clabel(ax, cs, fmt="%d", fontsize=6)
            cf = ax.contourf(lon, lat, df, levels=np.arange(10, 86, 2), cmap="viridis",
                             transform=trans, alpha=0.8)
            plt.colorbar(cf, ax=ax, shrink=0.8, label="2 m dew point (\u00b0F)")
            cs = ax.contour(lon, lat, df, levels=[50, 55, 60, 65, 70, 75], colors="white",
                            linewidths=0.8, transform=trans)
            _clabel(ax, cs, fmt="%d\u00b0F", fontsize=6)
            u, v = f("UGRD"), f("VGRD")
            if u is not None and v is not None:
                ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                         (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                         length=6, transform=trans, linewidth=0.4)
                wind = (u, v)
    elif product == "shear06":
        # 0-6 km bulk shear approximated as |V500 - V10m| (kt). The suffixed
        # keys keep both wind levels in one dict (level-suffix convention).
        u10, v10 = f("UGRD@10 m above ground"), f("VGRD@10 m above ground")
        if u10 is None or v10 is None:
            # HREF's ensemble mean ships no 10 m winds - fall back to 925 mb
            u10, v10 = f("UGRD@925 mb"), f("VGRD@925 mb")
        u5, v5 = f("UGRD@500 mb"), f("VGRD@500 mb")
        if any(x is None for x in (u10, v10, u5, v5)):
            raise RuntimeError("shear06: missing a wind level")
        shear_kt = np.hypot(u5 - u10, v5 - v10) * 1.94384
        p = f("PRMSL")
        if p is not None:
            hpa = p / 100.0
            cs = ax.contour(lon, lat, hpa, levels=np.arange(960, 1050, 4), colors="black",
                            linewidths=0.7, transform=trans)
            _clabel(ax, cs, fmt="%d", fontsize=6)
        cf = ax.contourf(lon, lat, shear_kt, levels=np.arange(0, 81, 4),
                         cmap=_spc_cmap(), transform=trans, alpha=0.85)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="0-6 km bulk shear (kt)")
        cs = ax.contour(lon, lat, shear_kt, levels=[15, 30, 40, 50], colors="black",
                        linewidths=0.9, transform=trans)
        _clabel(ax, cs, fmt="%d kt", fontsize=6)
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u10 * 1.94384)[::stride, ::stride], (v10 * 1.94384)[::stride, ::stride],
                 length=5, transform=trans, linewidth=0.4)
        wind = (u10, v10)
    elif product == "lr75":
        # 700-500 mb lapse rate (C/km): (T700 - T500) / dz(km), dz from the
        # two geopotential heights. Steep mid-level lapse rates (> 6.5 C/km)
        # mark hail-friendly cold pools.
        t7, t5 = f("TMP"), f("TMP@500 mb")
        h7, h5 = f("HGT"), f("HGT@500 mb")
        if any(x is None for x in (t7, t5, h7, h5)):
            raise RuntimeError("lr75: missing a temperature/height level")
        dz_km = (h5 - h7) / 1000.0
        lr = (t7 - t5) / dz_km
        hd = heights(ax, h7, 700, interval=30)
        cf = ax.contourf(lon, lat, lr, levels=np.arange(3.0, 9.6, 0.25),
                         cmap=_spc_cmap(), transform=trans, alpha=0.85)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="700-500 mb lapse rate (C/km)")
        cs = ax.contour(lon, lat, lr, levels=[5.5, 6.5, 7.0, 7.5], colors="black",
                        linewidths=0.8, transform=trans)
        _clabel(ax, cs, fmt="%.1f", fontsize=6)
    elif product == "scp":
        # Supercell Composite Parameter, SPC formula: (MUCAPE/1000) x
        # (ESRH/50) x (EBWD/20), each term capped at 1.5. EBWD approximated
        # with the 0-6 km bulk shear (10 m vs 500 mb winds). Values >= 1
        # mark marginal supercell environments, >= 4 significant ones.
        cape, srh = f("CAPE"), f("HLCY")
        # low-level wind: 10 m/surface where shipped; HREF's ensemble mean
        # files carry no 10 m winds, so 925 mb stands in for the shear base
        u10, v10 = f("UGRD"), f("VGRD")
        if u10 is None or v10 is None:
            u10, v10 = f("UGRD@925 mb"), f("VGRD@925 mb")
        u5, v5 = f("UGRD@500 mb"), f("VGRD@500 mb")
        if cape is None or srh is None:
            raise RuntimeError("scp: missing CAPE or helicity")
        if any(x is None for x in (u10, v10, u5, v5)):
            # RRFS 2dfld carries no 500 mb winds: fall back to the max-wind
            # level it does ship (a decent deep-shear proxy), else surface-only
            u5, v5 = f("UGRD@max wind"), f("VGRD@max wind")
        if any(x is None for x in (u10, v10, u5, v5)):
            u5, v5 = u10, v10
        if any(x is None for x in (u10, v10)):
            raise RuntimeError("scp: missing a wind level")
        shear_kt = np.hypot(u5 - u10, v5 - v10) * 1.94384
        scp = (np.minimum(cape / 1000.0, 1.5) * np.minimum(srh / 50.0, 1.5)
               * np.minimum(shear_kt / 20.0, 1.5))
        p = f("PRMSL")
        if p is not None:
            hpa = p / 100.0
            cs = ax.contour(lon, lat, hpa, levels=np.arange(960, 1050, 4), colors="black",
                            linewidths=0.7, transform=trans)
            _clabel(ax, cs, fmt="%d", fontsize=6)
        cf = ax.contourf(lon, lat, scp, levels=np.arange(0, 8.25, 0.25),
                         cmap=_spc_cmap(), transform=trans, alpha=0.85)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Supercell Composite Parameter")
        cs = ax.contour(lon, lat, scp, levels=[1, 2, 4, 6, 8], colors="black",
                        linewidths=0.9, transform=trans)
        _clabel(ax, cs, fmt="%.0f", fontsize=6)
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u10 * 1.94384)[::stride, ::stride], (v10 * 1.94384)[::stride, ::stride],
                 length=5, transform=trans, linewidth=0.4)
        wind = (u10, v10)
    elif product == "ehi":
        # Energy Helicity Index: (CAPE x 0-3 km ESRH) / 160000. EHI >= 1 is
        # the classic supercell threshold, >= 2.5 significant/violent.
        cape, srh = f("CAPE"), f("HLCY")
        if cape is None or srh is None:
            raise RuntimeError("ehi: missing CAPE or helicity")
        ehi = cape * srh / 160000.0
        cf = ax.contourf(lon, lat, ehi, levels=np.arange(0, 4.05, 0.1),
                         cmap=_spc_cmap(), transform=trans, alpha=0.85, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Energy Helicity Index")
        cs = ax.contour(lon, lat, ehi, levels=[1.0, 2.0, 3.0], colors="black",
                        linewidths=0.9, transform=trans)
        _clabel(ax, cs, fmt="%.1f", fontsize=6)
    elif product == "stp":
        # Significant Tornado Parameter (CIN-only effective term), SPC
        # formula: (mucape/1500) x (esrh/150) x (ebwd/22.5) x lcl_term,
        # each product term capped at 1.5; lcl_term = max((2000-LCLm)/1000, 0)
        # with the mixed-layer LCL from 2 m T/Td (Espy lift). STP >= 1 marks
        # tornado-favorable environments, >= 2 significant.
        cape, srh = f("CAPE"), f("HLCY")
        t2, td2 = f("TMP"), f("DPT")
        u10, v10 = f("UGRD"), f("VGRD")
        if u10 is None or v10 is None:
            u10, v10 = f("UGRD@925 mb"), f("VGRD@925 mb")   # HREF fallback
        u5, v5 = f("UGRD@500 mb"), f("VGRD@500 mb")
        if any(x is None for x in (cape, srh, t2, td2)):
            raise RuntimeError("stp: missing CAPE/helicity/2 m fields")
        if any(x is None for x in (u10, v10, u5, v5)):
            u5, v5 = f("UGRD@max wind"), f("VGRD@max wind")   # RRFS 2dfld fallback
        if any(x is None for x in (u10, v10, u5, v5)):
            u5, v5 = u10, v10
        if any(x is None for x in (u10, v10)):
            raise RuntimeError("stp: missing a wind level")
        shear_kt = np.hypot(u5 - u10, v5 - v10) * 1.94384
        # LCL height (m) from 2 m temps (Espy: 125 m per C of T-Td spread)
        lcl_m = np.clip(125.0 * (t2 - td2), 0.0, 4000.0)
        lcl_term = np.clip((2000.0 - lcl_m) / 1000.0, 0.0, 1.5)
        stp = (np.minimum(cape / 1500.0, 1.5) * np.minimum(srh / 150.0, 1.5)
               * np.minimum(shear_kt / 22.5, 1.5) * lcl_term)
        p = f("PRMSL")
        if p is not None:
            hpa = p / 100.0
            cs = ax.contour(lon, lat, hpa, levels=np.arange(960, 1050, 4), colors="black",
                            linewidths=0.7, transform=trans)
            _clabel(ax, cs, fmt="%d", fontsize=6)
        cf = ax.contourf(lon, lat, stp, levels=np.arange(0, 5.05, 0.2),
                         cmap=_spc_cmap(), transform=trans, alpha=0.85, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Significant Tornado Parameter")
        cs = ax.contour(lon, lat, stp, levels=[0.5, 1.0, 2.0, 4.0], colors="black",
                        linewidths=0.9, transform=trans)
        _clabel(ax, cs, fmt="%.1f", fontsize=6)
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u10 * 1.94384)[::stride, ::stride], (v10 * 1.94384)[::stride, ::stride],
                 length=5, transform=trans, linewidth=0.4)
        wind = (u10, v10)
    elif product == "ship":
        # Significant Hail Parameter: four hail ingredients in one index -
        # (mucape/1000) x (700-500 lapse / 5.6) x (850-700 mean RH / 50) x
        # (0-6 km shear / 50), each term capped at 1.5 (the site's SCP/STP
        # convention; SPC's exact constants are unpublished). The 700-500
        # layer uses a fixed 2.5 km depth - the same layer for every model
        # keeps the walls comparable. SHIP >= 1 marks significant-hail
        # environments. HREF mean files lack 850 mb RH and 10 m winds -
        # 700 mb RH + 925 mb winds stand in (shear06/stp style); the
        # 850 mb entry may also arrive under bare RH (the ("RH", "850 mb")
        # var decodes to the canonical bare key on every fetch path).
        cape = f("CAPE")
        t7, t5 = f("TMP"), f("TMP@500 mb")
        rh850 = f("RH@850 mb")
        rh700 = f("RH@700 mb")
        if rh850 is None:
            rh850 = rh700 if rh700 is not None else f("RH")
        if rh700 is None:
            rh700 = rh850
        u10, v10 = f("UGRD@10 m above ground"), f("VGRD@10 m above ground")
        if u10 is None or v10 is None:
            u10, v10 = f("UGRD"), f("VGRD")          # bare 10 m key (legacy fetch)
        if u10 is None or v10 is None:
            u10, v10 = f("UGRD@925 mb"), f("VGRD@925 mb")
        u5, v5 = f("UGRD@500 mb"), f("VGRD@500 mb")
        if any(x is None for x in (cape, u10, v10)):
            raise RuntimeError("ship: missing CAPE or winds")
        if any(x is None for x in (t7, t5)):
            lr = 6.0                      # RRFS 2dfld: no 700/500 mb temps
        else:
            lr = (t7 - t5) / 2.5
        if any(x is None for x in (u5, v5)):
            u5, v5 = f("UGRD@max wind"), f("VGRD@max wind")   # RRFS 2dfld fallback
        if any(x is None for x in (u5, v5)):
            u5, v5 = u10, v10
        shear_kt = np.hypot(u5 - u10, v5 - v10) * 1.94384
        if rh850 is None and rh700 is None:
            rh850 = rh700 = 60.0          # neutral mid-level RH default
        rhmid = 0.5 * (rh850 + rh700)
        ship = (np.minimum(cape / 1000.0, 1.5) * np.minimum(lr / 5.6, 1.5)
                * np.minimum(rhmid / 50.0, 1.5)
                * np.minimum(shear_kt / 50.0, 1.5))
        cf = ax.contourf(lon, lat, ship, levels=np.arange(0, 5.05, 0.25),
                         cmap=_spc_cmap(), transform=trans, alpha=0.85, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Significant Hail Parameter")
        cs = ax.contour(lon, lat, ship, levels=[1.0, 2.0, 3.0], colors="black",
                        linewidths=0.9, transform=trans)
        _clabel(ax, cs, fmt="%.0f", fontsize=6)
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u10 * 1.94384)[::stride, ::stride], (v10 * 1.94384)[::stride, ::stride],
                 length=5, transform=trans, linewidth=0.4)
        wind = (u10, v10)
    elif product == "850_vort":
        # tropical low-level spin chart: relative vorticity at 850 mb - the
        # wave/circulation tracker (2026-09-23 'more panels'). Same math as
        # the 500 mb chart but tuned for tropical scales (no coriolis add:
        # the storm's OWN spin is what marks the center).
        u, v, h = f("UGRD"), f("VGRD"), f("HGT")
        dx, dy = mpcalc.lat_lon_grid_deltas(lon, lat)
        relv = np.asarray(mpcalc.vorticity(u * units("m/s"), v * units("m/s"), dx=dx, dy=dy).to("1/s"))
        hd = heights(ax, h, 850, interval=30)
        cf = ax.contourf(lon, lat, relv * 1e5, levels=np.arange(-30, 32, 2), cmap="PuOr_r",
                         transform=trans, alpha=0.8)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="850 mb relative vorticity (10\u207b\u2075 s\u207b\u00b9)")
        cs = ax.contour(lon, lat, relv * 1e5, levels=[8, 16, 24], colors="darkred",
                        linewidths=1.2, transform=trans)
        _clabel(ax, cs, fmt="%d", fontsize=6)
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product == "200_div":
        # upper outflow: horizontal divergence at 200 mb - where mass is
        # leaving aloft (intensifying tropical cyclones, jet entrances).
        # Green contours = divergent (favorable) outflow.
        u, v, h = f("UGRD"), f("VGRD"), f("HGT")
        dx, dy = mpcalc.lat_lon_grid_deltas(lon, lat)
        div = np.asarray(mpcalc.divergence(u * units("m/s"), v * units("m/s"), dx=dx, dy=dy).to("1/s"))
        hd = heights(ax, h, 200, interval=120)
        cf = ax.contourf(lon, lat, div * 1e5, levels=np.arange(-15, 16.5, 1.5), cmap="RdBu",
                         transform=trans, alpha=0.8)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="200 mb divergence (10\u207b\u2075 s\u207b\u00b9)")
        cs = ax.contour(lon, lat, div * 1e5, levels=[3, 6, 10], colors="darkgreen",
                        linewidths=1.2, transform=trans)
        _clabel(ax, cs, fmt="%d", fontsize=6)
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product == "3var_fronts":
        # the classic surface analysis composite: labeled isobars, the 540
        # dam rain/snow line, 2 m temperature field and 10 m wind barbs -
        # one map that reads like the printed surface chart.
        p, h5, h1 = f("PRMSL"), f("HGT"), f("HGT@1000 mb")
        t2 = f("TMP")
        th = ((h5 - h1) / 10.0) if (h5 is not None and h1 is not None) else None
        if p is not None:
            hpa = p / 100.0
            cs = ax.contour(lon, lat, hpa, levels=np.arange(960, 1050, 4), colors="black",
                            linewidths=0.9, transform=trans)
            _clabel(ax, cs, fmt="%d", fontsize=7)
        if th is not None:
            cs540 = ax.contour(lon, lat, th, levels=[540], colors="blue", linewidths=2.2,
                               transform=trans)
            try:
                ax.clabel(cs540, fmt="540 dam", fontsize=7)
            except Exception:                  # noqa: BLE001
                pass
        if t2 is not None:
            tf = (t2 - 273.15) * 9 / 5 + 32
            cf = ax.contourf(lon, lat, tf, levels=np.arange(-10, 111, 5), cmap="RdYlBu_r",
                             transform=trans, alpha=0.55)
            plt.colorbar(cf, ax=ax, shrink=0.8, label="2 m temperature (\u00b0F)")
            cs = ax.contour(lon, lat, tf, levels=np.arange(20, 101, 10), colors="darkorange",
                            linewidths=0.6, transform=trans)
            _clabel(ax, cs, fmt="%d", fontsize=5)
        u, v = f("UGRD"), f("VGRD")
        if u is not None and v is not None:
            ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                     (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                     length=6, transform=trans, linewidth=0.4)
            wind = (u, v)
        if p is None and t2 is None:
            raise RuntimeError("3var_fronts: no MSLP or 2 m temperature decoded")
    elif product == "frz_lvl":
        # 0C isotherm height: the winter precipitation splitter. Blue-below
        # (surface-frozen) through red-above (warm nose = zr / cold rain).
        z = f("HGT")
        if z is None:
            raise RuntimeError("frz_lvl: no 0C isotherm height decoded")
        zft = z * 3.28084
        cf = ax.contourf(lon, lat, zft, levels=np.arange(0, 14001, 1000),
                         cmap="RdYlBu_r", transform=trans, alpha=0.7, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="0C isotherm height (ft)")
        cs = ax.contour(lon, lat, zft, levels=(1000, 3000, 5000, 8000), colors="black",
                        linewidths=0.7, transform=trans)
        _clabel(ax, cs, fmt="%d ft", fontsize=6)
    elif product == "700_rh":
        rh, h = f("RH"), f("HGT")
        hd = heights(ax, h, 700, interval=30)
        cf = ax.contourf(lon, lat, rh, levels=np.arange(10, 105, 10), cmap="Greens",
                         transform=trans, alpha=0.8)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Relative humidity (%)")
        u7, v7 = f("UGRD"), f("VGRD")
        if u7 is not None and v7 is not None:
            ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                     (u7 * 1.94384)[::stride, ::stride], (v7 * 1.94384)[::stride, ::stride],
                     length=6, transform=trans, linewidth=0.4)
            wind = (u7, v7)
    elif product == "500_tmp":
        u, v, h, t = f("UGRD"), f("VGRD"), f("HGT"), f("TMP")
        tc = t - 273.15
        hd = heights(ax, h, 500, interval=60)
        cf = ax.contourf(lon, lat, tc, levels=np.arange(-48, 5, 3), cmap="coolwarm",
                         transform=trans, alpha=0.7)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Temperature (\u00b0C)")
        cs = ax.contour(lon, lat, tc, levels=np.arange(-48, 1, 5), colors="darkblue",
                        linewidths=0.7, transform=trans)
        _clabel(ax, cs, fmt="%d\u00b0C", fontsize=6)
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
        _clabel(ax, cs, fmt="%dmm", fontsize=6)
        _mslp_overlay(ax, lon, lat, fields, trans, stride)
    elif product == "mucape":
        cape = f("CAPE")
        # SPC palette (2026-09-22 'match the severe page'): instability walls
        # now share the categorical ramp - a yellow MUCAPE bullseye reads
        # exactly like an SLGT area on the severe page
        cf = ax.contourf(lon, lat, cape, levels=np.arange(100, 5001, 250),
                         cmap=_spc_cmap(), transform=trans, alpha=0.85, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Most-unstable CAPE (J/kg)")
        cs = ax.contour(lon, lat, cape, levels=(1000, 2500, 4000), colors="black",
                        linewidths=0.7, transform=trans)
        _clabel(ax, cs, fmt="%d", fontsize=6)
        _mslp_overlay(ax, lon, lat, fields, trans, stride, barbs=False)
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
        _mslp_overlay(ax, lon, lat, fields, trans, stride, barbs=False)
    elif product == "tcdc":
        c = f("TCDC")
        if np.nanmax(c) <= 1.0:
            c = c * 100.0   # ECMWF encodes 0-1
        cf = ax.contourf(lon, lat, c, levels=np.arange(0, 101, 10), cmap="gist_gray_r",
                         transform=trans, alpha=0.75)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Cloud cover (%)")
        _mslp_overlay(ax, lon, lat, fields, trans, stride)
    elif product == "vis":
        vis_mi = f("VIS") / 1609.34
        cf = ax.contourf(lon, lat, vis_mi, levels=np.arange(0, 10.5, 0.5), cmap="RdYlGn",
                         transform=trans, alpha=0.8, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Visibility (mi)")
        _mslp_overlay(ax, lon, lat, fields, trans, stride)
    elif product == "snow":
        s = f("WEASD") * 39.37   # meters -> inches
        fill = np.where(s < 0.1, np.nan, s)
        cf = ax.contourf(lon, lat, fill, levels=[0.1, 1, 2, 4, 6, 9, 12, 18, 24],
                         cmap="cool", transform=trans, alpha=0.85, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Snow depth (in)")
        _mslp_overlay(ax, lon, lat, fields, trans, stride)
    elif product == "ai_precip":
        ap = f("APCP") * 39.37   # meters -> inches over 6 h
        fill = np.where(ap < 0.01, np.nan, ap)
        cf = ax.contourf(lon, lat, fill, levels=[0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0],
                         cmap="YlGnBu", transform=trans, alpha=0.85, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="6-h precipitation (in)")
        _mslp_overlay(ax, lon, lat, fields, trans, stride)
    elif product == "prate":
        pr = f("PRATE") * 141.732   # kg m-2 s-1 -> in/hr
        fill = np.where(pr < 0.01, np.nan, pr)
        cf = ax.contourf(lon, lat, fill, levels=[0.01, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0],
                         cmap="turbo", transform=trans, alpha=0.85, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Precip rate (in/hr)")
        _mslp_overlay(ax, lon, lat, fields, trans, stride)
    elif product in ("300_jet", "250_jet", "200_jet"):
        u, v, h = f("UGRD"), f("VGRD"), f("HGT")
        spd_kt = np.hypot(u, v) * 1.94384
        hd = heights(ax, h, int(product[:3]), interval=120)
        cf = ax.contourf(lon, lat, spd_kt, levels=np.arange(40, 181, 10), cmap="turbo",
                         transform=trans, alpha=0.75)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Wind speed (kt)")
        cs = ax.contour(lon, lat, spd_kt, levels=np.arange(70, 181, 30), colors="white",
                        linewidths=0.8, transform=trans)
        _clabel(ax, cs, fmt="%dkt", fontsize=6)
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=5, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product == "sfc_mslp":
        p, u, v, t = f("PRMSL"), f("UGRD"), f("VGRD"), f("TMP")
        hpa = p / 100.0
        cs = ax.contour(lon, lat, hpa, levels=np.arange(960, 1050, 4), colors="black",
                        linewidths=1.0, transform=trans)
        _clabel(ax, cs, fmt="%d", fontsize=7, inline=True)
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
        cf = ax.contourf(lon, lat, cape, levels=np.arange(250, 5001, 250),
                         cmap=_spc_cmap(), transform=trans, alpha=0.85)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="CAPE (J/kg)")
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u, v)
        _mslp_overlay(ax, lon, lat, fields, trans, stride, barbs=False)
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
        _clabel(ax, cs, fmt="%ddBZ", fontsize=6)
    elif product == "cam_mean_srh":
        # ensemble-mean 0-3 km storm-relative helicity
        srh = f("HLCY")
        cf = ax.contourf(lon, lat, srh, levels=np.arange(0, 501, 25), cmap="Spectral_r",
                         transform=trans, alpha=0.8, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="0-3 km SRH (m\u00b2/s\u00b2)")
        cs = ax.contour(lon, lat, srh, levels=(150, 250, 400), colors="black",
                        linewidths=0.8, transform=trans)
        _clabel(ax, cs, fmt="%d", fontsize=6)
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
        _clabel(ax, cs, fmt="%d%%", fontsize=6)
    elif product in NBM_ELEMENTS:
        # NBM blend-core: one pre-blended field per product. NBM_ELEMENTS rows
        # are (grib shortName, grib level, cmap, legend label, fill levels).
        _elem, _level, cmap, units_lbl, levels = NBM_ELEMENTS[product]
        data_arr = np.asarray(f("DATA"), dtype=float)
        fill = np.where(data_arr < 0.5, np.nan, data_arr) if product == "nbm_refc" \
            else data_arr
        cf = ax.contourf(lon, lat, fill, levels=levels,
                         cmap=_spc_cmap() if cmap == "spc" else cmap,
                         transform=trans, alpha=0.8, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label=units_lbl)
        # NOTE: no contour-line overlay for NBM. The blend is NaN over ocean,
        # and contour paths touching NaN produce degenerate points that
        # crash cartopy's projection at draw time (GEOSException "getX called
        # on empty Point"). The filled bands + colorbar carry the signal.
    elif product == "sref_500_vort":
        # SREF ensemble mean: ABSV (absolute vorticity) ships natively in the
        # mean file - no MetPy derivation needed (2026-09-20)
        u, v, h, av = f("UGRD"), f("VGRD"), f("HGT"), f("ABSV")
        hd = heights(ax, h, 500, interval=60)
        cf = ax.contourf(lon, lat, av * 1e5, levels=np.arange(8, 34, 2), cmap="YlGnBu",
                         transform=trans, alpha=0.75)
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Absolute vorticity (10\u207b\u2075 s\u207b\u00b9)")
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u * 1.94384)[::stride, ::stride], (v * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u, v)
    elif product in ("sref_csnow", "sref_cfrzr", "sref_cicep"):
        prob = f("CSNOW") if product == "sref_csnow" else (
            f("CFRZR") if product == "sref_cfrzr" else f("CICEP"))
        fill = np.where(prob < 2, np.nan, prob)
        cmap = {"sref_csnow": "Blues", "sref_cfrzr": "OrRd",
                "sref_cicep": "BuPu"}[product]
        cf = ax.contourf(lon, lat, fill, levels=np.arange(5, 100, 5), cmap=cmap,
                         transform=trans, alpha=0.8, extend="both")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Ensemble probability (%)")
        cs = ax.contour(lon, lat, fill, levels=(25, 50, 75), colors="black",
                        linewidths=0.8, transform=trans)
        _clabel(ax, cs, fmt="%d%%", fontsize=6)
        _mslp_overlay(ax, lon, lat, fields, trans, stride, barbs=False)
    elif product == "hail":
        # HRRR/RRFS hourly max hail diameter (mm) - SPC-style ramp
        fill = np.asarray(f("HAIL"), dtype=float)
        fill = np.where(fill < 1, np.nan, fill)
        cf = ax.contourf(lon, lat, fill, levels=[1, 5, 10, 15, 20, 25, 30, 40,
                                                 50, 65, 80],
                         colors=("#e8f4ff", "#a8d4ff", "#7ab8f5", "#4d94e8",
                                 "#2f6fce", "#ffd54d", "#ffb300", "#ff7a1a",
                                 "#e53935", "#b71c1c"),
                         transform=trans, alpha=0.85, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="Hail diameter (mm)")
        _mslp_overlay(ax, lon, lat, fields, trans, stride, barbs=False)
    elif product == "uphl":
        # HRRR/RRFS hourly max updraft helicity (m2/s2) - rotation proxy:
        # purple ramp, >= 150 marks mesocyclone-strength rotating updrafts
        fill = np.asarray(f("MXUPHL"), dtype=float)
        fill = np.where(fill < 25, np.nan, fill)
        cf = ax.contourf(lon, lat, fill, levels=[25, 50, 75, 100, 125, 150,
                                                 200, 250, 300],
                         colors=("#f3e5f5", "#e1bee7", "#ce93d8", "#ab47bc",
                                 "#8e24aa", "#6a1b9a", "#4a148c", "#311b92"),
                         transform=trans, alpha=0.85, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8,
                     label="Updraft helicity 2-5 km (m\u00b2/s\u00b2)")
        _mslp_overlay(ax, lon, lat, fields, trans, stride, barbs=False)
    elif product == "shear01":
        # 0-1 km bulk shear (kt) from 925 mb winds (the required leg; the
        # 10 m surface leg is optional - RRFS prslev lacks it, verified
        # 2026-09-25, so 925 mb anchors the shear alone there)
        u2 = fields.get("UGRD@925 mb")
        if u2 is None:
            u2 = f("UGRD@925 mb")
        if u2 is None:
            u2 = f("UGRD")
        v2 = fields.get("VGRD@925 mb")
        if v2 is None:
            v2 = f("VGRD@925 mb")
        if v2 is None:
            v2 = f("VGRD")
        u1, v1 = f("UGRD"), f("VGRD")
        if u1 is None or v1 is None:
            u1, v1 = u2, v2
        mag = np.sqrt((u2 - u1) ** 2 + (v2 - v1) ** 2) * 1.94384
        cf = ax.contourf(lon, lat, mag, levels=np.arange(5, 55, 5),
                         cmap="PuBuGn", transform=trans, alpha=0.8,
                         extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.8, label="0-1 km shear (kt)")
        ax.barbs(lon[::stride, ::stride], lat[::stride, ::stride],
                 (u2 * 1.94384)[::stride, ::stride],
                 (v2 * 1.94384)[::stride, ::stride],
                 length=6, transform=trans, linewidth=0.4)
        wind = (u2, v2)
        _mslp_overlay(ax, lon, lat, fields, trans, stride, barbs=False)
    else:
        raise ValueError(product)

    prod_label = PRODUCTS[product]["label"] if product in PRODUCTS \
        else NBM_LABELS.get(product, product)
    region_lbl = reg["label"] if region != DEFAULT_REGION else ""
    from data._tz import full, produced
    ax.set_title(
        f"{prod_label}{' \u2014 ' + region_lbl if region_lbl else ''}\n"
        f"{m['label']} \u00b7 init {full(cycle)} \u00b7 F{fh:03d} \u00b7 valid {full(valid)}",
        fontsize=11, loc="left",
    )
    ax.text(0.99, 0.01, f"Tennessee Weather Network \u00b7 data: NOAA/NCEP \u00b7 MetPy"
            f" \u00b7 {produced()}",
            transform=ax.transAxes, ha="right", fontsize=7, color="#444444")

    fig.tight_layout()
    # Atomic-ish write: save to a unique tmp then rename, so a concurrent
    # rotation pass can never see or package a torn half-written PNG when
    # two renderers land on the same combo (backfills do this by design).
    # format="png" is required - savefig infers the format from the suffix,
    # and the .tmp<pid> suffix makes it raise ValueError (2026-09-18).
    png_tmp = png + f".tmp{os.getpid()}"
    fig.savefig(png_tmp, bbox_inches="tight", format="png")
    # Palette-quantize in place: weather maps have few distinct colors, so
    # this shrinks PNGs ~60-70% with no visible change. Loops (7 frames per
    # combo) multiplied the catalog ~6x - unquantized that is 1.9 GB and
    # blows past GitHub Pages' 1 GB site cap; quantized it stays ~650 MB.
    try:
        from PIL import Image as _Image
        _im = _Image.open(png_tmp)
        if _im.mode not in ("P", "L"):
            _im.convert("RGB").quantize(colors=128).save(png_tmp, optimize=True)
    except Exception:                      # noqa: BLE001 - keep the RGB png
        pass
    os.replace(png_tmp, png)
    plt.close(fig)
    valid = cycle + dt.timedelta(hours=fh)
    return png, {"cycle": full(cycle), "fh": fh,
                 "valid": full(valid), "region": region}


def clear_map_cache():
    try:
        for name in os.listdir(MAP_DIR):
            if name.endswith(".png"):
                os.remove(os.path.join(MAP_DIR, name))
    except OSError:
        pass

"""River & stream gauges: NWS NWPS (AHPS) statuses for Tennessee waters.

The Winter/Hurricane pages cover sky and sea; this covers the water under
your feet. Every gauge the National Water Prediction Service publishes for
Tennessee rivers is fetched per-gauge from the official NWPS API
(api.water.noaa.gov) - the list endpoint ignores bbox filters, but the
single-gauge endpoint is fast and reliable (verified 2026-09-20).

The gauge list is curated from NWPS's own search index: 95 gauge points on
~20 Tennessee rivers, weighted toward East Tennessee (the Nolichucky,
French Broad, Holston, Doe, Pigeon and Little Pigeon all drain Greene
County). Each gauge carries observed stage, flood category, flood
thresholds, and a link to the official hydrograph.

Data is keyless and free (NWS NWPS). Cache: 10 minutes.
"""
import datetime as dt
import json
import os
import threading
import time

import requests

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
GAUGE_API = "https://api.water.noaa.gov/nwps/v1/gauges"
GAUGE_URL = "https://water.noaa.gov/gauges/{}"

_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()

# East Tennessee first (closest to home), then Middle/West TN.
# LIDs from NWPS search index (state==TN verified); format: LID, river group
GAUGES = [
    # --- East Tennessee (Tennessee River tributaries) ---
    ("NOLT1", "Nolichucky"), ("EMBT1", "Nolichucky"), ("LLDT1", "Nolichucky"),
    ("NWPT1", "French Broad"), ("DUGT1", "French Broad"), ("DGTT1", "French Broad"),
    ("MCIT1", "French Broad"),
    ("CRKT1", "Holston"), ("CRLT1", "Holston"), ("BOOT1", "Holston"),
    ("FPHT1", "Holston"), ("SHKT1", "Holston"), ("SHDT1", "Holston"),
    ("DOET1", "Doe"), ("EZBT1", "Watauga"), ("WTGT1", "Watauga"),
    ("NEPT1", "Pigeon"), ("HRFT1", "Pigeon"),
    ("SEVT1", "Little Pigeon"), ("GATT1", "Little Pigeon"),
    ("CPAT1", "Little Pigeon"), ("LPNT1", "Little Pigeon"),
    ("NRST1", "Clinch"), ("NRTT1", "Clinch"), ("TAZT1", "Clinch"),
    ("CCLT1", "Clinch"), ("MHDT1", "Clinch"), ("MHTT1", "Clinch"),
    ("ARTT1", "Powell"),
    ("CHLT1", "Hiwassee"), ("HADT1", "Hiwassee"),
    ("OCAT1", "Ocoee"), ("OCCT1", "Ocoee"), ("OCTT1", "Ocoee"), ("CPHT1", "Ocoee"),
    ("OBDT1", "Obed"), ("OAKT1", "Emory"),
    # --- Middle Tennessee ---
    ("NAST1", "Cumberland"), ("OHHT1", "Cumberland"), ("OHGT1", "Cumberland"),
    ("CHET1", "Cumberland"), ("CKVT1", "Cumberland"), ("CTHT1", "Cumberland"),
    ("DOVT1", "Cumberland"), ("CLAT1", "Cumberland"),
    ("DONT1", "Stones"), ("JPPT1", "Stones"), ("MUGT1", "Stones"),
    ("FRAT1", "Harpeth"), ("HBFT1", "Harpeth"), ("BELT1", "Harpeth"),
    ("KINT1", "Harpeth"),
    ("COLT1", "Duck"), ("CNVT1", "Duck"), ("SHVT1", "Duck"), ("HMLT1", "Duck"),
    ("FYTT1", "Elk"), ("PELT1", "Elk"),
    ("STWT1", "Caney Fork"), ("WHTT1", "Sequatchie"),
    # --- West Tennessee ---
    ("FLTT1", "Buffalo"), ("LBVT1", "Buffalo"),
]

# floodCategory -> (color, human word); NWPS official categories
CAT_STYLE = {
    "major": ("#d32f2f", "MAJOR FLOODING"),
    "moderate": ("#ef6c00", "Moderate flooding"),
    "minor": ("#f9a825", "Minor flooding"),
    "action": ("#0288d1", "Near flood stage"),
    "no_flooding": ("#43a047", "No flooding"),
    "low_threshold": ("#81c784", "Below normal"),
    "obs_not_current": ("#9e9e9e", "Data not current"),
    "out_of_service": ("#757575", "Out of service"),
    "not_defined": ("#9e9e9e", "No flood categories"),
}
FLOOD_RANK = {"major": 5, "moderate": 4, "minor": 3, "action": 2}


def _norm_cat(cat):
    cat = (cat or "").lower()
    return cat if cat in CAT_STYLE else "obs_not_current"


def _fetch_gauge(lid):
    """One gauge -> dict with observed stage + flood info (None on failure)."""
    try:
        r = requests.get(f"{GAUGE_API}/{lid}", headers=UA, timeout=25)
        if not r.ok:
            return None
        g = r.json()
    except Exception:                              # noqa: BLE001
        return None
    st = g.get("status") or {}
    obs = st.get("observed") or {}
    fcst = st.get("forecast") or {}
    flood = g.get("flood") or {}
    cats = flood.get("categories") or {}
    lat, lon = g.get("latitude"), g.get("longitude")
    if not lat or not lon:
        return None

    def _val(d):
        v = d.get("primary")
        return None if v is None or v == -999 else round(float(v), 2)

    cat = _norm_cat(obs.get("floodCategory"))
    return {
        "lid": lid,
        "name": g.get("name") or lid,
        "state": (g.get("state") or {}).get("abbreviation", ""),
        "lat": round(float(lat), 4), "lon": round(float(lon), 4),
        "stage": _val(obs), "stageUnit": obs.get("primaryUnit") or "ft",
        "flow": _val({"primary": obs.get("secondary")}),
        "flowUnit": obs.get("secondaryUnit") or "kcfs",
        "obsTime": obs.get("validTime") or "",
        "fcstStage": _val(fcst), "fcstCat": _norm_cat(fcst.get("floodCategory")),
        "category": cat,
        "catColor": CAT_STYLE[cat][0], "catWord": CAT_STYLE[cat][1],
        "thresholds": {k: (v or {}).get("stage")
                       for k, v in cats.items() if (v or {}).get("stage")},
        "url": GAUGE_URL.format(lid),
    }


def rivers_bundle(max_age=600):
    """All curated gauges with statuses, cached (updater calls each cycle)."""
    with _lock:
        now = time.time()
        if _cache["data"] is not None and now - _cache["at"] < max_age:
            return _cache["data"]

    gauges = []
    for lid, group in GAUGES:
        g = _fetch_gauge(lid)
        if g:
            g["group"] = group
            gauges.append(g)
    # worst first (major flooding on top), then by river group
    gauges.sort(key=lambda x: (-FLOOD_RANK.get(x["category"], 0), x["group"], x["name"]))

    counts = {}
    for g in gauges:
        counts[g["category"]] = counts.get(g["category"], 0) + 1
    out = {
        "ok": bool(gauges),
        "gauges": gauges,
        "counts": counts,
        "floodCount": sum(FLOOD_RANK.get(c, 0) >= 3 for c in counts),
        "generated": time.strftime("%Y-%m-%d %H:%M"),
    }
    with _lock:
        _cache.update(at=time.time(), data=out)
    return out


if __name__ == "__main__":
    b = rivers_bundle(max_age=0)
    print("gauges:", len(b["gauges"]), "| categories:", b["counts"])
    for g in b["gauges"][:5]:
        print(f"  {g['lid']} {g['name'][:52]}: {g['stage']} {g['stageUnit']} - {g['catWord']}")

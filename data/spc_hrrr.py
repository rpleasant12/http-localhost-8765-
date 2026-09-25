"""SPC HRRR browser frames (free, no key) + site RRFS/HRRR render bridge.

SPC hosts the official 3-km HRRR rendered as hourly forecast frames:

    https://www.spc.noaa.gov/exper/hrrr/data/hrrr3/<sector>/
        R<run>_F<fff>_V<valid>_S<sector>_<product>.gif

Verified live 2026-09-25: frames F001..F018 hourly per run; F000 does not
exist; <run> comes from <dir>/matrixinfo.txt (plain 10-digit stamp, e.g.
2026092507). Sectors (from SPC's carto.js setMap): 19=CONUS, 14=Southern
Plains, 17=Southeast, 20=Midwest - all 1000x750 GIFs with baked-in maps.
Products verified live: refc (composite reflectivity), pmsl, wmax, uh,
ttd, ptype, srh3, cape.

The page combines these live SPC frames with the site's own MetPy RRFS +
HRRR renders (from data.model_maps' catalog) so both CAMs appear even
when SPC is down: every frame entry is URL-guarded and the module never
raises. Frames are cached in memory for 15 minutes (the browser refreshes
hourly); remote GIFs are NOT proxied - the <img> tags point at SPC
directly, so visitors' browsers fetch them (no bandwidth cost here).
"""
import datetime as dt
import threading
import time
import os
import re

import requests

BASE = "https://www.spc.noaa.gov/exper/hrrr/data/hrrr3"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0"}
MAX_FH = 18                        # SPC publishes F001..F018 hourly
PRODUCTS = (
    ("refc", "Composite reflectivity (radar forecast)"),
    ("pmsl", "MSL pressure & wind"),
    ("wmax", "Max surface wind"),
    ("uh", "Updraft helicity"),
    ("srh3", "0-3 km storm-relative helicity"),
    ("cape", "CAPE"),
    ("ttd", "Temperature / dewpoint"),
    ("ptype", "Precipitation type"),
)
SECTORS = (
    ("s19", "CONUS"),
    ("s14", "Southern Plains"),
    ("s17", "Southeast"),
    ("s20", "Midwest"),
)

_CACHE = {}
_LOCK = threading.Lock()
_TTL = 900                        # 15 min; SPC rebuilds per model run

_EMPTY = {"run": "", "runLabel": "", "products": [], "sectors": []}


def _get(url, timeout=20):
    return requests.get(url, headers=UA, timeout=timeout)


def _latest_run():
    """Newest HRRR cycle SPC has rendered (plain 'YYYYMMDDHH' text)."""
    try:
        r = _get(f"{BASE}/matrixinfo.txt", timeout=12)
        if r.status_code == 200:
            m = re.match(r"\s*(\d{10})", r.text)
            if m:
                return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    # fallback: probe the last few cycles for any existing frame
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0)
    for back in range(4):
        run = (now - dt.timedelta(hours=back)).strftime("%Y%m%d%H")
        v = run
        try:
            r = _get(f"{BASE}/s19/R{run}_F001_V{v}_S19_refc.gif", timeout=10)
            if r.status_code == 200 and len(r.content) > 3000:
                return run
        except Exception:  # noqa: BLE001
            continue
    return ""


def _run_label(run):
    """'Fri 08Z' style cycle label in Eastern time (site convention)."""
    if not run:
        return ""
    try:
        w = dt.datetime.strptime(run, "%Y%m%d%H").replace(
            tzinfo=dt.timezone.utc)
        h = w.hour % 12 or 12
        ampm = "AM" if w.hour < 12 else "PM"
        days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        return f"{days[w.weekday()]} {h}{ampm} ET ({run[-2:]}Z)"
    except ValueError:
        return run


def _frame_ok(sector, run, fh, product):
    """True when a frame URL exists (and is a real image)."""
    try:
        v = (dt.datetime.strptime(run, "%Y%m%d%H")
             + dt.timedelta(hours=fh)).strftime("%Y%m%d%H")
        u = f"{BASE}/{sector}/R{run}_F{fh:03d}_V{v}_S{sector[1:]}_{product}.gif"
        r = _get(u, timeout=12)
        return (r.status_code == 200 and len(r.content) > 3000
                and r.headers.get("Content-Type", "").startswith("image"))
    except Exception:  # noqa: BLE001
        return False


def payload():
    """Cached payload: latest run + per-sector frame availability per product.

    Frames are {url, fh, valid}. Availability is probed for the last frame
    (F018) only - hourly frames are contiguous when the run exists, and a
    truncated run simply has no F018, so one probe detects run completeness
    without 8x4x18 requests per cycle.
    """
    with _LOCK:
        hit = _CACHE.get("p")
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    try:
        val = _build()
    except Exception:  # noqa: BLE001
        val = dict(_EMPTY)
    with _LOCK:
        _CACHE["p"] = (time.time(), val)
    return val


def _build():
    run = _latest_run()
    if not run:
        return dict(_EMPTY)
    out = {"run": run, "runLabel": _run_label(run), "products": [], "sectors": []}
    # products list (static, verified live at build time)
    out["products"] = [{"k": k, "label": lbl} for k, lbl in PRODUCTS]
    # per-sector: complete = does F018 exist for refc?
    for sector, label in SECTORS:
        complete = _frame_ok(sector, run, MAX_FH, "refc")
        last_fh = MAX_FH if complete else 1
        if not complete and not _frame_ok(sector, run, 1, "refc"):
            continue                       # sector not rendered this run
        out["sectors"].append({"sector": sector, "label": label,
                               "lastFh": last_fh})
    return out


def frame_url(sector, run, fh, product):
    """Deterministic frame URL (no network) for the page builder."""
    try:
        v = (dt.datetime.strptime(run, "%Y%m%d%H")
             + dt.timedelta(hours=fh)).strftime("%Y%m%d%H")
    except ValueError:
        v = run
    return f"{BASE}/{sector}/R{run}_F{fh:03d}_V{v}_S{sector[1:]}_{product}.gif"


if __name__ == "__main__":
    import json
    p = payload()
    print("run:", p["run"], p["runLabel"])
    print("sectors:", [(s["sector"], s["lastFh"]) for s in p["sectors"]])
    print("products:", [x["k"] for x in p["products"]])
    if p["run"]:
        print("sample:", frame_url("s19", p["run"], 6, "refc"))

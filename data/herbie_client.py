"""Herbie-backed GRIB2 access for the forecast models.

Herbie (herbie-data) locates each model file across partner archives -
AWS Open Data, NOMADS, Google, Azure, ECMWF's mirror - with automatic
fallback, so a single bucket hiccup no longer kills a map or a point
series. This module wraps Herbie as a *subset fetcher*: callers hand in
wgrib2-style search strings, Herbie downloads just those GRIB2 messages
(cached on disk under static/herbie), and the caller decodes the bytes
with the existing cfgrib path. Any failure returns None so callers fall
back to their hand-rolled bucket fetchers.

App model key -> Herbie model/product:
  HRRR        hrrr prs / sfc (surface files carry REFC/CAPE/MSLMA)
  RAP         rap (awp130pgrb)
  NAM         nam awip12 (12 km AWIPS CONUS)
  GFS         gfs pgrb2.0p25
  GEFS        gefs atmos.5 member=avg
  GEFS-Spread gefs atmos.5 member=spr
  HREF        href mean (conus domain)
  ECMWF       ifs oper / scda (00/12Z -> oper, 06/18Z -> scda)
  AIFS        aifs oper
  AIFS-ENS    aifs enfo
RRFS, CFS, NBM and the AIWP AI models keep their dedicated fetchers.
"""
import logging
import os
from pathlib import Path

_log = logging.getLogger("tnwx.herbie")

try:  # herbie logs via loguru to stderr - keep the server log clean
    from herbie import logger as _herbie_logger
    _herbie_logger.remove()
except Exception:  # noqa: BLE001 - herbie optional at import time
    pass

# Subset downloads land inside the project (mirrors the other static caches)
SAVE_DIR = os.path.join("static", "herbie")


def prune_grib_cache(max_mb=600, keep_days=2):
    """Delete cached GRIB subsets from cycles older than keep_days.

    Herbie subsets can add up fast (a full AIFS day was 2.3 GB once), and a
    bloated static/ trips Streamlit's 1 GB static-serving cap - which breaks
    every PNG overlay served from /app/static. Runs at import and from the
    renderer loop; cheap because it only stats date-named folders.
    """
    import shutil
    import time as _time

    now = _time.time()
    removed = 0
    try:
        roots = os.listdir(SAVE_DIR)
    except OSError:
        return 0
    for name in roots:
        path = os.path.join(SAVE_DIR, name)
        if not os.path.isdir(path):
            continue
        # model dirs contain date-named cycle folders (YYYYMMDD)
        if len(name) == 8 and name.isdigit():
            continue  # handled below via model dirs
        for cyc in os.listdir(path):
            cpath = os.path.join(path, cyc)
            if not (len(cyc) == 8 and cyc.isdigit() and os.path.isdir(cpath)):
                continue
            age_days = (now - os.path.getmtime(cpath)) / 86400.0
            if age_days > keep_days:
                try:
                    shutil.rmtree(cpath, ignore_errors=True)
                    removed += 1
                except OSError:
                    pass
    # absolute cap: if the cache still exceeds max_mb, drop oldest cycle dirs
    def _dir_mb(p):
        total = 0
        for root, _dirs, files in os.walk(p):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
        return total / (1024 * 1024)

    if _dir_mb(SAVE_DIR) > max_mb:
        cycle_dirs = []
        for model in os.listdir(SAVE_DIR):
            mpath = os.path.join(SAVE_DIR, model)
            if not os.path.isdir(mpath):
                continue
            for cyc in os.listdir(mpath):
                cpath = os.path.join(mpath, cyc)
                if len(cyc) == 8 and cyc.isdigit() and os.path.isdir(cpath):
                    cycle_dirs.append((cyc, cpath))
        cycle_dirs.sort()
        for _cyc, cpath in cycle_dirs:
            if _dir_mb(SAVE_DIR) <= max_mb:
                break
            shutil.rmtree(cpath, ignore_errors=True)
            removed += 1
    return removed


try:  # keep the disk footprint bounded on every import
    prune_grib_cache()
except Exception:  # noqa: BLE001 - never block startup on cache cleanup
    pass


def herbie_kwargs(model, cycle, swap=None, surface=False):
    """-> kwargs for Herbie(), or None when the model stays on its legacy fetcher.

    swap   - the caller's (from, to) file-type substitution (map products)
    surface- True when the caller reads near-surface fields (point series)
    """
    if model == "HRRR":
        product = "sfc" if (surface or (swap and "sfcf" in str(swap[1]))) else "prs"
        return {"model": "hrrr", "product": product}
    if model == "RAP":
        return {"model": "rap"}
    if model == "NAM":
        return {"model": "nam", "product": "awip12"}
    if model == "GFS":
        return {"model": "gfs", "product": "pgrb2.0p25"}
    if model == "GEFS":
        return {"model": "gefs", "product": "atmos.5", "member": "avg"}
    if model == "GEFS-Spread":
        return {"model": "gefs", "product": "atmos.5", "member": "spr"}
    if model == "HREF":
        return {"model": "href", "product": "mean", "domain": "conus"}
    if model == "ECMWF":
        # ECMWF renames the product directory by cycle: oper (00/12Z) / scda (06/18Z)
        return {"model": "ifs", "product": "scda" if cycle.hour % 12 == 6 else "oper"}
    if model == "AIFS":
        return {"model": "aifs", "product": "oper"}
    if model == "AIFS-ENS":
        return {"model": "aifs", "product": "enfo"}
    return None


HERBIE_MODELS = frozenset((
    "HRRR", "RAP", "NAM", "GFS", "GEFS", "GEFS-Spread",
    "HREF", "ECMWF", "AIFS", "AIFS-ENS",
))

# AWIPS files pack U/V into ONE message with two same-offset idx entries;
# Herbie's range math fails for the first of the pair, so retry a missed
# wind component with its sibling (the message carries both).
_UV_SIBLING = {"UGRD": "VGRD", "VGRD": "UGRD", "UGD": "VGD", "VGD": "UGD",
               "10U": "10V", "10V": "10U"}


def _sibling_search(ss):
    """:UGRD... -> :VGRD... (sibling component of a packed U/V message)."""
    for u, v in _UV_SIBLING.items():
        if f":{u}" in ss:
            return ss.replace(f":{u}", f":{v}", 1)
    return None


def search_string(short, level):
    """wgrib2-style regex filter for idx entries.

    The level is matched as a PREFIX (no trailing colon) because callers
    store shortened levels ("entire atmosphere" vs the idx's "entire
    atmosphere (considered as a single layer)"). The shortName allows a
    packed sibling (AWIPS files list UGRD/VGRD in ONE message), so
    ":UGRD" matches the line ":UGRD/VGRD:10 m above ground:".
    """
    import re
    return f":{re.escape(short)}[^:]*:{re.escape(level)}"


def fetch_subset_blobs(model, cycle, fh, searches, swap=None, surface=False):
    """Herbie-first subset fetch -> [GRIB2 bytes | None] aligned with `searches`.

    Returns None when Herbie cannot serve the file at all (unsupported
    model, unpublished run, every partner missed) so the caller falls back
    to its legacy fetcher. Individual unmatched messages come back as None
    entries - callers already tolerate partially-decoded steps.
    """
    if not searches:
        return None
    kw = herbie_kwargs(model, cycle, swap=swap, surface=surface)
    if kw is None:
        return None
    try:
        from herbie import Herbie
        naive = cycle.replace(tzinfo=None) if getattr(cycle, "tzinfo", None) else cycle
        H = Herbie(naive, fxx=int(fh), verbose=False, **kw)
        if not getattr(H, "grib", None):
            return None
        os.makedirs(SAVE_DIR, exist_ok=True)
        blobs = []
        for ss in searches:
            try:
                p = H.download(searchString=ss, save_dir=SAVE_DIR, verbose=False)
            except Exception:  # noqa: BLE001 - one unmatched message is not fatal
                p = None
            if (p is None or not Path(p).exists()) and _UV_SIBLING:
                sib = _sibling_search(ss)
                if sib:
                    try:   # packed U/V message: the sibling search fetches both
                        p = H.download(searchString=sib, save_dir=SAVE_DIR, verbose=False)
                    except Exception:  # noqa: BLE001
                        p = None
            if p is None or not Path(p).exists():
                blobs.append(None)
                continue
            data = Path(p).read_bytes()
            blobs.append(data if len(data) > 60 else None)
        return blobs
    except Exception as exc:  # noqa: BLE001 - any Herbie/network failure -> fallback
        _log.debug("herbie miss %s %s f%03d: %s", model, cycle, fh, exc)
        return None

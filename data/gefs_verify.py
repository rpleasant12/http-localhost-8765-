"""GEFS day-7 verification: last week's F168 ensemble-mean forecast vs
the observed analysis for the same valid time.

Once a day-7 cycle has scrolled out of the 48 h KEEP_HOURS window in
data/gefs.py, its frames are gone - so this module re-fetches the old
cycle's geavg F168 messages on demand and re-renders single-panel
ensemble-mean maps. The "observed" truth for the same valid hour is the
GFS 0.25-deg ANALYSIS (f000, noaa-gfs-bdp-pds - also keyless AWS open
data), falling back to the GEFS cycle's own initial state if the GFS
archive has rotated off. Both panels use the same levels/palette/map
extent as the grid's mean panels, so forecast vs observation is a fair
eyeball comparison.

Payload (bundle, cached 6 h): rows of {label, fcstUrl, obsUrl} for
mslp / t2m / cape / pwat - the products whose single fields verify
cleanly against analyses. Never raises.
"""
import datetime as dt
import os
import re
import threading
import time

import numpy as np
import requests

from data import _tz
from data.gefs import (_MSGS, PRODUCTS, _fetch_product, _fields_sane,
                       _msg_range, _na_crop)
from data.model_maps import _decode_grib_bytes, _fetch_range

UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}
OUT_DIR = os.path.join("static", "gefs_verify")
KEEP_HOURS = 21 * 24          # keep ~3 weeks of verification pairs
CACHE = {"t": 0.0, "b": None}
CACHE_LOCK = threading.Lock()

VERIFY_LEAD = 168             # 7 days
GFS_BASE = ("https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
            "gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f000")

# verified products -> the one message that drives the panel
_TRIPLES = {"mslp": _MSGS["mslp"][0],    # PRMSL @ mean sea level
            "t2m": _MSGS["t2m"][0],      # TMP @ 2 m above ground
            "cape": _MSGS["cape"][0],    # CAPE @ 180-0 mb
            "pwat": _MSGS["pwat"][0]}    # PWAT @ entire atmosphere


def _cycle_ago(days=7):
    """The verification init: one week before the newest complete cycle."""
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0,
                                                   microsecond=0)
    cyc = now - dt.timedelta(hours=6)
    cyc = cyc.replace(hour=(cyc.hour // 6) * 6)
    return cyc - dt.timedelta(days=days)


def _get_idx(url, attempts=3):
    """idx lines, or None on 404 (cycle rotated off) / network failure."""
    for i in range(attempts):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            if r.status_code == 200:
                return r.text.splitlines()
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(1 + i)
    return None


def _fetch_gfs_analysis(cyc):
    """(fields, lat, lon) from the GFS 0.25-deg analysis, or None.

    Same message triples as the GEFS products - the 0p25 pgrb2 file uses
    the same shortNames/levels, just on its own native grid (the crop in
    the renderer works off coordinates, so the grid difference is fine).
    """
    url = GFS_BASE.format(ymd=f"{cyc:%Y%m%d}", hh=f"{cyc:%H}")
    idx = _get_idx(url + ".idx")
    if not idx:
        return None
    fields, lat, lon = {}, None, None
    for prod, (short, level, out_key) in _TRIPLES.items():
        rng = _msg_range(idx, short, level)
        if not rng:
            continue
        try:
            blob = _fetch_range(url, rng[0], rng[1])
            decoded, la, lo = _decode_grib_bytes(blob)
        except Exception:      # noqa: BLE001 - one bad message -> skip
            continue
        for sn, vals in (decoded or {}).items():
            v = np.asarray(vals, dtype=float)
            if v.ndim == 2:
                fields[out_key] = v
                lat, lon = la, lo
    if not fields or lat is None or not _fields_sane(fields):
        return None
    return fields, lat, lon


def _fetch_gefs_analysis(cyc):
    """Fallback truth: the GEFS cycle's own f000 initial state."""
    fields, lat, lon = {}, None, None
    for prod in _TRIPLES:
        got = _fetch_product(cyc, 0, prod)
        if not got:
            continue
        f, la, lo = got
        for k in _TRIPLES.values():
            if k[2] in f:
                fields[k[2]] = f[k[2]]
        lat, lon = la, lo
    if not fields or lat is None:
        return None
    return fields, lat, lon


def _render_panel(fields, lat, lon, prod, cyc, fh, out_path, tag):
    """Single-panel map with the grid's mean levels/palette.

    tag "fcst" -> 'F168 ensemble-mean forecast' suptitle;
    tag "obs"  -> 'Observed analysis' suptitle. Same canvas either way so
    the two panels of a pair align pixel for pixel.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from data._tz import full

    if prod == "mslp":
        va, _, la, lo = _na_crop(lat, lon, fields["PRMSL"], None)
        val, lev = va / 100.0, np.arange(960, 1051, 2)
        cmap, lbl = "RdYlBu_r", "MSLP (hPa)"
    elif prod == "t2m":
        va, _, la, lo = _na_crop(lat, lon, fields["T2M"], None)
        val, lev = va * 1.8 - 459.67, np.arange(-20, 116, 3)
        cmap, lbl = "turbo", "\u00b0F"
    elif prod == "cape":
        va, _, la, lo = _na_crop(lat, lon, fields["CAPE"], None)
        val = np.where(va >= 100, va, np.nan)
        lev = np.arange(100, 5001, 250)
        cmap, lbl = "YlOrRd", "J/kg"
    else:  # pwat
        va, _, la, lo = _na_crop(lat, lon, fields["PWAT"], None)
        val, lev = va / 25.4, np.arange(0.1, 2.61, 0.1)
        cmap, lbl = "PuBuGn", "in"

    valid = cyc + dt.timedelta(hours=fh)
    fig = plt.figure(figsize=(8.6, 6.6), dpi=100)
    proj = ccrs.LambertConformal(central_longitude=-100, central_latitude=42)
    ax = fig.add_subplot(1, 1, 1, projection=proj)
    ax.set_extent([-170, -50, 10, 72], crs=ccrs.PlateCarree())
    ax.coastlines("50m", linewidth=0.5)
    ax.add_feature(cfeature.STATES, linewidth=0.35, edgecolor="gray")
    ax.add_feature(cfeature.BORDERS, linewidth=0.5, edgecolor="dimgray")
    cf = ax.contourf(lo, la, val, levels=lev, cmap=cmap,
                     transform=ccrs.PlateCarree(), alpha=0.85, extend="max")
    plt.colorbar(cf, ax=ax, shrink=0.8, label=lbl)
    if tag == "fcst":
        ax.set_title(f"GEFS F{fh:03d} ensemble-mean forecast - init "
                     f"{full(cyc)}", fontsize=11)
    else:
        ax.set_title(f"Observed analysis - valid {full(valid)}", fontsize=11)
    tmp = out_path.replace(".png", ".tmp.png")
    fig.savefig(tmp, bbox_inches="tight")
    plt.close(fig)
    os.replace(tmp, out_path)
    return out_path


def _panel_path(kind, prod, cyc, fh):
    return os.path.join(OUT_DIR, f"gefsver_{kind}_{prod}_f{fh:03d}_"
                                 f"{cyc:%Y%m%d%H}_na.png")


def _ensure_fcst_panel(cyc, prod):
    """Render (or reuse) the old cycle's F168 ensemble-mean panel."""
    path = _panel_path("fcst", prod, cyc, VERIFY_LEAD)
    if os.path.exists(path) and os.path.getsize(path) > 6_000:
        return path
    got = _fetch_product(cyc, VERIFY_LEAD, prod)
    if not got:
        return None
    fields, lat, lon = got
    try:
        return _render_panel(fields, lat, lon, prod, cyc, VERIFY_LEAD,
                             path, "fcst")
    except Exception:          # noqa: BLE001 - one bad frame never kills
        return None


def _ensure_obs_panel(cyc, prod, obs):
    """Render (or reuse) the observed-analysis panel for the valid time."""
    path = _panel_path("obs", prod, cyc, 0)
    if os.path.exists(path) and os.path.getsize(path) > 6_000:
        return path
    fields, lat, lon = obs
    if prod not in _TRIPLES or _TRIPLES[prod][2] not in fields:
        return None
    try:
        return _render_panel(fields, lat, lon, prod, cyc, 0, path, "obs")
    except Exception:          # noqa: BLE001
        return None


def _prune_old():
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=KEEP_HOURS)
    try:
        for fn in os.listdir(OUT_DIR):
            m = re.search(r"_(\d{10})_na\.png$", fn)
            if not m:
                continue
            try:
                tv = dt.datetime.strptime(m.group(1), "%Y%m%d%H")\
                    .replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            if tv < cutoff:
                try:
                    os.remove(os.path.join(OUT_DIR, fn))
                except OSError:
                    pass
    except OSError:
        pass


def _build():
    cyc = _cycle_ago()
    obs = _fetch_gfs_analysis(cyc)
    src = "GFS 0.25-deg analysis (noaa-gfs-bdp-pds, no keys)"
    if not obs:
        obs = _fetch_gefs_analysis(cyc)
        src = "GEFS initial analysis (GFS archive unavailable)"
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = []
    if obs:
        for prod in _TRIPLES:
            fpath = _ensure_fcst_panel(cyc, prod)
            opath = _ensure_obs_panel(cyc, prod, obs)
            if not (fpath and opath):
                continue
            rows.append({
                "prod": prod,
                "label": PRODUCTS[prod]["label"],
                "fh": VERIFY_LEAD,
                "fcstUrl": f"../gefs_verify/{os.path.basename(fpath)}",
                "obsUrl": f"../gefs_verify/{os.path.basename(opath)}",
            })
    _prune_old()
    return {
        "ok": bool(rows),
        "cycle": _tz.stamp(cyc),
        "obsSource": src if rows else "",
        "rows": rows,
        "source": "GEFS day-7 verification: F168 ensemble-mean forecast "
                  "(re-fetched from the NOAA AWS open-data archive) vs the "
                  "GFS analysis at the same valid hour - no keys",
    }


def bundle(max_age=6 * 3600):
    """Payload for the gefs.html verification row (cached 6 h)."""
    with CACHE_LOCK:
        now = time.time()
        if CACHE["b"] is not None and now - CACHE["t"] < max_age:
            cached = CACHE["b"]
            refs_ok = all(os.path.isfile(os.path.join(OUT_DIR, r[k].rsplit("/", 1)[-1]))
                          for r in cached.get("rows", [])
                          for k in ("fcstUrl", "obsUrl"))
            if refs_ok:
                return cached
            CACHE.update(t=0.0, b=None)
    try:
        b = _build()
    except Exception:          # noqa: BLE001 - never break a build
        b = {"ok": False, "rows": [], "cycle": None, "obsSource": ""}
    with CACHE_LOCK:
        CACHE.update(t=time.time(), b=b)
    return b

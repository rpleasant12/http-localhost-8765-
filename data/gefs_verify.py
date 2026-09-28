"""GEFS lead-time verification: day 3 / 5 / 7 / 10 forecasts vs the
observed analysis at the same valid hour.

Every lead verifies against ONE shared valid time V = the newest complete
GEFS cycle. For each lead L the module fetches the week-old-style init
(V - L) geavg ensemble mean at f{L} from NOAA's AWS open-data archive
(the interactive grid only keeps frames 48 h, so these are re-fetches)
and renders it beside the GFS 0.25-deg ANALYSIS at V (noaa-gfs-bdp-pds,
also keyless; GEFS f000 fallback). Because all leads share V, the rows
show exactly how skill decays with lead time. Panels reuse the grid's
levels/palette/extent so differences are the forecast's, not the map's.

f240 (day 10) is attempted opportunistically - if the archive cycle
lacks it the lead simply drops out of the payload and the page disables
that picker button. Payload cached 6 h; panels kept ~3 weeks. Never
raises.
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
SCORE_FILE = os.path.join(OUT_DIR, "scores.json")   # pair -> {rms, r, bias}

# display units per product for the score caption
_UNITS = {"mslp": "hPa", "t2m": "\u00b0F", "cape": "J/kg", "pwat": "in"}

# (forecast lead hours, picker label)
LEADS = ((72, "Day 3"), (120, "Day 5"), (168, "Day 7"), (240, "Day 10"))
GFS_BASE = ("https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
            "gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f000")

# verified products -> the one message that drives the panel
_TRIPLES = {"mslp": _MSGS["mslp"][0],    # PRMSL @ mean sea level
            "t2m": _MSGS["t2m"][0],      # TMP @ 2 m above ground
            "cape": _MSGS["cape"][0],    # CAPE @ 180-0 mb
            "pwat": _MSGS["pwat"][0]}    # PWAT @ entire atmosphere


def _valid_time():
    """V: the newest complete GEFS cycle (floored to 00/06/12/18 UTC)."""
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0,
                                                   microsecond=0)
    cyc = now - dt.timedelta(hours=6)
    return cyc.replace(hour=(cyc.hour // 6) * 6)


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


def _fetch_gfs_analysis(ts):
    """(fields, lat, lon) from the GFS 0.25-deg analysis at hour ts, or None.

    Same message triples as the GEFS products - the 0p25 pgrb2 file uses
    the same shortNames/levels, just on its own native grid (the crop in
    the renderer works off coordinates, so the grid difference is fine).
    """
    url = GFS_BASE.format(ymd=f"{ts:%Y%m%d}", hh=f"{ts:%H}")
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


def _fetch_gefs_analysis(ts):
    """Fallback truth: the GEFS cycle at ts's own f000 initial state."""
    fields, lat, lon = {}, None, None
    for prod in _TRIPLES:
        got = _fetch_product(ts, 0, prod)
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


def _render_panel(fields, lat, lon, prod, ts, fh, out_path, tag):
    """Single-panel map with the grid's mean levels/palette.

    tag "fcst" -> 'F{fh} ensemble-mean forecast - init {ts}' title;
    tag "obs"  -> 'Observed analysis - valid {ts}' title. Same canvas
    either way so fcst/obs panels of a pair align pixel for pixel.
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
                     f"{full(ts)}", fontsize=11)
    else:
        ax.set_title(f"Observed analysis - valid {full(ts)}", fontsize=11)
    tmp = out_path.replace(".png", ".tmp.png")
    fig.savefig(tmp, bbox_inches="tight")
    plt.close(fig)
    os.replace(tmp, out_path)
    return out_path


def _panel_path(kind, prod, ts, fh):
    return os.path.join(OUT_DIR, f"gefsver_{kind}_{prod}_f{fh:03d}_"
                                 f"{ts:%Y%m%d%H}_na.png")


def _load_scores():
    try:
        import json
        with open(SCORE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_scores(scores):
    try:
        import json
        os.makedirs(OUT_DIR, exist_ok=True)
        tmp = SCORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(scores, f)
        os.replace(tmp, SCORE_FILE)
    except OSError:
        pass


def _pair_score(fval, flat, flon, oval, olat, olon, prod):
    """(rms, r, bias) of forecast vs obs in display units over the map crop.

    Both grids are regular lat-lon (GEFS 0.5 deg, GFS 0.25 deg): crop each
    to the mapped domain, then bilinearly interpolate the obs onto the
    forecast grid so the comparison is point for point. bias = fcst - obs
    (positive = forecast too high).
    """
    if prod == "mslp":
        fval, oval = fval / 100.0, oval / 100.0
    elif prod == "t2m":
        fval, oval = fval * 1.8 - 459.67, oval * 1.8 - 459.67
    elif prod == "pwat":
        fval, oval = fval / 25.4, oval / 25.4
    fc, fla, flo = _na_crop(flat, flon, fval)
    oc, ola, olo = _na_crop(olat, olon, oval)
    try:
        from scipy.interpolate import RegularGridInterpolator
    except ImportError:
        return None
    lata, lona = ola[:, 0], olo[0, :]
    if lata[0] > lata[-1]:                      # GFS ships pole-down sometimes
        lata, oc = lata[::-1], oc[::-1, :]
    if lona[0] > lona[-1]:
        lona, oc = lona[::-1], oc[:, ::-1]
    interp = RegularGridInterpolator((lata, lona), oc,
                                     bounds_error=False, fill_value=np.nan)
    oi = interp(np.column_stack([fla.ravel(), flo.ravel()])).reshape(fla.shape)
    m = np.isfinite(fc) & np.isfinite(oi)
    if m.sum() < 500:
        return None
    d = fc[m] - oi[m]
    rms = float(np.sqrt(np.mean(d * d)))
    bias = float(np.mean(d))
    sd_f, sd_o = float(np.std(fc[m])), float(np.std(oi[m]))
    r = None
    if sd_f > 1e-9 and sd_o > 1e-9:
        r = float(np.corrcoef(fc[m], oi[m])[0, 1])
    return {"rms": round(rms, 2), "r": (round(r, 3) if r is not None else None),
            "bias": round(bias, 2), "unit": _UNITS.get(prod, "")}


def _ensure_fcst_panel(init, prod, lead):
    """Render (or reuse) one lead's ensemble-mean forecast panel."""
    path = _panel_path("fcst", prod, init, lead)
    if os.path.exists(path) and os.path.getsize(path) > 6_000:
        return path
    got = _fetch_product(init, lead, prod)
    if not got:
        return None
    fields, lat, lon = got
    try:
        return _render_panel(fields, lat, lon, prod, init, lead, path, "fcst")
    except Exception:          # noqa: BLE001 - one bad frame never kills
        return None


def _ensure_obs_panel(ts, prod, obs):
    """Render (or reuse) the observed-analysis panel at hour ts."""
    path = _panel_path("obs", prod, ts, 0)
    if os.path.exists(path) and os.path.getsize(path) > 6_000:
        return path
    fields, lat, lon = obs
    if prod not in _TRIPLES or _TRIPLES[prod][2] not in fields:
        return None
    try:
        return _render_panel(fields, lat, lon, prod, ts, 0, path, "obs")
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
    v = _valid_time()
    obs = _fetch_gfs_analysis(v)
    src = "GFS 0.25-deg analysis (noaa-gfs-bdp-pds, no keys)"
    if not obs:
        obs = _fetch_gefs_analysis(v)
        src = "GEFS initial analysis (GFS archive unavailable)"
    os.makedirs(OUT_DIR, exist_ok=True)
    scores = _load_scores()
    dirty = False
    rows = []
    leads = []
    if obs:
        o_fields, o_lat, o_lon = obs
        opaths = {p: _ensure_obs_panel(v, p, obs) for p in _TRIPLES}
        for lead, lbl in LEADS:
            # f240 (day 10) only exists for some cycle hours in the archive:
            # exact init first, then the nearest 12Z cycle (valid shifts <= 12 h)
            init = v - dt.timedelta(hours=lead)
            inits = [init]
            if lead == 240:
                alt = init.replace(hour=12)
                if alt != init:
                    inits.append(alt)
            n = 0
            for prod in _TRIPLES:
                if not opaths.get(prod):
                    continue
                fpath = iu = None
                for cand in inits:
                    cand_path = _panel_path("fcst", prod, cand, lead)
                    ckey = f"{lead}:{prod}:{cand:%Y%m%d%H}:{v:%Y%m%d%H}"
                    if os.path.exists(cand_path) and ckey in scores \
                            and os.path.getsize(cand_path) > 6_000:
                        fpath, iu = cand_path, cand
                        break
                    got = _fetch_product(cand, lead, prod)
                    if not got:
                        continue
                    f_fields, f_lat, f_lon = got
                    try:
                        fpath = _render_panel(f_fields, f_lat, f_lon, prod,
                                              cand, lead, cand_path, "fcst")
                    except Exception:   # noqa: BLE001 - one bad frame never kills
                        fpath = None
                    if not fpath:
                        continue
                    iu = cand
                    sc = _pair_score(f_fields[_TRIPLES[prod][2]], f_lat,
                                     f_lon, o_fields[_TRIPLES[prod][2]],
                                     o_lat, o_lon, prod)
                    if sc:
                        scores[ckey] = sc
                        dirty = True
                    break
                if not fpath:
                    continue
                rows.append({
                    "lead": lead,
                    "leadLabel": lbl,
                    "prod": prod,
                    "label": PRODUCTS[prod]["label"],
                    "fh": lead,
                    "init": _tz.stamp(iu),
                    "fcstUrl": f"../gefs_verify/{os.path.basename(fpath)}",
                    "obsUrl": f"../gefs_verify/{os.path.basename(opaths[prod])}",
                    "score": scores.get(
                        f"{lead}:{prod}:{iu:%Y%m%d%H}:{v:%Y%m%d%H}"),
                })
                n += 1
            if n:
                leads.append({"lead": lead, "label": lbl, "rows": n})
    if dirty:
        _save_scores(scores)
    _prune_old()
    _prune_old()
    return {
        "ok": bool(rows),
        "valid": _tz.stamp(v),
        "leads": leads,
        "rows": rows,
        "obsSource": src if rows else "",
        "source": "GEFS lead-time verification: each lead's ensemble-mean "
                  "forecast (re-fetched from the NOAA AWS open-data archive) "
                  "vs the GFS analysis at the shared valid hour - no keys",
    }


def bundle(max_age=6 * 3600):
    """Payload for the gefs.html verification card (cached 6 h)."""
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
        b = {"ok": False, "rows": [], "leads": [], "valid": None,
             "obsSource": ""}
    with CACHE_LOCK:
        CACHE.update(t=time.time(), b=b)
    return b

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

Scores are also accumulated per valid hour in scores_history.json (kept
HIST_DAYS days) and rendered into a skill-vs-lead chart: pattern
correlation r vs the GFS analysis, one panel per product, one point per
day (HIST_DAYS window). backfill_history() seeds the chart from the
archive for past days.

Each score also carries ACC, the anomaly correlation coefficient: fcst
and obs are both converted to anomalies against a same-day climatology
(the mean of the CLIMO_YEARS_BACK prior years of GFS analyses, built
on demand and cached per MMDD+hour), then correlated. ACC > 0 means the
forecast beat climatology - the standard yes/no skill question.
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
HIST_FILE = os.path.join(OUT_DIR, "scores_history.json")  # vYYYYMMDDHH -> lead:prod -> score
HIST_DAYS = 90               # chart window: skill vs lead over ~90 days

# ACC climatology: same-day GFS analyses from the N prior years (bucket
# coverage starts 2021; the window slides forward automatically).
CLIMO_YEARS_BACK = 5
CLIMO_MIN_YEARS = 3
CLIMO_DIR = os.path.join(OUT_DIR, "climo")
_CLIMO_MEM = {}              # "MMDDHH" -> (means, lat, lon)

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


def _decode_gefs_mean(ts, fh):
    """(fields, lat, lon) of the geavg ensemble mean at hour ts + fh hours.

    Same byte-range/decoder helpers as the GFS truth fetch - the pgrb2ap5
    geavg file carries the same standard shortNames, so scores need no
    render pass at all.
    """
    url = ("https://noaa-gefs-pds.s3.amazonaws.com/gefs.{ymd}/{hh}/atmos/"
           "pgrb2ap5/geavg.t{hh}z.pgrb2a.0p50.f{fff:03d}").format(
        ymd=f"{ts:%Y%m%d}", hh=f"{ts:%H}", fff=fh)
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
    if not fields or lat is None:
        return None
    return fields, lat, lon


def _decode_gefs_spr(ts, fh):
    """(fields, lat, lon) of the gespr ensemble SPREAD at hour ts + fh.

    The gespr file's CAPE/PWAT/U/V messages are true standard deviations
    (the ptype flags are not, but ptype is not verified here). PRMSL and
    TMP @2m in gespr also carry the spread of those variables.
    """
    url = ("https://noaa-gefs-pds.s3.amazonaws.com/gefs.{ymd}/{hh}/atmos/"
           "pgrb2ap5/gespr.t{hh}z.pgrb2a.0p50.f{fff:03d}").format(
        ymd=f"{ts:%Y%m%d}", hh=f"{ts:%H}", fff=fh)
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
    if not fields or lat is None:
        return None
    return fields, lat, lon


def _get_climo(v):
    """(means, lat, lon) same-day climatology for valid hour v, or None.

    means: {out_key: mean field} over the CLIMO_YEARS_BACK prior years of
    GFS 0.25-deg analyses (>= CLIMO_MIN_YEARS required), on the obs crop
    grid - i.e. the same grid the obs panel uses, so _pair_score can
    interpolate it once alongside the obs. Cached in memory and as an
    npz sidecar so repeated builds/backfills never re-download.
    """
    key = f"{v:%m%d%H}"
    if key in _CLIMO_MEM:
        return _CLIMO_MEM[key]
    os.makedirs(CLIMO_DIR, exist_ok=True)
    npz = os.path.join(CLIMO_DIR, f"climo_{key}.npz")
    if os.path.exists(npz) and os.path.getsize(npz) > 50_000:
        try:
            z = np.load(npz)
            out = ({k: z[k] for k in z.files if k not in ("lat", "lon")},
                   z["lat"], z["lon"])
            _CLIMO_MEM[key] = out
            return out
        except Exception:      # noqa: BLE001 - corrupt sidecar -> rebuild
            pass
    years, lat, lon = [], None, None
    for back in range(1, CLIMO_YEARS_BACK + 1):
        try:
            ts = v.replace(year=v.year - back)
        except ValueError:     # Feb 29 has no prior-year match
            continue
        got = _fetch_gfs_analysis(ts)
        if got:
            fields, lat, lon = got
            years.append(fields)
    if len(years) < CLIMO_MIN_YEARS or lat is None:
        return None
    keys = set(years[0])
    for f in years[1:]:
        keys &= set(f)
    means = {k: np.mean([f[k] for f in years], axis=0)
             for k in keys if np.asarray(years[0][k]).ndim == 2}
    if not means:
        return None
    try:
        np.savez_compressed(npz, lat=lat, lon=lon, **means)
    except OSError:
        pass
    _CLIMO_MEM[key] = (means, lat, lon)
    return _CLIMO_MEM[key]


def _pair_score(fval, flat, flon, oval, olat, olon, prod, climo=None,
                s_fields=None):
    """(rms, r, bias) of forecast vs obs in display units over the map crop.

    Both grids are regular lat-lon (GEFS 0.5 deg, GFS 0.25 deg): crop each
    to the mapped domain, then bilinearly interpolate the obs onto the
    forecast grid so the comparison is point for point. bias = fcst - obs
    (positive = forecast too high). climo (optional) is the same-day
    climatology mean on the obs grid; when given, ACC is also computed
    from anomalies vs that climatology (>= 0 means it beat climatology).
    """
    if prod == "mslp":
        fval, oval = fval / 100.0, oval / 100.0
        conv = lambda x: x / 100.0
        sconv = conv
    elif prod == "t2m":
        fval, oval = fval * 1.8 - 459.67, oval * 1.8 - 459.67
        conv = lambda x: x * 1.8 - 459.67
        # spread is a DEVIATION (~K scale): scale it, never apply the
        # -459.67 offset (that would turn 1 K of spread into -458 F)
        sconv = lambda x: x * 1.8
    elif prod == "pwat":
        fval, oval = fval / 25.4, oval / 25.4
        conv = lambda x: x / 25.4
        sconv = conv
    else:
        conv = sconv = lambda x: x
    if climo is not None:
        climo = conv(climo)
    if s_fields is not None:
        s_fields = sconv(s_fields)
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
    acc = None
    if climo is not None:
        # climo lives on the obs grid: crop it the same way and give it
        # its own interpolator onto the forecast grid (the one above only
        # knows the obs array).
        ci = None
        try:
            cc, cla, clo = _na_crop(olat, olon, climo)
            clata, clona = cla[:, 0], clo[0, :]
            if clata[0] > clata[-1]:
                clata, cc = clata[::-1], cc[::-1, :]
            if clona[0] > clona[-1]:
                clona, cc = clona[::-1], cc[:, ::-1]
            cinterp = RegularGridInterpolator((clata, clona), cc,
                                              bounds_error=False,
                                              fill_value=np.nan)
            ci = cinterp(np.column_stack([fla.ravel(), flo.ravel()]))\
                .reshape(fla.shape)
        except Exception:      # noqa: BLE001 - degenerate climo grid
            ci = None
        if ci is not None:
            m2 = m & np.isfinite(ci)
            if m2.sum() >= 500:
                af, ao = fc[m2] - ci[m2], oi[m2] - ci[m2]
                den = float(np.sqrt(np.sum(af * af) * np.sum(ao * ao)))
                if den > 1e-9:
                    acc = float(np.sum(af * ao) / den)
    d = fc[m] - oi[m]
    rms = float(np.sqrt(np.mean(d * d)))
    bias = float(np.mean(d))
    spr = None
    if s_fields is not None:
        # spread-skill: the gespr std-dev lives on the forecast grid, so
        # it aligns with fc after the same crop - compare it with the
        # mean's actual error. ratio ~ 1 is a calibrated ensemble; corr
        # links spread to where the error actually is.
        try:
            scrop, _sa, _so = _na_crop(flat, flon, s_fields)
            ms = m & np.isfinite(scrop)
            if ms.sum() >= 500:
                sd = float(np.sqrt(np.mean(scrop[ms] ** 2)))
                rmse = float(np.sqrt(np.mean(d ** 2)))   # d is already masked
                if sd > 1e-9 and rmse > 1e-9:
                    a, b = scrop[ms], np.abs(fc[ms] - oi[ms])
                    rc = None
                    if a.std() > 1e-9 and b.std() > 1e-9:
                        rc = float(np.corrcoef(a, b)[0, 1])
                    spr = {"ratio": round(sd / rmse, 2),
                           "corr": (round(rc, 3) if rc is not None else None),
                           "overconf": round(100.0 * (1.0 - sd / rmse), 1)}
        except Exception:      # noqa: BLE001 - spread is best-effort
            spr = None
    sd_f, sd_o = float(np.std(fc[m])), float(np.std(oi[m]))
    r = None
    if sd_f > 1e-9 and sd_o > 1e-9:
        r = float(np.corrcoef(fc[m], oi[m])[0, 1])
    return {"rms": round(rms, 2), "r": (round(r, 3) if r is not None else None),
            "bias": round(bias, 2),
            "acc": (round(acc, 3) if acc is not None else None),
            "spread": spr,
            "unit": _UNITS.get(prod, "")}


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
    try:                              # climo sidecars: keep the last ~100
        cl = sorted((os.path.getmtime(os.path.join(CLIMO_DIR, f)), f)
                    for f in os.listdir(CLIMO_DIR) if f.endswith(".npz"))
        for _mt, fn in cl[:-100]:
            try:
                os.remove(os.path.join(CLIMO_DIR, fn))
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
    climo = _get_climo(v) if obs else None
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
            spr_cache = None
            spr_key = None
            for prod in _TRIPLES:
                if not opaths.get(prod):
                    continue
                fpath = iu = None
                for cand in inits:
                    cand_path = _panel_path("fcst", prod, cand, lead)
                    ckey = f"{lead}:{prod}:{cand:%Y%m%d%H}:{v:%Y%m%d%H}"
                    if os.path.exists(cand_path) and ckey in scores \
                            and scores[ckey].get("acc") is not None \
                            and scores[ckey].get("spread") is not None \
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
                    if spr_key != cand:
                        sday = _decode_gefs_spr(cand, lead)
                        spr_cache = (sday[0] if sday else {})
                        spr_key = cand
                    cm = (climo[0] if climo else {}).get(_TRIPLES[prod][2])
                    sc = _pair_score(f_fields[_TRIPLES[prod][2]], f_lat,
                                     f_lon, o_fields[_TRIPLES[prod][2]],
                                     o_lat, o_lon, prod, climo=cm,
                                     s_fields=spr_cache.get(_TRIPLES[prod][2]))
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
        _extend_history(scores)
        _prune_history()
    _prune_old()
    _history_chart()
    return {
        "ok": bool(rows),
        "valid": _tz.stamp(v),
        "leads": leads,
        "rows": rows,
        "history": _load_history(),
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


def _extend_history(scores):
    """Append this build's pair scores to the per-valid-hour history file.

    Keyed vYYYYMMDDHH -> "lead:prod" -> {rms, r, bias, unit, init}.
    Re-running the same valid hour overwrites (idempotent), and only the
    current build's valid hour is appended - past hours belong to their
    own builds or to backfill_history().
    """
    import json
    v = _valid_time()
    vs = f"{v:%Y%m%d%H}"
    try:
        with open(HIST_FILE, encoding="utf-8") as f:
            hist = json.load(f)
    except (OSError, ValueError):
        hist = {}
    day = hist.get(f"v{vs}") or {}
    for ckey, sc in (scores or {}).items():
        try:
            lead_s, prod, init_s, valid_s = ckey.split(":")
        except (ValueError, AttributeError):
            continue
        if valid_s != vs or lead_s.startswith("v"):
            continue
        # this build's scores are authoritative for the current valid hour
        day[f"{lead_s}:{prod}"] = {
            "rms": sc.get("rms"), "r": sc.get("r"), "bias": sc.get("bias"),
            "acc": sc.get("acc"), "spread": sc.get("spread"),
            "unit": sc.get("unit"), "init": init_s,
        }
    if not day:
        return
    hist[f"v{vs}"] = day
    try:
        tmp = HIST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f)
        os.replace(tmp, HIST_FILE)
    except OSError:
        pass


def _prune_history(days=HIST_DAYS):
    """Keep scores for the last `days` valid hours (fixed-width keys sort)."""
    import json
    try:
        with open(HIST_FILE, encoding="utf-8") as f:
            hist = json.load(f)
    except (OSError, ValueError):
        return
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=days)).strftime("%Y%m%d%H")
    kept = {k: d for k, d in hist.items()
            if isinstance(k, str) and k.startswith("v") and k[1:] >= cutoff}
    if len(kept) == len(hist):
        return
    try:
        tmp = HIST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(kept, f)
        os.replace(tmp, HIST_FILE)
    except OSError:
        pass


def _load_history():
    try:
        import json
        with open(HIST_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _history_chart(path=None):
    """Static skill-vs-lead chart over the kept history, or None.

    2x2 panels (one per product) of pattern correlation r vs valid time,
    one colored line per lead - the spread between the lines IS the
    skill decay with lead time, and the rightward slope of each line is
    how that lead's skill changes day to day.
    """
    hist = _load_history()
    if not hist:
        return None
    leads_avail = [L for L, _lbl in LEADS]
    series = {p: {L: ([], []) for L in leads_avail} for p in _TRIPLES}
    any_pt = False
    now = dt.datetime.now(dt.timezone.utc)
    for key in sorted(hist):
        try:
            vts = dt.datetime.strptime(key[1:], "%Y%m%d%H").replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        for lk, sc in (hist[key] or {}).items():
            try:
                lead_s, prod = lk.split(":")
                lead = int(lead_s)
            except (ValueError, AttributeError):
                continue
            if prod not in series or lead not in series[prod]:
                continue
            r = (sc or {}).get("r")
            if r is None:
                continue
            age = (now - vts).total_seconds() / 86400.0
            series[prod][lead][0].append(-age)
            series[prod][lead][1].append(r)
            any_pt = True
    if not any_pt:
        return None
    path = path or os.path.join(OUT_DIR, "gefsver_skill_history.png")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(2, 2, figsize=(9.6, 6.8), dpi=100,
                                 sharex=True)
        fig.suptitle("GEFS forecast skill vs lead time - last "
                     f"{HIST_DAYS} days (pattern correlation r vs GFS "
                     "analysis)", fontsize=11)
        for ax, prod in zip(axes.ravel(), _TRIPLES):
            ax.set_title(f"{PRODUCTS[prod]['label']}  (r)", fontsize=10)
            for L, color in zip(leads_avail,
                                ("#4ea1ff", "#39d98a", "#ffb020", "#ff6b6b")):
                xs, ys = series[prod][L]
                if not xs:
                    continue
                order = sorted(range(len(xs)), key=lambda i: xs[i])
                ax.plot([xs[i] for i in order], [ys[i] for i in order],
                        "o-", ms=2.6, lw=1.3, color=color,
                        label=f"Day {L // 24}")
            ax.set_ylim(0, 1.02)
            ax.grid(alpha=0.25, lw=0.4)
            ax.legend(fontsize=7, loc="lower left")
            ax.axhline(0.6, color="gray", lw=0.6, ls=":")
        for ax in axes[1]:
            ax.set_xlabel("valid time (days ago)", fontsize=8)
        for ax in axes.ravel():
            ax.xaxis.set_major_formatter(
                matplotlib.ticker.FuncFormatter(lambda x, _p: f"{-x:.0f}d"))
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        tmp = path.replace(".png", ".tmp.png")
        fig.savefig(tmp)
        plt.close(fig)
        os.replace(tmp, path)
        return path
    except Exception:          # noqa: BLE001 - chart never breaks the build
        return None


def backfill_history(days=HIST_DAYS):
    """Recompute pair scores for past valid hours straight from the archive.

    For each past valid hour V (00Z cycles, newest first) the GFS 0.25-deg
    analysis at V is scored against the GEFS geavg ensemble mean at
    f{V - init} from the V-lead init - no panels rendered, scores only.
    Idempotent: hours already in the history file are skipped. The
    in-memory climo cache is popped per key after each hour (each key is
    used once per run; ~1 MB of arrays apiece).
    """
    import json
    os.makedirs(OUT_DIR, exist_ok=True)
    hist = _load_history()
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0,
                                                   microsecond=0)
    done = 0
    for d in range(1, days + 1):
        v = (now - dt.timedelta(days=d)).replace(hour=0)
        key = f"v{v:%Y%m%d%H}"
        if hist.get(key) and all(
                (sc or {}).get("acc") is not None
                and (sc or {}).get("spread") is not None
                for sc in hist[key].values()):
            continue
        obs = _fetch_gfs_analysis(v)
        if not obs:
            print(f"backfill {v:%m-%d}: no GFS analysis, skip", flush=True)
            continue
        o_fields, o_lat, o_lon = obs
        climo = _get_climo(v)
        day = {}
        for lead, _lbl in LEADS:
            init = v - dt.timedelta(hours=lead)
            fday = _decode_gefs_mean(init, lead)
            if not fday:
                print(f"backfill {v:%m-%d} lead {lead}: no GEFS mean, skip",
                      flush=True)
                continue
            f_fields, f_lat, f_lon = fday
            sday = _decode_gefs_spr(init, lead)
            s_all = sday[0] if sday else {}
            for prod, (_short, _level, out_key) in _TRIPLES.items():
                if out_key not in f_fields or out_key not in o_fields:
                    continue
                cm = (climo[0] if climo else {}).get(out_key)
                sc = _pair_score(f_fields[out_key], f_lat, f_lon,
                                 o_fields[out_key], o_lat, o_lon, prod,
                                 climo=cm, s_fields=s_all.get(out_key))
                if sc:
                    day[f"{lead}:{prod}"] = {
                        "rms": sc.get("rms"), "r": sc.get("r"),
                        "bias": sc.get("bias"), "acc": sc.get("acc"),
                        "spread": sc.get("spread"),
                        "unit": sc.get("unit"),
                        "init": f"{init:%Y%m%d%H}",
                    }
        if day:
            hist[key] = day
            done += 1
            print(f"backfill {v:%m-%d}: {len(day)} scores", flush=True)
        _CLIMO_MEM.pop(f"{v:%m%d%H}", None)
        try:
            tmp = HIST_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(hist, f)
            os.replace(tmp, HIST_FILE)
        except OSError:
            pass
    _prune_history()
    return done

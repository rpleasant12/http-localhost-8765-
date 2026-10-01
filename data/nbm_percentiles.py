"""NBM percentile-uncertainty maps from the blend QMD files (free, keyless).

The NBM's Quantile-Modelled Distribution (qmd) GRIB2 ships the FULL forecast
distribution per element: messages tagged "10% level", "25% level", ... on
the NOMADS blend open-data bucket. Where the regular NBM walls show the
pre-blended median, these maps show the UNCERTAINTY:

- 10th/25th/75th/90th percentile maps for 2 m temperature, 10 m wind and
  6-h QPF (percentile = chance of staying below the mapped value)
- a p90-p10 SPREAD map per element: where the distribution is wide, the
  forecast itself is uncertain

Source: blend.t{HH}z.qmd.f{FFF}.co.grib2 (+.idx) under
https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod/blend.{Ymd}/{HH}/qmd/
Byte-range fetches through the shared model_maps helpers; renders with the
same cartopy stack as the model walls into static/nbm_pct/.

QMD publication lags the core files by a few hours (a 04Z walk often finds
only the 18Z qmd live), so the cycle finder probes the tiny .idx up to a
day back. Never raises; bundle() reports ok=False when the desk is down.
"""
import datetime as dt
import os
import pickle
import re
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np
import requests

from data import _tz
from data.model_maps import _decode_grib_bytes, _fetch_range, MAP_REGIONS

# Render watchdog: cartopy/matplotlib inside this process has hung repeatedly
# (observed 2026-09-30/10-01: contour reprojection wedging the whole site
# updater for >45 min). Every map render now runs in a child process killed
# after this budget, so a wedged render can never freeze a build cycle.
RENDER_TIMEOUT = 180          # per-map child budget
REFRESH_BUDGET = 20 * 60      # whole-render budget: never outlive the watchdog

UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}
OUT_DIR = os.path.join("static", "nbm_pct")
CACHE = {"t": 0.0, "b": None}
LAST_GOOD = {"t": 0.0, "b": None}   # survives transient NOMADS rate limits
CACHE_LOCK = threading.Lock()
MIRROR_PATH = os.path.join(".freebuff", "nbm_percentiles.json")
_SESSION = requests.Session()


def _load_mirror():
    """Seed the in-memory last-good from the previous process's mirror.

    Without this, every updater restart re-rendered every NBM map from
    scratch during a build - exactly when the render path was hanging.
    With it, a restarted updater serves last-good instantly and re-renders
    on its own schedule.
    """
    try:
        import json
        with open(MIRROR_PATH, encoding="utf-8") as f:
            b = json.load(f)
        if b.get("ok"):
            LAST_GOOD.update(t=time.time(), b=b)
    except (OSError, ValueError):
        pass


_load_mirror()

# NOMADS throttles bursts: one polite request per candidate stamp, with
# synoptic cycles probed first (see find_qmd_cycle).
_PROBE_SLEEP = 2.0
_LAST_CYCLE = {"c": None}   # steady state: 1-2 probes per refresh, not a walk

FH = 12                       # guidance hour rendered (12 h out)
PERCENTILES = (10, 25, 75, 90)
KEEP_HOURS = 30               # self-prune window for rendered PNGs

QMD_BASE = ("https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod/"
            "blend.{ymd}/{hh}/qmd/blend.t{hh}z.qmd.f{fh:03d}.co.grib2")

# element spec: grib element + level (+ acc window for precip), display
# units, colormap and fill levels per percentile family
ELEMENTS = {
    "temp": {
        "elem": "TMP", "level": "2 m above ground", "acc": None,
        "label": "Temperature", "unit": "\u00b0F",
        "cmap": "RdBu_r",
        "levels": np.arange(-10.0, 111.0, 5.0),
        "spread_levels": np.arange(0, 22, 2), "spread_cmap": "magma",
        "convert": "F",
    },
    "wind": {
        "elem": "WIND", "level": "10 m above ground", "acc": None,
        "label": "Wind", "unit": "mph",
        "cmap": "YlOrRd",
        "levels": np.arange(5, 61, 5),
        "spread_levels": np.arange(2, 27, 2), "spread_cmap": "magma",
        "convert": "MPH",
    },
    "qpf": {
        "elem": "APCP", "level": "surface", "acc": 6,
        "label": "6-hour QPF", "unit": "in",
        "cmap": "PuBuGn",
        "levels": (0.0, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0),
        "spread_levels": (0.0, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0),
        "spread_cmap": "PuBu",
        "convert": "IN",
    },
}


def _qmd_url(cycle, fh):
    return QMD_BASE.format(ymd=f"{cycle:%Y%m%d}", hh=f"{cycle:%H}", fh=fh)


def _idx_ok(c):
    """One polite idx probe; True when this cycle's QMD f012 is live."""
    try:
        r = _SESSION.get(_qmd_url(c, FH) + ".idx", headers=UA, timeout=15)
        return r.ok and "level" in r.text
    except requests.RequestException:
        return False
    finally:
        time.sleep(_PROBE_SLEEP)


def find_qmd_cycle():
    """Most recent cycle whose QMD f012 .idx is live.

    QMD publishes reliably on the 6-hourly synoptic stamps (00/06/12/18Z)
    and lags the core files by hours. The steady state costs 1-2 requests:
    probe newer synoptic stamps past the remembered cycle, then the
    remembered one itself; only fall back to a slow full walk when that
    state is cold. (A naive hourly walk bursts requests at NOMADS and
    earns a temporary rate-limit ban - observed 2026-09-27: after ~40
    rapid probes even the LIVE cycle 403'd for minutes.)
    """
    now = dt.datetime.now(dt.timezone.utc)
    last = _LAST_CYCLE["c"]
    if last is not None:
        for k in range(6, 31, 6):          # newer synoptic stamps first
            c = last + dt.timedelta(hours=k)
            if c >= now:
                break
            if _idx_ok(c):
                _LAST_CYCLE["c"] = c
                return c
        if _idx_ok(last):                   # old faithful still live
            return last
    # cold state: synoptic-first walk. back=1..48 builds each list
    # newest->oldest already, so no reversal (reversing probes the oldest
    # stamps first and burns a dozen requests before reaching the live one).
    syn, other = [], []
    for back in range(1, 49):
        c = (now - dt.timedelta(hours=back)).replace(
            minute=0, second=0, microsecond=0)
        (syn if c.hour % 6 == 0 else other).append(c)
    for c in syn + other:
        if _idx_ok(c):
            _LAST_CYCLE["c"] = c
            return c
    return None


def _idx_lines(cycle, fh):
    r = _SESSION.get(_qmd_url(cycle, fh) + ".idx", headers=UA, timeout=30)
    r.raise_for_status()
    return r.text.splitlines()


def _pct_ranges(idx_lines, spec, fh):
    """idx -> {pct: (start, end)} byte ranges for one element's percentile msgs.

    TMP/DPT/etc are instantaneous at the forecast hour; APCP uses the
    6-hour accumulation window ending at fh (only present when fh % 6 == 0).
    """
    # APCP range labels use absolute hours ('6-12 hour acc fcst')
    want = f"{fh - spec['acc']}-{fh} hour acc fcst" if spec["acc"] else None
    out = {}
    for i, l in enumerate(idx_lines):
        f = l.split(":")
        if len(f) < 7 or f[3] != spec["elem"]:
            continue
        rest = ":".join(f[4:])
        if spec["level"] not in rest:
            continue
        if spec["acc"] and want not in rest:
            continue
        m = re.search(r"(\d{1,3})% level", f[6])
        if not m:
            continue
        pct = int(m.group(1))
        if pct not in PERCENTILES or pct in out:
            continue
        start = int(f[1])
        end = start
        for j in range(i + 1, len(idx_lines)):
            nxt = int(idx_lines[j].split(":")[1])
            if nxt > start:
                end = nxt
                break
        out[pct] = (start, end if end > start else start + 4_000_000)
    return out


def _fetch_fields(cycle, fh, spec):
    """{pct: 2-D array} for one element, plus lat/lon (None when unavailable)."""
    try:
        lines = _idx_lines(cycle, fh)
        ranges = _pct_ranges(lines, spec, fh)
        if not ranges:
            return None
        url = _qmd_url(cycle, fh)
        fields = {}
        lat = lon = None
        for pct, (s, e) in sorted(ranges.items()):
            blob = _fetch_range(url, s, e)
            if blob is None:
                continue
            decoded, la, lo = _decode_grib_bytes(blob)
            if not decoded or la is None:
                continue
            vals = np.asarray(next(iter(decoded.values())), dtype=float)
            if vals.ndim != 2:
                continue
            if spec["convert"] == "F":
                vals = vals * 9.0 / 5.0 + 32.0
            elif spec["convert"] == "MPH":
                vals = vals * 2.23694
            elif spec["convert"] == "IN":
                vals = vals * 0.0393700787
            fields[pct] = vals
            lat, lon = la, lo
        return (fields, lat, lon) if fields and lat is not None else None
    except Exception:  # noqa: BLE001 - network/decode failures -> caller skips
        return None


def _render(vals, lat, lon, spec, title, cmap, levels, out_path):
    """Render one map in a killable child; raise if it wedges or fails.

    The matplotlib/cartopy work runs in data/_nbm_render_worker.py under
    subprocess.run(timeout=RENDER_TIMEOUT); a wedged render is killed and
    surfaces here, where _render_all skips the map and keeps the rest of
    the set. Replaces the in-process render that repeatedly hung the whole
    updater (watchdog kills at 45 min, publishes skipped).
    """
    job = (vals, lat, lon, spec, title, cmap, levels)
    fd, job_path = tempfile.mkstemp(prefix="nbmjob_", suffix=".pickle")
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(job, f)
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "_nbm_render_worker.py")
        try:
            subprocess.run(
                [sys.executable, worker, job_path, out_path],
                timeout=RENDER_TIMEOUT, check=True)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"nbm render child exceeded {RENDER_TIMEOUT}s: {out_path}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"nbm render child failed (exit {exc.returncode}): {out_path}")
    finally:
        try:
            os.remove(job_path)
        except OSError:
            pass
    if not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
        raise RuntimeError(f"nbm render produced no file: {out_path}")
    return out_path


def _render_all(cycle, fh, spec_key, spec, fields, lat, lon):
    """Render the 4 percentile maps + the p90-p10 spread for one element."""
    from data._tz import full
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = f"{cycle:%Y%m%d%H}"
    valid = cycle + dt.timedelta(hours=fh)
    urls = {}
    for pct in sorted(fields):
        fn = f"nbm{pct}_{spec_key}_f{fh:03d}_{stamp}.png"
        path = os.path.join(OUT_DIR, fn)
        if not (os.path.exists(path) and os.path.getsize(path) > 10_000):
            try:
                _render(fields[pct], lat, lon, spec,
                        f"NBM {spec['label']} - {pct}th percentile "
                        f"({full(valid)})", spec["cmap"], spec["levels"], path)
            except Exception as exc:                 # noqa: BLE001 - skip map, keep set
                print(f"nbm render failed for {fn}: {type(exc).__name__}: {exc}",
                      flush=True)
                continue
        urls[str(pct)] = f"../nbm_pct/{fn}"
    if 10 in fields and 90 in fields:
        spread = fields[90] - fields[10]
        spread = np.where(np.isnan(spread), np.nan, spread)
        fn = f"spread_{spec_key}_f{fh:03d}_{stamp}.png"
        path = os.path.join(OUT_DIR, fn)
        if not (os.path.exists(path) and os.path.getsize(path) > 10_000):
            try:
                _render(spread, lat, lon, spec,
                        f"NBM {spec['label']} - spread (90th-10th pct, "
                        f"{full(valid)})", spec["spread_cmap"],
                        spec["spread_levels"], path)
            except Exception as exc:                 # noqa: BLE001 - skip map, keep set
                print(f"nbm render failed for {fn}: {type(exc).__name__}: {exc}",
                      flush=True)
            else:
                urls["spread"] = f"../nbm_pct/{fn}"
    return urls


def _prune_old():
    """Delete rendered PNGs older than KEEP_HOURS (stamp in filename)."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=KEEP_HOURS)
    try:
        for fn in os.listdir(OUT_DIR):
            m = re.search(r"_(\d{10})\.png$", fn)
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


def refresh():
    """Render every element's percentile set for the newest live QMD cycle."""
    cycle = find_qmd_cycle()
    if cycle is None:
        return None, [], 0
    lines = _idx_lines(cycle, FH)
    elements = []
    rendered = 0
    deadline = time.monotonic() + REFRESH_BUDGET
    for key, spec in ELEMENTS.items():
        if time.monotonic() > deadline:
            print("nbm refresh: budget exhausted, serving partial set",
                  flush=True)
            break
        got = _fetch_fields(cycle, FH, spec)
        if not got:
            continue
        fields, lat, lon = got
        urls = _render_all(cycle, FH, key, spec, fields, lat, lon)
        rendered += len(urls)
        if urls:
            elements.append({
                "key": key,
                "label": spec["label"],
                "unit": spec["unit"],
                "urls": urls,
            })
    _prune_old()
    from data._tz import full
    meta = {
        "cycle": full(cycle),
        "fh": FH,
        "valid": full(cycle + dt.timedelta(hours=FH)),
    }
    return meta, elements, rendered


def bundle(max_age=3600):
    """Payload for the models page: rendered percentile sets + meta."""
    with CACHE_LOCK:
        now = time.time()
        if CACHE["b"] is not None and now - CACHE["t"] < max_age:
            cached = CACHE["b"]
            refs_ok = all(
                os.path.isfile(os.path.join(OUT_DIR, os.path.basename(u)))
                for e in cached.get("elements", []) for u in e.get("urls", {}).values())
            if refs_ok:
                return cached
            CACHE.update(t=0.0, b=None)
    try:
        meta, elements, n = refresh()
        if not elements:
            # rate-limit window may still be open - one patient retry
            time.sleep(45)
            meta, elements, n = refresh()
    except Exception:  # noqa: BLE001 - never break a build
        meta, elements, n = None, [], 0
    if not elements and LAST_GOOD.get("b") and time.time() - LAST_GOOD["t"] < 6 * 3600:
        # NOMADS throttle / blip: keep serving the last good render set
        return LAST_GOOD["b"]
    b = {
        "ok": bool(elements),
        "generated": _tz.stamp(dt.datetime.now(dt.timezone.utc)),
        "cycle": (meta or {}).get("cycle"),
        "valid": (meta or {}).get("valid"),
        "fh": FH,
        "elements": elements,
        "count": n,
        "source": ("NWS NBM QMD percentile distribution - NOMADS open data, "
                   "no keys"),
    }
    with CACHE_LOCK:
        CACHE.update(t=time.time(), b=b)
    if b["ok"]:
        LAST_GOOD.update(t=time.time(), b=b)
        try:
            import json
            with open(MIRROR_PATH, "w", encoding="utf-8") as f:
                json.dump(b, f)
        except OSError:
            pass
    return b

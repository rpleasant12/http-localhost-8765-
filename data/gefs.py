"""GEFS North America: ensemble mean + spread from NOAA's 31-member
Global Ensemble Forecast System.

Renders the newest COMPLETE 6-hourly cycle (00/06/12/18 UTC) of the 0.5-
degree pgrb2a files for North America. Every frame is TWO side-by-side
panels - the geavg ensemble MEAN (left) and the gespr ensemble std dev,
i.e. the spread of the 31 members (right) - so each product shows the
forecast AND its uncertainty in one glance:

- mslp:  mean sea-level pressure + 500 mb heights / MSLP spread
- t2m:   2 m temperature ensemble mean / spread (deg F)
- wind:  10 m wind speed ensemble mean / spread (mph)
- qpf:   6-h accumulated precipitation ensemble mean / spread (inches)

Source: noaa-gefs-pds.s3.amazonaws.com open-data mirror (no keys, no rate
limit) - gefs.{Ymd}/{HH}/atmos/pgrb2ap5/{geavg|gespr}.t{HH}z.pgrb2a.0p50
.f{FFF} (+.idx). Cycle is accepted only once the f192 idx exists, which
guarantees the whole f000-f192 run is on disk - a cycle grabbed
mid-upload renders torn frames (the RAP nowcast 14Z lesson, 2026-09-27).
Physical-range sanity gates discard any corrupted decode.

Files land in static/gefs/ as gefs_<prod>_f###_<cycle>_na.png. The bundle
is cached 3 h and carries the last-good set for up to 24 h if the mirror
is unreachable. Never raises.
"""
import datetime as dt
import os
import re
import threading
import time

import numpy as np
import requests

from data import _tz
from data.model_maps import _decode_grib_bytes, _fetch_range

UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}
OUT_DIR = os.path.join("static", "gefs")
CACHE = {"t": 0.0, "b": None}
LAST_GOOD = {"t": 0.0, "b": None}
CACHE_LOCK = threading.Lock()
_SESSION = requests.Session()

GEFS_BASE = ("https://noaa-gefs-pds.s3.amazonaws.com/"
             "gefs.{ymd}/{hh}/atmos/pgrb2ap5/geavg.t{hh}z.pgrb2a.0p50.f{fff}")

FHS = (0, 24, 48, 72, 96, 120, 144, 168, 192)     # daily steps, 8 days
FHS_BY_PROD = {"qpf": FHS[1:]}                     # f000 acc window is 0-0
KEEP_HOURS = 48

PRODUCTS = {
    "mslp": {"label": "MSLP + 500 mb heights"},
    "t2m": {"label": "Temperature (2 m)"},
    "wind": {"label": "Wind speed (10 m)"},
    "qpf": {"label": "6-h QPF"},
}

# product messages: (idx shortName, level substring)
_MSGS = {
    "mslp": [("PRMSL", "mean sea level"), ("HGT", "500 mb")],
    "t2m": [("TMP", "2 m above ground")],
    "wind": [("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")],
    "qpf": [("APCP", "acc")],
}

# idx shortName -> canonical field key (cfgrib may ship either spelling)
_KEY = {"PRMSL": "PRMSL", "GH": "HGT", "HGT": "HGT",
        "TMP": "T2M", "2T": "T2M",
        "UGRD": "U10M", "10U": "U10M",
        "VGRD": "V10M", "10V": "V10M",
        "APCP": "QPF", "TP": "QPF"}

# physical-range gates per field (a torn decode yields absurd values)
_SANITY = {
    "PRMSL": (85000.0, 108000.0),        # Pa
    "HGT": (3800.0, 6100.0),             # m at 500 mb
    "T2M": (193.0, 333.0),               # K
    "U10M": (-75.0, 75.0),               # m/s
    "V10M": (-75.0, 75.0),
    "QPF": (0.0, 400.0),                 # mm per 6 h
}


def _gefs_url(cycle, fh, stem="geavg"):
    return GEFS_BASE.format(ymd=f"{cycle:%Y%m%d}", hh=f"{cycle:%H}",
                            fff=f"{fh:03d}").replace("geavg", stem)


def _get(url, timeout=30):
    r = _SESSION.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def find_gefs_cycle():
    """Newest 6-hourly cycle complete through f192 (probes 3 slots back).

    The f192 idx is written only after the f192 file itself, so its
    existence means every earlier lead time is on disk and stable.
    """
    now = dt.datetime.now(dt.timezone.utc)
    for back in range(0, 4):
        c = now - dt.timedelta(hours=6 * back)
        c = c.replace(minute=0, second=0, microsecond=0)
        c = c.replace(hour=(c.hour // 6) * 6)   # snap to 00/06/12/18 grid
        try:
            r = _get(_gefs_url(c, 192) + ".idx", timeout=15)
            if r.ok and "PRMSL" in r.text:
                return c
        except requests.RequestException:
            pass
        time.sleep(1.0)
    return None


def _msg_range(idx_lines, short, level_sub):
    """(start, end) byte range of the FIRST matching message, or None."""
    for i, l in enumerate(idx_lines):
        f = l.split(":")
        if len(f) < 7 or f[3] != short:
            continue
        if level_sub.lower() not in ":".join(f[4:]).lower():
            continue
        start = int(f[1])
        end = start
        for j in range(i + 1, len(idx_lines)):
            nxt = int(idx_lines[j].split(":")[1])
            if nxt > start:
                end = nxt
                break
        return (start, end if end > start else start + 4_000_000)
    return None


# spread (std dev) fields are small deviations - the mean gates above
# would reject a perfectly valid 2 hPa MSLP spread, so use their own
_SANITY_SPR = {
    "PRMSL": (0.0, 8000.0),              # Pa
    "HGT": (0.0, 1500.0),                # m
    "T2M": (0.0, 25.0),                  # K
    "U10M": (0.0, 60.0),                 # m/s
    "V10M": (0.0, 60.0),
    "QPF": (0.0, 200.0),                 # mm per 6 h
}


def _fields_sane(fields, table=None):
    for k, (lo, hi) in (table or _SANITY).items():
        if k not in fields:
            continue
        v = np.asarray(fields[k], dtype=float)
        fin = v[np.isfinite(v)]
        if fin.size and (fin.min() < lo or fin.max() > hi):
            return False
    return True


def _fetch_product(cycle, fh, prod, spread=False):
    """{FIELD: 2-D array} + lat/lon for one product/hour (None when down).

    spread=True reads the gespr (ensemble std dev) file instead of geavg.
    """
    try:
        base = _gefs_url(cycle, fh, "gespr" if spread else "geavg")
        idx = _get(base + ".idx").text.splitlines()
        url = base
        fields = {}
        lat = lon = None
        for short, level in _MSGS[prod]:
            rng = _msg_range(idx, short, level)
            if not rng:
                continue
            blob = _fetch_range(url, rng[0], rng[1])
            if blob is None:
                continue
            decoded, la, lo = _decode_grib_bytes(blob)
            if not decoded or la is None:
                continue
            for sn, vals in decoded.items():
                v = np.asarray(vals, dtype=float)
                if v.ndim != 2:
                    continue
                fields[_KEY.get(sn, sn)] = v
                lat, lon = la, lo
        if fields and not _fields_sane(fields,
                                       _SANITY_SPR if spread else _SANITY):
            return None
        return (fields, lat, lon) if fields and lat is not None else None
    except Exception:  # noqa: BLE001 - network/decode -> caller skips
        return None


def _na_crop(lat, lon, *arrays):
    """Crop global 0.5-deg fields to North America (10-75 N, 170-50 W).

    Returns (*cropped_arrays, cropped_lat, cropped_lon).
    """
    lonc = np.where(lon > 180, lon - 360.0, lon)
    rows = (lat[:, 0] >= 10) & (lat[:, 0] <= 75)
    cols = (lonc[0, :] >= -170) & (lonc[0, :] <= -50)
    latc, lonc2 = lat[np.ix_(rows, cols)], lon[np.ix_(rows, cols)]
    out = [a[np.ix_(rows, cols)] if a is not None else None for a in arrays]
    out.extend([latc, lonc2])
    return out


def _render(mean, spread, lat, lon, prod, cycle, fh, out_path):
    """Two side-by-side panels: ensemble mean (left) | std dev (right)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from metpy.plots.ctables import registry
    from data._tz import full

    if "NWSQPF" not in matplotlib.colormaps:
        matplotlib.colormaps.register(registry.get_colortable("precipitation"),
                                      name="NWSQPF")

    valid = cycle + dt.timedelta(hours=fh)
    fig = plt.figure(figsize=(14.5, 5.6), dpi=90)
    proj = ccrs.LambertConformal(central_longitude=-100, central_latitude=42)
    ext = [-170, -50, 10, 72]

    def _panel(idx):
        ax = fig.add_subplot(1, 2, idx, projection=proj)
        ax.set_extent(ext, crs=ccrs.PlateCarree())
        ax.coastlines("50m", linewidth=0.5)
        ax.add_feature(cfeature.STATES, linewidth=0.35, edgecolor="gray")
        ax.add_feature(cfeature.BORDERS, linewidth=0.5, edgecolor="dimgray")
        return ax

    # ---- panel value transforms: mean + spread in display units ----
    if prod == "mslp":
        pa, ha, la, lo = _na_crop(lat, lon, mean["PRMSL"], mean.get("HGT"))
        sa, _, _, _ = _na_crop(lat, lon, spread["PRMSL"], None)
        mval, sval = pa / 100.0, sa / 100.0
        mlev, slev = np.arange(960, 1051, 2), np.arange(0.5, 5.01, 0.5)
        mcmap, mlbl, slbl = "RdYlBu_r", "MSLP (hPa)", "MSLP spread (hPa)"
    elif prod == "t2m":
        ta, _, la, lo = _na_crop(lat, lon, mean["T2M"], None)
        sa, _, _, _ = _na_crop(lat, lon, spread["T2M"], None)
        mval, sval = ta * 1.8 - 459.67, sa * 1.8
        mlev, slev = np.arange(-20, 116, 3), np.arange(0.5, 6.01, 0.5)
        mcmap, mlbl, slbl = "turbo", "\u00b0F", "\u00b0F"
    elif prod == "wind":
        ua, va, la, lo = _na_crop(lat, lon, mean["U10M"], mean["V10M"])
        mval = np.hypot(ua, va) * 2.23694
        sua, sva, _, _ = _na_crop(lat, lon, spread["U10M"], spread["V10M"])
        sval = np.hypot(sua, sva) * 2.23694
        mlev, slev = np.arange(5, 66, 5), np.arange(1, 12.1, 1)
        mcmap, mlbl, slbl = "YlGnBu", "mph", "mph"
    else:  # qpf
        va, _, la, lo = _na_crop(lat, lon, mean["QPF"], None)
        sa, _, _, _ = _na_crop(lat, lon, spread["QPF"], None)
        mval = np.where(va / 25.4 >= 0.02, va / 25.4, np.nan)
        sval = sa / 25.4
        mlev, slev = np.arange(0.05, 1.31, 0.05), np.arange(0.05, 0.81, 0.05)
        mcmap, mlbl, slbl = "NWSQPF", "in / 6 h", "in / 6 h"

    # ---- left: ensemble mean ----
    ax = _panel(1)
    cf = ax.contourf(lo, la, mval, levels=mlev, cmap=mcmap,
                     transform=ccrs.PlateCarree(), alpha=0.85,
                     extend="max")
    plt.colorbar(cf, ax=ax, shrink=0.8, label=mlbl)
    if prod == "mslp" and mean.get("HGT") is not None:
        cs = ax.contour(lo, la, ha / 10.0, levels=np.arange(480, 601, 6),
                        colors="k", linewidths=0.6,
                        transform=ccrs.PlateCarree())
        ax.clabel(cs, fmt="%.0f", fontsize=6)
    ax.set_title("Ensemble mean", fontsize=11)

    # ---- right: ensemble spread (std dev) ----
    ax = _panel(2)
    cf = ax.contourf(lo, la, sval, levels=slev, cmap="cividis_r",
                     transform=ccrs.PlateCarree(), alpha=0.9,
                     extend="max")
    plt.colorbar(cf, ax=ax, shrink=0.8, label=f"std dev ({slbl})")
    ax.set_title("Ensemble spread (std dev)", fontsize=11)

    fig.suptitle(f"GEFS {PRODUCTS[prod]['label']} - valid {full(valid)} - "
                 f"mean | member disagreement", fontsize=11, y=0.98)
    tmp = out_path.replace(".png", ".tmp.png")
    fig.savefig(tmp, bbox_inches="tight")
    plt.close(fig)
    os.replace(tmp, out_path)
    return out_path


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


def refresh():
    """Render mean|spread panels for every product off the newest cycle."""
    cycle = find_gefs_cycle()
    if cycle is None:
        return None, [], 0
    from data._tz import full
    os.makedirs(OUT_DIR, exist_ok=True)
    items = []
    rendered = 0
    for prod, spec in PRODUCTS.items():
        frames = []
        for fh in FHS_BY_PROD.get(prod, FHS):
            got = _fetch_product(cycle, fh, prod)
            if not got:
                continue
            fields, lat, lon = got
            spr = _fetch_product(cycle, fh, prod, spread=True)
            if not spr:
                continue                     # need both panels to draw a frame
            sfields, _, _ = spr
            fn = f"gefs_{prod}_f{fh:03d}_{cycle:%Y%m%d%H}_na.png"
            path = os.path.join(OUT_DIR, fn)
            if not (os.path.exists(path) and os.path.getsize(path) > 10_000):
                try:
                    _render(fields, sfields, lat, lon, prod, cycle, fh, path)
                except Exception:  # noqa: BLE001 - one bad frame never kills
                    continue
            frames.append({
                "fh": fh,
                "url": f"../gefs/{fn}",
                "label": f"F{fh:03d} \u00b7 {full(cycle + dt.timedelta(hours=fh))}",
            })
            rendered += 1
        if frames:
            items.append({"key": prod, "label": spec["label"],
                          "cycle": _tz.stamp(cycle), "frames": frames})
    _prune_old()
    return cycle, items, rendered


def bundle(max_age=3 * 3600):
    """Payload for the models page: newest-cycle mean|spread frames."""
    with CACHE_LOCK:
        now = time.time()
        if CACHE["b"] is not None and now - CACHE["t"] < max_age:
            cached = CACHE["b"]
            refs_ok = all(os.path.isfile(os.path.join(OUT_DIR, os.path.basename(f["url"])))
                          for it in cached.get("items", []) for f in it.get("frames", []))
            if refs_ok:
                return cached
            CACHE.update(t=0.0, b=None)
    try:
        cycle, items, n = refresh()
    except Exception:  # noqa: BLE001 - never break a build
        cycle, items, n = None, [], 0
    if not items and LAST_GOOD.get("b") and time.time() - LAST_GOOD["t"] < 24 * 3600:
        return LAST_GOOD["b"]          # mirror down - ride the last set
    b = {
        "ok": bool(items),
        "generated": _tz.stamp(dt.datetime.now(dt.timezone.utc)),
        "cycle": _tz.stamp(cycle) if cycle else None,
        "items": items,
        "count": n,
        "source": "NOAA Global Ensemble Forecast System (GEFS) 31-member "
                  "ensemble mean + spread (std dev), 0.5 deg - AWS open data, "
                  "no keys",
    }
    with CACHE_LOCK:
        CACHE.update(t=time.time(), b=b)
    if b["ok"]:
        LAST_GOOD.update(t=time.time(), b=b)
    return b

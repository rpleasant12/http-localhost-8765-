"""RAP nowcast: the newest hourly Rapid Refresh cycle rendered immediately.

RAP updates EVERY HOUR with ~45-60 min latency - faster than HRRR's big
files - making it the ideal "what happens in the next 3 hours" guidance.
This module renders the newest cycle's first four hours (f000-f003) for
East Tennessee:

- refc: simulated composite reflectivity (REFC ships in RAP's PRESSURE
  file awp130pgrbf##, not the native hybrid-level file - verified in the
  live 2026-09-27 07Z idx)
- cape_wind: SBCAPE + 10 m wind barbs (the storm-scale setup at a glance;
  taken from the pressure file too, where CAPE@surface + 10 m winds sit
  alongside REFC - one small idx fetch per product/hour)

Source: NOMADS rap.t{HH}z.awp130{pgrb|bgrb}f{FF}.grib2 under
/pub/data/nccf/com/rap/prod/rap.{Ymd}/ (13 km, CONUS, hourly f00-f18 +
3-hourly to f39). Byte-range fetches bound only the needed messages.

Files land in static/rapnow/ as rap_<prod>_f###_<cycle>_etn.png. The
bundle is cached 45 min (one RAP cycle) and carries the last-good set
for up to 6 h when NOMADS throttles. Never raises.
"""
import datetime as dt
import os
import re
import threading
import time

import numpy as np
import requests

from data import _tz
from data.model_maps import _decode_grib_bytes, _fetch_range, MAP_REGIONS

UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}
OUT_DIR = os.path.join("static", "rapnow")
CACHE = {"t": 0.0, "b": None}
LAST_GOOD = {"t": 0.0, "b": None}
CACHE_LOCK = threading.Lock()
_SESSION = requests.Session()

FHS = (0, 1, 2, 3)
KEEP_HOURS = 12

RAP_BASE = ("https://nomads.ncep.noaa.gov/pub/data/nccf/com/rap/prod/"
            "rap.{ymd}/rap.t{hh}z.{stem}.grib2")

PRODUCTS = {
    "refc": {"stem": "awp130pgrbf{fh:02d}",
             "label": "Simulated radar (composite reflectivity)"},
    "cape_wind": {"stem": "awp130pgrbf{fh:02d}", "label": "SBCAPE + 10 m wind"},
    "qpf": {"stem": "awp130pgrbf{fh:02d}", "label": "Hourly QPF"},
    "tmpdew": {"stem": "awp130pgrbf{fh:02d}", "label": "Temperature / dew point"},
}

# product messages: (shortName, level substring, hourly-accumulation filter)
_MSGS = {
    "refc": [("REFC", "entire atmosphere", None)],
    "cape_wind": [("CAPE", "surface", None), ("UGRD", "10 m above ground", None),
                  ("VGRD", "10 m above ground", None)],
    # APCP ships 0-N storm totals AND N-1-N hourly buckets (0-1, 1-2, 2-3
    # - verified in the live 2026-09-27 11Z idx); keep only the hourly
    # buckets so the loop animates rain rate, not a growing total.
    "qpf": [("APCP", "surface", True)],
    "tmpdew": [("TMP", "2 m above ground", None),
               ("DPT", "2 m above ground", None)],
}
# f01 quirk: the only 1 h bucket at f01 is "0-1 hour acc" (no "1-1"), so the
# hourly filter accepts any (end-start) == 1 window, not just N-1-N.
# RAP idx level strings carry "anl"/"N hour fcst" - CAPE@surface is the
# pressure file's name for what the native file calls CAPE@255-0 mb; both
# are the most-unstable-ish surface-based field the model ships. The plain
# "surface" match also skips the 180-0/90-0/0-3000 m variants.


def _rap_url(cycle, stem, fh):
    return RAP_BASE.format(ymd=f"{cycle:%Y%m%d}", hh=f"{cycle:%H}",
                           stem=stem.format(fh=fh))


def _get(url, timeout=30):
    r = _SESSION.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def find_rap_cycle():
    """Newest cycle whose pressure-file f00 idx is live (probes 8 h back)."""
    now = dt.datetime.now(dt.timezone.utc)
    for back in range(0, 8):
        c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0,
                                                     microsecond=0)
        try:
            r = _get(_rap_url(c, "awp130pgrbf{fh:02d}".format(fh=0), 0)
                     + ".idx", timeout=12)
            if r.ok and "REFC" in r.text:
                return c
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return None


def _msg_ranges(idx_lines, short, level_sub, fh, hourly_acc=False):
    """[(start, end)] byte ranges for all matches (RAP dedupes per file)."""
    out = []
    for i, l in enumerate(idx_lines):
        f = l.split(":")
        if len(f) < 7 or f[3] != short:
            continue
        lvl = ":".join(f[4:]).lower()
        if level_sub.lower() not in lvl:
            continue
        if hourly_acc:
            # keep only 1 h accumulation windows ("0-1", "1-2", "2-3",
            # ..." hour acc fcst") - skip the 0-N storm totals
            m = re.search(r"(\d+)-(\d+) hour acc", lvl)
            if not m or int(m.group(2)) - int(m.group(1)) != 1:
                continue
        start = int(f[1])
        end = start
        for j in range(i + 1, len(idx_lines)):
            nxt = int(idx_lines[j].split(":")[1])
            if nxt > start:
                end = nxt
                break
        out.append((start, end if end > start else start + 3_000_000))
    return out


def _fetch_product(cycle, fh, prod):
    """{'VAR': 2-D array} + lat/lon for one product/hour (None when down)."""
    spec = PRODUCTS[prod]
    try:
        stem = spec["stem"].format(fh=fh)
        idx = _get(_rap_url(cycle, stem, fh) + ".idx").text.splitlines()
        url = _rap_url(cycle, stem, fh)
        fields = {}
        lat = lon = None
        for short, level, hourly_acc in _MSGS[prod]:
            for (s, e) in _msg_ranges(idx, short, level, fh, hourly_acc):
                blob = _fetch_range(url, s, e)
                if blob is None:
                    continue
                decoded, la, lo = _decode_grib_bytes(blob)
                if not decoded or la is None:
                    continue
                for sn, vals in decoded.items():
                    v = np.asarray(vals, dtype=float)
                    if v.ndim != 2:
                        continue
                    key = {"UGRD": "WIND_U", "VGRD": "WIND_V",
                           "CAPE": "CAPE", "APCP": "QPF", "TP": "QPF",
                           "TMP": "TMP2M", "2T": "TMP2M",
                           "DPT": "DPT2M", "2D": "DPT2M"}.get(sn, sn)
                    fields[key] = v
                    lat, lon = la, lo
        return (fields, lat, lon) if fields and lat is not None else None
    except Exception:  # noqa: BLE001 - network/decode -> caller skips
        return None


def _render(fields, lat, lon, prod, cycle, fh, out_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from metpy.plots.ctables import registry
    from data._tz import full

    if "NWSRef" not in matplotlib.colormaps:
        cmap_ref = registry.get_colortable("NWSReflectivity")
        matplotlib.colormaps.register(cmap_ref, name="NWSRef")
    if "NWSQPF" not in matplotlib.colormaps:
        matplotlib.colormaps.register(registry.get_colortable("precipitation"),
                                      name="NWSQPF")

    dec = 2
    la, lo = lat[::dec, ::dec], lon[::dec, ::dec]
    valid = cycle + dt.timedelta(hours=fh)
    fig = plt.figure(figsize=(9, 5.8), dpi=90)
    proj = ccrs.LambertConformal(central_longitude=-85, central_latitude=36)
    ax = fig.add_subplot(1, 1, 1, projection=proj)
    ax.set_extent(MAP_REGIONS["etn"]["extent"], crs=ccrs.PlateCarree())
    ax.coastlines("50m", linewidth=0.5)
    ax.add_feature(cfeature.STATES, linewidth=0.4, edgecolor="gray")

    if prod == "refc":
        v = np.asarray(fields["REFC"], dtype=float)[::dec, ::dec]
        v = np.where(v < 5, np.nan, v)
        cf = ax.contourf(lo, la, v, levels=np.arange(10, 71, 5), cmap="NWSRef",
                         transform=ccrs.PlateCarree(), alpha=0.85, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.85, label="dBZ")
        title = f"RAP simulated radar - valid {full(valid)}"
    elif prod == "qpf":
        v = np.asarray(fields["QPF"], dtype=float)[::dec, ::dec]
        v = np.where(v <= 0.05, np.nan, v)      # mm - hide the trace speckle
        v = v / 25.4                            # -> inches
        cf = ax.contourf(lo, la, v, levels=np.arange(0.05, 1.31, 0.05),
                         cmap="NWSQPF", transform=ccrs.PlateCarree(),
                         alpha=0.85, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.85, label="in/hr")
        title = f"RAP hourly QPF - valid {full(valid)}"
    elif prod == "tmpdew":
        tk = np.asarray(fields["TMP2M"], dtype=float)[::dec, ::dec]
        td = np.asarray(fields["DPT2M"], dtype=float)[::dec, ::dec]
        tf, tdf = tk * 1.8 - 459.67, td * 1.8 - 459.67
        lv = np.arange(30, 106, 3)
        cf = ax.contourf(lo, la, tf, levels=lv, cmap="turbo",
                         transform=ccrs.PlateCarree(), alpha=0.8)
        plt.colorbar(cf, ax=ax, shrink=0.85, label="\u00b0F")
        ax.contour(lo, la, tdf, levels=lv[::2], colors="k", linewidths=0.5,
                   linestyles="dashed", transform=ccrs.PlateCarree())
        ax.clabel(ax.contour(lo, la, tdf, levels=lv[::4], colors="k",
                             linewidths=0.7, linestyles="dashed",
                             transform=ccrs.PlateCarree()),
                  fmt="%.0f", fontsize=6)
        title = f"RAP 2 m temp (fill) / dew point (dashed) - valid {full(valid)}"
    else:
        cape = np.asarray(fields.get("CAPE", np.nan), dtype=float)[::dec, ::dec]
        cf = ax.contourf(lo, la, cape, levels=np.arange(100, 5001, 250),
                         cmap="YlOrRd", transform=ccrs.PlateCarree(),
                         alpha=0.8, extend="max")
        plt.colorbar(cf, ax=ax, shrink=0.85, label="SBCAPE (J/kg)")
        u = fields.get("WIND_U"); v = fields.get("WIND_V")
        if u is not None and v is not None:
            ax.barbs(lo[::3, ::3], la[::3, ::3],
                     np.asarray(u)[::dec, ::dec][::3, ::3] * 1.94384,
                     np.asarray(v)[::dec, ::dec][::3, ::3] * 1.94384,
                     length=5, linewidth=0.4, transform=ccrs.PlateCarree())
        title = f"RAP SBCAPE + 10 m wind - valid {full(valid)}"

    ax.set_title(title, fontsize=11)
    tmp = out_path.replace(".png", ".tmp.png")
    fig.savefig(tmp, bbox_inches="tight")
    plt.close(fig)
    os.replace(tmp, out_path)
    return out_path


def _prune_old():
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=KEEP_HOURS)
    try:
        for fn in os.listdir(OUT_DIR):
            m = re.search(r"_(\d{10})_etn\.png$", fn)
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
    """Render f000-f003 for every product off the newest live RAP cycle."""
    cycle = find_rap_cycle()
    if cycle is None:
        return None, [], 0
    from data._tz import full
    os.makedirs(OUT_DIR, exist_ok=True)
    items = []
    rendered = 0
    for prod, spec in PRODUCTS.items():
        frames = []
        for fh in FHS:
            got = _fetch_product(cycle, fh, prod)
            if not got:
                continue
            fields, lat, lon = got
            fn = f"rap_{prod}_f{fh:03d}_{cycle:%Y%m%d%H}_etn.png"
            path = os.path.join(OUT_DIR, fn)
            if not (os.path.exists(path) and os.path.getsize(path) > 10_000):
                try:
                    _render(fields, lat, lon, prod, cycle, fh, path)
                except Exception:  # noqa: BLE001 - one bad frame never kills the run
                    continue
            frames.append({
                "fh": fh,
                "url": f"../rapnow/{fn}",
                "label": f"F{fh:03d} \u00b7 {full(cycle + dt.timedelta(hours=fh))}",
            })
            rendered += 1
        if frames:
            items.append({"key": prod, "label": spec["label"],
                          "cycle": _tz.stamp(cycle), "frames": frames})
    _prune_old()
    return cycle, items, rendered


def bundle(max_age=45 * 60):
    """Payload for the hrrr page: newest-cycle nowcast frames."""
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
    if not items and LAST_GOOD.get("b") and time.time() - LAST_GOOD["t"] < 6 * 3600:
        return LAST_GOOD["b"]          # NOMADS throttle - ride the last set
    b = {
        "ok": bool(items),
        "generated": _tz.stamp(dt.datetime.now(dt.timezone.utc)),
        "cycle": _tz.stamp(cycle) if cycle else None,
        "items": items,
        "count": n,
        "source": "NOAA Rapid Refresh (RAP) 13 km - NOMADS open data, no keys",
    }
    with CACHE_LOCK:
        CACHE.update(t=time.time(), b=b)
    if b["ok"]:
        LAST_GOOD.update(t=time.time(), b=b)
    return b

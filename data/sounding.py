"""Skew-T soundings decoded from RAP 13 km pressure levels (no key).

Files: noaa-rap-pds rap.t{HH}z.awip32f{FF}.grib2 (277x349 CONUS grid). RAP
ships ONE GRIB message per (variable, isobaric level), so a sounding is
~100 small byte-range fetches (only the plot levels), decoded with cfgrib
and rendered with MetPy's SkewT. Dewpoint aloft is derived from T + RH
(RAP has no DPT on isobaric levels); the parcel starts from the 2 m fields.
"""
import datetime as dt
import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
SOUNDING_DIR = os.path.join("static", "soundings")
MAX_HOURS = 18

# isobaric levels (hPa) fetched for the plot (subset of RAP's 39)
LEVELS = [1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500,
          450, 400, 350, 300, 250, 200, 150, 100]

_ISO_VARS = ["HGT", "TMP", "RH", "UGRD", "VGRD"]
_ISO_RE = re.compile(r"^(\d+) mb$")
_CANON = {"T": "TMP", "GH": "HGT", "U": "UGRD", "V": "VGRD", "R": "RH"}


def _cycle(forecast_hour):
    """(cycle_dt, file_fh) for the newest data covering forecast_hour."""
    now = dt.datetime.now(dt.timezone.utc)
    for back in (1, 2, 3):
        fh = forecast_hour + back
        if fh <= MAX_HOURS:
            c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
            return c, fh
    c = (now - dt.timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    return c, MAX_HOURS


def _file_url(cycle, fh):
    return f"https://noaa-rap-pds.s3.amazonaws.com/rap.{cycle:%Y%m%d}/rap.t{cycle:%H}z.awip32f{fh:02d}.grib2"


def _entry_end(lines, i, start):
    for j in range(i + 1, len(lines)):
        nxt = int(lines[j].split(":")[1])
        if nxt > start:
            return nxt
    return start + 500_000


def _iso_entries(idx_text, short):
    """[(level_hPa, (start, end))] for one variable's isobaric messages."""
    out = []
    lines = idx_text.splitlines()
    for i, line in enumerate(lines):
        f = line.split(":")
        if len(f) > 4 and f[3] == short:
            m = _ISO_RE.match(f[4])
            if m and int(m.group(1)) in LEVELS:
                start = int(f[1])
                out.append((int(m.group(1)), (start, _entry_end(lines, i, start))))
    return out


def _sfc_entries(idx_text):
    """2 m T/Td, 10 m wind and orography ranges (RAP packs 10u/10v together)."""
    def one(short, level_sub):
        lines = idx_text.splitlines()
        for i, line in enumerate(lines):
            f = line.split(":")
            if len(f) > 4 and f[3] == short and level_sub in f[4]:
                start = int(f[1])
                return start, _entry_end(lines, i, start)
        return None
    return {
        "2T": one("TMP", "2 m above ground"),
        "2D": one("DPT", "2 m above ground"),
        "10W": one("UGRD", "10 m above ground"),
        "OROG": one("HGT", "surface"),
    }


def _fetch_range(url, start, end):
    r = requests.get(url, headers={**UA, "Range": f"bytes={start}-{end - 1}"}, timeout=60)
    r.raise_for_status()
    return r.content


def _decode_one(blob, kind):
    """Decode a small GRIB blob -> {key: (values, level_or_None)} via cfgrib."""
    import tempfile
    import xarray as xr

    tmp = os.path.join(tempfile.gettempdir(), f"tnwx_snd_{abs(hash(blob[:64])) % 99999}.grib2")
    try:
        with open(tmp, "wb") as f:
            f.write(blob)
        backend = {"indexpath": ""}
        if kind == "iso":
            backend["filter_by_keys"] = {"typeOfLevel": "isobaricInhPa"}
        elif kind == "hag":
            backend["filter_by_keys"] = {"typeOfLevel": "heightAboveGround"}
        elif kind == "sfc":
            backend["filter_by_keys"] = {"typeOfLevel": "surface"}
        ds = xr.open_dataset(tmp, engine="cfgrib", backend_kwargs=backend)
        out = {}
        for var in ds.data_vars:
            sn = str(ds[var].attrs.get("GRIB_shortName", var)).upper()
            sn = _CANON.get(sn, sn)
            arr = np.asarray(ds[var].values, dtype=float)
            arr = np.squeeze(arr)
            lev = None
            if kind == "iso" and "isobaricInhPa" in ds[var].coords:
                lev = float(np.asarray(ds[var]["isobaricInhPa"].values).reshape(-1)[0])
            out[sn] = (arr, lev)
        g_lat = np.asarray(ds["latitude"].values, dtype=float)
        g_lon = np.asarray(ds["longitude"].values, dtype=float)
        del ds
        return out, g_lat, g_lon
    except Exception:  # noqa: BLE001 - a bad message must not kill the sounding
        return {}, None, None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _dewpoint_c(t_c, rh_pct):
    """Magnus-formula dewpoint (degC) from temperature (degC) and RH (%)."""
    rh = np.clip(np.asarray(rh_pct, dtype=float), 1.0, 100.0) / 100.0
    a, b = 17.625, 243.04
    gamma = np.log(rh) + a * np.asarray(t_c, dtype=float) / (b + np.asarray(t_c, dtype=float))
    return b * gamma / (a - gamma)


def _sounding_cache_path(cycle, fh, lat, lon):
    h = hashlib.md5(f"{cycle:%Y%m%d%H}_{fh}_{lat:.3f}_{lon:.3f}".encode()).hexdigest()[:12]
    return os.path.join(SOUNDING_DIR, f"rap_{h}.png")


def _render_skewt(prof, path, place=""):
    """Render the MetPy Skew-T; returns meta dict for the caption."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from metpy.calc import lcl, parcel_profile, surface_based_cape_cin
    from metpy.plots import SkewT
    from metpy.units import units

    p = np.asarray(prof["p"], dtype=float) * units.hPa
    T = np.asarray(prof["T"], dtype=float) * units.degC
    Td = np.asarray(prof["Td"], dtype=float) * units.degC
    u = np.asarray(prof["u"], dtype=float) * units("m/s")
    v = np.asarray(prof["v"], dtype=float) * units("m/s")

    fig = plt.figure(figsize=(9, 9), dpi=110)
    skew = SkewT(fig, rotation=45)
    skew.plot(p, T, "r", linewidth=2, label="Temperature")
    skew.plot(p, Td, "g", linewidth=2, label="Dewpoint")
    skew.plot_barbs(p[::2], u[::2], v[::2], length=6)
    try:
        sbcape, sbcin = surface_based_cape_cin(p, T, Td)
    except Exception:  # noqa: BLE001
        sbcape, sbcin = np.nan * units("J/kg"), np.nan * units("J/kg")
    try:
        lclp, lclt = lcl(p[0], T[0], Td[0])
        skew.plot(lclp, lclt, "ko", markerfacecolor="black", markersize=6)
        prof_p = parcel_profile(p, T[0], Td[0]).to("degC")
        skew.shade_cape(p, T, prof_p)
        skew.shade_cin(p, T, prof_p, Td)
        skew.plot(p, prof_p, "k", linewidth=1.5, linestyle="--", alpha=0.7)
    except Exception:  # noqa: BLE001
        pass
    skew.plot_dry_adiabats(alpha=0.25)
    skew.plot_moist_adiabats(alpha=0.25)
    skew.plot_mixing_lines(alpha=0.25)
    ax = skew.ax
    ax.set_ylim(1050, 100)
    ax.set_xlim(-45, 45)
    ax.set_xlabel("Temperature (°C)")
    ax.set_ylabel("Pressure (hPa)")
    ax.set_title(f"RAP Skew-T · {place} · {prof['valid']:%Y-%m-%d %H:%M UTC}".replace("·", "-"),
                 fontsize=11)
    cape_v = getattr(sbcape, "magnitude", sbcape)
    cin_v = getattr(sbcin, "magnitude", sbcin)
    ax.text(0.02, 0.02, f"SBCAPE {cape_v:.0f} J/kg   SBCIN {cin_v:.0f} J/kg",
            transform=ax.transAxes, fontsize=10,
            bbox=dict(boxstyle="round", fc="white", ec="#888", alpha=0.85))
    ax.legend(loc="upper right", fontsize=9)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return {"cape": float(cape_v) if np.isfinite(cape_v) else None,
            "cin": float(cin_v) if np.isfinite(cin_v) else None}


def build_sounding(lat, lon, fh=0, place=""):
    """Fetch, decode, render. Returns {'png','meta','error'} (png = static path)."""
    cycle, fh_eff = _cycle(fh)
    png = _sounding_cache_path(cycle, fh_eff, lat, lon)
    meta = {"cycle": cycle.strftime("%Y-%m-%d %H:%M UTC"), "fh": fh_eff,
            "valid": (cycle + dt.timedelta(hours=fh_eff)).strftime("%Y-%m-%d %H:%M UTC")}
    if os.path.exists(png) and os.path.getsize(png) > 10_000:
        return {"png": png, "meta": meta}
    try:
        url = _file_url(cycle, fh_eff)
        idx = requests.get(url + ".idx", headers=UA, timeout=20)
        idx.raise_for_status()

        # task list: (short_or_key, kind, level_or_None, (start, end))
        tasks = []
        for short in _ISO_VARS:
            for lev, rng in _iso_entries(idx.text, short):
                tasks.append((short, "iso", float(lev), rng))
        sfc = _sfc_entries(idx.text)
        for key, rng in sfc.items():
            if rng:
                tasks.append((key, "sfc" if key == "OROG" else "hag", None, rng))

        url_base = url
        def work(t):
            short, kind, lev, (s, e) = t
            try:
                return (short, kind, lev, _fetch_range(url_base, s, e))
            except requests.RequestException:
                return (short, kind, lev, None)

        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(work, tasks))

        iso = {v: {} for v in _ISO_VARS}      # short -> {level: values}
        sfc_fields = {}                        # key -> values
        glat = glon = None
        for short, kind, lev, blob in results:
            if blob is None:
                continue
            fields, la, lo = _decode_one(blob, kind)
            if la is not None and (glat is None or la.size > glat.size):
                glat, glon = la, lo
            for key, (vals, lv) in fields.items():
                if kind == "iso":
                    iso.setdefault(short, {})[lv if lv is not None else lev] = vals
                else:
                    sfc_fields[key] = vals
        if glat is None:
            return {"png": None, "meta": meta, "error": "RAP decode failed"}
        i = int(np.argmin(np.abs(((glon - lon + 180) % 360) - 180) + np.abs(glat - lat)))

        def iso_at(short, lev):
            vals = iso.get(short, {}).get(lev)
            if vals is None:
                # nearest available level within 25 hPa
                cand = sorted(iso.get(short, {}))
                if not cand:
                    return np.nan
                near = min(cand, key=lambda L: abs(L - lev))
                if abs(near - lev) > 25:
                    return np.nan
                vals = iso[short][near]
            return float(np.asarray(vals).reshape(-1)[i])

        prof = {"p": [], "T": [], "Td": [], "u": [], "v": []}
        for lev in LEVELS:
            t_v = iso_at("TMP", lev)
            rh_v = iso_at("RH", lev)
            u_v = iso_at("UGRD", lev)
            v_v = iso_at("VGRD", lev)
            if not all(np.isfinite(x) for x in (t_v, u_v, v_v)):
                continue
            t_c = t_v - 273.15
            if np.isfinite(rh_v):
                td_c = float(_dewpoint_c(t_c, rh_v * 100.0 if rh_v <= 1.5 else rh_v))
            else:
                td_c = t_c - 15.0
            prof["p"].append(lev)
            prof["T"].append(t_c)
            prof["Td"].append(td_c)
            prof["u"].append(u_v)
            prof["v"].append(v_v)
        # parcel starts from 2 m T/Td when available (true surface parcel)
        if "2T" in sfc_fields and "2D" in sfc_fields:
            t2 = float(np.asarray(sfc_fields["2T"]).reshape(-1)[i]) - 273.15
            d2 = float(np.asarray(sfc_fields["2D"]).reshape(-1)[i]) - 273.15
            if np.isfinite(t2) and np.isfinite(d2) and d2 > -60:
                prof["T"][0] = t2
                prof["Td"][0] = d2
        if len(prof["p"]) < 10:
            return {"png": None, "meta": meta, "error": "Profile too thin to plot"}
        prof["valid"] = (cycle + dt.timedelta(hours=fh_eff))
        meta2 = _render_skewt(prof, png, place)
        meta.update(meta2)
        return {"png": png, "meta": meta}
    except requests.RequestException as exc:
        return {"png": None, "meta": meta, "error": f"RAP fetch failed: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"png": None, "meta": meta, "error": f"Sounding failed: {exc}"}

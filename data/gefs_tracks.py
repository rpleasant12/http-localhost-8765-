"""GEFS tropical-storm tracks: 32 member spaghetti for Atlantic storms.

For each active Atlantic/NHC storm (seeded from NHC CurrentStorms.json),
every GEFS member (control gec00 + 31 perturbed gep01..gep31) is tracked
through the lead files by its 850 mb ABSOLUTE VORTICITY maximum - the
standard cyclone-center proxy: a tropical vortex is a tight bullseye of
cyclonic absolute vorticity that stands out even when the storm is a
minimal depression (sea-level pressure on the smooth 0.5-deg ensemble
grid is nearly blind to a 30-kt TD - TD Fay 2026-09-27: ~3 hPa anomaly,
while her 850 vorticity bullseye ran 6.1e-4/s vs 0.8e-4 background).
At each lead the vorticity max within NEIGHBORHOOD_DEG of the previous
position extends the track; a member's track requires at least
MIN_FIXES fixes with the max staying above VORT_MIN - dissipated
members honestly draw nothing.

The ensemble MEAN track (heavy line) averages the members' positions at
each lead; thin colored lines are the individual members.

Fetch economics: one idx + one ~230 KB ABSV@850 range per member/lead
(ABSV ships once per file, mid-file at 850 mb). Source:
noaa-gefs-pds.s3.amazonaws.com open-data mirror, no keys.

Files land in static/gefs/ as gefs_trk_<stormid>_<cycle>_na.png. The
bundle is cached 3 h and carries the last-good set for 24 h. Never
raises. No Atlantic storms -> ok:True with zero cards (honest empty
state, not an outage).
"""
import datetime as dt
import hashlib
import json
import math
import os
import re
import threading
import time

import numpy as np
import requests

from data import _tz

UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}
OUT_DIR = os.path.join("static", "gefs")
CACHE = {"t": 0.0, "b": None}
LAST_GOOD = {"t": 0.0, "b": None}
CACHE_LOCK = threading.Lock()
_SESSION = requests.Session()

GEFS_BASE = ("https://noaa-gefs-pds.s3.amazonaws.com/"
             "gefs.{ymd}/{hh}/atmos/pgrb2ap5/{stem}.t{hh}z.pgrb2a.0p50.f{fff}")

# the tracker's own lead set: 6-hourly to f060, 12-hourly to f240
# 32 members x this lead list x (idx + ~230 KB range) per fetch - every
# trimmed lead saves 32 requests, so: 3-hourly while the vortex is strong
# (f000-f060), then 12-hourly out to f168 (day 7)
TRACK_FHS = [0, 6, 12, 18, 24, 30, 36, 42, 48, 54, 60,
             72, 84, 96, 108, 120, 144, 168]
# members whose files are probed to detect the newest complete cycle
SEED_MEMBERS = ("gec00", "gep01", "gep02")
TRACK_LEADS_MAX = 168
MEMBERS = ["gec00"] + [f"gep{i:02d}" for i in range(1, 32)]
MEMBER_COLORS = ["#e53935", "#1e88e5", "#43a047", "#fb8c00", "#8e24aa",
                 "#00acc1", "#f4511e", "#3949ab", "#7cb342", "#d81b60",
                 "#5e35b1", "#00897b", "#fdd835", "#6d4c41", "#546e7a",
                 "#c0ca33", "#039be5", "#f06292", "#7e57c2", "#26a69a",
                 "#ef6c00", "#42a5f5", "#9ccc65", "#ec407a", "#5c6bc0",
                 "#ffa726", "#66bb6a", "#ab47bc", "#29b6f6", "#ff7043",
                 "#9e9d24", "#b39ddb"]

# tracker geometry (calibrated on live TD Fay 2026-09-27, 12Z cycle):
# 850 mb absolute vorticity bullseye of a 30-kt TD ~= 6e-4/s vs 0.8e-4
# background - a 7x contrast that survives even weak/degenerate systems.
BASIN_LAT = (5.0, 45.0)
BASIN_LON = (-100.0, -15.0)
NEIGHBORHOOD_DEG = 5.0      # search box around the anchor (~550 km)
VORT_MIN = 4.0e-4           # 1/s - a real vortex max, not background shear
MIN_FIXES = 3               # fewer fixes is not a track
WARN_P_DEV = 12.0           # warn if tracked min is >12 hPa off the mean
CACHE_MAX_AGE = 3 * 3600
LAST_GOOD_TTL = 24 * 3600


def _gefs_url(cycle, stem, fh):
    return GEFS_BASE.format(ymd=f"{cycle:%Y%m%d}", hh=f"{cycle:%H}",
                            stem=stem, fff=f"{fh:03d}")


# ---- grib disk cache --------------------------------------------------
# One full tracker pass is ~1.3k requests (~15 min); the site build calls
# the bundle up to twice per update cycle. Cache every fetched blob on
# disk (keyed by URL) for 30 h - all fetches for a cycle happen once,
# rebuilds walk the cache instead of the network.
CACHE_DIR = os.path.join("static", "gefs", "_gcache")
GCACHE_TTL = 30 * 3600


def _cache_path(url):
    h = hashlib.sha256(url.encode()).hexdigest()[:32]
    return os.path.join(CACHE_DIR, f"{h}.grib")


def _disk_get(url):
    p = _cache_path(url)
    try:
        if time.time() - os.path.getmtime(p) < GCACHE_TTL \
                and os.path.getsize(p) > 1000:
            with open(p, "rb") as f:
                return f.read()
    except OSError:
        pass
    return None


def _disk_put(url, blob):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _cache_path(url) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, _cache_path(url))
    except OSError:
        pass


def _cache_prune():
    """Drop grib cache files older than 30 h (their cycle is stale)."""
    try:
        now = time.time()
        for fn in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, fn)
            try:
                if now - os.path.getmtime(p) > GCACHE_TTL:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def _get(url, timeout=30):
    r = _SESSION.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


# ------------------------------------------------------------
# Circuit breaker: when NOAA's bucket is down (every request timing
# out or 404ing), 32 members x 21 leads would each burn their full
# timeout and spam the log for hours. After this many consecutive
# failures the run stops asking until the next bundle() call.
# ------------------------------------------------------------
BREAKER_AFTER = 40
_breaker_fails = 0
_breaker_open = False


def _fail():
    global _breaker_fails, _breaker_open
    _breaker_fails += 1
    if _breaker_fails >= BREAKER_AFTER and not _breaker_open:
        _breaker_open = True
        print(f"gefs_tracks: circuit breaker OPEN after "
              f"{_breaker_fails} consecutive failures - skipping the "
              f"rest of this run (resets next cycle)", flush=True)


def _ok():
    global _breaker_fails
    _breaker_fails = 0


def _file_size(url, timeout=15):
    """Total size via 1-byte range GET (mirrors data/gefs.py's probe)."""
    try:
        r = _SESSION.get(url, headers={**UA, "Range": "bytes=0-0"},
                         timeout=timeout)
        cr = r.headers.get("Content-Range") or ""
        if r.status_code in (200, 206) and "/" in cr:
            return int(cr.rsplit("/", 1)[1])
        if r.status_code == 200:
            return int(r.headers.get("Content-Length") or 0)
    except (requests.RequestException, ValueError):
        pass
    return None


def find_gefs_cycle():
    """Newest 6-hourly cycle with all seed members complete through f240."""
    now = dt.datetime.now(dt.timezone.utc)
    for back in range(0, 4):
        c = now - dt.timedelta(hours=6 * back)
        c = c.replace(hour=(c.hour // 6) * 6, minute=0, second=0,
                      microsecond=0)
        ok = True
        for stem in SEED_MEMBERS:
            n = _file_size(_gefs_url(c, stem, TRACK_LEADS_MAX))
            if not n:
                ok = False
                break
            time.sleep(0.6)
        if ok:
            return c
    return None


def _nhc_atlantic_storms():
    """[(storm_id, name, lat, lon, classification)] for Atlantic basin."""
    try:
        r = _get("https://www.nhc.noaa.gov/CurrentStorms.json", timeout=20)
        storms = r.json().get("activeStorms") or []
    except Exception:  # noqa: BLE001 - NHC hiccup -> no seeds
        return []
    out = []
    for s in storms:
        sid = str(s.get("id") or "")
        if not sid.lower().startswith("al"):
            continue                      # this wall is Atlantic-only
        try:
            lat = float(s.get("latitudeNumeric"))
            lon = float(s.get("longitudeNumeric"))
        except (TypeError, ValueError):
            continue
        out.append((sid, str(s.get("name") or sid),
                    lat, lon, str(s.get("classification") or "")))
    return out


def _absv850(cycle, member, fh):
    """(values, lat, lon) 850 mb absolute vorticity for one member/lead.

    One idx + one ~230 KB range GET (ABSV@850 ships once per b-file).
    """
    from data.model_maps import _decode_grib_bytes, _fetch_range
    global _breaker_open
    if _breaker_open:
        return None                       # this run is dead; stop asking
    try:
        base = (_gefs_url(cycle, member, fh)
                .replace("pgrb2ap5", "pgrb2bp5")
                .replace(".pgrb2a.", ".pgrb2b."))
        cached = _disk_get(base)
        if cached is not None:
            blob = cached
        else:
            idx = _get(base + ".idx", timeout=25).text.splitlines()
            rng = None
            for i, l in enumerate(idx):
                f = l.split(":")
                if len(f) > 6 and f[3] == "ABSV" and f[4].startswith("850"):
                    s = int(f[1])
                    e = s
                    for j in idx[i + 1:]:
                        try:
                            nxt = int(j.split(":")[1])
                        except (ValueError, IndexError):
                            continue
                        if nxt > s:
                            e = nxt
                            break
                    rng = (s, e if e > s else s + 400_000)
                    break
            if not rng:
                return None
            blob = _fetch_range(base, rng[0], rng[1])
            _disk_put(base, blob)
        decoded, lat, lon = _decode_grib_bytes(blob)
        if not decoded or lat is None:
            return None
        v = np.asarray(decoded["ABSV"] if "ABSV" in decoded
                       else next(iter(decoded.values())), dtype=float)
        if v.ndim != 2:
            return None
        fin = v[np.isfinite(v)]
        if not fin.size or not (-1e-2 <= fin.min() and fin.max() <= 5e-2):
            return None                   # sanity: torn/absurd decode
        _ok()
        return v, lat, lon
    except Exception as exc:  # noqa: BLE001 - one bad fetch skips
        _fail()
        if not _breaker_open:            # breaker prints its own line, once
            print(f"gefs_tracks: absv {member} f{fh:03d} failed "
                  f"({type(exc).__name__}: {exc})", flush=True)
        return None



def _local_max(v, lat, lon, c_lat, c_lon, radius=None):
    """(vort, lat, lon) of the greatest ABSV within `radius` deg of anchor."""
    r = NEIGHBORHOOD_DEG if radius is None else radius
    lonc = np.where(lon > 180, lon - 360.0, lon)
    latv, lonv = lat[:, 0], lonc[0, :]
    box = ((np.abs(latv - c_lat) <= r)[:, None]
           & (np.abs(lonv - c_lon) <= r)[None, :])
    if not box.any():
        return None
    masked = np.where(box & np.isfinite(v), v, -np.inf)
    k = np.unravel_index(np.argmax(masked), masked.shape)
    if not np.isfinite(masked[k]):
        return None
    return float(masked[k]), float(lat[k]), float(lonc[k])


def _track_member(cycle, member, seed_lat, seed_lon):
    """[(fh, lat, lon, vort)] for one member, or None.

    Follows the 850 mb vorticity maximum starting from the storm's
    current position: at each lead the max within NEIGHBORHOOD_DEG of
    the last fix (or the seed) is the candidate; above-threshold
    candidates extend the track, weak leads are skipped (the vortex may
    re-resolve later). Members whose max never exceeds VORT_MIN
    (system dissipated / never resolved) honestly draw nothing.
    """
    track = []
    anchor_lat, anchor_lon = seed_lat, seed_lon
    for fh in TRACK_FHS:
        got = _absv850(cycle, member, fh)
        time.sleep(0.1)                   # polite pacing
        if not got:
            continue                      # missing lead -> just skip it
        v, lat, lon = got
        found = _local_max(v, lat, lon, anchor_lat, anchor_lon)
        if not found:
            continue
        vor, la, lo = found
        if vor >= VORT_MIN:
            track.append((fh, la, lo, vor))
            anchor_lat, anchor_lon = la, lo
    if len(track) < MIN_FIXES:
        return None
    return track


def _mean_track(tracks):
    """[(fh, lat, lon, n_members)] averaged over members at each lead."""
    by_fh = {}
    for tr in tracks:
        for fh, la, lo, _p in tr:
            by_fh.setdefault(fh, []).append((la, lo))
    out = []
    for fh in sorted(by_fh):
        pts = by_fh[fh]
        if len(pts) < max(3, len(tracks) // 3):
            continue
        out.append((fh, float(np.mean([p[0] for p in pts])),
                    float(np.mean([p[1] for p in pts])), len(pts)))
    return out


def _render(storm, members, mean, cycle, out_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from data._tz import full

    sid, name, seed_lat, seed_lon, cls = storm
    valid = cycle
    fig = plt.figure(figsize=(11, 7.5), dpi=90)
    proj = ccrs.PlateCarree(central_longitude=0)
    ax = fig.add_subplot(1, 1, 1, projection=proj)

    # frame on the storm: seed + all member points, padded
    all_pts = [(seed_lat, seed_lon)] + [(la, lo) for tr in members
                                        for _f, la, lo, _p in tr]
    las = [p[0] for p in all_pts]
    los = [p[1] for p in all_pts]
    pad = 6.0
    lo_min, lo_max = min(los) - pad, max(los) + pad
    la_min, la_max = min(las) - pad, max(las) + pad
    if lo_max - lo_min > 90:
        lo_min, lo_max = max(-105, np.mean(los) - 45), min(-10, np.mean(los) + 45)
    if la_max - la_min > 70:
        la_min, la_max = max(2, np.mean(las) - 30), min(48, np.mean(las) + 30)
    # keep the US East Coast / Bahamas in frame for geographic context
    lo_min = min(lo_min, -78.0)
    la_min = min(la_min, 20.0)
    la_max = max(la_max, 38.0)
    ax.set_extent([lo_min, lo_max, la_min, la_max],
                  crs=ccrs.PlateCarree())
    ax.coastlines("50m", linewidth=0.6)
    ax.add_feature(cfeature.STATES, linewidth=0.4, edgecolor="gray")
    ax.add_feature(cfeature.BORDERS, linewidth=0.6, edgecolor="dimgray")
    gl = ax.gridlines(draw_labels=True, linewidth=0.3, color="gray",
                      alpha=0.5, linestyle=":")
    gl.top_labels = gl.right_labels = False

    for k, tr in enumerate(members):
        col = MEMBER_COLORS[k % len(MEMBER_COLORS)]
        ax.plot([p[2] for p in tr], [p[1] for p in tr], color=col,
                linewidth=0.9, alpha=0.65, transform=ccrs.PlateCarree())
        ax.plot(tr[-1][2], tr[-1][1], marker="o", markersize=3,
                color=col, alpha=0.8, transform=ccrs.PlateCarree())
    if mean:
        ax.plot([p[2] for p in mean], [p[1] for p in mean], color="white",
                linewidth=2.8, alpha=0.9, transform=ccrs.PlateCarree(),
                zorder=6)
        ax.plot([p[2] for p in mean], [p[1] for p in mean], color="black",
                linewidth=1.2, transform=ccrs.PlateCarree(), zorder=7)
        for fh, la, lo, n in mean:
            if fh % 24 == 0 and fh > 0:
                ax.plot(lo, la, marker="o", markersize=5, color="yellow",
                        markeredgecolor="black", markeredgewidth=0.7,
                        transform=ccrs.PlateCarree(), zorder=8)
                ax.annotate(f"+{fh}h", (lo, la), textcoords="offset points",
                            xytext=(5, 4), fontsize=7, color="white",
                            weight="bold", zorder=9,
                            transform=ccrs.PlateCarree())
    ax.plot(seed_lon, seed_lat, marker="*", markersize=14, color="red",
            markeredgecolor="white", markeredgewidth=0.6,
            transform=ccrs.PlateCarree(), zorder=10)

    ax.set_title(f"GEFS 32-member tracks - {name} ({sid.upper()}, {cls}) - "
                 f"init {full(valid)}\n"
                 f"thin = members, heavy = ensemble mean, * = current "
                 f"position ({seed_lat:.1f}, {seed_lon:.1f})", fontsize=10)
    tmp = out_path.replace(".png", ".tmp.png")
    fig.savefig(tmp, bbox_inches="tight")
    plt.close(fig)
    os.replace(tmp, out_path)
    return out_path


def _render_job(job):
    """Child-side entry for the killable-render guard (data/render_guard.py)."""
    return _render(job["storm"], job["members"], job["mean"], job["cycle"],
                   job["out_path"])


def _render_guarded(storm, members, mean, cycle, out_path):
    """Render one track map in a killable child (wedge costs a timeout only)."""
    from data.render_guard import run_sub
    return run_sub({"mod": __name__, "out_path": out_path, "storm": storm,
                    "members": members, "mean": mean, "cycle": cycle})


def _prune_old():
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
    try:
        for fn in os.listdir(OUT_DIR):
            m = re.search(r"_trk_.+_(\d{10})_na\.png$", fn)
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


def _track_storm(cycle, storm):
    """Bundle entry for one storm, or None when no member develops it."""
    sid, name, seed_lat, seed_lon, cls = storm
    members, skipped = [], 0
    for k, member in enumerate(MEMBERS):
        tr = _track_member(cycle, member, seed_lat, seed_lon)
        if tr:
            members.append(tr)
        else:
            skipped += 1
        if k in (7, 15, 23):
            time.sleep(1.0)               # pacing between member batches
    if len(members) < 4:
        return None                       # not a real ensemble signal
    mean = _mean_track(members)
    fn = f"gefs_trk_{sid.lower()}_{cycle:%Y%m%d%H}_na.png"
    path = os.path.join(OUT_DIR, fn)
    if not (os.path.exists(path) and os.path.getsize(path) > 10_000):
        _render_guarded(storm, members, mean, cycle, path)
    # spread: mean distance of members from the mean track at shared leads
    devs = []
    mean_by_fh = {fh: (la, lo) for fh, la, lo, _n in mean}
    for tr in members:
        for fh, la, lo, _p in tr:
            if fh in mean_by_fh:
                mla, mlo = mean_by_fh[fh]
                devs.append(math.hypot(la - mla, lo - mlo))
    spread = float(np.mean(devs)) if devs else 0.0
    return {
        "id": sid,
        "name": name,
        "class": cls,
        "seed": f"{seed_lat:.1f}, {seed_lon:.1f}",
        "members": len(members),
        "skipped": skipped,
        "spreadDeg": round(spread, 2),
        "mean": [{"fh": fh, "lat": round(la, 2), "lon": round(lo, 2),
                  "n": n} for fh, la, lo, n in mean],
        "img": f"../gefs/{fn}",
        "cycle": _tz.stamp(cycle),
    }


def refresh():
    """Track every active Atlantic storm off the newest complete cycle.

    The final payload is also cached on disk: the ~15-min member walk
    happens once per cycle (6-hourly), rebuilds within that window load
    the JSON and skip straight to rendering.
    """
    from data._tz import full
    os.makedirs(OUT_DIR, exist_ok=True)
    _cache_prune()

    # payload disk cache
    import json as _json
    pcache = os.path.join(CACHE_DIR, "payload.json")
    try:
        with open(pcache) as f:
            pc = _json.load(f)
        if pc.get("cycle") and time.time() - pc.get("t", 0) < 4 * 3600 \
                and all(os.path.isfile(os.path.join(
                    OUT_DIR, os.path.basename(e.get("img") or "x")))
                    for e in pc.get("storms", [])):
            return (dt.datetime.strptime(pc["cycle"], "%Y%m%d%H")
                    .replace(tzinfo=dt.timezone.utc),
                    pc["storms"], len(pc["storms"]))
    except (OSError, ValueError, KeyError):
        pass

    global _breaker_open, _breaker_fails
    cycle = find_gefs_cycle()
    if cycle is None:
        return None, [], 0
    _breaker_open = False                # fresh run: breaker resets
    _breaker_fails = 0
    storms_in = _nhc_atlantic_storms()
    out = []
    for st in storms_in:
        try:
            entry = _track_storm(cycle, st)
        except Exception:  # noqa: BLE001 - one storm never kills the run
            entry = None
        if entry:
            out.append(entry)
    _prune_old()
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = pcache + ".tmp"
        with open(tmp, "w") as f:
            _json.dump({"t": time.time(), "cycle": f"{cycle:%Y%m%d%H}",
                        "storms": out}, f)
        os.replace(tmp, pcache)
    except OSError:
        pass
    return cycle, out, len(out)


def bundle(max_age=CACHE_MAX_AGE):
    """Payload for the tropical page: per-storm GEFS spaghetti."""
    with CACHE_LOCK:
        now = time.time()
        if CACHE["b"] is not None and now - CACHE["t"] < max_age:
            cached = CACHE["b"]
            refs_ok = all(os.path.isfile(os.path.join(
                OUT_DIR, os.path.basename(e.get("img") or "x")))
                for e in cached.get("storms", []))
            if refs_ok:
                return cached
            CACHE.update(t=0.0, b=None)
    try:
        cycle, storms, n = refresh()
    except Exception:  # noqa: BLE001 - never break a build
        cycle, storms, n = None, [], 0
    if not storms and LAST_GOOD.get("b") and \
            time.time() - LAST_GOOD["t"] < LAST_GOOD_TTL:
        return LAST_GOOD["b"]
    b = {
        "ok": bool(cycle),
        "generated": _tz.stamp(dt.datetime.now(dt.timezone.utc)),
        "cycle": _tz.stamp(cycle) if cycle else None,
        "storms": storms,
        "count": n,
        "note": ("no Atlantic storms on the NHC board"
                 if cycle and not storms else None),
        "source": "NOAA GEFS 32-member tracks (min SLP tracker, seeded "
                  "from NHC) - AWS open data, no keys",
    }
    with CACHE_LOCK:
        CACHE.update(t=time.time(), b=b)
    if storms:
        LAST_GOOD.update(t=time.time(), b=b)
    return b

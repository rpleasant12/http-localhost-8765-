"""Polar vortex tracking: Arctic Oscillation index + CPC stratosphere monitoring.

All keyless NOAA/CPC products, cached locally:

- Daily normalized AO index since 1950 (CPC cwlinks, CDAS 1000 mb heights) -
  current value, 7/30-day means, season-to-date, every DJF winter since 1950
  for the record book
- GEFS ensemble AO forecast (31 members x 16 days): ensemble mean with the
  10th/90th percentile spread, so the page can show where the index is
  heading - not just where it has been
- CPC stratosphere monitoring graphics (mirrored into static/polar/): observed
  10 hPa polar-cap temperature (60-90N), 10 hPa zonal-mean zonal wind at 60N
  and 10 hPa eddy heat flux (45-75N) - the three diagnostics forecasters watch
  for a sudden stratospheric warming - plus the 16-day GEFS versions of each
- GFS 10 hPa height/temperature maps (analysis, +48 h, +120 h) showing the
  vortex shape and any split or displacement

The SSW graphics rotate their filename by season (T_10mb_6090N_2026-2027.png),
so the season is derived from today's date and neighbouring seasons are tried
as fallbacks. Text (the AO index) is parsed into compact JSON; graphics are
mirrored for speed. polar.html renders it.
"""
import datetime
import json
import os
import threading
import time

from data.climate import _fetch_graphic, _get

OUT_DIR = os.path.join("static", "polar")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(HERE, ".freebuff", "polar_vortex.json")

CACHE = {"t": 0.0, "b": None}
CACHE_LOCK = threading.Lock()
MAX_AGE = 3 * 3600.0          # AO/GEFS files update ~daily, graphics daily
STALE_OK = 48 * 3600.0        # disk fallback window

AO_CSV = ("https://ftp.cpc.ncep.noaa.gov/cwlinks/"
          "norm.daily.ao.cdas.z1000.19500101_current.csv")
AO_GEFS_CSV = ("https://ftp.cpc.ncep.noaa.gov/cwlinks/"
               "norm.daily.ao.gefs.z1000.120days.csv")
ONI_ASCII = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
SSW_BASE = "https://www.cpc.ncep.noaa.gov/products/stratosphere/SSW/gif_files/"
MAP_BASE = ("https://www.cpc.ncep.noaa.gov/products/stratosphere/"
            "strat_a_f/gif_files/")

# (url stem on the SSW page, output name, label, caption)
OBSERVED = (
    ("U_10mb_60N", "ssw_u10.png",
     "10 hPa zonal wind at 60°N - observed",
     "The vortex strength itself: the average west-to-east wind around the "
     "60°N circle at 16 miles up. Strongly positive and steady = a tight, "
     "undisturbed vortex. A sustained drop toward zero - or a reversal to "
     "easterly - is the signature of a sudden stratospheric warming."),
    ("T_10mb_6090N", "ssw_t10.png",
     "10 hPa temperature, 60-90°N - observed",
     "Polar-cap temperature: how cold the air inside the vortex is. A jump of "
     "25-30°F in a few days is the warming in \"sudden stratospheric warming\"."),
    ("vT_10mb_4575N", "ssw_vt10.png",
     "10 hPa eddy heat flux, 45-75°N - observed",
     "Wave driving - how hard the troposphere's storm track is pushing energy "
     "upward. Spikes here usually come first; the vortex weakens a few days to "
     "a week later."),
)
FORECAST_GFX = (
    ("GEFS_U_10mb_60N", "ssw_u10_fcst.png",
     "10 hPa zonal wind at 60°N - GEFS 16-day forecast",
     "Where the ensemble takes the vortex strength from here. Forecasters "
     "watch for the ensemble mean bending toward zero."),
    ("GEFS_T_10mb_6090N", "ssw_t10_fcst.png",
     "10 hPa temperature, 60-90°N - GEFS 16-day forecast",
     "The forecast polar-cap temperature. A forecast climb toward the "
     "climatological (black) line is a warming event in the making."),
    ("GEFS_vT_10mb_4575N", "ssw_vt10_fcst.png",
     "10 hPa eddy heat flux, 45-75°N - GEFS 16-day forecast",
     "Forecast wave driving: the model's own view of whether the vortex is "
     "about to get hit."),
)
MAPS = (
    ("gfs_z10_nh_f00.png", "map_z10_f00.png",
     "10 hPa height - GFS analysis"),
    ("gfs_z10_nh_f48.png", "map_z10_f48.png",
     "10 hPa height - +48 h forecast"),
    ("gfs_z10_nh_f120.png", "map_z10_f120.png",
     "10 hPa height - +120 h forecast"),
    ("gfs_t10_nh_f00.png", "map_t10_f00.png",
     "10 hPa temperature - GFS analysis"),
)


# ------------------------------------------------------------------ AO index
def ao_history():
    """Daily normalized AO since 1950 -> [{date, y, m, d, v}, ...]."""
    txt = _get(AO_CSV, timeout=45).text
    rows = []
    for line in txt.splitlines()[1:]:
        p = line.split(",")
        if len(p) < 4:
            continue
        try:
            y, m, d, v = int(p[0]), int(p[1]), int(p[2]), float(p[3])
        except ValueError:
            continue
        rows.append({"date": f"{y:04d}-{m:02d}-{d:02d}", "y": y, "m": m, "d": d,
                     "v": round(v, 2)})
    return rows


def ao_forecast():
    """GEFS AO ensemble forecast: latest issue, mean + 10th/90th percentile.

    The file holds 120 days of daily 16-day forecasts (31 members); the
    newest issue date is the live forecast. Returns the ensemble spread too -
    a fanning spread is the honest uncertainty signal.
    """
    lines = _get(AO_GEFS_CSV, timeout=60).text.strip().splitlines()
    rows = [l.split(",") for l in lines[1:] if l.strip()]
    rows = [r for r in rows if len(r) >= 5]
    if not rows:
        return {}
    latest = max(r[2] for r in rows)
    by_day = {}
    for r in rows:
        if r[2] != latest:
            continue
        try:
            by_day.setdefault(r[4], []).append(float(r[3]))
        except ValueError:
            continue
    days = []
    for day in sorted(by_day):
        vals = sorted(by_day[day])
        n = len(vals)
        if not n:
            continue
        days.append({"date": day, "mean": round(sum(vals) / n, 2),
                     "p10": round(vals[max(0, int(0.1 * n) - 1)], 2),
                     "p90": round(vals[min(n - 1, int(0.9 * n))], 2),
                     "members": n})
    return {"issue": latest, "days": days}


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _ao_state(v):
    """(label, color, vortex read) for one AO value."""
    if v is None:
        return "unknown", "#9e9e9e", "AO index unavailable right now."
    if v <= -2.0:
        return ("strongly negative", "#1e88e5",
                "A strongly negative AO means a badly disturbed, wobbly vortex - "
                "the setup that opens the door to Arctic outbreaks in the "
                "mid-latitudes.")
    if v <= -1.0:
        return ("negative", "#64b5f6",
                "A negative AO means a weaker-than-normal vortex: it is more "
                "stretched and prone to wobbling south.")
    if v < 1.0:
        return ("neutral", "#9e9e9e",
                "The vortex is near its climatological strength - no strong "
                "signal either way for cold-air delivery.")
    if v < 2.0:
        return ("positive", "#ef9a9a",
                "A positive AO means a strong, tight vortex locking cold air "
                "up north - typically milder for the eastern U.S.")
    return ("strongly positive", "#e53935",
            "A strongly positive AO means an unusually strong, locked vortex - "
            "cold air stays bottled up over the Arctic.")


def _winter_label(start_year):
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def oni_seasons():
    """CPC 3-month ONI means, oldest first: [{season, year, anom}, ...].

    The file labels a season by its ENDING year ("DJF 1951" = Dec 1950 to
    Feb 1951), which is what the DJF lookup below reverses.
    """
    out = []
    for line in _get(ONI_ASCII, timeout=45).text.splitlines():
        p = line.split()
        if len(p) < 4 or len(p[0]) != 3 or not p[0].isalpha() or not p[1].isdigit():
            continue
        try:
            out.append({"season": p[0].upper(), "year": int(p[1]),
                        "anom": round(float(p[3]), 2)})
        except ValueError:
            continue
    return out


def _oni_block(seasons):
    """DJF ONI per winter start year + the latest published 3-month mean."""
    djf = {}
    for s in seasons or []:
        if s["season"] == "DJF":
            djf[s["year"] - 1] = s["anom"]      # DJF 1998 -> winter 1997-98
    latest = seasons[-1] if seasons else None
    return djf, latest


def _winters(rows):
    """DJF means since 1950 plus the current in-progress season."""
    buckets = {}
    for r in rows:
        if r["m"] not in (12, 1, 2):
            continue
        key = r["y"] if r["m"] == 12 else r["y"] - 1
        buckets.setdefault(key, []).append(r["v"])
    complete, partial = [], None
    for key in sorted(buckets):
        vals = buckets[key]
        entry = {"start": key, "years": _winter_label(key),
                 "ao": _mean(vals), "days": len(vals),
                 "partial": len(vals) < 75,
                 "min": round(min(vals), 2), "max": round(max(vals), 2)}
        if entry["partial"]:
            # only the newest, still-running winter counts as "in progress";
            # an old short bucket is a data gap, not a season
            if key == max(buckets) and len(vals) > 5:
                partial = entry
        else:
            complete.append(entry)
    return complete, partial


def _ao_block(rows, forecast, oni=None):
    complete, partial = _winters(rows)
    oni_djf, oni_latest = _oni_block(oni)
    # each winter carries its own ENSO state, so the analog search can match
    # on AO and ONI together without a second join downstream
    for w in complete:
        w["oni"] = oni_djf.get(w["start"])
    if partial:
        partial["oni"] = oni_djf.get(partial["start"])
    latest = rows[-1]
    recent = [r["v"] for r in rows[-7:]]
    prev7 = [r["v"] for r in rows[-14:-7]]
    last30 = [r["v"] for r in rows[-30:]]
    last90 = [r["v"] for r in rows[-90:]]
    trend = None
    if recent and prev7:
        trend = round(sum(recent) / len(recent) - sum(prev7) / len(prev7), 2)

    # the cold-season half-year the vortex is monitored over (Oct 1-Mar 31).
    # A lagging AO feed can sit in Apr-Sep, when the latest season is already
    # over - label that one "final" instead of pretending it is in progress.
    ly, lm = latest["y"], latest["m"]
    season_year = ly if lm >= 10 else ly - 1
    season_start = datetime.date(season_year, 10, 1)
    season_end = datetime.date(season_year + 1, 3, 31)
    in_progress = season_start <= datetime.date(ly, lm, latest["d"]) <= season_end
    season_vals = [r["v"] for r in rows
                   if season_start <= datetime.date(r["y"], r["m"], r["d"])
                   <= season_end]

    label, color, read = _ao_state(latest["v"])
    fc_days = (forecast or {}).get("days") or []
    fc7 = [d["mean"] for d in fc_days[:7]]
    fc14 = [d["mean"] for d in fc_days[:14]]
    return {
        "daily": rows[-140:],
        "current": {
            "date": latest["date"], "value": latest["v"],
            "label": label, "color": color, "read": read,
            "mean7": _mean(recent), "mean30": _mean(last30),
            "min90": round(min(last90), 2), "max90": round(max(last90), 2),
            "trend": trend,
            "daysNeg90": sum(1 for v in last90 if v <= -1.0),
            "daysStrong90": sum(1 for v in last90 if v <= -2.0),
        },
        "season": {
            "label": _winter_label(season_start.year),
            "start": season_start.isoformat(),
            "end": season_end.isoformat(),
            "inProgress": in_progress,
            "mean": _mean(season_vals), "days": len(season_vals),
        },
        "forecast": {
            "issue": (forecast or {}).get("issue"),
            "days": fc_days,
            "mean7": _mean(fc7), "mean14": _mean(fc14),
        },
        # every complete DJF winter since 1950, oldest first: the Tennessee
        # outcome table joins on these labels, so it needs the full record
        # and not just the 20 shown in the history table
        "wintersAll": complete,
        "winters": complete[-20:][::-1],
        # latest published 3-month ONI - the ENSO half of this winter's state
        "oniLatest": oni_latest,
        "partial": partial,
        "records": {
            "negative": sorted(complete, key=lambda w: w["ao"])[:5],
            "positive": sorted(complete, key=lambda w: -w["ao"])[:5],
        },
    }


# ------------------------------------------------------------------ graphics
def _seasons(now=None):
    """Candidate SSW filename seasons, newest first (Jul-Jun labelled)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    start = now.year if now.month >= 7 else now.year - 1
    return [f"{s}-{s + 1}" for s in (start, start - 1, start + 1)]


def _mirror_stem(stem, out_name, seasons):
    """Mirror one season-suffixed SSW graphic; None when none exist."""
    for season in seasons:
        url = f"{SSW_BASE}{stem}_{season}.png"
        dst = os.path.join(OUT_DIR, out_name)
        try:
            _fetch_graphic(url, dst)
            if os.path.getsize(dst) > 5_000:
                return f"../polar/{out_name}", season
        except Exception:                              # noqa: BLE001
            continue
    return None, None


def _graphics():
    seasons = _seasons()
    observed, forecast, used = [], [], None
    for stem, out_name, label, caption in OBSERVED:
        href, season = _mirror_stem(stem, out_name, seasons)
        if href:
            used = used or season
            observed.append({"url": href, "label": label, "caption": caption})
    for stem, out_name, label, caption in FORECAST_GFX:
        href, season = _mirror_stem(stem, out_name, seasons)
        if href:
            used = used or season
            forecast.append({"url": href, "label": label, "caption": caption})
    maps = []
    for src, out_name, label in MAPS:
        dst = os.path.join(OUT_DIR, out_name)
        try:
            _fetch_graphic(MAP_BASE + src, dst)
            if os.path.getsize(dst) > 5_000:
                maps.append({"url": f"../polar/{out_name}", "label": label})
        except Exception:                              # noqa: BLE001
            continue
    return {"season": used, "observed": observed, "forecast": forecast,
            "maps": maps}


def _stamp():
    from data import _tz
    return _tz.stamp(datetime.datetime.now(datetime.timezone.utc))


# -------------------------------------------------------------------- bundle
def bundle(max_age=MAX_AGE):
    """Everything the polar vortex page needs; 3-h memory cache.

    Self-healing like the ENSO/climate bundles: a cached bundle that points
    at graphics no longer on disk is discarded and re-mirrored, so a sweep
    can never leave the page with broken images.
    """
    with CACHE_LOCK:
        if CACHE["b"] is not None and time.time() - CACHE["t"] < max_age:
            cached = CACHE["b"]
            refs = [(cached.get("strat") or {}).get(k) or []
                    for k in ("observed", "forecast", "maps")]
            refs_ok = all(
                os.path.isfile(os.path.join(OUT_DIR, g["url"].split("/")[-1]))
                for group in refs for g in group if g.get("url"))
            if refs_ok and (cached.get("ao") or {}).get("daily"):
                return cached
            CACHE.update(t=0.0, b=None)      # stale refs -> full re-mirror

    os.makedirs(OUT_DIR, exist_ok=True)
    rows = ao_history()
    if not rows:
        raise RuntimeError("AO history came back empty")
    try:
        forecast = ao_forecast()
    except Exception:                                  # noqa: BLE001
        forecast = {}
    try:
        oni = oni_seasons()
    except Exception:                                  # noqa: BLE001
        oni = []
    strat = _graphics()
    b = {
        "ok": True,
        "generated": _stamp(),
        "asOf": rows[-1]["date"],
        "ao": _ao_block(rows, forecast, oni),
        "strat": strat,
        "source": ("NOAA CPC Arctic Oscillation index (CDAS 1000 mb heights, "
                   "1950-present) + GEFS ensemble forecast; CPC Stratosphere / "
                   "Sudden Stratospheric Warming monitoring and NCEP GFS 10 hPa "
                   "analyses and forecasts"),
    }
    with CACHE_LOCK:
        CACHE.update(t=time.time(), b=b)
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH + ".tmp", "w", encoding="utf-8") as f:
            json.dump(b, f)
        os.replace(CACHE_PATH + ".tmp", CACHE_PATH)
    except OSError:
        pass
    return b


def disk_fallback():
    """Last good payload from disk (process restarts / NOAA outages)."""
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            disk = json.load(f)
        if disk and time.time() - os.path.getmtime(CACHE_PATH) < STALE_OK:
            return disk
    except (OSError, ValueError):
        pass
    return None


if __name__ == "__main__":
    b = bundle(max_age=0)
    ao = b["ao"]
    print("as of:", b["asOf"], "| generated:", b["generated"])
    print("current:", ao["current"])
    print("season:", ao["season"])
    print("forecast issue:", ao["forecast"]["issue"],
          "| 7-day mean:", ao["forecast"]["mean7"],
          "| 14-day mean:", ao["forecast"]["mean14"],
          "| days:", len(ao["forecast"]["days"]))
    print("winters:", len(ao["winters"]), "| last:",
          ao["winters"][0]["years"], ao["winters"][0]["ao"])
    print("record negative:", [(w["years"], w["ao"]) for w in ao["records"]["negative"]])
    print("record positive:", [(w["years"], w["ao"]) for w in ao["records"]["positive"]])
    print("season-graphics:", b["strat"]["season"],
          "| observed:", len(b["strat"]["observed"]),
          "| forecast:", len(b["strat"]["forecast"]),
          "| maps:", len(b["strat"]["maps"]))

"""Tennessee winter outcomes, for the polar-vortex record book.

The polar page ranks winters by Arctic Oscillation; this module answers the
question a reader asks next - did a negative-AO winter actually deliver cold
and snow in Tennessee?

One keyless source, cached to .freebuff:
- ACIS (NOAA Regional Climate Centers, data.rcc-acis.org) daily station
  records for Nashville, Memphis and Knoxville: average temperature and
  snowfall.

The 1991-2020 normals are DERIVED from those same records (the mean of the
1990-91 .. 2019-20 seasons), so a "normal" can never contradict the history
it is being compared against, and a station with a short record simply
drops out of the average instead of skewing it.

Those three stations are also the state's three grand divisions - Memphis
west, Nashville middle, Knoxville east - so the same fetch answers "did the
cold reach my corner of the state?" separately. Division values are a single
station each: honest about being one record, not a gridded regional mean,
which is why the division columns are shown alongside the 3-station state
index rather than instead of it.

Winter = Dec-Feb, labelled by the starting year ("1950-51" = Dec 1950 to
Feb 1951), which matches data/polar_vortex.py's AO winters exactly so the
two tables join on the label. Snow counts trace ("T") as 0.0 and skips
missing ("M"); a winter with too many missing days is reported as null
rather than as a suspiciously dry total. The three stations are averaged
into one Tennessee index - honest about being a 3-station index, not a
gridded statewide product.
"""
import datetime
import json
import os
import threading
import time

import requests

UA = {"User-Agent": "TNWN-WeatherCenter/1.0 (polar vortex winter outcomes)"}
ACIS = "https://data.rcc-acis.org/StnData"

# (station id, city, grand division) - the three divisions, one station each.
STATIONS = (("KMEM", "Memphis", "West"),
            ("KBNA", "Nashville", "Middle"),
            ("KTYS", "Knoxville", "East"))
DIVISIONS = tuple(div for _, _, div in STATIONS)

START_YEAR = 1950
BASELINE = (1990, 2019)      # start years of the 1991-2020 DJF normals
MIN_SNOW_DAYS = 80           # of ~90 winter days; below this the total is junk
MIN_TEMP_DAYS = 80
MAX_MISSING_SNOW = 10

CACHE_PATH = os.path.join(".freebuff", "tn_winter.json")
SCHEMA = 2                   # bump when the payload shape changes
MAX_AGE = 7 * 86400.0        # ACIS station histories change monthly at most
STALE_OK = 60 * 86400.0      # disk copy stays useful through a long outage
CACHE = {"t": 0.0, "b": None}
CACHE_LOCK = threading.Lock()

_SESSION = requests.Session()


def _series(sid, elem, edate=None):
    """{date object: float} for one ACIS element, or raise on a bad reply.

    ACIS CSV is a station-name header followed by "YYYY-MM-DD,value" rows.
    Trace ("T") is a real value: zero accumulation. Missing ("M", "S", "A",
    blank) has no number and is simply absent from the result.
    """
    end = edate or datetime.date.today().isoformat()
    r = _SESSION.get(ACIS, params={"sid": sid, "sdate": f"{START_YEAR}-12-01",
                                  "edate": end, "elems": elem, "output": "csv"},
                     headers=UA, timeout=90)
    r.raise_for_status()
    lines = r.text.strip().splitlines()
    if len(lines) < 3:
        raise RuntimeError(f"{sid} {elem}: short reply ({len(lines)} lines)")
    out = {}
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        raw = parts[1].strip()
        try:
            day = datetime.date.fromisoformat(parts[0].strip())
        except ValueError:
            continue
        if raw == "T":
            out[day] = 0.0
            continue
        try:
            out[day] = float(raw)
        except ValueError:
            continue          # M / S / A / blank -> not observed
    return out


def _winter_key(day):
    """Start year of the Dec-Feb winter a date belongs to."""
    return day.year if day.month == 12 else day.year - 1


def _aggregate(values, kind):
    """Per-winter stats from one station's {date: value}.

    kind="snow": total, plus how many winter days are missing.
    kind="tavg": mean, plus how many winter days are present.
    Days absent from the series count as missing either way, so a station
    that simply stops reporting cannot look like a snowless winter.
    """
    buckets = {}
    for day, v in values.items():
        if day.month not in (12, 1, 2):
            continue
        b = buckets.setdefault(_winter_key(day), {"vals": [], "seen": 0})
        b["vals"].append(v)
        b["seen"] += 1
    out = {}
    for start, b in buckets.items():
        expected = 90 if not _is_leap_winter(start) else 91
        missing = max(0, expected - b["seen"])
        if kind == "snow":
            out[start] = {"value": round(sum(b["vals"]), 2),
                          "days": b["seen"], "missing": missing}
        else:
            out[start] = {"value": round(sum(b["vals"]) / len(b["vals"]), 2),
                          "days": b["seen"], "missing": missing}
    return out


def _is_leap_winter(start):
    """Feb of start+1 decides the winter's length (Dec/Jan are fixed)."""
    y = start + 1
    return (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)


def _station(sid):
    """Both elements for one station, each failure confined to its element."""
    snow = tavg = None
    try:
        snow = _aggregate(_series(sid, "snow"), "snow")
    except Exception:                                  # noqa: BLE001
        pass
    try:
        tavg = _aggregate(_series(sid, "avgt"), "tavg")
    except Exception:                                  # noqa: BLE001
        pass
    return snow or {}, tavg or {}


def _usable_snow(rec):
    return bool(rec) and rec["days"] >= MIN_SNOW_DAYS \
        and rec["missing"] <= MAX_MISSING_SNOW


def _usable_temp(rec):
    return bool(rec) and rec["days"] >= MIN_TEMP_DAYS


def _mean(vals):
    vals = [v for v in (vals or []) if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


# ------------------------------------------------------------------ assembly
def _build():
    """Join the three stations into per-winter Tennessee values + normals.

    Each station is also kept on its own as its grand division, so the same
    winter can be read statewide (3-station mean) or division by division.
    """
    per_snow, per_temp, used = {}, {}, []
    div_snow, div_temp, div_station = {}, {}, {}
    for sid, name, div in STATIONS:
        snow, tavg = _station(sid)
        if snow or tavg:
            used.append(name)
        div_station[div] = name
        for start, rec in snow.items():
            if _usable_snow(rec):
                per_snow.setdefault(start, []).append(rec["value"])
                div_snow.setdefault(div, {}).setdefault(start, rec["value"])
        for start, rec in tavg.items():
            if _usable_temp(rec):
                per_temp.setdefault(start, []).append(rec["value"])
                div_temp.setdefault(div, {}).setdefault(start, rec["value"])
    if len(used) < 2:
        raise RuntimeError(f"only {len(used)} station(s) answered ({used})")

    rows = []
    for start in range(START_YEAR, datetime.date.today().year + 1):
        snow = _mean(per_snow.get(start))
        temp = _mean(per_temp.get(start))
        if snow is None and temp is None:
            continue
        rows.append({"start": start,
                     "winter": f"{start}-{str(start + 1)[-2:]}",
                     "snow": snow, "temp": temp,
                     "stations": len(per_snow.get(start, []))})
    if not rows:
        raise RuntimeError("no winter had enough station coverage")

    base = [r for r in rows if BASELINE[0] <= r["start"] <= BASELINE[1]]
    snow_normal, temp_normal = _mean([r["snow"] for r in base]), \
        _mean([r["temp"] for r in base])
    # Division normals come from the SAME baseline seasons, each division's
    # own record - so small snow numbers stay comparable to their own normal
    # instead of being judged against a statewide figure they never match.
    div_norm = {}
    for div in DIVISIONS:
        div_norm[div] = {
            "snow": _mean([div_snow.get(div, {}).get(y)
                           for y in range(BASELINE[0], BASELINE[1] + 1)]),
            "temp": _mean([div_temp.get(div, {}).get(y)
                           for y in range(BASELINE[0], BASELINE[1] + 1)]),
            "station": div_station.get(div),
        }
    for r in rows:
        if r["snow"] is not None and snow_normal:
            r["snowPct"] = round(100 * r["snow"] / snow_normal)
            r["snowAnom"] = round(r["snow"] - snow_normal, 1)
        else:
            r["snowPct"] = r["snowAnom"] = None
        r["tempAnom"] = (round(r["temp"] - temp_normal, 1)
                         if r["temp"] is not None and temp_normal is not None
                         else None)
        r["div"] = {}
        for div in DIVISIONS:
            n = div_norm[div]
            s = div_snow.get(div, {}).get(r["start"])
            t = div_temp.get(div, {}).get(r["start"])
            r["div"][div] = {
                "snow": s, "temp": t,
                "snowPct": (round(100 * s / n["snow"])
                            if s is not None and n["snow"] else None),
                "tempAnom": (round(t - n["temp"], 1)
                             if t is not None and n["temp"] is not None
                             else None),
            }
    return rows, snow_normal, temp_normal, used, len(base), div_norm


def bundle(max_age=MAX_AGE):
    """Tennessee winter outcomes; memory + disk cache, 7 days."""
    with CACHE_LOCK:
        if CACHE["b"] is not None and CACHE["b"].get("schema") == SCHEMA \
                and time.time() - CACHE["t"] < max_age:
            return CACHE["b"]
    rows, snow_normal, temp_normal, used, base_n, div_norm = _build()
    b = {
        "ok": True,
        "schema": SCHEMA,
        "divisions": [{"name": div,
                       "station": div_norm[div]["station"],
                       "snowNormal": div_norm[div]["snow"],
                       "tempNormal": div_norm[div]["temp"]}
                      for div in DIVISIONS],
        "generated": _stamp(),
        "winters": rows,
        "snowNormal": snow_normal,
        "tempNormal": temp_normal,
        "baseline": f"{BASELINE[0] + 1}-{BASELINE[1] + 1}",
        "stations": used,
        "baselineWinters": base_n,
        "source": ("NOAA ACIS daily station records (Memphis, Nashville, "
                   "Knoxville) - winter temperature and snowfall, with "
                   f"{BASELINE[0] + 1}-{BASELINE[1] + 1} normals derived from "
                   "the same records"),
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
    """Last good payload from disk (process restarts / ACIS outages)."""
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            disk = json.load(f)
        if disk and disk.get("winters") and disk.get("schema") == SCHEMA \
                and time.time() - os.path.getmtime(CACHE_PATH) < STALE_OK:
            return disk
    except (OSError, ValueError):
        pass
    return None


def _stamp():
    from data import _tz
    return _tz.stamp(datetime.datetime.now(datetime.timezone.utc))


if __name__ == "__main__":
    b = bundle(max_age=0)
    rows = b["winters"]
    print("stations:", b["stations"], "| normals:", b["snowNormal"], "in snow /",
          b["tempNormal"], "F | baseline winters:", b["baselineWinters"])
    for d in b["divisions"]:
        print(f'  {d["name"]:>6} ({d["station"]}): {d["snowNormal"]} in snow, '
              f'{d["tempNormal"]} F winter mean')
    print("winters:", len(rows), "| first:", rows[0]["winter"], rows[0]["snow"],
          "in", f'{rows[0]["tempAnom"]:+}F')
    for r in rows[-6:]:
        print(f'{r["winter"]}  snow {r["snow"]:>5} in ({r["snowPct"]:>4}% of '
              f'normal)  temp {r["tempAnom"]:+}F  stations {r["stations"]}')

"""Sun & moon times for the home point - sun motion drives the whole day.

Keyless source: https://api.sunrise-sunset.org (verified live 2026-09-20).
Returns times already formatted in Eastern time via the site's _tz helpers,
plus a plain-English daytime phase ("Golden hour" etc.) and moon phase name.
Moon phase is computed locally (synodic month arithmetic) - no API needed.
"""
import datetime as dt
import json
import math
import os
import urllib.request

from data._tz import ET, stamp, _hm

LAT = 36.1627          # Greeneville TN (matches config.DEFAULT_LATITUDE)
LON = -82.8332
_CACHE = os.path.join(".freebuff", "sun-cache.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_MOON_PHASES = [
    (1.0, "Full Moon"), (0.925, "Waning Gibbous"), (0.655, "Last Quarter"),
    (0.375, "Waning Crescent"), (0.235, "New Moon"), (0.095, "Waxing Crescent"),
    (0.345, "First Quarter"), (0.615, "Waxing Gibbous"),
]


def _moon_phase_name(when):
    """Approximate phase from the synodic month (good to ~1 day)."""
    ref = dt.datetime(2000, 1, 6, 18, 14, tzinfo=dt.timezone.utc)  # known new moon
    synodic = 29.53058867
    frac = ((when - ref).total_seconds() / 86400.0) % synodic / synodic
    for hi, name in _MOON_PHASES:
        if abs(frac - (1 - hi if hi > 0.5 else hi)) < 0.07 or \
           (hi == 1.0 and frac > 0.93):
            return name, frac
    return "Waxing Moon", frac


def _phase_icon(name):
    return {"Full Moon": "🌕", "Waning Gibbous": "🌖", "Last Quarter": "🌗",
            "Waning Crescent": "🌘", "New Moon": "🌑", "Waxing Crescent": "🌒",
            "First Quarter": "🌓", "Waxing Gibbous": "🌔"}.get(name, "🌙")


def _fmt_local(iso_utc, tz):
    """'2026-09-20T23:20:44+00:00' -> '7:20 PM ET' (no tz suffix -> None)."""
    try:
        w = dt.datetime.fromisoformat(iso_utc)
        if w.tzinfo is None:
            w = w.replace(tzinfo=dt.timezone.utc)
        return _hm(w.astimezone(tz))
    except (ValueError, TypeError):
        return None


def _day_phase(now_et, sunrise, sunset):
    """Plain-English where-we-are-in-the-day label."""
    if not (sunrise and sunset):
        return None
    sh = _mins(sunrise)
    eh = _mins(sunset)
    m = now_et.hour * 60 + now_et.minute
    if m < sh - 60:
        return "Night"
    if m < sh:
        return "Pre-dawn"
    if m < sh + 60:
        return "Sunrise"
    if m < (sh + eh) / 2 - 90:
        return "Morning sun"
    if m < eh - 90:
        return "Afternoon sun"
    if m < eh:
        return "Golden hour"
    return "Evening"


def _mins(hm):
    t = dt.datetime.strptime(hm.strip(), "%I:%M %p")   # _tz._hm -> "7:16 AM"
    return t.hour * 60 + t.minute


def sun_bundle():
    """Today's sun/moon card data for the home point, ET-formatted."""
    today = dt.datetime.now(ET).date()
    cache_key = today.isoformat()
    try:
        with open(_CACHE, encoding="utf-8") as f:
            b = json.load(f)
        if b.get("day") == cache_key:
            return b
    except (OSError, ValueError):
        pass

    out = {"ok": False, "day": cache_key}
    try:
        url = (f"https://api.sunrise-sunset.org/json?lat={LAT}&lng={LON}"
               f"&formatted=0&date={cache_key}")
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.load(r)
        if res.get("status") != "OK":
            raise ValueError(res.get("status"))
        r0 = res["results"]
        # API returns UTC (formatted=0); note: sunrise-sunset.org returns
        # naive UTC strings - attach UTC then convert to ET.
        sunrise = _fmt_local(r0["sunrise"], ET)
        sunset = _fmt_local(r0["sunset"], ET)
        civil_begin = _fmt_local(r0["civil_twilight_begin"], ET)
        civil_end = _fmt_local(r0["civil_twilight_end"], ET)
        day_len_s = int(r0.get("day_length") or 0)
        now_et = dt.datetime.now(ET)
        phase, frac = _moon_phase_name(dt.datetime.now(dt.timezone.utc))
        out = {
            "ok": True, "day": cache_key,
            "sunrise": sunrise, "sunset": sunset,
            "civilBegin": civil_begin, "civilEnd": civil_end,
            "dayLength": f"{day_len_s // 3600}h {day_len_s % 3600 // 60:02d}m",
            "dayPhase": _day_phase(now_et, sunrise, sunset),
            "moonPhase": phase, "moonIcon": _phase_icon(phase),
            "moonFrac": round(frac, 2),
            "fetched": stamp(now_et),
        }
        os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
        with open(_CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f)
    except Exception:                          # noqa: BLE001 - never break build
        return out
    return out


if __name__ == "__main__":
    b = sun_bundle()
    print("ok:", b["ok"])
    for k in ("sunrise", "sunset", "civilBegin", "civilEnd", "dayLength",
              "dayPhase", "moonPhase", "moonIcon"):
        print(f"  {k}: {b.get(k)}")

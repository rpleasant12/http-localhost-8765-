"""Season-to-date snow tracker for the Winter page.

Logs each observed winter snow event (>= 0.1") at the four anchor stations
used by the almanac panel - Knoxville (TYS), Chattanooga (CHA), Tri-Cities
(TRI) and Crossville (CSV) - from NOAA's free ACIS daily database
(data.rcc-acis.org, the same API the Crossville almanac numbers came from),
then pairs the season-to-date record against the CPC seasonal outlook
windows (NDJ / DJF / JFM) the seasonal tilt map already ships.

The season runs Jul 1 -> Jun 30 (a southern-appalachian snow year), so an
October 1 event lands in the NDJ window's run-up and a March 20 storm
belongs to JFM. Verification is deliberately honest and simple: compare
observed season-to-date snow at each station against that station's
1991-2020 normal through the same calendar fraction of the season, and
classify the CPC seasonal snow signal (from the almanac priors) as
tracking above / near / below the look the outlook promised. No key, no
cost - ACIS is a free NOAA Regional Climate Center service.
"""
import json
import time
import urllib.request

ACIS = "https://data.rcc-acis.org/StnData"

# (ACIS sid, town label, station code, 1991-2020 normal seasonal snow)
# ACIS ids use the FAA-3-letter + " 3" form ("KTNV"-style ids match nothing);
# these resolve to KNOXVILLE AP / CHATTANOOGA AP / BRISTOL AP /
# CROSSVILLE MEMORIAL AP.
STATIONS = [
    ("TYS 3", "Knoxville", "TYS", 4.6),
    ("CHA 3", "Chattanooga", "CHA", 3.6),
    ("TRI 3", "Tri-Cities", "TRI", 9.2),
    ("CSV 3", "Crossville", "CSV", 6.5),  # spotty reports; almanac says ~6-7"
]

# Climatological snow-share of the season by calendar day (fraction of the
# Oct-Mar snow that has typically fallen by that date, from the 1991-2020
# normals' shape: quiet Oct/Nov, ramping Dec, peak Jan-early Feb, tail Feb).
# Interpolated stepwise; good enough for an honest "near/above/below" call.
_SNOW_SHARE = [
    (1001, 0.00), (1101, 0.03), (1201, 0.15), (1231, 0.40),
    (131, 0.65), (215, 0.85), (301, 0.95), (331, 1.00), (1231, 1.00),
]

MIN_EVENT = 0.1      # an "event" logs at 0.1" or more
CONNECT_GAP = 1      # days with no snow allowed inside one event (warm spells)


def _mmdd(mon, day):
    return mon * 100 + day


def _snow_share(mon, day):
    """Fraction of a normal season's snow expected by this calendar date."""
    v = _mmdd(mon, day)
    share = 0.0
    for (start, s) in _SNOW_SHARE:
        if v >= start:
            share = s
    return share


def _acis(sid, start, end):
    """Daily snowfall + snow depth for one station; None on failure.

    ACIS takes plain elem-name strings here - the {"name": ...} dict form
    is rejected (400 bad args), same lesson as the almanac build.
    """
    body = json.dumps({
        "sid": sid, "sdate": start, "edate": end,
        "elems": ["snow"],
    }).encode()
    req = urllib.request.Request(
        ACIS, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "TNWN/1.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        out = json.loads(r.read().decode())
    rows = []
    for d, vals in out.get("data", []):
        y, m, dd = (int(x) for x in d.split("-"))
        def _f(s):
            try:
                v = float(s)
                return None if v < 0 else v      # ACIS uses -999 for missing
            except (TypeError, ValueError):
                return None
        rows.append({"date": f"{y:04d}-{m:02d}-{dd:02d}",
                     "mon": m, "day": dd, "snow": _f(vals[0])})
    return rows


def _events(daily):
    """Group consecutive snowy days into storm events (<=1 dry day between)."""
    events = []
    cur = None
    for r in daily:
        if (r["snow"] or 0) >= MIN_EVENT:
            if cur is None:
                cur = {"start": r["date"], "end": r["date"], "total": 0.0,
                       "days": 0}
            cur["end"] = r["date"]
            cur["total"] += r["snow"]
            cur["days"] += 1
        elif cur and r["snow"] is not None and r["snow"] == 0 and \
                _days_between(cur["end"], r["date"]) <= CONNECT_GAP:
            continue                                   # warm gap inside event
        else:
            if cur:
                events.append(cur)
                cur = None
    if cur:
        events.append(cur)
    for e in events:
        e["total"] = round(e["total"], 1)
    return events


def _days_between(d1, d2):
    import datetime
    a = datetime.date(*map(int, d1.split("-")))
    b = datetime.date(*map(int, d2.split("-")))
    return (b - a).days


def _window_of(mon):
    """CPC window label this month's snow belongs to."""
    if mon in (10, 11, 12):
        return "NDJ" if mon != 12 else "DJF"
    if mon in (1,):
        return "DJF"
    if mon in (2, 3):
        return "JFM"
    return "pre"       # Oct runs under NDJ's aegis in the south; Oct->NDJ


def _window_label(mon):
    return {"pre": "Oct (NDJ run-up)", "NDJ": "NDJ", "DJF": "DJF",
            "JFM": "JFM"}.get(_window_of(mon), "pre")


def _season_dates(today=None):
    """(season start, fetch end, phase) for the Jul 1 - Jun 30 snow year.

    Before Oct 1 (July-September) there is no in-progress season, so the
    tracker falls back to the most recent COMPLETE season - the tracker
    proves its format with last winter's real events while the new season
    is still pre-season.
    """
    import datetime
    t = today or datetime.date.today()
    if t.month >= 10 or t.month <= 6:
        start = datetime.date(t.year, 10, 1) if t.month >= 10 \
            else datetime.date(t.year - 1, 10, 1)
        if t < start:                       # Jan-Jun: season ended Jun 30
            end = datetime.date(t.year, 6, 30)
            phase = "complete"
        else:
            end = t
            phase = "in-progress"
    else:                                    # Jul-Sep: pre-season
        start = datetime.date(t.year - 1, 10, 1)
        end = datetime.date(t.year, 6, 30)
        phase = "complete"
    return start, end, phase


def _verdict(station):
    """One station's season-to-date story + CPC pairing."""
    start, end, phase = _season_dates()
    daily = _acis(station["sid"], start.isoformat(), end.isoformat())
    if daily is None:
        return {**station, "ok": False, "reason": "ACIS fetch failed"}
    events = _events(daily)
    total = round(sum(e["total"] for e in events), 1)
    bymonth = {}
    for e in events:
        mn = _MONTHS[int(e["end"][5:7])]
        bymonth[mn] = round(bymonth.get(mn, 0) + e["total"], 1)
    # CPC pairing: normal share of the season's snow by today, applied to
    # the station's 1991-2020 normal seasonal total. Only meaningful once
    # the season is under way (a complete season gets a plain summary).
    share = _snow_share(end.month, end.day) if phase == "in-progress" else 1.0
    expected = round(station["normal"] * share, 1)
    if phase == "complete":
        if total >= station["normal"] * 1.3:
            verdict, vcol = "finished well above normal", "#7b3fc9"
        elif total >= station["normal"] * 0.8:
            verdict, vcol = "finished near normal", "#43a047"
        else:
            verdict, vcol = "finished below normal", "#ef6c00"
    elif expected <= 0.3:
        # too early in the season for a meaningful call
        verdict, vcol = ("on pace", "#43a047") if total <= 0.5 \
            else ("ahead of pace", "#42a5f5")
    else:
        ratio = total / expected
        if ratio >= 1.5:
            verdict, vcol = "well above the CPC look", "#7b3fc9"
        elif ratio >= 1.1:
            verdict, vcol = "above the CPC look", "#42a5f5"
        elif ratio >= 0.7:
            verdict, vcol = "tracking the CPC look", "#43a047"
        elif ratio > 0:
            verdict, vcol = "below the CPC look", "#ef6c00"
        else:
            verdict, vcol = "shut out so far", "#ef6c00"
    # CPC snow-chance prior from the almanac build (station-level word)
    chance = station.get("chance", "occasional")
    return {**station, "ok": True, "phase": phase,
            "seasonStart": start.isoformat(), "through": end.isoformat(),
            "events": events,
            "total": total,
            "expectedByNow": expected, "shareOfSeason": share,
            "verdict": verdict, "verdictColor": vcol,
            "cpcPrior": chance, "byMonth": bymonth}


# CPC snow-chance priors (from the almanac build's 1991-2020 read):
# Plateau + Tri-Cities run frequent; valley towns occasional.
_PRIORS = {
    "TYS": "occasional", "CHA": "occasional",
    "TRI": "frequent", "CSV": "frequent",
}


_MONTHS = {1: "Jan", 2: "Feb", 3: "Mar", 10: "Oct", 11: "Nov", 12: "Dec"}


def bundle():
    """Full tracker payload; cached in-process for 3 h (records move slowly)."""
    key = "snowtrack_bundle"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < 10800:
        return hit[1]
    out = {"ok": True, "generated": time.strftime("%Y-%m-%d %H:%M"),
           "stations": [], "source": "NOAA ACIS daily database "
           "(data.rcc-acis.org); normals: 1991-2020 U.S. Climate Normals"}
    for sid, town, code, normal in STATIONS:
        st = {"sid": sid, "town": town, "code": code, "normal": normal,
              "chance": _PRIORS.get(code, "occasional")}
        try:
            out["stations"].append(_verdict(st))
        except Exception as exc:                       # noqa: BLE001
            out["stations"].append({**st, "ok": False,
                                    "reason": str(exc)[:120]})
    out["ok"] = any(s.get("ok") for s in out["stations"])
    _CACHE[key] = (time.time(), out)
    return out


_CACHE = {}

if __name__ == "__main__":
    print(json.dumps(bundle(), indent=1)[:1200])

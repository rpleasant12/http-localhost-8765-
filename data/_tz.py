"""Eastern-Time formatting for every visitor-facing timestamp.

The site serves East Tennessee: all model cycle stamps, frame labels and
table times are displayed in America/New_York (ET, auto EST/EDT) instead of
UTC. Raw ISO epochs stay UTC in payloads/data.json (machinery compares them);
only DISPLAY strings are converted.
"""
import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def to_et(when):
    """aware/naive-UTC datetime -> America/New_York datetime."""
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(ET)


def _hm(w):
    """'8 PM' or '7:55 PM' - 12-hour clock, no leading zero, :00 dropped."""
    h = w.hour % 12 or 12
    ampm = "AM" if w.hour < 12 else "PM"
    return f"{h}:{w.minute:02d} {ampm}" if w.minute else f"{h} {ampm}"


def stamp(when, with_tz=True):
    """'Thu 8 PM ET' style display stamp (safe for None)."""
    w = to_et(when)
    if w is None:
        return "-"
    return f"{w:%a} {_hm(w)} ET" if with_tz else f"{w:%a} {_hm(w)}"


def day_hm(when):
    """'Thu 8 PM' (no ET suffix) - frame labels."""
    w = to_et(when)
    return f"{w:%a} {_hm(w)}" if w else "-"


def full(when):
    """'2026-09-10 8 PM ET' - cycle/valid stamps."""
    w = to_et(when)
    return f"{w:%Y-%m-%d} {_hm(w)} ET" if w else "-"


def full_day(when):
    """'Thu Sep 10, 8 PM ET'."""
    w = to_et(when)
    return f"{w:%a %b %d}, {_hm(w)} ET" if w else "-"


def iso_local(when):
    """'2026-09-10 20:00 ET' - compact, table-friendly (24 h + ET suffix)."""
    w = to_et(when)
    return f"{w:%Y-%m-%d %H:%M} ET" if w else "-"


def hm(when):
    """'7:55 PM' - clock-only stamp for observation times."""
    w = to_et(when)
    return _hm(w) if w else "-"


def iso_z(iso_s):
    """ISO-8601 UTC string ('2026-09-11T01:30:00Z') -> '2026-09-11 1:30 PM ET'.
    Returns the input unchanged when it is not parseable (already-converted
    strings, empty values, or odd formats) so callers can pipe anything."""
    if not iso_s:
        return ""
    try:
        return full(dt.datetime.fromisoformat(str(iso_s).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return str(iso_s)


def produced():
    """'Produced Thu Sep 25, 6:20 AM ET' - footer stamp for rendered
    graphics (storm cards, season graphic, model maps): when THIS image
    was drawn, not when the underlying data is valid."""
    return "Produced " + full_day(dt.datetime.now(dt.timezone.utc))

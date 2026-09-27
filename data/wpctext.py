"""WPC forecast discussion texts, parsed from free raw-text files.

WPC's /discussions/ directory ships clean plain-text bodies (*body.txt,
medrero_discussion.txt) - no HTML scraping needed. Products shipped here:

- pmdspdbody.txt        Short Range Forecast Discussion (Days 1-2; the desk's
                        surface/QPF/severe narrative - WPC folded the old
                        standalone QPF discussion into this one in 2018)
- qpferdbody.txt        Excessive Rainfall Discussion (Days 1-2 combined)
- qpferd3body.txt       ERO Day 3
- qpferd4-5body.txt     ERO Days 4-5
- qpfhsdbody.txt        Probabilistic Heavy Snow & Icing Discussion

(Other files on the index - medrero, hazards, pmdhmd - are RETIRED
products kept online for years; a staleness guard drops anything whose
issue stamp is older than 3 days so a future retirement can't ship a
stale card as current.)

Each product is parsed into {title, issued, valid, highlights[], body,
 forecaster}: `highlights` are the ALL-CAPS ...framed... summary bullets,
 `body` the remaining discussion paragraphs. Never raises; a dead feed
 simply drops out of the payload. Cached ~30 min (desk cadence).
"""
import datetime as dt
import re
import threading
import time

import requests

from data import _tz

BASE = "https://www.wpc.ncep.noaa.gov/discussions/"
UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}
CACHE = {"t": 0.0, "b": None}
CACHE_LOCK = threading.Lock()
TTL = 30 * 60

PRODUCTS = [
    ("shortrange", "pmdspdbody.txt"),
    ("ero", "qpferdbody.txt"),
    ("eroDay3", "qpferd3body.txt"),
    ("eroDay45", "qpferd4-5body.txt"),
    ("snowicing", "qpfhsdbody.txt"),
]
_MAX_AGE_DAYS = 3.0      # staleness guard against retired products

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"))}


def _get(url, timeout=25):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.text


def _issued_dt(line):
    """'859 PM EDT Sat Sep 26 2026' -> aware UTC datetime (best effort).

    WPC times run to 3 digits ('859 PM'): the last two digits are minutes,
    the leading 1-2 the hour.
    """
    try:
        m = re.match(r"(\d{1,2})(\d{2})\s*(AM|PM)\s*(EDT|EST|CDT|CST|MDT|MST|PDT|PST)\s+"
                     r"\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{4})", line.strip())
        if not m:
            return None
        hh, mm, ampm, tzname, mon, day, yr = m.groups()
        hh = int(hh) % 12 + (12 if ampm.upper() == "PM" else 0)
        t = dt.datetime(int(yr), _MONTHS[mon.lower()], int(day), hh, int(mm),
                        tzinfo=dt.timezone.utc)
        off = 5 if tzname.endswith(("EST", "CST", "MST", "PST")) else 4
        return t - dt.timedelta(hours=off)
    except (ValueError, KeyError):
        return None


def _paragraphs(text):
    """Split on blank lines, strip hard-wraps, drop MOF/KIWI junk."""
    paras = []
    for blk in re.split(r"\n\s*\n", text):
        p = re.sub(r"\s+", " ", blk).strip()
        if p:
            paras.append(p)
    return paras


def _parse(txt):
    """Raw product text -> structured card.

    Products are blank-line-separated blocks; a block that starts with
    '...' and is mostly capitals is a highlight bullet (they hard-wrap,
    so the whole block is joined before testing). Everything else is body.
    """
    t = txt.replace("\r", "")
    # some bodies ship trailing HTML (Graphics available at <a href=...>) -
    # strip tags so the boilerplate can be detected and dropped below
    t = re.sub(r"<[^>]+>", " ", t)
    lines_all = [l.strip() for l in t.splitlines() if l.strip()]
    if not lines_all:
        return None
    title = lines_all[0]

    issued = valid = ""
    issued_dt = None
    highlights, body = [], []
    for blk in re.split(r"\n\s*\n", t):
        lines = [l.strip() for l in blk.splitlines() if l.strip()]
        if not lines:
            continue
        content = []
        for l in lines:
            low = l.lower()
            if not issued:
                idt = _issued_dt(l)
                if idt is not None:
                    issued_dt = idt
                    issued = _tz.stamp(idt)
                    continue
            if not valid and low.startswith("valid "):
                valid = l
                continue
            if low in ("&&", "$$") or l == title \
                    or l.startswith("NWS Weather Prediction"):
                continue
            content.append(l)
        if not content:
            continue
        joined = re.sub(r"\s+", " ", " ".join(content)).strip()
        # graphics-notes / URL-only trailer blocks are boilerplate
        if joined.startswith("Graphics available") or re.match(
                r"^(?:https?://|www\.)\S+$", joined):
            continue
        if joined.startswith("..."):
            core = joined.strip(".").strip()
            letters = [c for c in core if c.isalpha()]
            caps = (sum(c.isupper() for c in letters) / max(1, len(letters))) \
                if letters else 0.0
            if caps > 0.65 and len(core) > 15:
                highlights.append(core)
                continue
        body.append(joined)

    # trailing one-liner body block = forecaster signature
    forecaster = ""
    if body and len(body[-1].split()) <= 4:
        forecaster = body.pop()
    if not body and not highlights:
        return None
    return {
        "title": title,
        "issued": issued,
        "issuedEpoch": issued_dt.timestamp() if issued_dt else 0.0,
        "valid": valid,
        "highlights": highlights[:4],
        "body": body[:8],
        "forecaster": forecaster,
    }


def fetch_all():
    """Every reachable discussion, parsed; {key: product-dict}."""
    out = {}
    for key, fn in PRODUCTS:
        try:
            txt = _get(BASE + fn)
            if txt and len(txt) > 80:
                p = _parse(txt)
                if p:
                    p["source"] = fn
                    out[key] = p
        except Exception:  # noqa: BLE001 - one dead feed never blocks the rest
            continue
    return out


def bundle(max_age=TTL):
    """Payload for the national page: parsed discussions + stamp."""
    with CACHE_LOCK:
        now = time.time()
        if CACHE["b"] is not None and now - CACHE["t"] < max_age:
            return CACHE["b"]
    try:
        products = fetch_all()
    except Exception:  # noqa: BLE001 - never break a build
        products = {}
    # staleness guard: drop retired products (the index keeps years-old
    # files online - medrero/hazards lessons) and any undated text
    cutoff = time.time() - _MAX_AGE_DAYS * 86400
    products = {k: p for k, p in products.items()
                if p.get("issuedEpoch", 0.0) >= cutoff}
    b = {
        "generated": _tz.stamp(dt.datetime.now(dt.timezone.utc)),
        "products": products,
        "count": len(products),
        "source": "NWS WPC forecast discussions (free raw-text feed)",
    }
    with CACHE_LOCK:
        CACHE.update(t=time.time(), b=b)
    return b

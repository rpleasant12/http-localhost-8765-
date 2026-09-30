"""CFSv2 (NCEP Climate Forecast System v2) long-range maps: mirrored.

Sources (free, keyless, verified 2026-09-29):

- CPC CFSv2 WEEKLY climate forecasts (cpc.ncep.noaa.gov/products/CFSv2/weekly/)
  four fixed-name PNGs (800x1000), updated with each model cycle:
    wk1.wk2_latest.NAprec / NAsfcT  - North America weeks 1-2
    wk3.wk4_latest.NAprec / NAsfcT  - North America weeks 3-4
- CPC CFSv2 MONTHLY climate forecasts (../monthly/): four GIFs (1100x850)
  tagged with the forecast month (summaryCFSv2.NaT2m.<YYYYMM>.gif etc.).
  The month tag rotates monthly, so the module discovers the current tag
  from the monthly page HTML and falls back to trying the next month - a
  failed month fetch degrades that one image, never the page.

Mirroring (not hot-linking) follows the site rule for external graphics:
the site serves its own copy, survives source cache-busting, and renders
offline. Fails soft: a missing image just drops from the payload.
"""
import datetime as dt
import json
import os
import re
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
OUT_DIR = os.path.join("static", "cfsv2")
CACHE = {"t": 0.0, "b": None}
CACHE_LOCK = __import__("threading").Lock()
CACHE_FILE = os.path.join("static", "cfsv2_meta.json")

CPC = "https://www.cpc.ncep.noaa.gov/products/CFSv2"
WEEKLY = f"{CPC}/weekly/images"
MONTHLY = f"{CPC}/monthly/images"

WEEKLY_IMGS = [
    ("wk12_prec", f"{WEEKLY}/wk1.wk2_latest.NAprec.png",
     "Weeks 1-2 precipitation",
     "North America precipitation anomaly, model weeks 1-2. Blues = wet "
     "signal, browns = dry. This is the rain CFSv2 expects in days 1-14."),
    ("wk12_t2m", f"{WEEKLY}/wk1.wk2_latest.NAsfcT.png",
     "Weeks 1-2 temperature",
     "North America surface-temperature anomaly, model weeks 1-2. This is "
     "the temperature CFSv2 expects in days 1-14 - same style as the CPC "
     "6-10/8-14 outlooks, but straight from the model."),
    ("wk34_prec", f"{WEEKLY}/wk3.wk4_latest.NAprec.png",
     "Weeks 3-4 precipitation",
     "North America precipitation anomaly, model weeks 3-4 (days ~15-28)."),
    ("wk34_t2m", f"{WEEKLY}/wk3.wk4_latest.NAsfcT.png",
     "Weeks 3-4 temperature",
     "North America surface-temperature anomaly, model weeks 3-4."),
]

MONTHLY_KINDS = [
    ("mo_t2m", "summaryCFSv2.NaT2m.{tag}.gif",
     "Monthly temperature anomaly"),
    ("mo_t2m_prob", "summaryCFSv2.NaT2mProb.{tag}.gif",
     "Monthly temperature probability"),
    ("mo_prec", "summaryCFSv2.NaPrec.{tag}.gif",
     "Monthly precipitation anomaly"),
    ("mo_prec_prob", "summaryCFSv2.NaPrecProb.{tag}.gif",
     "Monthly precipitation probability"),
]


def _fetch(url):
    try:
        r = requests.get(url, headers=UA, timeout=45)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "")
        if "html" in ct.lower() or len(r.content) < 400:
            return None
        return r.content
    except Exception:                                # noqa: BLE001
        return None


def _current_month_tag():
    now = dt.datetime.now(dt.timezone.utc)
    return f"{now.year}{now.month:02d}"


def _stamp():
    return dt.datetime.now(dt.timezone.utc).astimezone() \
        .strftime("%a %b %d, %I:%M %p ET")


def _save(fn, blob):
    tmp = os.path.join(OUT_DIR, fn + ".tmp")
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, os.path.join(OUT_DIR, fn))


def _discover_month_tag():
    """Scrape the monthly page for the current month tag; fall back to
    this month / next month by date arithmetic (the tag rotates monthly)."""
    try:
        r = requests.get(f"{CPC}/monthly/", headers=UA, timeout=30)
        tags = re.findall(r"summaryCFSv2\.[A-Za-z0-9]+\.(\d{6})\.gif",
                          r.text)
        if tags:
            return max(tags)                # latest month present on the page
    except Exception:                       # noqa: BLE001
        pass
    now = dt.datetime.now(dt.timezone.utc)
    nxt = dt.datetime(now.year + (now.month == 12),
                      now.month % 12 + 1, 1, tzinfo=dt.timezone.utc)
    return f"{nxt.year}{nxt.month:02d}"


def refresh(force=False):
    """Mirror current CFSv2 weekly + monthly maps; build the payload."""
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
    except OSError:
        pass
    with CACHE_LOCK:
        if not force and CACHE["b"] and time.time() - CACHE["t"] < 3600:
            return CACHE["b"]

        weekly = []
        for key, url, label, caption in WEEKLY_IMGS:
            fn = f"{key}.png"
            blob = _fetch(url)
            if blob is None:
                # keep the previous copy on disk if we have one
                if os.path.isfile(os.path.join(OUT_DIR, fn)):
                    blob = open(os.path.join(OUT_DIR, fn), "rb").read()
            if blob is None:
                continue
            if not os.path.isfile(os.path.join(OUT_DIR, fn)) or \
                    os.path.getsize(os.path.join(OUT_DIR, fn)) != len(blob):
                _save(fn, blob)
            weekly.append({"key": key, "label": label, "caption": caption,
                           "url": f"/app/static/cfsv2/{fn}"})

        monthly = []
        tag = _discover_month_tag()
        try:
            month_lbl = dt.datetime.strptime(tag, "%Y%m").strftime("%B %Y")
        except ValueError:
            month_lbl = tag
        for key, pat, label in MONTHLY_KINDS:
            fn = f"{key}.gif"
            url = f"{MONTHLY}/{pat.format(tag=tag)}"
            blob = _fetch(url)
            if blob is None and os.path.isfile(os.path.join(OUT_DIR, fn)):
                blob = open(os.path.join(OUT_DIR, fn), "rb").read()
            if blob is None:
                continue
            if not os.path.isfile(os.path.join(OUT_DIR, fn)) or \
                    os.path.getsize(os.path.join(OUT_DIR, fn)) != len(blob):
                _save(fn, blob)
            monthly.append({"key": key, "label": f"{label} - {month_lbl}",
                            "url": f"/app/static/cfsv2/{fn}"})

        payload = {
            "ok": bool(weekly or monthly),
            "updated": _stamp(),
            "month": month_lbl,
            "weekly": weekly,
            "monthly": monthly,
            "note": ("CFSv2 is NOAA's coupled ocean-atmosphere model run "
                     "4x daily to ~9 months. Weekly panels are North "
                     "America means/anomalies for model weeks 1-4; monthly "
                     "panels are ensemble-mean anomalies + probability "
                     "plumes for the calendar month. Skill decays with "
                     "lead - treat week 3-4 and monthly as trends, not "
                     "weather.")}
        try:
            with open(CACHE_FILE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(CACHE_FILE + ".tmp", CACHE_FILE)
        except OSError:
            pass
        CACHE["t"] = time.time()
        CACHE["b"] = payload
        return payload


if __name__ == "__main__":
    print(json.dumps(refresh(force=True), indent=1)[:1200])

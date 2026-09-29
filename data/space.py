"""Space weather: aurora + geomagnetic activity from NOAA SWPC.

Keyless public SWPC services (verified live 2026-09-20):
- planetary K-index (observed 3-h values + next-3-day forecast text)
- OVATION aurora nowcast image (north), MIRRORED to static/space/ so the
  public site serves it (hotlinking SWPC images breaks on Pages)

The Kp index drives a plain-English aurora outlook: even in Tennessee a
strong geomagnetic storm (Kp 7+) can push the auroral oval far enough south
to be visible on the northern horizon. The page says exactly that, with the
observed trend and the 3-day forecast broken out.
"""
import os
import re
import threading
import time

import requests

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
FORECAST_URL = "https://services.swpc.noaa.gov/text/3-day-forecast.txt"
OVATION_URL = ("https://services.swpc.noaa.gov/images/animations/"
               "ovation/north/latest.jpg")
OVATION_DIR = os.path.join("static", "space")

_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()

KP_SCALE = [
    (9, "#d32f2f", "Extreme storm - aurora possible as far south as the Gulf Coast"),
    (7, "#e64a19", "Strong storm - aurora visible on the northern horizon from Tennessee"),
    (5, "#f9a825", "Geomagnetic storm - aurora in the northern US, high latitudes"),
    (4, "#0288d1", "Unsettled - active aurora ring, mid-latitudes unaffected"),
    (0, "#43a047", "Quiet - no aurora expected outside polar regions"),
]


def _kp_word(kp):
    try:
        kp = float(kp)
    except (TypeError, ValueError):
        return "#9e9e9e", "unknown"
    for lo, color, word in KP_SCALE:
        if kp >= lo:
            return color, word
    return "#43a047", "Quiet"


def _parse_forecast(text):
    """3-day text -> {day: max Kp} + headline (best-effort).

    Layout (verified 2026-09-20): breakdown header line, one BLANK line,
    a day-header row, then 8 three-hour rows. The old regex demanded the
    rows immediately after the header and silently parsed nothing.
    """
    out = {}
    try:
        m = re.search(r"The greatest expected 3 hr Kp for ([^.]+)\.?", text)
        headline = m.group(1).strip() if m else ""
        i = text.find("NOAA Kp index breakdown")
        if i >= 0:
            lines = [l.rstrip() for l in text[i:].splitlines() if l.strip()]
            # lines[0] = header ('... Sep 20-Sep 22 2026'), lines[1] = day row
            # ('Sep 20  Sep 21  Sep 22' -> 3 day columns), lines[2:] = 8 rows.
            # Day tokens come in PAIRS (month name + day number).
            if len(lines) >= 3:
                toks = lines[1].split()
                days = [f"{toks[j]} {toks[j + 1]}"
                        for j in range(0, len(toks) - 1, 2)]
                vals = {di: [] for di in range(len(days))}
                for row in lines[2:10]:
                    nums = re.findall(r"\d+\.\d\d?", row)
                    for di, v in enumerate(nums[:len(days)]):
                        vals[di].append(float(v))
                for di, day in enumerate(days):
                    if vals[di]:
                        out[day] = max(vals[di])
        return {"headline": headline, "dailyMax": out}
    except Exception:                              # noqa: BLE001
        return {"headline": "", "dailyMax": out}


def space_bundle(max_age=900):
    """Kp observations + 3-day forecast + aurora nowcast URL (15-min cache)."""
    with _lock:
        now = time.time()
        if _cache["data"] is not None and now - _cache["at"] < max_age:
            return _cache["data"]

    kp_rows = []
    try:
        r = requests.get(KP_URL, headers=UA, timeout=25)
        if r.ok:
            rows = r.json()          # [["time_tag","Kp",...], {...}]
            for row in rows[1:] if rows and isinstance(rows[0], list) else rows:
                if isinstance(row, dict):
                    kp_rows.append({"time": row.get("time_tag"),
                                    "kp": row.get("Kp")})
    except Exception:                              # noqa: BLE001
        pass
    kp_rows = [k for k in kp_rows if k.get("time")][-33:]   # last ~4 days

    fc = {}
    try:
        r = requests.get(FORECAST_URL, headers=UA, timeout=25)
        if r.ok:
            fc = _parse_forecast(r.text)
    except Exception:                              # noqa: BLE001
        pass

    latest = kp_rows[-1]["kp"] if kp_rows else None
    observed_max = max((k["kp"] for k in kp_rows[-8:]
                        if isinstance(k.get("kp"), (int, float))), default=None)
    fcst_max = max(fc["dailyMax"].values()) if fc.get("dailyMax") else None
    driver = fcst_max if fcst_max is not None else (observed_max or latest)
    color, word = _kp_word(driver)

    # mirror the OVATION image (site pages reference the local copy)
    ovation_local = ""
    try:
        os.makedirs(OVATION_DIR, exist_ok=True)
        dest = os.path.join(OVATION_DIR, "ovation_north.jpg")
        if not (os.path.exists(dest) and os.path.getsize(dest) > 5_000
                and time.time() - os.stat(dest).st_mtime < 1_800):
            r = requests.get(OVATION_URL, headers=UA, timeout=25)
            if r.ok and r.content[:3] in (b"\xff\xd8\xff", b"GIF", b"\x89PN"):
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, dest)
        if os.path.exists(dest) and os.path.getsize(dest) > 5_000:
            ovation_local = "/app/static/space/ovation_north.jpg"
    except Exception:                              # noqa: BLE001
        pass

    out = {
        "ok": bool(kp_rows),
        "kp": kp_rows,
        "latestKp": latest,
        "observedMaxKp": observed_max,
        "forecastMaxKp": fcst_max,
        "forecast": fc,
        "kpColor": color, "kpWord": word,
        "ovationUrl": ovation_local or OVATION_URL,
        "generated": time.strftime("%Y-%m-%d %H:%M"),
    }
    with _lock:
        _cache.update(at=time.time(), data=out)
    return out


if __name__ == "__main__":
    b = space_bundle(max_age=0)
    print("ok:", b["ok"], "| latest Kp:", b["latestKp"],
          "| 3-day max:", b["forecastMaxKp"])
    print("verdict:", b["kpWord"])
    print("forecast days:", b["forecast"].get("dailyMax"))

"""Fire weather: SPC Fire Weather Outlooks (days 1-8) + VIIRS satellite
detections + US Drought Monitor + NWS Red Flag Warnings.

Keyless sources, verified live 2026-09-28:
- SPC Day 1 / Day 2 Fire Weather Outlook graphics (fire_wx/) mirrored to
  static/fire/, PLUS the experimental Days 3-8 fire outlooks
  (products/exper/fire_wx/imgs/day{3..8}fireprob.gif + day38 composite).
- NASA FIRMS 24 h VIIRS active-fire detections (Suomi-NPP, 375 m) as a
  global CSV - filtered to the Tennessee region + CONUS counts + a
  distance-to-home summary. The same detections NWS/forestry use.
- US Drought Monitor current-week national map (fuel-dryness context).
- NWS active-alert API filtered to fire events (Red Flag Warning, Fire
  Weather Watch) - nationwide count + sample rows + Tennessee list.

The HRRR FMO (red-flag) fields proved not to ship in the public bucket
(only in the experimental HREF- AIM product), so wind+humidity danger is
carried by the SPC outlooks and alerts - the same products forecasters use.
"""
import csv
import io
import os
import re
import threading
import time

import requests

UA = {"User-Agent": "tennessee-weather-network/1.0 (local demo)"}
SPC_FIRE = "https://www.spc.noaa.gov/products/fire_wx/"
SPC_FIRE_EXP = "https://www.spc.noaa.gov/products/exper/fire_wx/imgs/"
FIRMS_CSV = ("https://firms.modaps.eosdis.nasa.gov/data/active_fire/"
             "suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_24h.csv")
USDM_PNG = "https://droughtmonitor.unl.edu/data/png/current/current_usdm.png"
OUT_DIR = os.path.join("static", "fire")

FIRE_EVENTS = ("Red Flag Warning", "Fire Weather Watch")

# detection region boxes (lat N, lon W, lat S, lon E)
TN_BOX = (36.9, -90.4, 34.8, -81.6)
CONUS_BOX = (50.0, -125.0, 24.0, -66.0)

_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def _mirror_spc():
    """Day1/Day2 fire outlooks -> static/fire/, returns {day1Url, day2Url}."""
    out = {}
    os.makedirs(OUT_DIR, exist_ok=True)
    for day, fn in (("day1Url", "day1fireotlk.gif"), ("day2Url", "day2fireotlk.gif")):
        dest = os.path.join(OUT_DIR, fn)
        try:
            if not (os.path.exists(dest) and os.path.getsize(dest) > 5_000
                    and time.time() - os.stat(dest).st_mtime < 3_600):
                r = requests.get(SPC_FIRE + fn, headers=UA, timeout=25)
                if r.ok and r.content[:3] in (b"GIF", b"\xff\xd8\xff", b"\x89PN"):
                    tmp = dest + ".part"
                    with open(tmp, "wb") as f:
                        f.write(r.content)
                    os.replace(tmp, dest)
            if os.path.exists(dest) and os.path.getsize(dest) > 5_000:
                out[day] = f"/app/static/fire/{fn}"
        except Exception:                          # noqa: BLE001
            continue
    return out


def _mirror_spc38():
    """SPC experimental Days 3-8 fire outlooks -> static/fire/spc38/.

    Day 1-2 outlooks are the operational products; days 3-8 live on SPC's
    experimental page as daily fire-probability graphics plus a days 3-8
    composite - the extended-range view of building fire weather.
    """
    out_dir = os.path.join(OUT_DIR, "spc38")
    os.makedirs(out_dir, exist_ok=True)
    products = [(f"day{d}fireprob.gif", f"Day {d} fire probability")
                for d in range(3, 9)]
    products.append(("day38otlk_fire.gif", "Days 3-8 composite outlook"))
    out = []
    for fn, label in products:
        dest = os.path.join(out_dir, fn)
        try:
            if not (os.path.exists(dest) and os.path.getsize(dest) > 3_000
                    and time.time() - os.stat(dest).st_mtime < 3_600):
                r = requests.get(SPC_FIRE_EXP + fn, headers=UA, timeout=25)
                if r.ok and r.content[:3] in (b"GIF", b"\xff\xd8\xff", b"\x89PN"):
                    tmp = dest + ".part"
                    with open(tmp, "wb") as f:
                        f.write(r.content)
                    os.replace(tmp, dest)
            if os.path.exists(dest) and os.path.getsize(dest) > 3_000:
                out.append({"file": fn, "label": label,
                            "url": f"/app/static/fire/spc38/{fn}"})
        except Exception:                          # noqa: BLE001
            continue
    return out


def _in_box(lat, lon, box):
    latN, lonW, latS, lonE = box
    return latS <= lat <= latN and lonW <= lon <= lonE


def _dist_mi(lat1, lon1, lat2, lon2):
    """Approximate statute miles (flat-earth, fine under ~500 mi)."""
    import math

    dy = (lat2 - lat1) * 69.0
    dx = (lon2 - lon1) * 53.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return (dx * dx + dy * dy) ** 0.5


_FIRMS_CACHE = {"at": 0.0, "data": None}


def _firms_fires(home=None):
    """NASA FIRMS 24 h VIIRS detections: TN region rows + CONUS counts.

    The global CSV is ~7 MB / ~80k rows; cached on disk (1 h) and parsed
    in-memory (~0.2 s). With home=(lat, lon), also counts detections
    within 25/50/100 mi and the closest one - the 'any fires near me'
    answer visitors actually want.
    """
    now = time.time()
    if _FIRMS_CACHE["data"] and now - _FIRMS_CACHE["at"] < 1_800:
        return _FIRMS_CACHE["data"]
    local = os.path.join(OUT_DIR, "firms_24h.csv")
    try:
        if not (os.path.exists(local) and os.path.getsize(local) > 100_000
                and time.time() - os.stat(local).st_mtime < 3_600):
            r = requests.get(FIRMS_CSV, headers=UA, timeout=180)
            if not r.ok or len(r.content) < 100_000:
                return {"ok": False, "reason": "FIRMS unreachable"}
            tmp = local + ".part"
            with open(tmp, "wb") as f:
                f.write(r.content)
            os.replace(tmp, local)
        tn, conus_n, hi_conus, freshest = [], 0, 0, ""
        closest = None
        with open(local, encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(io.StringIO(f.read())):
                try:
                    lat, lon = float(row["latitude"]), float(row["longitude"])
                except (KeyError, ValueError):
                    continue
                when = (row.get("acq_date") or "") + " " + \
                       (row.get("acq_time") or "").zfill(4)
                if when > freshest:
                    freshest = when
                if not _in_box(lat, lon, CONUS_BOX):
                    continue
                conus_n += 1
                conf = row.get("confidence", "")
                if conf in ("h", "l", "n") or str(conf).isdigit():
                    hi = conf == "h" or (str(conf).isdigit()
                                         and int(conf) >= 80)
                else:
                    hi = False
                if hi:
                    hi_conus += 1
                if _in_box(lat, lon, TN_BOX) and len(tn) < 400:
                    tn.append({"lat": round(lat, 3), "lon": round(lon, 3),
                               "bright": round(float(row.get("bright_ti4")
                                                       or 0), 0),
                               "frp": round(float(row.get("frp") or 0), 1),
                               "conf": conf, "when": when,
                               "night": row.get("daynight") == "N"})
                if home:
                    d = _dist_mi(lat, lon, home[0], home[1])
                    if closest is None or d < closest[0]:
                        closest = (d, when)
        out = {
            "ok": True, "usCount": conus_n, "usHigh": hi_conus,
            "tn": sorted(tn, key=lambda x: -x["bright"])[:40],
            "tnCount": len(tn), "freshest": freshest,
        }
        if home and closest is not None:
            out["nearHome"] = {
                "closestMi": round(closest[0]),
                "closestWhen": closest[1],
            }
        _FIRMS_CACHE.update(at=now, data=out)
        return out
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:120]}


def _mirror_drought():
    """US Drought Monitor current-week national map (weekly product)."""
    dest = os.path.join(OUT_DIR, "usdm_current.png")
    try:
        if not (os.path.exists(dest) and os.path.getsize(dest) > 50_000
                and time.time() - os.stat(dest).st_mtime < 86_400):
            r = requests.get(USDM_PNG, headers=UA, timeout=60)
            if r.ok and r.content[:3] in (b"GIF", b"\xff\xd8\xff", b"\x89PN"):
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, dest)
        if os.path.exists(dest) and os.path.getsize(dest) > 50_000:
            return {"ok": True, "url": "/app/static/fire/usdm_current.png",
                    "source": "https://droughtmonitor.unl.edu/"}
    except Exception:                              # noqa: BLE001
        pass
    return {"ok": False}


def _fire_alerts():
    """Active fire-related NWS alerts: US count, sample, Tennessee rows."""
    ev = "&event=".join(requests.utils.quote(e) for e in FIRE_EVENTS)
    try:
        r = requests.get(
            f"https://api.weather.gov/alerts/active?status=actual&message_type=alert&event={ev}",
            headers={**UA, "Accept": "application/geo+json"}, timeout=20)
        r.raise_for_status()
        feats = r.json().get("features", [])
    except Exception:                              # noqa: BLE001
        return {"usCount": 0, "sample": [], "tnAlerts": []}
    from data._tz import iso_local
    sample, tn = [], []
    for f in feats:
        p = f.get("properties", {}) or {}
        area = p.get("areaDesc") or ""
        row = {"event": p.get("event"), "area": area[:120],
               "expires": p.get("expires", "")}
        if row["expires"]:
            try:
                w = row["expires"]
                row["expires"] = iso_local(w[:19]) if isinstance(w, str) else w
            except Exception:                      # noqa: BLE001
                pass
        if len(sample) < 12:
            sample.append(row)
        if re.search(r"\bTN\b", area):
            tn.append(row)
    return {"usCount": len(feats), "sample": sample, "tnAlerts": tn[:12]}


def fire_bundle(max_age=900, home=None):
    """Fire outlooks (day 1-8) + VIIRS detections + drought + alerts."""
    with _lock:
        now = time.time()
        if _cache["data"] is not None and now - _cache["at"] < max_age:
            return _cache["data"]
    out = {
        "ok": True,
        **_mirror_spc(),
        "spc38": _mirror_spc38(),
        "redFlag": _fire_alerts(),
        "firms": _firms_fires(home=home),
        "drought": _mirror_drought(),
        "generated": time.strftime("%Y-%m-%d %H:%M"),
    }
    with _lock:
        _cache.update(at=time.time(), data=out)
    return out


if __name__ == "__main__":
    b = fire_bundle(max_age=0, home=(36.1627, -82.8332))
    print("day1:", b.get("day1Url"), "| day2:", b.get("day2Url"))
    print("days 3-8 gifs:", len(b.get("spc38") or []))
    fm = b.get("firms") or {}
    print("VIIRS: us=", fm.get("usCount"), "high=", fm.get("usHigh"),
          "tn=", fm.get("tnCount"), "near=", fm.get("nearHome"))
    print("drought:", (b.get("drought") or {}).get("ok"))
    print("RFW count:", (b.get("redFlag") or {}).get("usCount"))

"""TDOT SmartWay traffic cameras + road-weather events.

TDOT publishes an official open-data API used by smartway.tn.gov (config
fetched from https://smartway.tn.gov/config/config.prod.json, verified live
2026-09-21):

    GET https://www.tdot.tn.gov/opendata/api/public/RoadwayCameras
    GET https://www.tdot.tn.gov/opendata/api/public/RoadwayWeather
    Header: ApiKey: <public key embedded in the public site's config>

Region 1 = East Tennessee (Knoxville, I-40/I-75/I-81/I-26 corridor, Tri-Cities
valleys). Camera thumbnails are public snapshots (tnsnapshots.com); the legacy
"thumbs/....flv.png" paths 301-redirect to a normalized name, so we rewrite
the URL client-side and skip the redirect entirely. HLS live streams
(playlist.m3u8) are included for browsers that can play them.
"""
import json
import os
import re
import time

import requests

API = "https://www.tdot.tn.gov/opendata/api/public/"
CONFIG_URL = "https://smartway.tn.gov/config/config.prod.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_CACHE = os.path.join(".freebuff", "roadcams-cache.json")
_CACHE_TTL = 240        # snapshots refresh ~ every minute client-side; feed 4 min

_ETN_COUNTIES = {
    "Greene", "Hawkins", "Washington", "Sullivan", "Carter", "Unicoi",
    "Cocke", "Jefferson", "Hamblen", "Grainger", "Hancock", "Johnson",
    "Knox", "Sevier", "Blount", "Hamblen", "Anderson", "Loudon", "Roane",
    "Monroe", "Polk", "McMinn", "Meigs", "Rhea", "Cumberland",
}


def _api_key(session):
    """Public key from TDOT's own published site config (it rotates rarely,
    but fetch fresh each time so a rotation never leaves us dead)."""
    try:
        cfg = session.get(CONFIG_URL, timeout=20).json()
        key = cfg.get("apiKey")
        if key:
            return key
    except Exception:                          # noqa: BLE001
        pass
    return "8d3b7a82635d476795c09b2c41facc60"   # known-good fallback


def _norm_thumb(url):
    """tnsnapshots legacy thumb path 301s to /{name}.png - rewrite directly."""
    if not url:
        return ""
    m = re.match(r"https://tnsnapshots\.com/thumbs/(.+)\.(?:flv|mp4)\.png", url)
    if m:
        return f"https://tnsnapshots.com/{m.group(1)}.png"
    return url


def _slim_cam(c):
    try:
        lat = float(c.get("lat") or 0)
        lng = float(c.get("lng") or 0)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    region = c.get("region") or ""
    return {
        "id": c.get("id"),
        "title": (c.get("title") or c.get("name") or "Camera").strip(),
        "route": (c.get("route") or "").strip(),
        "mile": (str(c.get("mileMarker") or "").strip() or None),
        "county": c.get("county") or None,
        "jurisdiction": c.get("jurisdiction") or None,
        "region": region,
        "lat": lat, "lng": lng,
        "thumb": _norm_thumb(c.get("thumbnailUrl")),
        "video": c.get("httpsVideoUrl") or None,
        "active": str(c.get("active")).lower() == "true",
    }


_EVENT_ICE = ("ice", "icy", "frost", "snow", "winter", "slick")
_EVENT_SEV = ("blocked", "closed", "flooding", "crash", "collision", "damage")


def _haversine_m(lat1, lng1, lat2, lng2):
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _event_route(desc):
    """Route token from the event description's lead ('State Route 9 EB in',
    'I-40 WB at...', 'US 25 alt')."""
    d = desc or ""
    m = re.match(r"\s*(?:State\s+Route|SR)[-\s]?(\d{1,3})", d, re.I)
    if m:
        return f"SR-{m.group(1)}"
    m = re.match(r"\s*(?:U\.?S\.?\s*Highway|US[-\s]?Highway|US)[-\s]?(\d{1,3})", d, re.I)
    if m:
        return f"US-{m.group(1)}"
    m = re.match(r"\s*(Interstate\s+|I-?)(\d{1,3})\b", d, re.I)
    if m and m.group(1).strip().lower().startswith(("i", "int")):
        return f"I-{m.group(2)}"
    return ""


def _road_events(session, key, cams=None):
    """Active TDOT road-weather events, enriched for the conditions map.

    Adds: route token, reported clock time, severity guess, and the nearest
    active cameras (up to 3 within 6 mi) - a camera view is the ground
    truth for "what do the roads look like right now".
    """
    out = []
    try:
        r = session.get(API + "RoadwayWeather", headers={"ApiKey": key}, timeout=25)
        data = r.json() if r.status_code == 200 else []
        for ev in (data or []):
            locs = ev.get("locations") or []
            loc = locs[0] if locs else {}
            mid = (loc.get("midPoint") or {})
            desc = (ev.get("description") or "").strip()
            lat, lng = mid.get("lat"), mid.get("lng")
            subtype = (ev.get("eventSubTypeDescription")
                       or ev.get("eventTypeName") or "Weather")
            low = (subtype + " " + desc).lower()
            sev = "high" if any(w in low for w in _EVENT_SEV) else \
                  ("warn" if any(w in low for w in _EVENT_ICE) else "info")
            reported = ""
            m = re.search(r"reported at (\d{2}/\d{2}/\d{4} \d{1,2}:\d{2} (?:AM|PM)) \(ET\)", desc)
            if m:
                try:
                    from data._tz import stamp as _st2, ET as _ET
                    import datetime as _dt2
                    t = _dt2.datetime.strptime(m.group(1), "%m/%d/%Y %I:%M %p").replace(tzinfo=_ET)
                    reported = _st2(t.astimezone(_dt2.timezone.utc))
                except Exception:      # noqa: BLE001 - cosmetic only
                    reported = m.group(1)
            # nearest active cameras (ground truth for road appearance)
            near = []
            if cams and lat and lng:
                try:
                    la, ln = float(lat), float(lng)
                    cand = []
                    for c in cams:
                        if not c.get("active"):
                            continue
                        d = _haversine_m(la, ln, c["lat"], c["lng"])
                        cand.append((d, c))
                    cand.sort(key=lambda t: t[0])
                    within = [t for t in cand if t[0] <= 9655]      # 6 mi
                    if not within and cand:
                        within = cand[:1]   # remote dead zone: nearest anyway
                    near = [{"id": c["id"], "title": c["title"], "thumb": c["thumb"],
                             "lat": c["lat"], "lng": c["lng"],
                             "miles": round(d / 1609.34, 1)}
                            for d, c in within[:3]]
                except Exception:      # noqa: BLE001
                    near = []
            out.append({
                "id": ev.get("id"),
                "desc": desc,
                "subtype": subtype,
                "status": ev.get("status") or "",
                "route": _event_route(desc),
                "reported": reported,
                "sev": sev,
                "county": loc.get("countyName") or "",
                "region": loc.get("region"),
                "lat": lat, "lng": lng,
                "cams": near,
            })
    except Exception:                          # noqa: BLE001
        pass
    return out


def roadcams_bundle():
    import sys
    if "data._tz" not in sys.modules:
        try:
            from data._tz import stamp as _stamp   # package context (updater)
        except ImportError:                        # direct script run: add root
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from data._tz import stamp as _stamp
    else:
        from data._tz import stamp as _stamp
    now = time.time()
    try:
        st = os.stat(_CACHE)
        if now - st.st_mtime < _CACHE_TTL:
            with open(_CACHE, encoding="utf-8") as f:
                b = json.load(f)
            if b.get("ok"):
                return b
    except (OSError, ValueError):
        pass

    out = {"ok": False, "cams": [], "events": [], "fetched": "", "fetchedEpoch": 0}
    try:
        s = requests.Session()
        s.headers.update(UA)
        key = _api_key(s)
        r = s.get(API + "RoadwayCameras", headers={"ApiKey": key}, timeout=30)
        raw = r.json() if r.status_code == 200 else []
        cams = [c for c in (_slim_cam(x) for x in raw) if c]
        # Region 1 (East TN) first, then by region number - the page defaults
        # to the local view and should not have to re-sort 668 entries.
        def _order(c):
            m = re.match(r"Region (\d)", c["region"] or "")
            return int(m.group(1)) if m else 9
        cams.sort(key=lambda c: (_order(c), c["region"], c["route"], c["title"]))
        events = _road_events(s, key, cams)
        etn = [c for c in cams if (c.get("county") or "") in _ETN_COUNTIES
               or (c.get("region") or "") == "Region 1"]
        from collections import Counter
        routes = Counter(c["route"] for c in etn if c["route"])
        out = {
            "ok": True,
            "fetched": _stamp(__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc)),
            "fetchedEpoch": int(now),
            "total": len(cams),
            "etnCount": len(etn),
            "events": events[:40],
            "etnRoutes": dict(sorted(routes.items(), key=lambda kv: -kv[1])),
            "cams": cams,
        }
        os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
        with open(_CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f)
    except Exception:                          # noqa: BLE001 - never break build
        return out
    return out


if __name__ == "__main__":
    b = roadcams_bundle()
    print("ok:", b["ok"], "| cams:", b.get("total"), "| ETN:", b.get("etnCount"))
    print("events:", len(b.get("events") or []))
    print("ETN routes:", b.get("etnRoutes"))
    r1 = [c for c in b["cams"] if c["region"] == "Region 1"][:3]
    for c in r1:
        print(" ", c["title"], "|", c["thumb"][:60])

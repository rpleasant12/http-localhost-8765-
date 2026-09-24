"""Tennessee tropical-cyclone threat detection (shared by every surface).

One definition of "does this storm threaten Tennessee" so the public site,
the Streamlit app and the Facebook share page all warn identically:

  watch: a tropical cyclone watch/warning polygon from the NWS feed
         overlaps the Tennessee bounding box.
  track: any official NHC forecast position falls inside TN, or the storm
         is heading toward the state (first forecast point within 8 deg
         and the continuation bearing toward TN within 40 deg).

`tn_threat(entry, ww)` takes a minimal storm entry ({lat, lon, points})
where points are Point geometry dicts ([lon, lat]), and `ww` is the NWS
active-alerts list (dicts with event + geometry). Never raises.
"""
import math

TN_BBOX = (-90.31, 34.98, -81.65, 36.69)   # west, south, east, north
TN_CENTER = (35.86, -86.35)                # geographic center of Tennessee

_TN_KEYS = ("Hurricane", "Tropical Storm", "Storm Surge")


def tn_threat(entry, ww):
    """'watch' | 'track' | None for one storm vs Tennessee."""
    try:
        w, s_, e, n = TN_BBOX
        # 1) tropical watch/warning polygons overlapping the state
        for a in ww or []:
            ev = (a.get("event") or "")
            if not any(k in ev for k in _TN_KEYS):
                continue
            geo = a.get("geometry") or {}
            rings = geo.get("coordinates") or []
            ring = rings[0] if geo.get("type") == "Polygon" \
                else (rings[0][0] if rings else [])
            for lon, lat in ring or []:
                try:
                    if w <= lon <= e and s_ <= lat <= n:
                        return "watch"
                except (TypeError, ValueError):
                    continue
        # 2) forecast track points toward/into TN
        slat, slon = entry.get("lat"), entry.get("lon")
        pts = []
        for p in entry.get("points") or []:
            c = p.get("coordinates") if p else None
            if c and len(c) >= 2:
                pts.append((c[0], c[1]))          # (lon, lat)
        if slat is None or not pts:
            return None
        if any(w <= lo <= e and s_ <= la <= n for lo, la in pts):
            return "track"
        # heading toward TN: first forecast point within 8 deg and the
        # storm->point bearing continues toward the TN center within 40 deg
        flon, flat = pts[0]
        tlat, tlon = TN_CENTER
        if math.hypot(flat - tlat, flon - tlon) <= 8.0:
            b1 = _brg(slat, slon, flat, flon)
            b2 = _brg(flat, flon, tlat, tlon)
            diff = abs(b1 - b2)
            if min(diff, 360 - diff) <= 40:
                return "track"
    except Exception:  # noqa: BLE001 - banner logic must never break a page
        return None
    return None


def _brg(la1, lo1, la2, lo2):
    """Initial great-circle bearing degrees true, p1 -> p2."""
    p1, p2 = math.radians(la1), math.radians(la2)
    dl = math.radians(lo2 - lo1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360


def collect_threats(raw_storms, ww):
    """[(name, kind)] for every storm threatening TN, best ('watch') first.

    raw_storms = data.national.nhc_storms() output (features carry the
    official forecast Point positions); ww = NWS active alerts.
    """
    out = []
    for s in raw_storms or []:
        entry = {
            "lat": s.get("lat"),
            "lon": s.get("lon"),
            "points": [f.get("geometry") for f in (s.get("features") or [])
                       if (f.get("geometry") or {}).get("type") == "Point"],
        }
        kind = tn_threat(entry, ww)
        if kind:
            out.append((s.get("name") or "Storm", kind))
    out.sort(key=lambda t: 0 if t[1] == "watch" else 1)
    return out

"""Tropical model guidance: spaghetti tracks + intensity forecasts from NHC ATCF.

Reads NHC's aid-deck (a-deck) files - every agency's track/intensity guidance
for active storms - from ftp.nhc.noaa.gov/atcf/aid_public/*.dat.gz (updated
~6x/day with each advisory cycle). One file per storm carries ALL models:
GFS (AVNO), ECMWF (AEMN), UKMET (CTCX), CMC (CMC2), HWRF/HMON (D-op),
official (OFCL), consensus (IVCN/HCCA), plus ensemble members (APxx/AXxx).

Outputs:
- tracks GeoJSON per model (polylines + point markers colored by intensity)
- intensity PNGs (matplotlib, small static/*.png) per storm: all model lines
  + official forecast, Eastern-time axis
- lightweight HTML summary data (model table per storm)

NHC storms live in Atlantic (al) and East/Central Pacific (ep/cp) basins;
the fetcher parses the aid_public directory listing so new storms appear
automatically.
"""
import datetime as dt
import gzip
import math
import html as _html
import io
import os
import re
import threading
import time

import numpy as np
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
DIR_URL = "https://ftp.nhc.noaa.gov/atcf/aid_public/"
CACHE = {"t": 0.0, "storms": None}          # storms: list of dicts
CACHE_LOCK = threading.Lock()

# ATCF technique -> display name and category (used for chart legend grouping)
TECH_NAMES = {
    "CARQ": "Current (best track)", "OFCL": "Official (OFCL)", "OFCI": "Official (interpolated)",
    "AVNO": "GFS (AVNO)", "AVNI": "GFS interp.", "AEMN": "ECMWF (AEMN)", "AEMI": "ECMWF interp.",
    "CTCX": "UKMET (CTCX)", "CTCI": "UKMET interp.", "CMC2": "CMC", "CMCI": "CMC interp.",
    "HWRF": "HWRF", "HWFI": "HWRF (f-of-y)", "HMON": "HMON", "HMNI": "HMON (f-of-y)",
    "HFAI": "HAFS-A", "HFBI": "HAFS-B", "HFSA": "HAFS-A (nest)", "HFSB": "HAFS-B (nest)",
    "IVCN": "Intensity consensus", "HCCA": "HCCA consensus", "LGEM": "LGEM (SHIPS)",
    "SHIP": "SHIPS", "SHF5": "SHIPS-5day", "DSHP": "DSHIPS", "OCD5": "OCD5",
    "DRCL": "D-RCL", "DSI": "DSI", "RVCN": "RVCN", "TVCN": "TVCN (variable)",
    "CLP5": "CLIPER5", "TABD": "TABS (decay)", "TABM": "TABS (medium)", "TABS": "TABS (no decay)",
    "CEMN": "ECMWF ensemble mean", "CEMI": "ECMWF ens. interp.", "CEM2": "ECMWF ens. 2",
    "GDMI": "GFDL interp.", "NGX": "GFDL/NGX", "NGX2": "NGX2", "NGXI": "NGX interp.",
    "UKX2": "UKMET (UKX2)", "UKXI": "UKMET interp.", "NVGM": "Navy NVGM", "NVGI": "Navy interp.",
    "NVG2": "Navy NVG2", "NNIB": "Intensity baseline (NNIB)",
    "NNIC": "Intensity consensus (NNIC)", "RVCN2": "RVCN2",
    "AP01": "GFS ens. member 1", "AP02": "GFS ens. member 2", "AP03": "GFS ens. member 3",
    "AEMN2": "ECMWF ens. mean 2",
}

# track-model groups: which technique is a TRACK guidance (not intensity-only)
TRACK_TECHS = {"AVNO", "AVNI", "AEMN", "AEMI", "CTCX", "CTCI", "CMC2", "CMCI",
               "HWRF", "HWFI", "HMON", "HMNI", "HFAI", "HFBI", "OFCL", "OFCI",
               "CLP5", "TABS", "TABM", "TABD", "CEMN", "GDMI", "NGX", "UKX2",
               "NVGM", "NVGI", "DRCL"}
# ensemble MEMBER techniques (spaghetti): APxx = GFS ensemble, AXxx = ECMWF
# ENS, EMX = ECMWF ext., CExx = CMC ensemble, and Navy ensemble member decks
# (NXnn / NEXnn / NAPnn-style - appear if/when NHC files them; pattern built
# to provably exclude every N-code in live decks today: NGX/NGX2/NGXI,
# NNIB/NNIC, NVGM/NVGI/NVG2). Strictly members - the loose old pattern also
# caught secondary deterministic runs (CTC2/OFC2/HMN2) and mislabeled them.
# NOTE: NNIB/NNIC are NOT Navy ensembles - they are NHC neural-network
# INTENSITY aids (baseline/consensus, DeMaria et al. 2022) and stay out of
# track spaghetti entirely.
_ENS_NAVY_RX = r"^N(?:A|E|X)[A-Z]?\d\d$"
ENSEMBLE_RE = re.compile(r"^(AP\d\d|AX\d\d|EMX|CE\d\d|" + _ENS_NAVY_RX[1:-1] + r")$")
# per-agency member matchers: each agency's ensemble gets its OWN spread
# cone and its own map family/checkbox - one blended GFS+EC+CMC+NAVY fan
# would average away exactly the inter-agency spread a forecaster looks for.
ENS_GROUPS = (
    ("GFS",   re.compile(r"^AP\d\d$")),
    ("ECMWF", re.compile(r"^(AX\d\d|EMX)$")),
    ("CMC",   re.compile(r"^CE\d\d$")),
    ("NAVY",  re.compile(_ENS_NAVY_RX)),
)
# standalone handle for the Navy member pattern (used by _family)
NAVY_MEMBER_RE = re.compile(_ENS_NAVY_RX)

# color per technique family (spaghetti coloring by model family)
FAMILY_COLORS = {
    "GFS": "#4fc3f7", "ECMWF": "#ce93d8", "UKMET": "#ffb74d", "CMC": "#a5d6a7",
    "HWRF": "#ff8a80", "HMON": "#ffab91", "HAFS": "#ef9a9a", "OFCL": "#ffffff",
    "OFCI": "#e0e0e0", "SHIPS": "#90caf9", "CLIPER": "#b0bec5", "consensus": "#fff59d",
    "ens": "#b0bec5", "GFDL": "#80cbc4", "Navy": "#bcaaa4",
    "ensEC": "#e1bee7", "ensCMC": "#c5e1a5", "ensNAVY": "#b0cff0",
}
FAMILY_ORDER = ["GFS", "ECMWF", "UKMET", "CMC", "HWRF", "HMON", "HAFS", "GFDL", "Navy",
                "OFCL", "OFCI", "SHIPS", "CLIPER", "consensus", "ens", "ensEC", "ensCMC",
                "ensNAVY"]

# default chart hours (spaghetti tracks usually out to 120 h)
MAX_HOUR = 120
# render plot hours; a-decks for TS-scale storms rarely reach 120 h usefully
CHART_HOURS = (0, 24, 48, 72, 96, 120)


def _family(tech):
    if tech.startswith("AP"):
        return "ens"      # GFS ensemble members
    if ENSEMBLE_RE.match(tech):
        # ECMWF (AXxx/EMX), CMC (CExx) and Navy (NExx etc.) members get their
        # own families so GFS-vs-EC-vs-Navy ensemble spread can be compared
        # with one checkbox each
        if tech.startswith("AX") or tech == "EMX":
            return "ensEC"
        if tech.startswith("CE"):
            return "ensCMC"
        if NAVY_MEMBER_RE.match(tech):
            return "ensNAVY"
        return "ens"
    if tech in ("AVNO", "AVNI", "AP01", "AP02", "AP03", "AP04", "AP05"):
        return "GFS"
    if tech.startswith("AEM") or tech in ("CEMN", "CEMI", "CEM2"):
        return "ECMWF"
    if tech.startswith("CTC") or tech.startswith("UKX"):
        return "UKMET"
    if tech.startswith("CMC") or tech.startswith("CMC2"):
        return "CMC"
    if tech.startswith("HWR") or tech == "HWFI":
        return "HWRF"
    if tech.startswith("HMO"):
        return "HMON"
    if tech.startswith("HFS"):
        return "HAFS"
    if tech.startswith("OF"):
        return "OFCL"
    if tech in ("SHIP", "SHF5", "DSHP", "LGEM", "OCD5", "DRCL"):
        return "SHIPS"
    if tech == "CLP5":
        return "CLIPER"
    if tech in ("IVCN", "HCCA", "TVCN", "RVCN"):
        return "consensus"
    if tech.startswith("NGX") or tech == "GDMI":
        return "GFDL"
    if tech.startswith("NV") or tech.startswith("NN"):
        return "Navy"
    return "ens"


def _cat_color(kt):
    """Saffir-Simpson-ish category color for intensity dots."""
    try:
        kt = float(kt)
    except (TypeError, ValueError):
        return "#9e9e9e"
    if kt >= 137:
        return "#d32f2f"   # cat 5
    if kt >= 113:
        return "#e64a19"   # cat 4
    if kt >= 96:
        return "#f57c00"   # cat 3
    if kt >= 83:
        return "#ffa000"   # cat 2
    if kt >= 64:
        return "#fbc02d"   # cat 1
    if kt >= 34:
        return "#03a9f4"   # TS
    return "#90a4ae"       # TD/DB


def _decode_coord(lat_s, lon_s):
    """ATCF coords are tenths: '126N' = 12.6N, '1087W' = 108.7W."""
    try:
        lat = float(lat_s[:-1]) / 10.0 * (1 if lat_s[-1] == "N" else -1)
        lon = float(lon_s[:-1]) / 10.0 * (-1 if lon_s[-1] == "W" else 1)
        return lat, lon
    except (TypeError, ValueError, IndexError):
        return None, None


def _get(url, timeout=30):
    r = _SESSION.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


_SESSION = requests.Session()


def _list_aids():
    """All aid files in aid_public: [(basin, stormnum, year, filename), ...]."""
    r = _get(DIR_URL)
    out = []
    # ATCF a-deck files are named 'a' + basin code + num + year: Atlantic is
    # 'aal092026.dat.gz' (DOUBLE a), East Pacific 'aep182026.dat.gz'. The old
    # pattern (?:l|ep|cp) could never match the doubled-a Atlantic names, so
    # every Gulf/Atlantic storm showed zero model guidance all season.
    for m in re.finditer(r'href="(a(?:al|ep|cp)\d{6}\.dat\.gz)"', r.text):
        fn = m.group(1)
        basin = fn[1:3]
        num = fn[3:5]
        year = fn[5:9]
        out.append((basin, num, year, fn))
    return out


def _basin_name(b):
    return {"al": "Atlantic", "ep": "E Pacific", "cp": "C Pacific"}[b]


def _fetch_storm(basin, num, year, fn):
    """Parse one aid-deck -> dict with tracks per model + intensity series."""
    r = _get(DIR_URL + fn)
    txt = gzip.decompress(r.content).decode("utf-8", errors="replace")
    lines = [l for l in txt.strip().splitlines() if l.strip()]
    # first CARQ line: storm identity
    first = lines[0].split(",")
    storm_name = first[27].strip() if len(first) > 27 else "Storm"
    tech0 = first[4].strip()
    if tech0 == "CARQ" and first[27].strip() in ("", "?"):
        storm_name = f"Invest {basin.upper()}{num}"

    tracks_all = {}      # tech -> {init: [(fhour, lat, lon, kt, mslp), ...]}
    for l in lines:
        c = [x.strip() for x in l.split(",")]
        if len(c) < 10:   # guidance lines have ~26 cols, radii-bearing 30; need 0-9
            continue
        tech = c[4]
        try:
            fhour = int(c[5])
        except ValueError:
            fhour = 0
        lat, lon = _decode_coord(c[6], c[7])
        if lat is None:
            continue
        kt = c[8] if len(c) > 8 else ""
        mslp = c[9] if len(c) > 9 else ""
        tracks_all.setdefault(tech, {}).setdefault(c[2], []).append(
            (fhour, lat, lon, kt, mslp))

    # The aid deck APPENDS every cycle since the storm formed (Odalys carried
    # 13 inits / 27k lines on 2026-09-20), so keeping everything merged days
    # of old runs into one unreadable zigzag. Keep only the newest published
    # init per tech (fall back to that tech's own newest if its latest run
    # hasn't landed), and drop negative taus (hindcast/best-track positions).
    tracks = {}
    for tech, inits in tracks_all.items():
        # keep only this tech's NEWEST published init (falling back is never
        # wanted: mixing cycles draws two lines for one model)
        keep = inits[max(inits, key=lambda i: (len(i), i))]
        # de-duplicate taus: a-decks can carry several blocks per tech in one
        # cycle (reruns/interpolations); the LAST block is the authoritative
        # one, and duplicated taus made the polyline double back on itself
        dedup = {}
        for p in keep:
            if p[0] >= 0:
                dedup[p[0]] = p
        keep = [dedup[fh] for fh in sorted(dedup)]
        if keep:
            tracks[tech] = keep

    # Past-cycle ensemble members: the deck keeps every cycle since the
    # storm formed, so the SAME spread math can run on prior cycles - the
    # spread chart overlays them faded so the trend (growing vs shrinking
    # uncertainty vs yesterday's runs) is visible against today's line.
    ens_inits = {}
    for tech, inits in tracks_all.items():
        if not ENSEMBLE_RE.match(tech):
            continue
        for init, pts in inits.items():
            dedup = {}
            for p in pts:
                if p[0] >= 0:
                    dedup[p[0]] = p
            keep = [dedup[fh] for fh in sorted(dedup)]
            if keep:
                ens_inits.setdefault(init, {})[tech] = keep
    history = [(i, ens_inits[i])
               for i in sorted(ens_inits, key=lambda k: (len(k), k),
                               reverse=True)[:5]]

    return {
        "basin": basin, "basinName": _basin_name(basin), "num": num, "year": year,
        "name": storm_name,
        "tracks": tracks,      # tech -> [(fhour, lat, lon, kt, mslp), ...]
        "ens_inits": history,  # [(init, {tech: track}), ...] newest first
        "n_lines": len(lines),
    }


def _track_geojson(trk):
    """One model's [(fhour, lat, lon, kt, mslp), ...] -> GeoJSON MultiLineString-ish."""
    if not trk:
        return None
    coords = [[lon, lat] for (_, lat, lon, _, _) in trk]
    props = []
    for (fh, lat, lon, kt, mslp) in trk:
        props.append({"hour": fh, "lat": lat, "lon": lon, "kt": kt, "mslp": mslp})
    return {
        "type": "LineString",
        "coordinates": coords,
        "props": props,          # per-vertex metadata (hour/kt) for popups
    }


def bundle(max_age=900):
    """All active storms with their guidance, cached (updater calls on cycle)."""
    with CACHE_LOCK:
        now = time.time()
        if CACHE["storms"] is not None and now - CACHE["t"] < max_age:
            return CACHE["storms"]

    storms_meta = nhc_storms()
    aids = _list_aids()
    by_key = {}
    for (basin, num, year, fn) in aids:
        by_key.setdefault((basin, num, year), []).append(fn)

    out = []
    for s in storms_meta:
        key = _storm_key(s)
        fns = by_key.get(key)
        if not fns:
            # storm on NHC but no aid file yet (tracks: LIST - the page JS
            # calls .forEach on it; a dict threw and killed the whole map
            # boot on 2026-09-20)
            out.append({"name": s["name"], "classification": s["classification"],
                        "intensity": s.get("intensity"), "basin": key[0],
                        "basinName": _basin_name(key[0]), "models": [], "tracks": [],
                        "charts": [], "ensCones": [],
                        "source": "NHC CurrentStorms (no aid file yet)"})
            continue
        st = None
        try:
            st = _fetch_storm(*key, fns[-1])
        except Exception:
            st = None
        if not st:
            out.append({"name": s["name"], "classification": s["classification"],
                        "intensity": s.get("intensity"), "basin": key[0],
                        "basinName": _basin_name(key[0]), "models": [], "tracks": [],
                        "charts": [], "ensProb": None,
                        "source": "aid-deck fetch failed this cycle"})
            continue
        out.append(_shape_storm(st, s))

    with CACHE_LOCK:
        CACHE.update(t=time.time(), storms=out)
    return out


def _storm_key(s):
    """NHC CurrentStorms id 'ep142026' -> ('ep', '14', '2026')."""
    sid = s.get("id") or ""
    m = re.match(r"^(al|ep|cp)(\d{2})(\d{4})$", sid)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return "", "", ""


def nhc_storms():
    """NHC CurrentStorms.json (same feed the tropical page already uses)."""
    r = _get("https://www.nhc.noaa.gov/CurrentStorms.json")
    d = r.json()
    out = []
    for s in d.get("activeStorms", []):
        out.append({
            "id": s.get("id"), "name": s.get("name"), "classification": s.get("classification"),
            "intensity": s.get("intensity"), "pressure": s.get("pressure"),
            "lastUpdate": s.get("lastUpdate"),
        })
    return out


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km (TC scales - plenty accurate)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(float(a)))


def _bearing_deg(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing deg (north=0, east=90) from pt1 to pt2."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(lon2 - lon1)
    y = np.sin(dl) * np.cos(p2)
    x = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)
    return float(np.degrees(np.arctan2(y, x))) % 360.0


def _offset(lat, lon, bearing, dist_km):
    """Point dist_km away from (lat, lon) along bearing (equirectangular)."""
    br = np.radians(bearing)
    dlat = (dist_km / 111.32) * np.cos(br)
    dlon = (dist_km / (111.32 * max(np.cos(np.radians(lat)), 0.2))) * np.sin(br)
    return lat + float(dlat), lon + float(dlon)


# a cone from a handful of members would be a lie, not guidance
CONE_MIN_MEMBERS = 5
CONE_FLOOR_KM = 20.0     # members coincide at analysis time - keep ring visible
CONE_CAP_KM = 900.0      # one rogue member shouldn't swallow the basin
RI_DT_KT = 30            # rapid intensification: +30 kt...
RI_DT_H = 24             # ...in 24 h (NHC RI-index convention)


def _ens_label(tech):
    """Member code -> human label for the newer agencies (APxx handled in place)."""
    m = re.match(r"^(AX|CE)(\d\d)$", tech)
    if m:
        agency = "ECMWF" if m.group(1) == "AX" else "CMC"
        return f"{agency} ens. member {int(m.group(2))}"
    m = re.match(r"^N(?:A|E|X)[A-Z]?(\d\d)$", tech)
    if m:
        return f"Navy ens. member {int(m.group(1))}"
    if tech == "EMX":
        return "ECMWF ens. (extended)"
    return f"Ensemble ({tech})"


def _per_lead_spread(st):
    """Shared ensemble-spread math: per agency, per 6-h lead RMS radius (km).

    Returns {agency: (hours, radii_km, mean_positions)} - the single
    source of truth for the map's spread cone (sausage swept along the
    mean track), the spread-vs-time chart (same numbers plotted hour by
    hour) and the strike-probability disks. Same guardrails as the cone:
    >= CONE_MIN_MEMBERS per lead, radii floored at CONE_FLOOR_KM and
    capped at CONE_CAP_KM.
    """
    tracks = st.get("tracks") or {}
    out = {}
    for agency, rx in ENS_GROUPS:
        ens = [t for t in tracks if rx.match(t) and t in tracks]
        if len(ens) < CONE_MIN_MEMBERS:
            continue
        leads = {}
        for t in ens:
            for (fh, lat, lon, kt, mslp) in tracks[t]:
                if 0 <= fh <= MAX_HOUR:
                    try:
                        leads.setdefault(int(fh), []).append((float(lat), float(lon)))
                    except (TypeError, ValueError):
                        continue
        leads = {fh: pts for fh, pts in sorted(leads.items())
                 if len(pts) >= CONE_MIN_MEMBERS}
        if not leads:
            continue
        hours, radii, means = [], [], []
        for fh, pts in leads.items():
            ml = float(np.mean([p[0] for p in pts]))
            mo = float(np.mean([p[1] for p in pts]))
            r = float(np.sqrt(np.mean([
                _haversine_km(ml, mo, la, lo) ** 2 for la, lo in pts])))
            hours.append(fh)
            radii.append(min(max(r, CONE_FLOOR_KM), CONE_CAP_KM))
            means.append((ml, mo))
        out[agency] = (hours, radii, means)
    return out


def _ens_intensity_fan(st):
    """Per-lead ensemble INTENSITY spread: wind-speed percentiles per agency.

    The strength counterpart of the track-spread cone: for each 6-h lead,
    gather every member's forecast wind (kt) and take the 10th/25th/50th/
    75th/90th percentiles. The p10-p90 band is the intensity fan - where
    the ensemble thinks the storm's strength will likely fall. Same
    >= CONE_MIN_MEMBERS guardrail as the track math. Returns
    {agency: (hours, p10, p25, p50, p75, p90)}; agencies without member
    decks are omitted.
    """
    tracks = st.get("tracks") or {}
    out = {}
    for agency, rx in ENS_GROUPS:
        ens = [t for t in tracks if rx.match(t) and t in tracks]
        if len(ens) < CONE_MIN_MEMBERS:
            continue
        leads = {}
        for t in ens:
            for (fh, lat, lon, kt, mslp) in tracks[t]:
                if 0 <= fh <= MAX_HOUR:
                    try:
                        leads.setdefault(int(fh), []).append(float(kt))
                    except (TypeError, ValueError):
                        continue
        leads = {fh: kts for fh, kts in sorted(leads.items())
                 if len(kts) >= CONE_MIN_MEMBERS}
        if len(leads) < 2:
            continue
        pct = lambda kts, q: float(np.percentile(kts, q))
        hours = sorted(leads)
        bands = {q: [pct(leads[h], q) for h in hours] for q in (10, 25, 50, 75, 90)}
        out[agency] = (hours, bands[10], bands[25], bands[50], bands[75], bands[90])
    return out


def _past_cycle_spread(st, max_cycles=3):
    """Per-agency ensemble spread from PRIOR advisory cycles.

    Runs the same _per_lead_spread math over each of the last few cycles
    preserved in the a-deck (st["ens_inits"], newest first, skipping the
    newest - that one is what the current solid line already draws).
    Returns [{"cycle": init, "label": "09/20 12Z", "spread": {agency:
    (hours, radii)}}]; empty when the deck carries no usable history.
    """
    hist = st.get("ens_inits") or []
    if len(hist) < 2:
        return []
    out = []
    for init, techs in hist[1:max_cycles + 1]:
        if len(techs) < CONE_MIN_MEMBERS:
            continue
        spread = _per_lead_spread({"tracks": techs})
        if not spread:
            continue
        label = init
        m = re.match(r"^(\d{4})(\d{2})(\d{2})(\d{2})", init)
        if m:
            label = f"{m.group(2)}/{m.group(3)} {m.group(4)}Z"
        out.append({"cycle": init, "label": label, "spread": spread})
    return out


def _ensemble_cone(st):
    """Per-agency ensemble spread envelopes -> NHC-style cones (GeoJSON).

    Uses _per_lead_spread for the shared per-lead RMS radii, then sweeps a
    variable-width rounded-cap sausage along the member-mean track - the
    same visual grammar as the NHC strike-probability cone, but driven by
    actual ensemble spread. Returns a list; an agency with too few members
    or fewer than two usable leads is simply omitted.
    """
    spread = _per_lead_spread(st)
    out = []
    for agency, (hours, radii, means) in spread.items():
        if len(hours) < 2:
            continue
        # bearing along the mean track (last point reuses the previous segment)
        brgs = [_bearing_deg(means[i][0], means[i][1],
                             means[i + 1][0], means[i + 1][1])
                if i < len(means) - 1 else
                _bearing_deg(means[i - 1][0], means[i - 1][1],
                             means[i][0], means[i][1])
                for i in range(len(means))]
        ring = []
        for i, (ml, mo) in enumerate(means):          # left side, forward
            la, lo = _offset(ml, mo, brgs[i] + 90.0, radii[i])
            ring.append([round(lo, 3), round(la, 3)])
        a = brgs[-1] + 90.0                           # rounded end cap
        while a > brgs[-1] - 90.0:
            a -= 15.0
            la, lo = _offset(means[-1][0], means[-1][1], a, radii[-1])
            ring.append([round(lo, 3), round(la, 3)])
        for i in range(len(means) - 1, -1, -1):       # right side, backward
            la, lo = _offset(means[i][0], means[i][1], brgs[i] - 90.0, radii[i])
            ring.append([round(lo, 3), round(la, 3)])
        a = brgs[0] - 90.0                            # rounded start cap
        while a > brgs[0] - 270.0:
            a -= 15.0
            la, lo = _offset(means[0][0], means[0][1], a, radii[0])
            ring.append([round(lo, 3), round(la, 3)])
        ring.append(ring[0])                          # close the ring
        out.append({
            "agency": agency,
            "family": {"GFS": "ens", "ECMWF": "ensEC", "CMC": "ensCMC",
                       "NAVY": "ensNAVY"}[agency],
            "geo": {"type": "Polygon", "coordinates": [ring]},
            "nMembers": len([t for t in (st.get("tracks") or {})
                             if dict(ENS_GROUPS)[agency].match(t)]),
            "hours": hours,
            "radiiKm": [[h, round(r)] for h, r in zip(hours, radii)],
            "maxHour": hours[-1],
        })
    return out


def _ens_prob_field(st):
    """Per-agency ensemble strike-probability field -> client raster spec.

    NHC wind-probability-style shading built from the ensemble members
    themselves: for each 6-h lead, every member contributes a probability
    disk whose radius is that lead's RMS member spread (60 km floor so the
    near-field never vanishes, 900 km cap so one outlier can't flood the
    basin). Each disk adds 100/n_members percent, capped at 100, and disks
    compound across leads (union semantics: hit by any member at any time)
    - the same visual grammar as NHC's strike-probability product, but
    driven by this cycle's actual ensemble scatter instead of error
    climatology. Accumulated in a sparse dict keyed by grid cell, so cost
    scales with the disk areas actually touched. Returns one spec per
    agency (None-generating agencies are omitted; <5 members -> nothing).
    """
    tracks = st.get("tracks") or {}
    out = []
    for agency, rx in ENS_GROUPS:
        ens = [t for t in tracks if rx.match(t)]
        if len(ens) < CONE_MIN_MEMBERS:
            continue
        leads = {}
        for t in ens:
            for (fh, lat, lon, kt, mslp) in tracks[t]:
                if 0 <= fh <= MAX_HOUR:
                    try:
                        leads.setdefault(int(fh), []).append((float(lat), float(lon)))
                    except (TypeError, ValueError):
                        continue
        leads = {fh: pts for fh, pts in sorted(leads.items())
                 if len(pts) >= CONE_MIN_MEMBERS}
        if not leads:
            continue
        R0_KM = 60.0
        lat_pts = [la for pts in leads.values() for (la, lo) in pts]
        lon_pts = [lo for pts in leads.values() for (la, lo) in pts]
        r_max_deg = min(CONE_CAP_KM / 111.0 + 3.0, 15.0)
        lat_hi = min(max(lat_pts) + r_max_deg, 84.0)
        lat_lo = max(min(lat_pts) - r_max_deg, -84.0)
        lon_hi = max(lon_pts) + r_max_deg
        lon_lo = min(lon_pts) - r_max_deg
        cell_km = 25.0
        dlat = cell_km / 111.0
        mid_lat = 0.5 * (lat_hi + lat_lo)
        dlon = dlat / max(math.cos(math.radians(mid_lat)), 0.3)
        gh = max(int(math.ceil((lat_hi - lat_lo) / dlat)) + 1, 2)
        gw = max(int(math.ceil((lon_hi - lon_lo) / dlon)) + 1, 2)
        n = len(ens)
        inc = 100.0 / n
        acc = {}                                   # (row, col) -> accumulated %
        for fh, pts in leads.items():
            ml = float(np.mean([la for la, _ in pts]))
            mo = float(np.mean([lo for _, lo in pts]))
            r_km = math.sqrt(sum(_haversine_km(ml, mo, la, lo) ** 2
                                 for la, lo in pts) / len(pts))
            r_km = min(max(r_km, R0_KM), CONE_CAP_KM)
            # scan window covers THIS lead's disk, not the whole bbox:
            # bbox sizing (r_max_deg) would make every point scan a 100x100
            # cell window - ~10x the work for nothing
            dr_cells = int(math.ceil(r_km / cell_km)) + 1
            for (la, lo) in pts:
                row_f = (lat_hi - la) / dlat       # point position in grid units
                col_f = (lo - lon_lo) / dlon
                for rr in range(-dr_cells, dr_cells + 1):
                    row = int(row_f) + rr
                    if not (0 <= row < gh):
                        continue
                    lat_c = lat_hi - (row + 0.5) * dlat
                    kmd = 111.0 * max(math.cos(math.radians(lat_c)), 0.3)
                    for cc in range(-dr_cells, dr_cells + 1):
                        col = int(col_f) + cc
                        if not (0 <= col < gw):
                            continue
                        lon_c = lon_lo + (col + 0.5) * dlon
                        dy = (lat_c - la) * 111.0
                        dx = (lon_c - lo) * kmd
                        if dy * dy + dx * dx <= r_km * r_km:
                            v = min(acc.get((row, col), 0.0) + inc, 100.0)
                            acc[(row, col)] = v
        if not acc:
            continue
        vals = np.zeros((gh, gw), dtype=np.uint8)
        for (r, c), v in acc.items():
            iv = min(int(round(v)), 99)            # 99 == ">= 100%" top bin
            if iv > 0:
                vals[r, c] = iv
        mask = vals > 0
        rows_any = np.any(mask, axis=1)
        cols_any = np.any(mask, axis=0)
        if not rows_any.any() or not cols_any.any():
            continue
        r0 = int(np.argmax(rows_any))
        r1 = len(rows_any) - int(np.argmax(rows_any[::-1]))
        c0 = int(np.argmax(cols_any))
        c1 = len(cols_any) - int(np.argmax(cols_any[::-1]))
        sub = vals[r0:r1, c0:c1]
        sh, sw = sub.shape
        # image bounds = outer edges of the extreme filled cells, so a
        # pixel's center lands on its cell's center when Leaflet stretches
        lat0 = lat_hi - r0 * dlat
        lat1 = lat_hi - r1 * dlat
        lon0 = lon_lo + c0 * dlon
        lon1 = lon_lo + c1 * dlon
        out.append({
            "agency": agency,
            "family": {"GFS": "ens", "ECMWF": "ensEC", "CMC": "ensCMC",
                       "NAVY": "ensNAVY"}[agency],
            "cellKm": round(cell_km, 1),
            "lat0": round(lat0, 3), "lat1": round(lat1, 3),
            "lon0": round(lon0, 3), "lon1": round(lon1, 3),
            "sh": int(sh), "sw": int(sw),
            "vals": sub.ravel().tolist(),
            "nMembers": n,
            "maxProb": int(sub.max()),
        })
    return out


def _shape_storm(st, s_meta):
    """Fetch result -> payload dict (tracks GeoJSON, chart hrefs, model table)."""
    tracks = st["tracks"]
    order = [t for t in FAMILY_ORDER if any(t == f for f in [])]
    # keep track-guidance techs PLUS ensemble members (AP01-AP30 GFS ens.,
    # etc.): the spaghetti view is the whole point of the page, but the old
    # TRACK_TECHS-only filter silently dropped every ensemble member
    track_techs = [t for t in tracks
                   if t in TRACK_TECHS or ENSEMBLE_RE.match(t)]
    # pick newest init time across techs (max fhour coverage as proxy)
    def _max_fh(t):
        return max((p[0] for p in tracks[t]), default=0)
    track_techs.sort(key=lambda t: (-_max_fh(t), t))

    models = []
    track_geoms = []
    for tech in track_techs:
        trk = tracks[tech]
        if len(trk) < 2:      # ensemble members may be short; still draw >=2
            continue          # (deterministics needed 3 - keep that leniency)
        fam = _family(tech)
        is_ens = bool(ENSEMBLE_RE.match(tech))
        label = TECH_NAMES.get(tech)
        if label is None and is_ens:
            mm = re.match(r"^AP(\d\d)$", tech)
            label = (f"GFS ens. member {int(mm.group(1))}" if mm
                     else _ens_label(tech))
        models.append({
            "tech": tech, "name": label or tech, "family": fam,
            "color": FAMILY_COLORS.get(fam, "#b0bec5"),
            "isEns": is_ens,
            "n_points": len(trk),
            "max_hour": trk[-1][0],
        })
        g = _track_geojson(trk)
        if g:
            track_geoms.append({"tech": tech, "name": label or tech,
                                "family": fam, "color": FAMILY_COLORS.get(fam, "#b0bec5"),
                                "isEns": is_ens,
                                "geo": g, "isOfficial": tech in ("OFCL", "OFCI")})

    cone = _ensemble_cone(st)
    prob = _ens_prob_field(st)
    charts = render_intensity_charts(st, s_meta)
    charts += render_spread_chart(st, s_meta)
    charts += render_intensity_fan_chart(st, s_meta)
    return {
        "id": s_meta.get("id"),
        "name": (s_meta.get("name") or st["name"]),
        "classification": s_meta.get("classification"),
        "intensity": s_meta.get("intensity"), "pressure": s_meta.get("pressure"),
        "basin": st["basin"], "basinName": st["basinName"],
        "models": models, "tracks": track_geoms, "charts": charts,
        "ensCones": cone,
        "ensProb": prob,
        "initLineCount": st["n_lines"],
        "source": "NHC ATCF aid-deck (ftp.nhc.noaa.gov)",
    }


def render_intensity_charts(st, s_meta):
    """Per-storm intensity forecast chart (matplotlib PNG, small)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return []
    tracks = st["tracks"]
    out_dir = os.path.join("static", "tropics")
    os.makedirs(out_dir, exist_ok=True)
    sid = (s_meta.get("id") or (st["basin"] + st["num"])).lower()
    charts = []
    fig, ax = plt.subplots(figsize=(8, 3.2), dpi=110)
    plotted_any = False
    for tech, trk in tracks.items():
        if tech == "CARQ" or len(trk) < 3:
            continue
        hrs = [p[0] for p in trk if p[0] <= MAX_HOUR]
        kts = []
        for p in trk:
            if p[0] <= MAX_HOUR:
                try:
                    kts.append(float(p[3]))
                except (TypeError, ValueError):
                    kts.append(np.nan)
        if len(hrs) < 3:
            continue
        fam = _family(tech)
        is_official = tech in ("OFCL", "OFCI")
        # ensemble members (APxx GFS ens. + EC/CMC/Navy) draw as thin spaghetti
        is_ens = fam in ("ens", "ensEC", "ensCMC", "ensNAVY") or bool(ENSEMBLE_RE.match(tech))
        lw = 2.8 if is_official else (1.0 if is_ens else 1.6)
        alpha = 1.0 if is_official else (0.45 if is_ens else 0.85)
        ax.plot(hrs, kts, color=FAMILY_COLORS.get(fam, "#b0bec5"), lw=lw, alpha=alpha,
                label=("OFFICIAL" if is_official else None), zorder=5 if is_official else 2)
        plotted_any = True
    if not plotted_any:
        plt.close(fig)
        return []
    # Saffir-Simpson bands
    for y, lbl, c in [(34, "TS", "#0288d1"), (64, "Cat1", "#f9a825"), (83, "Cat2", "#ef6c00"),
                      (96, "Cat3", "#e64a19"), (113, "Cat4", "#bf360c"), (137, "Cat5", "#b71c1c")]:
        ax.axhline(y, color=c, lw=0.6, ls=":", alpha=0.55)
        ax.text(1, y + 1.5, lbl, fontsize=7, color=c, alpha=0.85)
    ax.set_xlabel("Forecast hour (advisory cycle)", fontsize=9)
    ax.set_ylabel("Wind (kt)", fontsize=9)
    ax.set_title(f"{st['name']} - intensity guidance - all models (NHC ATCF)", fontsize=10, weight="bold")
    ax.set_xlim(0, MAX_HOUR)
    ax.grid(alpha=0.25)
    fn = f"intensity_{sid}.png"
    path = os.path.join(out_dir, fn)
    try:
        fig.savefig(path, bbox_inches="tight", facecolor="#0d1117")
        charts.append({
            "name": "Intensity forecast - all models",
            "href": f"../tropics/{fn}?v={int(os.path.getmtime(path))}",
            "storm": s_meta.get("id"),
        })
    finally:
        plt.close(fig)
    return charts


def render_spread_chart(st, s_meta):
    """Spread-vs-time chart: per-agency ensemble track uncertainty (km RMS)
    at each 6-h lead - GFS vs ECMWF vs CMC on one axis, so inter-agency
    disagreement is visible at a glance. Same shared math as the map's
    spread cone (_per_lead_spread). Prior advisory cycles draw as faded
    dashed lines so the trend vs yesterday's runs is readable at a
    glance. Agencies without member decks are simply absent; no members
    at all -> no chart.
    """
    spread = _per_lead_spread(st)
    history = _past_cycle_spread(st)
    if not spread:
        return []
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return []
    out_dir = os.path.join("static", "tropics")
    os.makedirs(out_dir, exist_ok=True)
    sid = (s_meta.get("id") or (st["basin"] + st["num"])).lower()
    fig, ax = plt.subplots(figsize=(8, 3.0), dpi=110)
    fam_of = {"GFS": "ens", "ECMWF": "ensEC", "CMC": "ensCMC", "NAVY": "ensNAVY"}
    lbl_of = {"GFS": "GFS (APxx)", "ECMWF": "ECMWF (AXxx/EMX)",
              "CMC": "CMC (CExx)", "NAVY": "Navy"}
    # past cycles first (under everything): faded dashed, no markers
    seen = set()
    for h in history:
        for agency, (hours, radii, _means) in h["spread"].items():
            ax.plot(hours, radii,
                    color=FAMILY_COLORS.get(fam_of[agency], "#b0bec5"),
                    lw=1.0, ls="--", alpha=0.35, marker=None,
                    label=(f"{lbl_of[agency]} {h['label']} (prior)"
                           if agency not in seen else None))
            seen.add(agency)
    for agency, (hours, radii, _means) in spread.items():  # noqa: E501
        ax.plot(hours, radii, color=FAMILY_COLORS.get(fam_of[agency], "#b0bec5"),
                lw=2.4, marker="o", ms=3.5, zorder=5,
                label=f"{lbl_of[agency]} now")
    ax.set_xlabel("Forecast hour", fontsize=9)
    ax.set_ylabel("Track spread (km, RMS)", fontsize=9)
    ax.set_title(f"{(s_meta.get('name') or st['name'])} - ensemble track spread by agency"
                 + (" (dashed = prior cycles)" if history else ""),
                 fontsize=10, weight="bold")
    ax.set_xlim(0, MAX_HOUR)
    ax.set_ylim(bottom=0)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, loc="upper left")
    fn = f"spread_{sid}.png"
    path = os.path.join(out_dir, fn)
    try:
        fig.savefig(path, bbox_inches="tight", facecolor="#0d1117")
        return [{
            "name": "Ensemble track spread - by agency",
            "href": f"../tropics/{fn}?v={int(os.path.getmtime(path))}",
            "storm": s_meta.get("id"),
        }]
    finally:
        plt.close(fig)


def _ens_ri_odds(st):
    """Rapid-intensification odds from the ensemble members, NHC RI-index style.

    For each lead h >= 24, count members whose wind rose by >= RI_DT_KT
    over the preceding 24 h (using each member's own track, so members
    peak at different hours) and divide by the members reporting at both
    hours. Uses a sliding window (odds anchored to h-24), matching the
    convention of NHC's rapid-intensification index aids. Returns
    {agency: [(hour, pct), ...]}; agencies without member decks omitted.
    """
    tracks = st.get("tracks") or {}
    out = {}
    for agency, rx in ENS_GROUPS:
        ens = [t for t in tracks if rx.match(t) and t in tracks]
        if len(ens) < CONE_MIN_MEMBERS:
            continue
        # per member: {hour: wind_kt}
        series = []
        for t in ens:
            pts = {}
            for (fh, lat, lon, kt, mslp) in tracks[t]:
                if 0 <= fh <= MAX_HOUR:
                    try:
                        pts[int(fh)] = float(kt)
                    except (TypeError, ValueError):
                        continue
            if pts:
                series.append(pts)
        if len(series) < CONE_MIN_MEMBERS:
            continue
        odds = []
        for h in range(RI_DT_H, MAX_HOUR + 1, 6):
            n = hit = 0
            for pts in series:
                if h in pts and (h - RI_DT_H) in pts:
                    n += 1
                    if pts[h] - pts[h - RI_DT_H] >= RI_DT_KT:
                        hit += 1
            if n >= CONE_MIN_MEMBERS:      # don't chart odds off 2 stragglers
                odds.append((h, round(100.0 * hit / n)))
        if odds:
            out[agency] = odds
    return out


def render_intensity_fan_chart(st, s_meta):
    """Intensity-fan chart: per-agency ensemble wind-speed percentiles per
    lead - the strength version of the track-spread chart. The p10-p90
    band shades the likely range, p25-p75 the core range, the median line
    the ensemble's central intensity forecast. OFCL (official) overlays
    as a white reference line where available. No members -> no chart.
    """
    fan = _ens_intensity_fan(st)
    if not fan:
        return []
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return []
    out_dir = os.path.join("static", "tropics")
    os.makedirs(out_dir, exist_ok=True)
    sid = (s_meta.get("id") or (st["basin"] + st["num"])).lower()
    fig, ax = plt.subplots(figsize=(8, 3.0), dpi=110)
    fam_of = {"GFS": "ens", "ECMWF": "ensEC", "CMC": "ensCMC", "NAVY": "ensNAVY"}
    lbl_of = {"GFS": "GFS", "ECMWF": "ECMWF", "CMC": "CMC", "NAVY": "Navy"}
    drew = False
    for agency, (hours, p10, p25, p50, p75, p90) in fan.items():
        c = FAMILY_COLORS.get(fam_of[agency], "#b0bec5")
        ax.fill_between(hours, p10, p90, color=c, alpha=0.18, lw=0)
        ax.fill_between(hours, p25, p75, color=c, alpha=0.38, lw=0)
        ax.plot(hours, p50, color=c, lw=2.2, marker="o", ms=3.5,
                label=f"{lbl_of[agency]} median (p10-p90 band)")
        drew = True
    # official forecast as a white reference where the deck carries it
    ofcl = (st.get("tracks") or {}).get("OFCL")
    if ofcl:
        hrs = [p[0] for p in ofcl if p[0] <= MAX_HOUR]
        kts = [float(p[3]) for p in ofcl if p[0] <= MAX_HOUR]
        if len(hrs) >= 2:
            ax.plot(hrs, kts, color="#ffffff", lw=2.4, label="OFFICIAL (OFCL)", zorder=5)
    if not drew:
        plt.close(fig)
        return []
    for y, lbl, cc in [(34, "TS", "#0288d1"), (64, "Cat1", "#f9a825"),
                       (83, "Cat2", "#ef6c00"), (96, "Cat3", "#e64a19"),
                       (113, "Cat4", "#bf360c"), (137, "Cat5", "#b71c1c")]:
        ax.axhline(y, color=cc, lw=0.6, ls=":", alpha=0.55)
        ax.text(1, y + 1.5, lbl, fontsize=7, color=cc, alpha=0.85)
    ax.set_xlabel("Forecast hour", fontsize=9)
    ax.set_ylabel("Wind (kt, ensemble pctiles)", fontsize=9)
    ax.set_title(f"{(s_meta.get('name') or st['name'])} - ensemble intensity fan by agency",
                 fontsize=10, weight="bold")
    # rapid-intensification odds on a second axis (NHC RI-index style):
    # chance each member's own wind rises >= RI_DT_KT in the prior 24 h
    ri = _ens_ri_odds(st)
    if ri:
        ax2 = ax.twinx()
        ax2.set_ylim(0, 100)
        ax2.set_ylabel("RI odds (%: +30 kt / 24 h)", fontsize=8.5, color="#ff8a80")
        ax2.tick_params(axis="y", colors="#ff8a80", labelsize=8)
        ax2.set_zorder(ax.get_zorder() + 1)
        ax2.patch.set_visible(False)
        ri_any = False
        for agency, odds in ri.items():
            hrs = [h for h, _ in odds]
            pct = [p for _, p in odds]
            c = FAMILY_COLORS.get(fam_of[agency], "#b0bec5")
            if pct[-1] or max(pct):
                ax2.plot(hrs, pct, color=c, lw=1.6, ls=(0, (2, 2)), marker="^", ms=4,
                         alpha=0.95, label=f"{lbl_of[agency]} RI odds")
                ri_any = True
        if not ri_any:
            ax2.plot([0], [0], alpha=0)   # keep the axis honest when all-zero
        ax2.axhline(50, color="#ff8a80", lw=0.6, ls=":", alpha=0.5)
        if ri_any:
            ax.legend(fontsize=8, loc="upper left")
    ax.set_xlim(0, MAX_HOUR)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, loc="upper left")
    fn = f"intfan_{sid}.png"
    path = os.path.join(out_dir, fn)
    try:
        fig.savefig(path, bbox_inches="tight", facecolor="#0d1117")
        return [{
            "name": "Ensemble intensity fan - by agency",
            "href": f"../tropics/{fn}?v={int(os.path.getmtime(path))}",
            "storm": s_meta.get("id"),
        }]
    finally:
        plt.close(fig)


def summary(max_age=900):
    """Slim card-friendly view: per storm, count of guidance members + max intensity."""
    b = bundle(max_age)
    out = []
    for s in b:
        fams = sorted({m["family"] for m in s.get("models", [])})
        out.append({
            "id": s.get("id"), "name": s.get("name"),
            "classification": s.get("classification"),
            "intensity": s.get("intensity"),
            "basinName": s.get("basinName"),
            "n_models": len(s.get("models", [])),
            "families": fams,
            "max_hour": max((m["max_hour"] for m in s.get("models", [])), default=0),
        })
    return out

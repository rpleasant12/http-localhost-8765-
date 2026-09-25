"""Historical storm index (HURDAT2) for the archive-wide search page.

Parses NOAA's official HURDAT2 best-track files (Atlantic 1851-present,
East Pacific 1949-present, no key) into a compact JSON index: one entry
per storm with name, year, basin, peak intensity/category, minimum
pressure, and track points (decimated to <= 24 per storm). `build_index()`
downloads + caches both files (static/hurdat/, ~11 MB, rebuilt monthly);
`search()` answers name/year lookups; `payload()` produces the site's
`stormSearch` payload: storm list + per-storm track data referenced by
the search page. The full index is ~30k storms since 1851; entries carry
no per-point payload beyond the decimated track so data.json stays small.
"""
import json
import os
import re
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(os.path.dirname(HERE), "static", "hurdat")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0"}

SOURCES = {
    "atl": ("https://www.nhc.noaa.gov/data/hurdat/hurdat2-1851-2025-091226.txt",
            1851),
    "epac": ("https://www.nhc.noaa.gov/data/hurdat/hurdat2-nepac-1949-2025-091426.txt",
             1949),
}
MAX_AGE = 30 * 86400        # re-download monthly
POINTS_PER_STORM = 24       # decimated track points per storm in the payload


def _cache_paths():
    return {b: os.path.join(CACHE_DIR, f"hurdat2_{b}.txt") for b in SOURCES}


def _fresh(p):
    try:
        return os.path.isfile(p) and time.time() - os.path.getmtime(p) < MAX_AGE
    except OSError:
        return False


def build_index(force=False):
    """Download (if stale) both HURDAT2 files; return {basin: path} or {}."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    paths = _cache_paths()
    ok = {}
    for basin, (url, _since) in SOURCES.items():
        p = paths[basin]
        if force or not _fresh(p):
            try:
                r = requests.get(url, headers=UA, timeout=120)
                if r.status_code == 200 and len(r.content) > 100_000:
                    tmp = p + ".tmp"
                    with open(tmp, "wb") as fh:
                        fh.write(r.content)
                    os.replace(tmp, p)
            except requests.RequestException:
                pass
        if os.path.isfile(p):
            ok[basin] = p
    return ok


def _cat(kt):
    """Saffir-Simpson category label from peak sustained wind (kt)."""
    if not isinstance(kt, int):
        return "?"
    if kt >= 137:
        return "5"
    if kt >= 113:
        return "4"
    if kt >= 96:
        return "3"
    if kt >= 83:
        return "2"
    if kt >= 64:
        return "1"
    if kt >= 34:
        return "TS"
    return "TD"


def parse_file(path, basin):
    """HURDAT2 file -> list of storm dicts (compact, JSON-ready)."""
    storms = []
    cur = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                ln = raw.rstrip("\n")
                if not ln.strip():
                    continue
                if re.match(r"^[A-Z]{2}\d{6},", ln):
                    # header: id, name, n_points
                    if cur is not None:
                        storms.append(_finalize(cur))
                        cur = None
                    parts = [p.strip() for p in ln.split(",")]
                    sid = parts[0]
                    name = parts[1] if parts[1] != "UNNAMED" else ""
                    year = int(sid[4:8]) if len(sid) >= 8 else 0
                    cur = {"id": sid, "name": name, "year": year,
                           "basin": "atl" if basin == "atl" else "epac",
                           "peak": 0, "minPres": None, "cat": "TD",
                           "points": []}
                    continue
                if cur is None:
                    continue
                cols = [c.strip() for c in ln.split(",")]
                if len(cols) < 7:
                    continue
                try:
                    lat = float(cols[4][:-1]) * (1 if cols[4].endswith("N")
                                                 else -1)
                    lon = float(cols[5][:-1]) * (-1 if cols[5].endswith("W")
                                                 else 1)
                except (ValueError, IndexError):
                    continue
                kt = int(cols[6]) if cols[6].lstrip("-").isdigit() else 0
                pres = int(cols[7]) if len(cols) > 7 and \
                    cols[7].lstrip("-").isdigit() else None
                if pres and pres > 800:
                    cur["minPres"] = min(cur["minPres"] or pres, pres)
                if kt > cur["peak"]:
                    cur["peak"] = kt
                    cur["cat"] = _cat(kt)
                cur["points"].append([round(lon, 1), round(lat, 1)])
        if cur is not None:
            storms.append(_finalize(cur))
    except OSError:
        return []
    return storms


def _finalize(cur):
    pts = cur.pop("points")
    n = len(pts)
    if n > POINTS_PER_STORM:
        step = max(1, (n - 1) / (POINTS_PER_STORM - 1))
        pts = [pts[min(n - 1, int(i * step))] for i in range(POINTS_PER_STORM)]
    cur["track"] = pts
    return cur


def index_all(force=False):
    """Full storm list (all basins), newest first. Uses the disk cache of
    the parsed index to keep cycles cheap."""
    cache = os.path.join(CACHE_DIR, "index.json")
    files = build_index(force)
    if not files:
        return []
    srcs = {b: (os.path.getmtime(p), len(p)) for b, p in files.items()}
    try:
        with open(cache, encoding="utf-8") as fh:
            meta = json.load(fh)
        if meta.get("sources") == {b: [t, n] for b, (t, n) in srcs.items()}:
            return meta["storms"]
    except (OSError, ValueError):
        pass
    storms = []
    for basin, p in files.items():
        storms.extend(parse_file(p, basin))
    storms.sort(key=lambda s: (s["year"], s["id"]), reverse=True)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = cache + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"sources": {b: [t, n] for b, (t, n) in srcs.items()},
                       "storms": storms}, fh)
        os.replace(tmp, cache)
    except OSError:
        pass
    return storms


def payload(max_storms=0):
    """Site payload: {"count", "updated", "storms":[...]} (newest first).
    max_storms=0 keeps every storm; the client filters/searches."""
    storms = index_all()
    return {"count": len(storms), "updated": time.strftime(
        "%Y-%m-%d"), "storms": storms if not max_storms
        else storms[:max_storms]}


def search(storms, q="", year=None, basin=None, limit=60):
    """Name/year lookup over the index. q matches name prefix/substring;
    year filters exactly; basin filters 'atl'|'epac'. Best matches first:
    exact name hits rank above prefix hits above substring hits."""
    qn = (q or "").strip().upper()
    yr = int(year) if year else None
    out = []
    for s in storms:
        if yr and s["year"] != yr:
            continue
        if basin and s["basin"] != basin:
            continue
        if qn:
            name = s["name"].upper()
            if name == qn:
                rank = 0
            elif name.startswith(qn):
                rank = 1
            elif qn in name:
                rank = 2
            else:
                continue
        else:
            rank = 3
        out.append((rank, s))
    out.sort(key=lambda t: (t[0], -t[1]["year"]))
    return [s for _r, s in out[:limit]]

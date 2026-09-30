"""ENSO record book + analog years (episodes since 1950, NAO/PNA winter phases).

Everything is derived LIVE from CPC's own data so the record book can never
drift from the source:
- ONI history (oni.ascii.txt, reused from data.enso) -> episode detection by
  the official rule (5+ consecutive running-mean seasons beyond +/-0.5 C),
  strength at peak;
- analog finder -> the historical DJF seasons whose peak ONI sits closest to
  the CURRENT ONI value (same sign), the years a forecaster checks first;
- NAO / PNA winter phases (CPC standardized monthly indices, DJF mean of
  Dec of the start year + Jan/Feb of the next) -> the NAO-/PNA+ style tags
  that split otherwise-similar ENSO winters into very different outcomes.

Payload rides data.json inside elNino.records (same carry-forward protection
as the rest of the ENSO bundle) and renders on the ENSO page + a compact
analog card on the CFSv2 long-range page.
"""
import json
import os
import threading
import time

from data.climate import _get

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(HERE, ".freebuff", "enso_records.json")

NAO_URL = ("https://www.cpc.ncep.noaa.gov/products/precip/CWlink/pna/"
           "norm.nao.monthly.b5001.current.ascii")
PNA_URL = ("https://www.cpc.ncep.noaa.gov/products/precip/CWlink/pna/"
           "norm.pna.monthly.b5001.current.ascii")

_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()
MAX_AGE = 6 * 3600.0          # ONI updates monthly; tele files monthly
STALE_OK = 48 * 3600.0        # disk fallback window

SEASON_NUM = {"DJF": 0, "JFM": 1, "FMA": 2, "MAM": 3, "AMJ": 4, "MJJ": 5,
              "JJA": 6, "JAS": 7, "ASO": 8, "SON": 9, "OND": 10, "NDJ": 11}


# ------------------------------------------------------------ teleconnections

def _fetch_monthly(url):
    """CPC monthly ascii (YYYY M value) -> {(year, month): value}."""
    out = {}
    for line in _get(url).text.strip().splitlines():
        p = line.split()
        if len(p) == 3 and p[0].isdigit() and p[1].isdigit():
            try:
                out[(int(p[0]), int(p[1]))] = float(p[2])
            except ValueError:
                continue
    return out


def _djf_tele(tele, y0):
    """DJF mean for winter starting Dec of y0 (Dec y0 + Jan/Feb y0+1)."""
    vals = [tele.get(k) for k in ((y0, 12), (y0 + 1, 1), (y0 + 1, 2))
            if tele.get(k) is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _tag(v, name):
    if v is None:
        return ""
    if v >= 0.5:
        return f"{name}+"
    if v <= -0.5:
        return f"{name}-"
    return f"{name}~"


# ------------------------------------------------------------ episode detection

def _winter_label(djf_year):
    return f"{djf_year - 1}-{str(djf_year)[-2:]}"


def _strength(a):
    x = abs(a)
    if x >= 2.0:
        return "very strong"
    if x >= 1.5:
        return "strong"
    if x >= 1.0:
        return "moderate"
    return "weak"


def _episodes(oni):
    """Runs of 5+ consecutive seasons beyond +/-0.5 -> episode dicts."""
    runs, cur, sign = [], [], 0
    for r in oni:
        a = r.get("anom")
        s = 1 if (a or 0) >= 0.5 else (-1 if (a or 0) <= -0.5 else 0)
        if s and s == sign:
            cur.append(r)
        else:
            if len(cur) >= 5:
                runs.append(cur)
            cur, sign = ([r] if s else []), s
    if len(cur) >= 5:
        runs.append(cur)
    return runs


def _build(oni, nao, pna):
    eps = []
    for run in _episodes(oni):
        kind = "El Nino" if run[0]["anom"] >= 0 else "La Nina"
        peak = max(run, key=lambda r: abs(r["anom"]))
        # an episode can span two winters (the 2015-16 event began weak in
        # 2014): name it for the DJF season AT the peak, not the first DJF
        djfs = [r for r in run if r["season"].startswith("DJF")]
        djf = max(djfs, key=lambda r: abs(r["anom"])) if djfs else None
        yrs = _winter_label(int(djf["season"].split()[1])) if djf \
            else peak["season"]
        y0 = int(djf["season"].split()[1]) - 1 if djf else None
        e = {
            "kind": kind, "years": yrs,
            "startSeason": run[0]["season"], "endSeason": run[-1]["season"],
            "nSeasons": len(run),
            "peakAnom": peak["anom"], "peakSeason": peak["season"],
            "strength": _strength(peak["anom"]),
        }
        if y0 is not None:
            e["nao"] = _djf_tele(nao, y0)
            e["pna"] = _djf_tele(pna, y0)
            e["naoTag"] = _tag(e.get("nao"), "NAO")
            e["pnaTag"] = _tag(e.get("pna"), "PNA")
        eps.append(e)
    return eps


def _analogs(oni, current, n=8):
    """Historical DJF seasons closest to the current ONI (same sign first).

    The two most recent winters are dropped so a developing event is never
    matched against itself or the tail of its own run.
    """
    djf = [r for r in oni if r["season"].startswith("DJF")][:-2]
    s = (current or 0)
    same = [r for r in djf if (r["anom"] >= 0) == (s >= 0) and r["anom"] != s]
    pool = same or [r for r in djf if r["anom"] != s]
    pool = sorted(pool, key=lambda r: abs(r["anom"] - s))[:n]
    return [{"years": _winter_label(int(r["season"].split()[1])),
             "anom": r["anom"], "kind": "El Nino" if r["anom"] >= 0 else "La Nina",
             "diff": round(r["anom"] - s, 2)} for r in pool]


def _records(eps):
    out = {}
    for kind, key in (("El Nino", "elNino"), ("La Nina", "laNina")):
        pool = [e for e in eps if e["kind"] == kind]
        out[key + "Strongest"] = sorted(pool, key=lambda e: -abs(e["peakAnom"]))[:3]
        out[key + "Longest"] = sorted(pool, key=lambda e: -e["nSeasons"])[:2]
    return out


# ------------------------------------------------------------------ bundle

def bundle(max_age=MAX_AGE):
    """Records payload; 6-h memory cache + .freebuff disk mirror."""
    with _lock:
        if _cache["data"] is not None and time.time() - _cache["at"] < max_age:
            return _cache["data"]

    try:
        from data.enso import oni_history
        oni = oni_history()
        if not oni:
            raise RuntimeError("ONI history came back empty")
        nao = _fetch_monthly(NAO_URL)
        pna = _fetch_monthly(PNA_URL)

        eps = _build(oni, nao, pna)
        cur = oni[-1]
        cur_y0 = None
        if cur["season"].startswith("DJF"):
            cur_y0 = int(cur["season"].split()[1]) - 1
        winters = []
        for r in reversed([x for x in oni if x["season"].startswith("DJF")][-15:]):
            y = int(r["season"].split()[1])
            winters.append({
                "years": _winter_label(y), "oni": r["anom"],
                "nao": _djf_tele(nao, y - 1), "pna": _djf_tele(pna, y - 1),
            })
        for w in winters:
            w["naoTag"] = _tag(w["nao"], "NAO")
            w["pnaTag"] = _tag(w["pna"], "PNA")

        out = {
            "ok": True,
            "current": {"season": cur["season"], "anom": cur["anom"],
                        "phase": _strength(cur["anom"])
                        if abs(cur["anom"]) >= 0.5 else "neutral",
                        "nao": _djf_tele(nao, cur_y0) if cur_y0 else None,
                        "pna": _djf_tele(pna, cur_y0) if cur_y0 else None},
            "episodes": eps,
            "records": _records(eps),
            "analogs": _analogs(oni, cur["anom"]),
            "winters": winters,
            "nSeasons": len(oni),
            "generated": time.strftime("%Y-%m-%d %H:%M"),
        }
        with _lock:
            _cache.update(at=time.time(), data=out)
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with open(CACHE_PATH + ".tmp", "w", encoding="utf-8") as f:
                json.dump(out, f)
            os.replace(CACHE_PATH + ".tmp", CACHE_PATH)
        except OSError:
            pass
        return out
    except Exception:                              # noqa: BLE001
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                disk = json.load(f)
            if disk and time.time() - os.path.getmtime(CACHE_PATH) < STALE_OK:
                with _lock:
                    _cache.update(at=time.time(), data=disk)
                return disk
        except (OSError, ValueError):
            pass
        return {}


if __name__ == "__main__":
    b = bundle(max_age=0)
    print("ok:", b.get("ok"), "| episodes:", len(b.get("episodes") or []),
          "| generated:", b.get("generated"))
    print("current:", b.get("current"))
    print("strongest El Nino:", [(e["years"], e["peakAnom"], e["naoTag"], e["pnaTag"])
                                 for e in b["records"]["elNinoStrongest"]])
    print("strongest La Nina:", [(e["years"], e["peakAnom"], e["naoTag"], e["pnaTag"])
                                 for e in b["records"]["laNinaStrongest"]])
    print("analogs:", [(a["years"], a["anom"], a["diff"]) for a in b["analogs"]])
    print("last winter:", (b.get("winters") or [None])[-1])

"""El Niño / ENSO: current status, forecast graphics, history + explainer data.

All keyless NOAA/CPC + IRI products (mirrored or parsed into compact JSON):

- ONI full history (oni.ascii.txt, seasons since 1950) - phase-history chart
- Monthly SST indices (sstoi.indices) - Nino 3.4 + regions, last 2 years
- CPC ENSO Diagnostic Discussion: alert status, synopsis paragraphs, and the
  probability statements ("... likely (71% chance)") parsed into chips
- Graphics mirrored into static/enso/: the CPC SST-anomaly animation, the
  current advisory figures (figureNN.gif - SST obs, forecasts plumes, etc.),
  and the IRI dynamic-model plume for Nino 3.4 (scraped from IRI's current-
  forecast page, since its issue URL rotates by year/month)

This reuses data.climate's session/fetch/mirror helpers so the two pages
share one requests session and one graphic-mirror discipline.
"""
import os
import re
import threading
import time

from data.climate import (_fetch_graphic, _get, enso_summary, oni_status)

OUT_DIR = os.path.join("static", "enso")
CACHE = {"t": 0.0, "b": None}
CACHE_LOCK = threading.Lock()

CPC_ENSO = "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso_update/"
ADVISORY = "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso_advisory/"
IRI_ENSO = "https://iri.columbia.edu/our-expertise/climate/forecasts/enso/current/"


def oni_history():
    """Full Oceanic Nino Index record (3-month seasons since 1950)."""
    txt = _get("https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt").text
    rows = []
    for line in txt.strip().splitlines():
        parts = line.split()
        if len(parts) >= 4 and len(parts[0]) == 3 and parts[0].isalpha() and parts[1].isdigit():
            try:
                rows.append({"season": f"{parts[0]} {parts[1]}", "anom": float(parts[3])})
            except ValueError:
                continue
    return rows


def monthly_sst(years=2):
    """Monthly Nino SSTs + anomalies for the regions, last `years` years.

    sstoi.indices columns: YR MON NINO1+2 ANOM NINO3 ANOM NINO4 ANOM NINO3.4 ANOM.
    """
    txt = _get("https://www.cpc.ncep.noaa.gov/data/indices/sstoi.indices").text
    mons = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    rows = []
    for line in txt.strip().splitlines()[1:]:
        p = line.split()
        if len(p) < 10 or not p[0].isdigit():
            continue
        try:
            rows.append({"label": f"{mons[int(p[1]) - 1]} {p[0][-2:]}",
                         "n34": float(p[8]), "anom": float(p[9]),
                         "n3": float(p[4]), "anom3": float(p[5]),
                         "n4": float(p[6]), "anom4": float(p[7])})
        except ValueError:
            continue
    return rows[-12 * years:]


def probability_chips(paras):
    """Pull CPC's explicit probability statements out of the discussion.

    CPC phrases them either parenthesized ('... (71% chance)') or inline
    ('... with a greater than 90% chance of a very strong event ...').
    Each match becomes a chip: the percentage + the sentence it sits in.
    """
    chips = []
    seen = set()
    for p in paras or []:
        for sent in re.split(r"(?<=[.!?])\s+", p):
            m = re.search(r"(\d{1,3})% chance", sent)
            if not m:
                continue
            pct = int(m.group(1))
            text = re.sub(r"\s+", " ", sent).strip()
            if len(text) > 180:
                # keep the tail around the match - it carries the meaning
                text = "\u2026" + text[max(0, m.start() - 150):].strip()
            key = (pct, text[:80])
            if key in seen:
                continue
            seen.add(key)
            chips.append({"pct": pct, "text": text})
    return chips[:4]


def advisory_figures(max_n=4):
    """Mirror the current advisory's figureNN.gif set (numbering rotates)."""
    out = []
    for i in range(1, max_n + 3):
        name = f"figure{i:02d}.gif"
        try:
            dst = _fetch_graphic(ADVISORY + name, os.path.join(OUT_DIR, name))
            if os.path.getsize(dst) > 5_000:
                out.append({"url": f"../enso/{name}",
                            "label": f"Advisory figure {i}"})
        except Exception:                              # noqa: BLE001
            continue
        if len(out) >= max_n:
            break
    return out


def sst_animation():
    """CPC SST-anomaly animation (rolling 12-month loop)."""
    try:
        dst = _fetch_graphic(CPC_ENSO + "sstanim.gif",
                             os.path.join(OUT_DIR, "sstanim.gif"))
        if os.path.getsize(dst) > 5_000:
            return {"url": "../enso/sstanim.gif",
                    "label": "Sea-surface temperature anomalies (12-month loop)"}
    except Exception:                                  # noqa: BLE001
        pass
    return None


def iri_plume():
    """IRI dynamic-model Nino-3.4 plume - the classic forecast graphic.

    IRI embeds it as ensoforecast.iri.columbia.edu/figure3_plot/{year}/{month}
    and the issue rotates monthly, so scrape the current forecast page.
    """
    try:
        page = _get(IRI_ENSO, timeout=40).text
        m = re.search(r"ensoforecast\.iri\.columbia\.edu/figure3_plot/[\d/]+",
                      page)
        if not m:
            return None
        url = "https://" + m.group(0)
        dst = _fetch_graphic(url, os.path.join(OUT_DIR, "iri_plume.png"))
        if os.path.getsize(dst) > 5_000:
            return {"url": "../enso/iri_plume.png",
                    "label": "IRI dynamic-model Nino 3.4 forecast plume"}
    except Exception:                                  # noqa: BLE001
        pass
    return None


def bundle(max_age=3600):
    """Everything the El Nino page needs; cached (NOAA updates daily-ish).

    Self-healing like the climate bundle: if a mirrored graphic the cached
    bundle references has vanished from disk, the cache is discarded and
    everything is re-mirrored, so a broken page can't persist a full day.
    """
    with CACHE_LOCK:
        now = time.time()
        if CACHE["b"] is not None and now - CACHE["t"] < max_age:
            cached = CACHE["b"]
            refs_ok = all(os.path.isfile(os.path.join(OUT_DIR, g["url"].split("/")[-1]))
                          for g in (cached.get("figures") or []) if g.get("url"))
            anim = cached.get("anim")
            if anim and not os.path.isfile(os.path.join(OUT_DIR, anim["url"].split("/")[-1])):
                refs_ok = False
            plume = cached.get("plume")
            if plume and not os.path.isfile(os.path.join(OUT_DIR, plume["url"].split("/")[-1])):
                refs_ok = False
            if refs_ok:
                return cached
            CACHE.update(t=0.0, b=None)   # stale refs -> full re-mirror below

    os.makedirs(OUT_DIR, exist_ok=True)

    oni = []
    try:
        oni = oni_history()
    except Exception:                                  # noqa: BLE001
        pass
    phase, phase_color = oni_status(oni[-12:] if oni else [])
    try:
        enso = enso_summary()
    except Exception:                                  # noqa: BLE001
        enso = {}
    sst = []
    try:
        sst = monthly_sst()
    except Exception:                                  # noqa: BLE001
        pass
    figures = advisory_figures()
    anim = sst_animation()
    plume = iri_plume()
    chips = probability_chips((enso or {}).get("paragraphs"))

    # crisp classification for the badge: use the alert status if present
    alert = ""
    t = (enso or {}).get("title") or ""
    m = re.search(r"Status:\s*(.+)$", t)
    if m:
        alert = m.group(1).strip()

    b = {
        "generated": _stamp(),
        "oni": oni,                       # full history for the chart
        "phase": phase, "phaseColor": phase_color,
        "alert": alert,
        "enso": enso,
        "chips": chips,
        "sst": sst,                       # monthly regional SSTs, 2 years
        "figures": figures,
        "anim": anim,
        "plume": plume,
        "source": "NOAA CPC ENSO diagnostics + IRI model plume",
    }
    with CACHE_LOCK:
        CACHE.update(t=time.time(), b=b)
    return b


def _stamp():
    import datetime
    from data import _tz
    return _tz.stamp(datetime.datetime.now(datetime.timezone.utc))

"""Flooding forecast maps: NWM streamflow overlays + WPC QPF, mirrored.

Sources (all free, keyless, verified 2026-09-29):

- NOAA National Water Model via maps.water.noaa.gov ArcGIS MapServer
  export endpoint. Transparent PNG exports over an East-TN Web-Mercator
  bbox for:
    * ana_anomaly              - current streamflow vs normal (percentile
                                  classes; the "rivers right now" layer)
    * mrf_nbm_5day_max_high_flow_magnitude
                               - NBM-blended 5-day peak-flow guidance as
                                  annual exceedance probability (the
                                  "flooding next 5 days" outlook layer)
  Exports are static PNGs (900x600 over a fixed bbox), so the site serves
  them like any other mirrored frame - no client calls to the ArcGIS
  server, no CORS, and leaflet overlays them with a fixed lat/lon bounds.

- WPC QPF fills (days 1/3/4-5 + 5-day total already mirrored by
  data/wpc.py) are linked/referenced, not duplicated here.

Everything fails soft: a dead source leaves that layer out of the payload
and the page renders the rest.
"""
import datetime as dt
import json
import os
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

OUT_DIR = os.path.join("static", "floodmaps")
CACHE = {"t": 0.0, "b": None}
CACHE_LOCK = __import__("threading").Lock()
CACHE_FILE = os.path.join("static", "floodmaps_meta.json")

SERVER = "https://maps.water.noaa.gov/server/rest/services/nwm"

# East-TN-centered Web Mercator bbox (EPSG:3857). lon -90.5..-80.0,
# lat 32.8..38.6 -> x/y mercator. 900x600 keeps each PNG ~40-80 KB.
X0, X1 = -10071503, -8905596
Y0, Y1 = 3889295, 4660300
SIZE = "900,600"

# (payload key, service, human label, when-to-use caption)
LAYERS = [
    ("nowAnomaly", "ana_anomaly",
     "Rivers right now vs normal",
     "Current NWM streamflow percentile: High (>95th) through Low. "
     "This is the 'ground truth' a flash-flood watch rides on - saturated "
     "basins here means the next storm floods faster."),
    ("outlook5day", "mrf_nbm_5day_max_high_flow_magnitude",
     "5-day high-flow outlook (AEP)",
     "NWM-NBM blend, peak streamflow over the next 5 days, as annual "
     "exceedance probability: 2% = a 50-year-ish peak, 10% = 10-year, "
     "50%/threshold = nuisance-to-minor flooding possible."),
]

# lat/lon bounds matching the bbox above (for leaflet imageOverlay);
# [lat_min, lon_min, lat_max, lon_max] like the winter frames.
LAT0, LON0 = 32.85, -90.45
LAT1, LON1 = 38.55, -80.05
BOUNDS = [LAT0, LON0, LAT1, LON1]


def _fetch_export(service, tries=3):
    """One transparent PNG export, retried (the ArcGIS server emits
    transient errors/empty responses under load), or None."""
    url = (f"{SERVER}/{service}/MapServer/export"
           f"?bbox={X0},{Y0},{X1},{Y1}&size={SIZE}"
           f"&f=image&transparent=true")
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            r.raise_for_status()
            if len(r.content) >= 500 and r.content[:8].startswith(b"\x89PNG"):
                return r.content
        except Exception:                     # noqa: BLE001
            pass
        time.sleep(2 * (i + 1))               # brief backoff, then retry
    return None


def _stamp():
    return dt.datetime.now().astimezone().strftime("%a %b %d, %I:%M %p ET")


def refresh(force=False):
    """Download current exports; payload cached in-process + on disk."""
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
    except OSError:
        pass
    with CACHE_LOCK:
        if not force and CACHE["b"] and time.time() - CACHE["t"] < 900:
            return CACHE["b"]
        layers = []
        for key, svc, label, caption in LAYERS:
            blob = _fetch_export(svc)
            if blob is None:
                continue
            fn = f"{key}.png"
            tmp = os.path.join(OUT_DIR, fn + ".tmp")
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, os.path.join(OUT_DIR, fn))
            layers.append({"key": key, "label": label, "caption": caption,
                           "pngUrl": f"/app/static/floodmaps/{fn}",
                           "bounds": BOUNDS})
        if not layers:
            # Transient ArcGIS failure (or full upstream outage): fall back
            # to the last good on-disk payload instead of publishing an
            # empty board - the PNGs on disk are still valid for hours.
            try:
                with open(CACHE_FILE, encoding="utf-8") as f:
                    prev = json.load(f)
                if prev.get("layers"):
                    prev["stale"] = True
                    CACHE["t"] = time.time()
                    CACHE["b"] = prev
                    return prev
            except (OSError, ValueError):
                pass
        payload = {"ok": bool(layers), "updated": _stamp(),
                   "layers": layers,
                   "note": ("Streamflow layers are NOAA National Water Model "
                            "graphics (water.noaa.gov); QPF outlooks are "
                            "WPC. The NWM paints entire river networks - "
                            "empty rivers are normal outside flood events.")}
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
    p = refresh(force=True)
    print(json.dumps(p, indent=1)[:900])

"""WPC analysis charts + extended outlook maps: mirrored to static/wpcmaps/.

All keyless public NOAA graphics, downloaded every refresh cycle so the site
survives the source pages' cache-busting and can render offline. Families:

- WPC unified surface analysis (fronts/isobars/station plots, latest run)
- WPC Day 2/4-5 QPF contour + filled charts (Day 1 already on national.html)
- WPC 5-day mean 500 mb + 5-day ANOMALY charts (pattern/blocked-flow view)
- SPC experimental Day 4-8 severe probability outlooks (same file family as
  the outlook/ day48 GIF; lives on the experimental pages first)
- WPC Mesoscale Precipitation Discussion current-discussion map
- OPC North Atlantic / North Pacific surface analyses + 500 mb forecasts
  (days 1-4) - the marine-desk analogs of the WPC national charts
- NOHRSC national snow model: snow depth, SWE, 24 h melt + blowing-snow
  sublimation (filename hour rotates; the /nsa/ index page is scraped for
  the current stamp, with an hour walk-back fallback)

Graphics land in static/wpcmaps/ under flat, stable names; the payload
(wpcMaps in data.json) lists everything that downloaded successfully and the
national page renders it in a selector grid. Never raises - worst case is an
empty group and a log-friendly "source unreachable" report in the payload.
"""
import datetime as dt
import os
import re
import threading
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
OUT_DIR = os.path.join("static", "wpcmaps")
CACHE = {"t": 0.0, "b": None}
CACHE_LOCK = threading.Lock()
_SESSION = requests.Session()

WPC = "https://www.wpc.ncep.noaa.gov"
OPC = "https://ocean.weather.gov"
NOHRSC = "https://www.nohrsc.noaa.gov"

# (group-id, group-label, source, [(flat name, remote URL, human label)])
CHARTS = [
    ("surface", "WPC Unified Surface Analysis",
     "wpc", [
         ("sfc_wpc.gif", f"{WPC}/sfc/usfntsfcwbg.gif",
          "Surface analysis - fronts, isobars, station plots (latest)"),
     ]),
    ("medr500", "WPC 5-day 500 mb means & anomalies",
     "wpc", [
         ("medr_500_5day.gif", f"{WPC}/medr/5dayfcst500_wbg.gif",
          "5-day mean 500 mb forecast (heights, GFS-based)"),
         ("medr_500_5day_anom.gif", f"{WPC}/medr/5dayfcst500diff_wbg.gif",
          "5-day mean 500 mb ANOMALY (ridge/trough vs normal)"),
         ("medr_500_5day_ens.gif", f"{WPC}/medr/5dayfcst500ens_wbg.gif",
          "5-day mean 500 mb ensemble spread"),
     ]),
    ("qpf", "WPC QPF days 2-5 + 5-day total",
     "wpc", [
         ("qpf_d2.gif", f"{WPC}/qpf/96ewbg.gif", "QPF - Day 2 contours"),
         ("qpf_d2_fill.gif", f"{WPC}/qpf/fill_96qwbg.gif", "QPF - Day 2 filled"),
         ("qpf_d3.gif", f"{WPC}/qpf/98ewbg.gif", "QPF - Day 3 contours"),
         ("qpf_d3_fill.gif", f"{WPC}/qpf/fill_98qwbg.gif", "QPF - Day 3 filled"),
         ("qpf_d4.gif", f"{WPC}/qpf/95ewbg.gif", "QPF - Day 4 contours"),
         ("qpf_d45.gif", f"{WPC}/qpf/99ewbg.gif", "QPF - Days 4-5 contours"),
         ("qpf_d5_fill.gif", f"{WPC}/qpf/fill_99qwbg.gif", "QPF - Days 4-5 filled"),
     ]),
    ("spc48", "SPC Day 4-8 severe outlooks",
     "spc", [
         ("spc_d4.gif", "https://www.spc.noaa.gov/products/exper/day4-8/day4prob.gif",
          "Day 4 severe probability outlook"),
         ("spc_d5.gif", "https://www.spc.noaa.gov/products/exper/day4-8/day5prob.gif",
          "Day 5 severe probability outlook"),
         ("spc_d6.gif", "https://www.spc.noaa.gov/products/exper/day4-8/day6prob.gif",
          "Day 6 severe probability outlook"),
         ("spc_d7.gif", "https://www.spc.noaa.gov/products/exper/day4-8/day7prob.gif",
          "Day 7 severe probability outlook"),
         ("spc_d8.gif", "https://www.spc.noaa.gov/products/exper/day4-8/day8prob.gif",
          "Day 8 severe probability outlook"),
         ("spc_d48.gif", "https://www.spc.noaa.gov/products/outlook/day48prob.gif",
          "Days 4-8 combined severe outlook"),
     ]),
    ("mpd", "WPC Mesoscale Precipitation Discussions",
     "mpd", [
         ("mpd_latest.gif", f"{WPC}/metwatch/latest_mdmap.gif",
          "Latest Mesoscale Precipitation Discussion map"),
     ]),
    ("opc", "OPC ocean analyses (Atlantic / Pacific)",
     "opc", [
         ("opc_atl_sfc.gif", f"{OPC}/shtml/A_full_00hrsfc.gif",
          "North Atlantic surface analysis (24 h valid)"),
         ("opc_atl_sfc_d2.gif", f"{OPC}/shtml/A_48hrsfc.gif",
          "North Atlantic surface forecast, day 2"),
         ("opc_atl_500_d1.gif", f"{OPC}/shtml/A_24hr500.gif",
          "North Atlantic 500 mb forecast, day 1"),
         ("opc_atl_500_d2.gif", f"{OPC}/shtml/A_48hr500.gif",
          "North Atlantic 500 mb forecast, day 2"),
         ("opc_atl_500_d3.gif", f"{OPC}/shtml/A_72hr500.gif",
          "North Atlantic 500 mb forecast, day 3"),
         ("opc_atl_500_d4.gif", f"{OPC}/shtml/A_96hr500.gif",
          "North Atlantic 500 mb forecast, day 4"),
         ("opc_pac_sfc.gif", f"{OPC}/shtml/P_full_00hrsfc.gif",
          "North Pacific surface analysis (24 h valid)"),
         ("opc_pac_sfc_d2.gif", f"{OPC}/shtml/P_48hrsfc.gif",
          "North Pacific surface forecast, day 2"),
         ("opc_pac_500_d1.gif", f"{OPC}/shtml/P_24hr500.gif",
          "North Pacific 500 mb forecast, day 1"),
         ("opc_pac_500_d2.gif", f"{OPC}/shtml/P_48hr500.gif",
          "North Pacific 500 mb forecast, day 2"),
         ("opc_pac_500_d3.gif", f"{OPC}/shtml/P_72hr500.gif",
          "North Pacific 500 mb forecast, day 3"),
         ("opc_pac_500_d4.gif", f"{OPC}/shtml/P_96hr500.gif",
          "North Pacific 500 mb forecast, day 4"),
     ]),
    ("snow", "NOHRSC national snow model",
     "nohrsc", [
         ("snow_depth.jpg", None, "Snow depth analysis (NOHRSC model)"),
         ("snow_swe.jpg", None, "Snow water equivalent (NOHRSC model)"),
         ("snow_melt24.jpg", None, "24-hour snow melt (NOHRSC model)"),
         ("snow_blow24.jpg", None, "24-hour blowing snow sublimation (NOHRSC)"),
     ]),
]


def _get(url, timeout=30):
    r = _SESSION.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def _fetch_graphic(url, dst_path):
    """Mirror one graphic if changed (size fast-path, atomic write)."""
    r = _get(url, timeout=40)
    if os.path.isfile(dst_path) and os.path.getsize(dst_path) == len(r.content):
        return dst_path
    tmp = dst_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, dst_path)
    return dst_path


def _nohrsc_urls():
    """Current NOHRSC national snow-model image URLs for the four elements.

    The /nsa/ index page embeds the full-res links with the current stamp
    (files rotate hourly as the model runs, ~6-hourly in practice); fall
    back to an hour walk-back when the page structure changes.
    """
    elements = {
        "snow_depth.jpg": ("nsm_depth", "Snow depth"),
        "snow_swe.jpg": ("nsm_swe", "SWE"),
        "snow_melt24.jpg": ("nsm_melt_24hr", "Melt"),
        "snow_blow24.jpg": ("nsm_blowing_snow_sub_24hr", "Blowing snow"),
    }
    found = {}
    stamps = []
    try:
        html = _get(f"{NOHRSC}/nsa/").text
        stamps = re.findall(
            r"/snow_model/images/full/National/(\w+)/(\d{6})/(\w+)_(\d{10})_National\.(?:jpg|png)",
            html)
        for flat, (elem, _lbl) in elements.items():
            hit = next((s for s in stamps if s[0] == elem), None)
            if hit:
                dset, ym, stem, stamp = hit
                found[flat] = (f"{NOHRSC}/snow_model/images/full/National/"
                               f"{dset}/{ym}/{stem}_{stamp}_National.jpg")
    except Exception:  # noqa: BLE001
        pass
    missing = [flat for flat in elements if flat not in found]
    if missing:
        # hour walk-back fallback (index page unreachable / renamed)
        now = dt.datetime.now(dt.timezone.utc)
        for flat in missing:
            elem, _ = elements[flat]
            for back in range(0, 40):
                h = now - dt.timedelta(hours=back)
                url = (f"{NOHRSC}/snow_model/images/full/National/"
                       f"{elem}/{h:%Y%m}/{elem}_{h:%Y%m%d%H}_National.jpg")
                try:
                    r = _SESSION.get(url, headers=UA, timeout=20, stream=True)
                    ok = r.status_code == 200
                    r.close()
                except Exception:  # noqa: BLE001
                    ok = False
                if ok:
                    found[flat] = url
                    break
    return found


def refresh():
    """Mirror every reachable chart into static/wpcmaps/ (idempotent)."""
    os.makedirs(OUT_DIR, exist_ok=True)
    nohrsc = _nohrsc_urls()
    groups = []
    ok_count = 0
    for gid, label, source, items in CHARTS:
        charts = []
        for flat, url, human in items:
            if source == "nohrsc":
                url = nohrsc.get(flat)
            if not url:
                continue
            try:
                _fetch_graphic(url, os.path.join(OUT_DIR, flat))
            except Exception:  # noqa: BLE001 - one dead chart never blocks the group
                continue
            charts.append({"file": flat, "label": human,
                           "url": f"../wpcmaps/{flat}"})
        if charts:
            ok_count += len(charts)
            groups.append({"id": gid, "label": label, "charts": charts})
    return groups, ok_count


def _stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bundle(max_age=3600):
    """Payload for the national page: mirrored groups + a source report."""
    with CACHE_LOCK:
        now = time.time()
        if CACHE["b"] is not None and now - CACHE["t"] < max_age:
            cached = CACHE["b"]
            refs_ok = all(
                os.path.isfile(os.path.join(OUT_DIR, c["file"]))
                for g in cached.get("groups", []) for c in g.get("charts", []))
            if refs_ok:
                return cached
            CACHE.update(t=0.0, b=None)   # stale refs -> re-mirror below

    try:
        groups, ok_count = refresh()
    except Exception:  # noqa: BLE001 - never break a build
        groups, ok_count = [], 0

    b = {
        "generated": _stamp(),
        "groups": groups,
        "count": ok_count,
        "source": ("NOAA WPC analysis charts, SPC Day 4-8 outlooks, OPC ocean "
                   "analyses, NOHRSC snow model - all free, no keys"),
    }
    with CACHE_LOCK:
        CACHE.update(t=time.time(), b=b)
    return b

"""Heat-Stress Index: how hot it FEELS and how dangerous it is.

One number visitors can act on, built from official inputs:
- Heat index (Rothfusz regression, NWS standard) - shade comfort;
- WBGT forecast (NWS wetBulbGlobeTemperature gridpoints) - sun/effort safety.

The card shows both plus the next-24h peak of each, an hourly curve, and
NWS-style risk bands (caution 80F / extreme caution 90F / danger 103F /
extreme danger 125F for heat index; 82/85/88/90/93F for WBGT - the same
bands the WBGT map already paints).
"""
import datetime as dt
import math
import re
import threading
import time

import requests

from data import _tz
from data.wbgt import _wbgt_series, _cat as _wbgt_cat

UA = {"User-Agent": "Mozilla/5.0 tnwx-site/1.0"}
_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()
_TTL = 1800          # 30 min - inputs refresh hourly upstream


def heat_index_f(t_f, rh):
    """NWS Rothfusz heat index (F); simple offset formula outside 80-112 F."""
    if t_f is None or rh is None:
        return None
    if t_f < 80:
        # below the regression's valid range: NWS uses the simple formula
        return 0.5 * (t_f + 61.0 + (t_f - 68.0) * 1.2 + rh * 0.094)
    t2, r2 = t_f * t_f, rh * rh
    hi = (-42.379 + 2.04901523 * t_f + 10.14333127 * rh
          - 0.22475541 * t_f * rh - 0.00683783 * t2 - 0.05481717 * r2
          + 0.00122874 * t2 * rh + 0.00085282 * t_f * r2 - 0.00000199 * t2 * r2)
    # adjustments per NWS footnote
    if rh < 13 and 80 <= t_f <= 112:
        hi -= ((13.0 - rh) / 4.0) * math.sqrt((17.0 - abs(t_f - 95.0)) / 17.0)
    elif rh > 85 and 80 <= t_f <= 87:
        hi += ((rh - 85.0) / 10.0) * ((87.0 - t_f) / 5.0)
    return hi


def _hi_cat(hi):
    """NWS heat-index risk bands."""
    if hi is None:
        return None, None
    if hi >= 125:
        return "extreme danger", "#c828c8"
    if hi >= 103:
        return "danger", "#eb3c3c"
    if hi >= 90:
        return "extreme caution", "#ffb242"
    if hi >= 80:
        return "caution", "#ffe066"
    return "low", "#81c784"


def _obs_home(lat, lon):
    """Latest METAR temp/RH near home (no API key)."""
    try:
        r = requests.get(
            f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}/stations",
            headers=UA, timeout=15)
        st = ((r.json().get("features") or [{}])[0].get("properties") or {})
        sid = st.get("stationIdentifier")
        if not sid:
            return None
        r2 = requests.get(
            f"https://api.weather.gov/stations/{sid}/observations/latest",
            headers=UA, timeout=15)
        p = (r2.json().get("properties") or {})
        tc = (p.get("temperature") or {}).get("value")
        rh = (p.get("relativeHumidity") or {}).get("value")
        if tc is None:
            return None
        return {"station": sid, "tempF": tc * 9 / 5 + 32,
                "rh": round(rh) if rh is not None else None,
                "time": p.get("timestamp")}
    except Exception:                              # noqa: BLE001
        return None


def _series_combo(lat, lon):
    """Next-24h hourly (valid_dt, heatIndex_F, wbgt_F) from NWS WBGT series.

    Heat index needs temp+RH hourly; the WBGT gridpoint payload does not
    carry them, so the hourly curve uses OBSERVED temp/RH scaled by the
    WBGT forecast's own diurnal shape - good to ~1 F for the card's purpose.
    """
    ser = _wbgt_series(lat, lon)
    obs = _obs_home(lat, lon)
    if not ser:
        return [], obs
    now = dt.datetime.now(dt.timezone.utc)
    nxt = [(t, w) for t, w in sorted(ser) if now <= t <= now + dt.timedelta(hours=24)]
    if not nxt:
        return [], obs
    out = []
    base_t = obs["tempF"] if obs else None
    base_rh = obs["rh"] if obs else 60
    w0 = nxt[0][1] or 0
    for t, w in nxt:
        if base_t is not None:
            t_f = base_t + (w - w0) * 1.6          # WBGT tracks temp ~1.6:1
            rh = max(15, min(100, (base_rh or 60) + (w0 - w)))
        else:
            t_f = w + 18.0                          # rough WBGT->temp fallback
            rh = 55
        out.append((t, heat_index_f(t_f, rh), w))
    return out, obs


def heat_index_bundle(lat=None, lon=None, place="home", max_age=_TTL):
    """Card payload: current + 24 h peaks + hourly curve, home and cities."""
    from data.observations import EAST_TN_CITIES
    if lat is None:
        from config import LATITUDE as lat, LONGITUDE as lon
    now = time.time()
    with _lock:
        if _cache["data"] and now - _cache["at"] < max_age:
            return _cache["data"]

    def build_for(la, lo):
        series, obs = _series_combo(la, lo)
        if not series:
            return None
        hi_now = series[0][1]
        wb_now = series[0][2]
        hi_peak_t, hi_peak = max(series, key=lambda x: x[1] or -999)[:1][0], \
            max(s[1] or -999 for s in series)
        wb_peak_t, wb_peak = max(series, key=lambda x: x[2])[:1][0], \
            max(s[2] for s in series)
        hcat, hcol = _hi_cat(hi_peak)
        wcat, wcol = _wbgt_cat(wb_peak)
        from data._tz import day_hm
        return {
            "nowHi": round(hi_now) if hi_now is not None else None,
            "nowHiCat": _hi_cat(hi_now)[0],
            "nowHiColor": _hi_cat(hi_now)[1],
            "nowWbgt": round(wb_now),
            "nowWbgtCat": _wbgt_cat(wb_now)[0],
            "nowWbgtColor": _wbgt_cat(wb_now)[1],
            "peakHi": round(hi_peak) if hi_peak > -999 else None,
            "peakHiTime": day_hm(hi_peak_t),
            "peakHiCat": hcat, "peakHiColor": hcol,
            "peakWbgt": round(wb_peak),
            "peakWbgtTime": day_hm(wb_peak_t),
            "peakWbgtCat": wcat, "peakWbgtColor": wcol,
            "hours": [{"t": day_hm(t), "hi": round(h) if h is not None else None,
                       "wbgt": round(w)} for t, h, w in series[::2]],
            "obs": obs,
        }

    data = {"ok": False, "place": place}
    try:
        data["home"] = build_for(lat, lon)
        data["ok"] = bool(data["home"])
    except Exception:                              # noqa: BLE001
        data["home"] = None

    cities = {}

    def city_job(kv):
        city, (la, lo) = kv
        try:
            r = build_for(la, lo)
            if r:
                cities[city] = {"peakHi": r["peakHi"], "peakHiCat": r["peakHiCat"],
                                "peakHiColor": r["peakHiColor"],
                                "peakWbgt": r["peakWbgt"],
                                "peakWbgtTime": r["peakWbgtTime"]}
        except Exception:                          # noqa: BLE001
            pass

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(city_job, EAST_TN_CITIES.items()))
    data["cities"] = cities
    data["fetched"] = _tz.full(dt.datetime.now(dt.timezone.utc))
    with _lock:
        _cache["at"] = now
        _cache["data"] = data
    return data

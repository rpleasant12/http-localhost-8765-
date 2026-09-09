"""PSU e-Wall HRRR future-radar loop (no key).

Penn State's e-Wall renders the *current* HRRR 3-km run as a smooth 15-minute
future-radar animation: rad1.gif = forecast hour 1 (V001), rad69.gif = forecast
hour 18 (V018). The frames are complete CONUS maps (state lines + colorbar baked
in), so they are shown as an auto-playing image loop (the MPAS/SHiELD pattern)
rather than as a transparent overlay.

The valid time of each frame is recovered from the HRRR cycle that the e-Wall is
currently showing. That cycle is the newest HRRR run whose f18 field has been
published on the NOAA open-data bucket (we probe it directly). The label on each
frame confirms the mapping: V001 = init + 1 h, V018 = init + 18 h.

Frames are downloaded to static/psu_hrrr/ and served from /app/static, cached on
disk so repeated loads are instant. The whole loop is refreshed whenever the
underlying cycle changes (roughly hourly).
"""
import datetime as dt
import os
import re
import threading
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tnwx/1.0"}

EWALL_URL = "https://www.meteo.psu.edu/ewall/HRRR15_CUR/"
HRRR_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"

STATIC_DIR = os.path.join("static", "psu_hrrr")
os.makedirs(STATIC_DIR, exist_ok=True)

# ----------------------------------------------------------------- caching
_CACHE = {}
_LOCK = threading.Lock()
_TTL = 900  # 15 min for the page + cycle probes


def _cached(key, fn, ttl=_TTL):
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _LOCK:
        _CACHE[key] = (time.time(), val)
    return val


def _get(url, timeout=25):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


# ----------------------------------------------------------------- frame list
_FRAME_RE = re.compile(r"modImages\[\d+\]\s*=\s*\"([^\"]+)\"")


def _ewall_frame_files():
    """Ordered list of radN.gif filenames as listed on the e-Wall page."""
    def _fetch():
        try:
            txt = _get(EWALL_URL).text
        except requests.RequestException:
            return []
        files = _FRAME_RE.findall(txt)
        # keep only radN.gif entries, dedupe, preserve page order
        seen, out = set(), []
        for f in files:
            if re.match(r"^rad\d+\.gif$", f) and f not in seen:
                seen.add(f)
                out.append(f)
        return out
    return _cached("ewall_files", _fetch)


# ----------------------------------------------------------------- init cycle
def _hrrr_init():
    """Newest HRRR cycle whose f18 field exists on the NOAA bucket.

    The e-Wall shows the current run, so this is the cycle its frames are valid
    for. Cached per cycle change.
    """
    def _probe():
        now = dt.datetime.now(dt.timezone.utc)
        for back in range(0, 10):
            c = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
            u = (f"{HRRR_BUCKET}/hrrr.{c:%Y%m%d}/conus/"
                 f"hrrr.t{c:%H}z.wrfsfcf18.grib2.idx")
            try:
                r = requests.head(u, headers=UA, timeout=15)
            except requests.RequestException:
                continue
            if r.status_code == 200:
                return c
        return None
    return _cached("hrrr_init", _probe)


# ----------------------------------------------------------------- download
def _download(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 5_000:
        return dest
    r = _get(url, timeout=60)
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, dest)
    return dest


# ----------------------------------------------------------------- bundle
def psu_hrrr_loop(max_frames=24):
    """Download (cached) the newest PSU HRRR future-radar frames.

    Returns {'init', 'cycle', 'frames': [{'file','label','time'}]} or None.
    Frames are pre-sampled to the latest `max_frames` (the newest forecast
    hours), oldest first, so the loop animates forward in time.
    """
    files = _ewall_frame_files()
    if not files:
        return None
    init = _hrrr_init()
    if init is None:
        return None

    sub = files[-max_frames:]
    # Frame i (0-based) is forecast hour 1 + 0.25*i, so valid = init + that.
    # The first file in `files` is rad1 = V001.
    first_idx = len(files) - len(sub)
    out = []
    for i, fname in enumerate(sub):
        frame_no = first_idx + i  # 0-based within the full loop
        fh = 1.0 + 0.25 * frame_no
        valid = init + dt.timedelta(hours=fh)
        dest = os.path.join(STATIC_DIR, fname)
        url = EWALL_URL + fname
        try:
            _download(url, dest)
        except requests.RequestException:
            continue
        out.append({
            "file": f"/app/static/psu_hrrr/{fname}",
            "label": f"F{fh:04.1f} \u00b7 {valid:%a %H:%MZ}",
            "time": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    if not out:
        return None
    return {
        "init": init.strftime("%Y-%m-%d %HZ"),
        "cycle": init.strftime("%Y%m%d%H"),
        "frames": out,
    }

"""Local radar nowcast: +10/+20/+30 min extrapolation of the MRMS mosaic.

RainViewer's free API intermittently publishes an EMPTY nowcast list (and its
tilecache has outages), so the site's "Nowcast (+10-30 min)" layer went dark.
This module builds the nowcast from what we already render every 2 minutes:

1. Load the newest MRMS composite-reflectivity (cref) PNGs (static/mrms/).
2. Measure echo motion: find the most active (highest-variance) block in the
   newest frame and locate its best match in a ~10-minute-older reference
   frame (mean-removed SSD search, +/-MAX_SEARCH_PX px). Block tracking on
   the echo field beats whole-image phase correlation here, which static
   map furniture (borders, colorbars) anchors at zero.
3. Extrapolate the newest frame +10/+20/+30 minutes along that motion and
   render PNGs into static/nowcast/ (same Mercator bounds as MRMS).

No network, no keys, no GRIB decode: pure numpy + Pillow on frames the MRMS
renderer already produced. Quiet maps (no echoes worth tracking) fall back
to persistence (motion 0,0), which is the honest nowcast for a dry regime.
"""
import datetime as dt
import json
import os
import threading
import time

import numpy as np
from PIL import Image

MRMS_DIR = "static/mrms"
OUT_DIR = "static/nowcast"
REGISTRY = os.path.join(OUT_DIR, "registry.json")

STEP_MIN = 10
STEPS = (1, 2, 3)          # +10, +20, +30 min
MAX_SEARCH_PX = 40         # echo-matching search radius (pixels)
BASELINE_MS = 600_000      # preferred motion baseline: 10 minutes
BLOCK = 160                # echo block size for tracking
MIN_BLOCK_STD = 0.02       # below this the block is furniture, not echoes
MIN_MAP_STD = 0.015        # below this the whole map is quiet -> persistence

_cache = {"at": 0.0, "frames": None}
_lock = threading.Lock()


# ------------------------------------------------------------ frame inputs

def _newest_cref_pngs(n=6):
    """Newest n rendered MRMS cref PNGs, oldest first."""
    try:
        with open(os.path.join(MRMS_DIR, "descriptors_cref.json"),
                  encoding="utf-8") as f:
            desc = json.load(f)
    except (OSError, ValueError):
        return []
    try:
        with open(os.path.join(MRMS_DIR, "registry.json"),
                  encoding="utf-8") as f:
            reg = json.load(f)
    except (OSError, ValueError):
        reg = {}
    out = []
    for d in desc:
        e = reg.get(d.get("id")) or {}
        if e.get("status") != "done":
            continue
        p = os.path.join(MRMS_DIR, f"{d['id']}.png")
        if os.path.isfile(p):
            out.append({"id": d["id"], "path": p, "time": d.get("time"),
                        "bounds": e.get("bounds")})
    out.sort(key=lambda x: x["id"])
    return out[-n:]


def _ts(fid):
    return dt.datetime.strptime(fid, "mrms_cref_%Y%m%d%H%M") \
        .replace(tzinfo=dt.timezone.utc).timestamp()


def _future_label(mins, valid_utc):
    hhmm = valid_utc.strftime("%H:%M")
    if mins < 60:
        return f"+{mins}m {hhmm}"
    return f"+{mins // 60}h{mins % 60:02d} {hhmm}"


# ------------------------------------------------------------ echo motion

def _gray(path):
    return np.asarray(Image.open(path).convert("L"), dtype=np.float32) / 255.0


def _best_block(b):
    """Position (y, x) and std of the highest-variance BLOCK in b."""
    h, w = b.shape
    c1 = np.cumsum(np.cumsum(b, axis=0), axis=1)
    c2 = np.cumsum(np.cumsum(b * b, axis=0), axis=1)
    c1 = np.pad(c1, ((1, 0), (1, 0)))
    c2 = np.pad(c2, ((1, 0), (1, 0)))
    s = c1[BLOCK:, BLOCK:] - c1[:-BLOCK, BLOCK:] - c1[BLOCK:, :-BLOCK] + c1[:-BLOCK, :-BLOCK]
    s2 = c2[BLOCK:, BLOCK:] - c2[:-BLOCK, BLOCK:] - c2[BLOCK:, :-BLOCK] + c2[:-BLOCK, :-BLOCK]
    var = s2 / BLOCK - (s / BLOCK) ** 2
    idx = int(np.argmax(var))
    y, x = divmod(idx, var.shape[1])
    return y, x, float(np.sqrt(max(0.0, var[y, x])))


def _motion_px(a_path, b_path):
    """Echo shift (dx, dy) pixels from older frame a to newer frame b."""
    a = _gray(a_path)
    b = _gray(b_path)
    if a.shape != b.shape:
        return 0, 0
    if b.std() < MIN_MAP_STD:
        return 0, 0                       # quiet map: persistence
    y0, x0, bstd = _best_block(b)
    if bstd < MIN_BLOCK_STD:
        return 0, 0
    tb = b[y0:y0 + BLOCK, x0:x0 + BLOCK]
    tb -= tb.mean()
    tvar = float((tb * tb).sum()) + 1e-9
    h, w = a.shape

    def ssd_at(dx, dy):
        ys, xs = y0 + dy, x0 + dx
        if ys < 0 or xs < 0 or ys + BLOCK > h or xs + BLOCK > w:
            return None
        ta = a[ys:ys + BLOCK, xs:xs + BLOCK]
        ta = ta - ta.mean()
        return float(((tb - ta) ** 2).sum()) / tvar

    # coarse scan (step 2 px), then refine (step 1 px) around the optimum
    best = (0, 0, ssd_at(0, 0) or 1e9)
    for dy in range(-MAX_SEARCH_PX, MAX_SEARCH_PX + 1, 2):
        for dx in range(-MAX_SEARCH_PX, MAX_SEARCH_PX + 1, 2):
            v = ssd_at(dx, dy)
            if v is not None and v < best[2]:
                best = (dx, dy, v)
    bx, by, _ = best
    for dy in range(max(-MAX_SEARCH_PX, by - 2), min(MAX_SEARCH_PX, by + 2) + 1):
        for dx in range(max(-MAX_SEARCH_PX, bx - 2), min(MAX_SEARCH_PX, bx + 2) + 1):
            v = ssd_at(dx, dy)
            if v is not None and v < best[2]:
                best = (dx, dy, v)
    dx, dy, ssd = best
    base0 = ssd_at(0, 0)
    if base0 is not None and ssd > 0.92 * base0:
        return 0, 0    # shifting does not explain the change: no real motion
    return int(dx), int(dy)


def _shift(arr, dx, dy):
    """Zero-filled integer shift (out-of-domain = empty radar)."""
    out = np.zeros_like(arr)
    h, w = arr.shape[:2]
    ys_src = slice(max(0, -dy), h - max(0, dy))
    ys_dst = slice(max(0, dy), h - max(0, -dy))
    xs_src = slice(max(0, -dx), w - max(0, dx))
    xs_dst = slice(max(0, dx), w - max(0, -dx))
    out[ys_dst, xs_dst] = arr[ys_src, xs_src]
    return out


# ---------------------------------------------------------------- rendering

def render_nowcast(max_age_min=30):
    """Build +10/+20/+30 min extrapolation PNGs; returns frame dicts."""
    with _lock:
        frames = _newest_cref_pngs(6)
        if len(frames) < 2:
            return []
        base = frames[-1]
        # measure motion over ~10 minutes when possible: 2-minute baselines
        # are too coarse for integer-pixel matching (sub-pixel drift is lost)
        bt = _ts(base["id"])
        best, best_err = None, None
        for c in frames[:-1]:
            err = abs((bt - _ts(c["id"])) - BASELINE_MS / 1000)
            if best_err is None or err < best_err:
                best, best_err = c, err
        prev = best or frames[-2]
        bt_dt = dt.datetime.fromtimestamp(bt, dt.timezone.utc)
        if (dt.datetime.now(dt.timezone.utc) - bt_dt).total_seconds() \
                > max_age_min * 60:
            return []

        dx, dy = _motion_px(prev["path"], base["path"])
        dt_min = max(1.0, (bt - _ts(prev["id"])) / 60.0)
        vx, vy = dx / dt_min, dy / dt_min   # px per minute

        img_b = Image.open(base["path"])
        img_b.load()
        arr_rgb = np.asarray(img_b.convert("RGB"), dtype=np.uint8)
        alpha = np.asarray(img_b.convert("RGBA"))[..., 3]

        reg = {}
        if os.path.isfile(REGISTRY):
            try:
                with open(REGISTRY, encoding="utf-8") as f:
                    reg = json.load(f)
            except (OSError, ValueError):
                reg = {}

        out = []
        for s in STEPS:
            mins = int(STEP_MIN * s)
            fid = f"nowcast_{base['id'].split('_')[-1]}_{mins:03d}"
            valid = bt_dt + dt.timedelta(minutes=mins)
            sx, sy = int(round(vx * mins)), int(round(vy * mins))
            png = os.path.join(OUT_DIR, fid + ".png")
            if not os.path.isfile(png) or sx or sy:
                rgb = _shift(arr_rgb, sx, sy)
                al = _shift(alpha, sx, sy)
                Image.fromarray(np.dstack([rgb, al]), "RGBA").save(
                    png, optimize=True)
            reg[fid] = {
                "id": fid, "status": "done",
                "label": _future_label(mins, valid),
                "time": valid.strftime("%Y-%m-%dT%H:%M:00Z"),
                "bounds": base["bounds"],
            }
            out.append({
                "kind": "nowcast", "id": fid,
                "label": _future_label(mins, valid),
                "time": int(valid.timestamp()),   # epoch: matches RainViewer frames
                "pngUrl": f"/app/static/nowcast/{fid}.png",
                "bounds": base["bounds"],
            })

        keep = {f["id"] for f in out}
        now_e = time.time()
        for fid in list(reg):
            if fid in keep:
                continue
            # keep previous generations for a while: a data.json built just
            # before this render still references those PNGs - deleting them
            # immediately 404s the live page (bit us 2026-09-10)
            try:
                base_ts = _ts("mrms_cref_" + fid.split("_")[1])
            except (IndexError, ValueError):
                base_ts = 0
            if now_e - base_ts < 900:
                continue
            try:
                os.remove(os.path.join(OUT_DIR, fid + ".png"))
            except OSError:
                pass
            reg.pop(fid, None)
        tmp = REGISTRY + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f)
        os.replace(tmp, REGISTRY)
        return out


# ----------------------------------------------------------------- bundle

def nowcast_bundle():
    """UI snapshot for the site: rendered frames with app-static pngUrls.

    Runs a render attempt (two PNG decodes + a small SSD search) at most
    every 60 s; the updater's 2-minute cycle keeps it fresh.
    """
    now = time.time()
    if _cache["frames"] is None or now - _cache["at"] > 60:
        try:
            _cache["frames"] = render_nowcast()
        except Exception:
            _cache["frames"] = []
        _cache["at"] = now
    return _cache["frames"] or []

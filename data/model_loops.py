"""Animated forecast-model map loops: background hour-by-hour rendering.

The NWS Models tab renders single maps on demand. Loops need every forecast
hour of a product rendered to disk, which can take minutes - so a persistent
worker thread renders hours one at a time (disk-cached by model_maps) and the
UI polls a fast snapshot: frames appear progressively, like the future-radar
renderer. Only one loop is active at a time; changing the selection swaps the
work queue and the worker drops its progress-free position.
"""
import datetime as dt
import os
import threading
import time

from data.model_maps import MAP_DIR, find_cycle, render_product_map

_LOCK = threading.Lock()
_WORKER = None
# the loop the worker should be chewing on
_DESIRED = {"key": None, "model": None, "product": None, "hours": [], "gen": 0}
# successful find_cycle results live 20 min; failures 2 min (S3/NOMADS blips)
_CYCLE_CACHE = {}


def cached_cycle(model):
    """find_cycle with a TTL cache - it probes several idx URLs per call."""
    now = time.time()
    hit = _CYCLE_CACHE.get(model)
    if hit:
        ts, cycle = hit
        ok = cycle is not None
        if now - ts < (1200 if ok else 120):
            return cycle
    cycle = find_cycle(model)
    _CYCLE_CACHE[model] = (now, cycle)
    return cycle


def _png_path(model, product, cycle, fh):
    return os.path.join(MAP_DIR, f"{model}_{product}_f{fh:03d}_{cycle:%Y%m%d%H}.png")


def _ensure_worker():
    global _WORKER
    if _WORKER and _WORKER.is_alive():
        return

    def _worker():
        while True:
            with _LOCK:
                gen = _DESIRED["gen"]
                model = _DESIRED["model"]
                product = _DESIRED["product"]
                hours = list(_DESIRED["hours"])
            if model is None:
                time.sleep(2)
                continue
            cycle = cached_cycle(model)
            if cycle is None:
                time.sleep(30)
                continue
            for fh in hours:
                with _LOCK:
                    if _DESIRED["gen"] != gen:
                        break  # selection changed - abandon this loop
                png = _png_path(model, product, cycle, fh)
                if os.path.exists(png) and os.path.getsize(png) > 10_000:
                    continue
                try:
                    render_product_map(model, cycle, fh, product)
                except Exception:  # noqa: BLE001 - skip bad hour, keep looping
                    time.sleep(20)
                time.sleep(1)
            time.sleep(60)  # loop done - idle, restart if hours go missing

    _WORKER = threading.Thread(target=_worker, daemon=True, name="model-loop")
    _WORKER.start()


def ensure_loop(model, product, hours):
    """Queue hour-by-hour rendering for this model/product (one active loop)."""
    hours = list(hours)
    with _LOCK:
        key = (model, product, tuple(hours))
        if _DESIRED["key"] != key:
            _DESIRED.update(key=key, model=model, product=product,
                            hours=hours, gen=_DESIRED["gen"] + 1)
    _ensure_worker()


def loop_bundle(model, product, hours):
    """Fast UI snapshot: which hours of this loop are already on disk."""
    cycle = cached_cycle(model)
    frames = []
    if cycle is not None:
        for fh in hours:
            png = _png_path(model, product, cycle, fh)
            if os.path.exists(png) and os.path.getsize(png) > 10_000:
                valid = cycle + dt.timedelta(hours=fh)
                frames.append({
                    "fh": fh,
                    "url": f"/app/static/model_maps/{os.path.basename(png)}",
                    "label": f"+{fh}h",
                    "valid": valid.strftime("%a %d %H:%MZ"),
                })
    return {
        "cycle": cycle,
        "total": len(list(hours)),
        "ready": len(frames),
        "frames": frames,
    }

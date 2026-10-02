"""Global network timeout shim + total-transfer deadline.

The updater has repeatedly hung mid-collect on NOAA/GEFS fetches whose
sockets stall without ever erroring. Two distinct hang flavors, two layers:

1. NO timeout at all (~25 call sites passed none). Fixed by wrapping
   Session.request once so EVERY HTTP call carries a hard (connect, read)
   timeout unless the caller set one. Idle-silence hangs die in 45 s.

2. THE DRIP HANG (2026-10-01 08:55 GEFS wedge: ssl.recv stuck for the rest
   of a 45-min cycle WITH layer 1 armed). requests' read timeout only
   bounds silence BETWEEN bytes - a server trickling one byte every few
   seconds never trips it, and the transfer grows without bound. Fixed
   with a total wall-clock deadline per request. When it trips:
   session.close() runs first (harmless), then a short grace later an
   async SystemExit is injected into the main thread to wake the wedged
   C-level recv. The wrapper CATCHES that injected exit and converts it
   into an ordinary requests ConnectionError - so callers just see a
   failed fetch (skipped, cycle continues), not a process death. A race
   guard clears any still-pending injection if the call completes anyway.
   A bounded, diagnosable rescue beats an unbounded stall (and the 1-min
   task restarts the updater in ~12 s even in the worst case).

Idempotent: importing twice never stacks wrappers. Escape hatches:
TNWX_NO_NET_SHIM=1 disables everything; TNWX_NET_TIMEOUT / TNWX_NET_DEADLINE
tune the two bounds (seconds).
"""
import collections
import json as _json
import os
import threading
import time

DEFAULT_TIMEOUT = 45          # (connect, read) silence bound - seconds
DEFAULT_DEADLINE = 300        # TOTAL wall-clock bound per request - seconds

# ---- upstream-latency telemetry (models-page feed-health badge) ----------
# Every request's duration lands in a small ring; a throttled writer
# mirrors rolling stats to .freebuff/net_latency.json so the site build
# can show how slow the upstream model feeds actually are (2026-10-01).
_LAT_RING = collections.deque(maxlen=400)     # (epochS, durS, err?)
_LAT_LOCK = threading.Lock()
_LAT_SINCE = {"n": 0, "t": 0.0}               # calls since last flush
_LAT_FLUSH_EVERY = 25                          # calls between flushes
_LAT_FLUSH_S = 60.0                            # ... or this many seconds
_LAT_WINDOW_S = 1800                           # stats cover the last 30 min
_LAT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".freebuff", "net_latency.json")


def _lat_stats():
    """Rolling latency stats over the ring's recent entries (or None)."""
    now = time.time()
    recent = [d for (ts, d, _e) in _LAT_RING if now - ts <= _LAT_WINDOW_S]
    if not recent:
        return None
    errs = sum(1 for (ts, _d, e) in _LAT_RING if e and now - ts <= _LAT_WINDOW_S)
    srt = sorted(recent)
    p95 = srt[min(len(srt) - 1, int(len(srt) * 0.95))]
    oldest = min(ts for (ts, _d, _e) in _LAT_RING if now - ts <= _LAT_WINDOW_S)
    return {"n": len(recent), "errN": errs,
            "avgS": round(sum(recent) / len(recent), 2),
            "p95S": round(p95, 2), "maxS": round(max(recent), 2),
            "windowMin": round((now - oldest) / 60.0, 1)}


def _lat_flush():
    """Atomically mirror the rolling stats (best-effort, never raises)."""
    try:
        st = _lat_stats()
        if st is None:
            return
        st["updatedEpochS"] = int(time.time())
        os.makedirs(os.path.dirname(_LAT_PATH), exist_ok=True)
        tmp = _LAT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_json.dumps(st))
        os.replace(tmp, _LAT_PATH)
    except Exception:                                    # noqa: BLE001
        pass


def _lat_record(dur_s, err=False):
    """Append one request duration; flush to the mirror when due."""
    try:
        due = False
        with _LAT_LOCK:
            _LAT_RING.append((time.time(), float(dur_s), bool(err)))
            _LAT_SINCE["n"] += 1
            if (_LAT_SINCE["n"] >= _LAT_FLUSH_EVERY
                    or time.time() - _LAT_SINCE["t"] >= _LAT_FLUSH_S):
                _LAT_SINCE["n"] = 0
                _LAT_SINCE["t"] = time.time()
                due = True
        if due:
            _lat_flush()
    except Exception:                                    # noqa: BLE001
        pass

_ENV_T = "TNWX_NET_TIMEOUT"
_ENV_D = "TNWX_NET_DEADLINE"
_GRACE_S = 2.0                # close() attempt -> async-injection delay

_applied = False
_main_tid = None


def _num(env, fallback):
    try:
        return float(os.environ.get(env, "") or fallback)
    except ValueError:
        return float(fallback)


def _timeout_of(kwargs):
    """Explicit caller timeout wins; otherwise the shim default."""
    t = kwargs.get("timeout")
    if t is None:
        t = _num(_ENV_T, DEFAULT_TIMEOUT)
    return t


def _deadline_of():
    return _num(_ENV_D, DEFAULT_DEADLINE)


def _async_exit(tid):
    """Inject SystemExit into the main thread (wake a wedged C-level recv)."""
    import ctypes
    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_long(tid), ctypes.py_object(SystemExit))


def _async_clear(tid):
    """Cancel a pending (never-delivered) injected exception."""
    import ctypes
    ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, ctypes.py_object(None))


def install():
    global _applied, _main_tid
    if _applied or os.environ.get("TNWX_NO_NET_SHIM"):
        return False
    import requests
    import requests.sessions
    import requests.exceptions  # noqa: F401 - used in the wrapper's except

    original = requests.sessions.Session.request
    _main_tid = threading.get_ident()   # installer runs on the main thread

    def request(self, method, url, **kwargs):
        kwargs["timeout"] = _timeout_of(kwargs)
        deadline = _deadline_of()
        trip = {"fired": False, "injected": False}

        def _on_deadline():
            trip["fired"] = True
            print(f"net deadline: {method} {url} exceeded {deadline:g}s "
                  f"total (drip hang?) - unblocking", flush=True)
            try:
                self.close()   # harmless; checked-out conns are not in pool
            except Exception:                                # noqa: BLE001
                pass

            def _inject():
                if trip["injected"]:
                    return
                trip["injected"] = True
                if threading.main_thread().is_alive():
                    print(f"net deadline: {method} {url} unblocking main "
                          f"thread via injected exit", flush=True)
                    _async_exit(_main_tid)

            t2 = threading.Timer(_GRACE_S, _inject)
            t2.daemon = True
            trip["t2"] = t2
            t2.start()

        timer = threading.Timer(deadline, _on_deadline)
        timer.daemon = True
        timer.start()
        t0 = time.monotonic()
        err = False
        try:
            return original(self, method, url, **kwargs)
        except SystemExit:
            # OUR injected exit (async wakeup of a wedged C-level recv):
            # convert to the ordinary exception every caller already catches.
            err = True
            if trip["injected"]:
                raise requests.exceptions.ConnectionError(
                    f"{method} {url} exceeded {deadline:g}s total deadline "
                    f"(unblocked by net shim)") from None
            raise
        except Exception:
            err = True
            raise
        finally:
            timer.cancel()
            t2 = trip.get("t2")
            if t2 is not None:
                t2.cancel()
            try:
                _lat_record(time.monotonic() - t0, err)
            except Exception:                                # noqa: BLE001
                pass
            if trip["injected"]:
                # RACE GUARD: the call completed after the injector checked
                # but before delivery - drop any still-pending async exit.
                try:
                    _async_clear(_main_tid)
                except Exception:                            # noqa: BLE001
                    pass

    requests.sessions.Session.request = request
    _applied = True
    return True


def report():
    """One line for the updater log: shim armed + effective bounds."""
    if not _applied:
        return "net shim: not installed (disabled or already present)"
    return (f"net shim: every requests call carries a "
            f"{_num(_ENV_T, DEFAULT_TIMEOUT):g}s (connect, read) timeout "
            f"and a {_num(_ENV_D, DEFAULT_DEADLINE):g}s total deadline")

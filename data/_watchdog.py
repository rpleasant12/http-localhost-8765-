"""Cycle watchdog: turn silent updater hangs into diagnosable deaths.

Every previous "updater died quietly mid-collect" left no evidence because
the process was killed externally (the startup-task watchdog) or blocked
forever with nothing in the log. faulthandler.dump_traceback() writes every
thread's Python stack - the exact frame the process sat in - so the next
hang produces a log section naming the culprit instead of a mystery.

budget(seconds): arms a timer; if it fires, the process dumps ALL thread
tracebacks into the updater log and hard-exits (os._exit(3)) so the
external watchdog restarts it and the next cycle begins clean.

cancel(): disarms after a successful cycle.

Escape hatch: TNWX_NO_HANGWATCHDOG=1 disables arming entirely.
"""
import faulthandler
import os
import sys
import threading
import time

_LOG = os.path.join(".freebuff", "site-updater.log")

_timer = None


def _trip(seconds):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts} WATCHDOG: cycle exceeded {seconds:g}s budget - "
                    f"active thread stacks follow, then hard exit for a "
                    f"clean restart\n")
            f.flush()
            faulthandler.dump_traceback(file=f.fileno())
            f.write(f"{ts} WATCHDOG: exiting via os._exit(3)\n")
    except Exception:                                    # noqa: BLE001
        # Even the log write failed - say whatever we can wherever we can.
        try:
            print(f"{ts} WATCHDOG: cycle exceeded {seconds:g}s", flush=True)
            faulthandler.dump_traceback(file=sys.stderr)
        except Exception:                                # noqa: BLE001
            pass
    os._exit(3)


def budget(seconds):
    """Arm the hang budget for one cycle. Returns True when armed."""
    global _timer
    if os.environ.get("TNWX_NO_HANGWATCHDOG"):
        return False
    cancel()
    try:
        _timer = threading.Timer(seconds, _trip, args=(seconds,))
        _timer.daemon = True
        _timer.start()
        return True
    except Exception:                                    # noqa: BLE001
        return False


def cancel():
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None

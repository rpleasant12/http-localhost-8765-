"""Global network timeout shim.

The updater has repeatedly hung mid-collect on NOAA/GEFS fetches whose
sockets stall without ever erroring (the 2026-09-29 20:10 hang sat silently
for 15+ minutes). ~25 call sites across data/*.py passed no timeout to
requests - and every future call site would inherit the same bug. Instead
of whack-a-mole, this module wraps requests' Session.request once so EVERY
HTTP call gets a hard (connect, read) timeout unless the caller set one.

Idempotent: importing twice never stacks wrappers. Disable with
TNWX_NO_NET_SHIM=1 (escape hatch for one-off diagnosis runs).
"""
import os

DEFAULT_TIMEOUT = 45          # seconds - generous, but finite
_ENV = "TNWX_NET_TIMEOUT"

_applied = False


def _timeout_of(kwargs):
    """Explicit caller timeout wins; otherwise the shim default."""
    t = kwargs.get("timeout")
    if t is None:
        try:
            t = float(os.environ.get(_ENV, "") or DEFAULT_TIMEOUT)
        except ValueError:
            t = DEFAULT_TIMEOUT
    return t


def install():
    global _applied
    if _applied or os.environ.get("TNWX_NO_NET_SHIM"):
        return False
    import requests.sessions

    original = requests.sessions.Session.request

    def request(self, method, url, **kwargs):
        kwargs["timeout"] = _timeout_of(kwargs)
        return original(self, method, url, **kwargs)

    requests.sessions.Session.request = request
    _applied = True
    return True


def report():
    """One line for the updater log: shim armed + effective default."""
    if not _applied:
        return "net shim: not installed (disabled or already present)"
    try:
        t = _timeout_of({})
    except Exception:                                    # noqa: BLE001
        t = DEFAULT_TIMEOUT
    return f"net shim: every requests call now carries a {t:g}s timeout"

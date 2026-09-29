"""Thread-safe cfgrib decoding.

Background: ecCodes lazily loads its GRIB definitions/samples the first
time a handle is created. On Windows + Python 3.13 + ecCodes 2.46, many
threads creating handles concurrently (the app's background renderers all
decode GRIB at startup) race that first load: some threads get
"Cannot create handle, no definitions found" and one can hard-abort the
whole process (ucrtbase 0xc0000409 fast-fail -> python.exe silently dies,
Streamlit server gone). Verified 2026-09-10: single-threaded decodes of
the same files always succeed; a 7-thread decode race failed 6/7 and
aborted once in three rounds.

Fix, in one place:
1. On first use, create+release one handle from a bundled sample while
   holding the lock -> definitions/samples are loaded before any real
   decode starts.
2. Serialize every cfgrib open with the same process-wide lock. GRIB
   decode cost is milliseconds-to-seconds per message here, so the
   serialized path is well below the network/render time each worker
   spends between decodes.

Usage:
    import griblock
    with griblock.open_dataset(tmp, backend_kwargs={...}) as ds:
        ...
"""
from __future__ import annotations

import contextlib
import threading

# Reentrant so a caller may nest a decode inside an open block (same thread).
_LOCK = threading.RLock()
_WARMED = False


def _warm_once() -> None:
    global _WARMED
    if _WARMED:
        return
    with _LOCK:
        if _WARMED:
            return
        try:
            import eccodes

            handle = eccodes.codes_grib_new_from_samples("GRIB2")
            if handle is not None:
                eccodes.codes_release(handle)
        except Exception:  # noqa: BLE001 - never make the app crash *here*
            pass
        _WARMED = True


@contextlib.contextmanager
def open_dataset(path, backend_kwargs=None):
    """xr.open_dataset(engine="cfgrib") under the global decode lock.

    The lock is held for the WHOLE block: cfgrib creates ecCodes handles
    lazily both on open (indexing) and on later `.values` loads, so the
    reads must happen inside the with-block to stay serialized.
    """
    import xarray as xr

    _warm_once()
    with _LOCK:
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs=backend_kwargs or {})
        try:
            yield ds
        finally:
            ds.close()

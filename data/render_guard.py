"""Killable-render guard shared by every cartopy renderer.

nbm_percentiles' in-process cartopy render wedged the whole site updater
repeatedly (2026-09-30/10-01: contour reprojection blocking forever in the
long-lived process; a GEFS network read wedged the same way later that
day). Every matplotlib/cartopy render now goes through run_sub(): the work
happens in a short-lived child interpreter under subprocess.run(timeout=...),
so a wedged render costs its timeout, not the whole site's freshness.

multiprocessing spawn is BROKEN on this box (WinError 2 from CreateProcess,
tested 2026-10-01) - subprocess.run with a worker script is the pattern
that works here.

Contract: the caller builds a picklable job dict that includes "mod" (the
owning module's name) and hands it to run_sub(). The child interpreter runs
data/_render_worker.py, which imports the module and calls its
module-level `_render_job(job)`; the return value (png path, (png, meta)
tuple, or a dict) comes back as a pickled result. Any non-zero child exit,
timeout, or missing result raises RenderFailed, which callers already
treat like any other failed frame (their per-frame try/except keeps the
rest of the set).

Note: children re-import the owning module - every participating module's
top level must stay import-clean (no heavy work, no GUI toolkits).
"""
import os
import pickle
import subprocess
import sys
import tempfile

RENDER_TIMEOUT = 180          # per-render child budget (seconds)

# The updater runs DETACHED (no console): without CREATE_NO_WINDOW every
# render child would allocate a VISIBLE console box on the user's screen
# (~64 renders/h in the rotation = constant popups, 2026-10-01 complaint).
NOWIN = 0x08000000 if os.name == "nt" else 0


class RenderFailed(RuntimeError):
    """A guarded render timed out, crashed, or produced nothing."""


def run_sub(job, timeout=RENDER_TIMEOUT):
    """Run one render job in data/_render_worker.py under a hard timeout."""
    if not job.get("mod"):
        raise ValueError("render job needs 'mod' (owning module name)")
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_render_worker.py")
    fd, job_path = tempfile.mkstemp(prefix="rendjob_", suffix=".pickle")
    res_path = job_path + ".res"
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(job, f)
        try:
            proc = subprocess.run(
                [sys.executable, worker, job_path, res_path],
                timeout=timeout, check=True,
                creationflags=NOWIN,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            err_tail = ""
        except subprocess.TimeoutExpired as exc:
            tail = (exc.stderr or b"").decode("utf-8", "replace")[-400:]
            raise RenderFailed(
                f"{job['mod']} render exceeded {timeout}s: "
                f"{job.get('out_path', '')}" + (f" | {tail}" if tail else "")
            ) from None
        except subprocess.CalledProcessError as exc:
            tail = ((exc.stderr or b"").decode("utf-8", "replace")[-400:])
            raise RenderFailed(
                f"{job['mod']} render failed (exit {exc.returncode}): "
                f"{job.get('out_path', '')}" + (f" | {tail}" if tail else "")
            ) from None
        if not os.path.exists(res_path):
            raise RenderFailed(
                f"{job['mod']} render left no result: {job.get('out_path', '')}")
        try:
            with open(res_path, "rb") as f:
                env = pickle.load(f)
        except (OSError, pickle.UnpicklingError) as exc:
            raise RenderFailed(f"{job['mod']} result unreadable: {exc}") from None
        if not env.get("ok"):
            raise RenderFailed(
                f"{job['mod']} render raised in child: "
                f"{(env.get('err') or 'unknown error').strip().splitlines()[-1]}")
        return env.get("result")
    finally:
        for p in (job_path, res_path):
            try:
                os.remove(p)
            except OSError:
                pass

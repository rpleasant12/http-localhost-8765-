"""Run one guarded render job, then exit.

Parent: data/render_guard.py run_sub() (killable subprocess pattern).
Contract: job["mod"] names the owning module; the child imports it and
calls its module-level `_render_job(job)`. The result is pickled to
res_path so the parent gets it back after the child exits; on any error
the child exits non-zero with a short stderr message (the parent turns
that into RenderFailed).
"""
import os
import pickle
import sys

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    job_path, res_path = sys.argv[1], sys.argv[2]
    with open(job_path, "rb") as f:
        job = pickle.load(f)
    result = None
    err = None
    try:
        import importlib
        mod = importlib.import_module(job["mod"])
        result = mod._render_job(job)
    except BaseException as exc:              # noqa: BLE001 - reported via exit code
        import traceback
        err = "".join(traceback.format_exc())
    out = None
    try:
        out = open(res_path, "wb")
    except OSError as exc:
        print(f"render worker: cannot open result file: {exc}",
              file=sys.stderr, flush=True)
        sys.exit(3)
    try:
        pickle.dump({"ok": err is None, "err": err, "result": result}, out)
        out.close()
    except (OSError, TypeError) as exc:
        print(f"render worker: result not picklable: {exc}",
              file=sys.stderr, flush=True)
        try:
            os.remove(res_path)
        except OSError:
            pass
        sys.exit(4)
    sys.exit(0 if err is None else 1)


if __name__ == "__main__":
    main()

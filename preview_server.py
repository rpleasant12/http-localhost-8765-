"""Local preview server (port 8766) with a self-rotating log.

`python -m http.server` wrote one line per request straight into
preview-server.log.err through an inherited cached-offset handle - 56 MB
over three weeks, impossible to rotate while it runs (Windows can neither
rename nor cleanly truncate under such a handle; see rotate_logs.py). This
wrapper serves the same docs/ tree on the same port but logs through a
RotatingFileHandler (5 MB x 2 generations), so its log caps itself at
~15 MB forever, in-process. Drop-in replacement for startup_task.py's
`python -m http.server 8766 --directory docs` spawn (both spawn sites).

IMPORTANT: serve via the handler's `directory=` parameter and NEVER chdir
into docs/ - a process CWD inside docs/ blocks github_deploy.package()'s
docs -> docs.old directory rename (Windows locks a CWD directory), which
silently downgrades every site publish from the atomic swap to a racy
per-file merge-copy (seen live 2026-10-04 23:10).
"""

import functools
import logging
import os
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from logging.handlers import RotatingFileHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8766
LOG = os.path.join(ROOT, ".freebuff", "preview-server.log.err")

_handler = RotatingFileHandler(LOG, maxBytes=5 * 1024 * 1024,
                               backupCount=2, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logging.raiseExceptions = False          # a full disk must never kill us


class _StderrSink:
    """Route stderr writes (http.server logs every request there) into the
    rotating log. Swallows the ConnectionAbortedError tracebacks browsers
    provoke by abandoning connections mid-reload - noise, not faults."""

    def write(self, s):
        try:
            for line in s.splitlines():
                if line.strip() and "ConnectionAbortedError" not in line \
                        and "ConnectionResetError" not in line \
                        and not line.startswith("---"):
                    logging.info(line)
        except Exception:                    # noqa: BLE001 - never die on log
            pass
        return len(s)

    def flush(self):
        pass


sys.stderr = _StderrSink()

if __name__ == "__main__":
    handler = functools.partial(
        SimpleHTTPRequestHandler, directory=os.path.join(ROOT, "docs"))
    ThreadingHTTPServer(("0.0.0.0", PORT), handler).serve_forever()

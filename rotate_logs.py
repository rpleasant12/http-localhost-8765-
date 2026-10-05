"""Size-based rotation for the .freebuff logs (2026-10-04).

The updater and preview server run for weeks with their stdout/stderr
redirected into fixed files (startup_task.py's start_detached opens them
"ab"), so site-updater.log(.err) and preview-server.log(.err) grew without
bound - 98 MB combined by 2026-10-04 while the disk sat at 94% full.

The hard part: on Windows a file another process holds open can neither be
renamed (opens lack FILE_SHARE_DELETE) nor truncated without consequences -
the children inherit their log handles WITHOUT the CRT's append flag (the
_O_APPEND flag lives in the parent's CRT fd table and does not survive
CreateProcess handle inheritance), so after a plain truncate the writer
keeps writing at its CACHED offset and the file instantly re-extends with a
NUL-filled gap at the head (learned live 2026-10-04; sizes snapped straight
back to 56/24/17 MB). Three mechanisms, one per situation:

1. rotate_own_logs()  - the updater rotates ITS OWN redirected fds in
   process: open a fresh O_APPEND handle on the same path, dup2 it over the
   inherited fd (releasing the cached-offset handle), point the Win32
   std-handle slots at it too (C libraries like ECCODES fetch stderr via
   GetStdHandle), copy the last TAIL_KEEP bytes to <name>.1, then truncate
   the file to 0. Every remaining writer now repositions to EOF - clean.
2. rename-rotate      - for files nobody holds open (publish.log, stale
   junk): full history kept per generation (.1, .2).
3. truncate-in-place  - ONLY for the preview server's logs once the writer
   is gone, and never otherwise; the sweep below deliberately SKIPS the
   process-owned logs (PROC_OWNED) because truncating under a live
   cached-offset handle just recreates the NUL gap. The preview server is
   now started via preview_server.py, whose RotatingFileHandler rotates its
   own log safely in-process.

Keep it boring: stat every candidate, act only past the threshold, never
raise - log housekeeping must never take the updater down.
"""

import os
import re
import sys
import threading

DEFAULT_MAX_BYTES = 10 * 1024 * 1024   # rotate a file past 10 MB
TAIL_KEEP = 512 * 1024                 # history kept by truncate modes
GENS = 2                               # rename mode keeps .1 and .2
_GEN_RE = re.compile(r"\.\d+$")        # rotated generations: name.log.1

# Logs owned by long-running processes' inherited handles. The sweep must
# never touch these: the updater rotates its own two via rotate_own_logs()
# and the preview wrapper's RotatingFileHandler rotates its own. A plain
# truncate under a live cached-offset handle only recreates the NUL gap.
PROC_OWNED = ("site-updater.log", "site-updater.log.err",
              "preview-server.log", "preview-server.log.err")

_LOCK = threading.Lock()


def _shift_gens(path, gens):
    """name -> name.1 -> name.2 (oldest deleted). Raises OSError if the
    active file is held open - callers decide the fallback."""
    for gen in range(gens, 0, -1):
        src = f"{path}.{gen - 1}" if gen > 1 else path
        dst = f"{path}.{gen}"
        if os.path.exists(src):
            if os.path.exists(dst):
                os.remove(dst)
            os.replace(src, dst)


def _tail_to(path, size):
    """Copy the last TAIL_KEEP bytes of `path` to <path>.1 (best effort)."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL_KEEP))
            tail = f.read()
        if tail:
            with open(f"{path}.1", "wb") as f:
                f.write(tail)
    except OSError:
        pass


def rotate_own_fd(fd, path, max_bytes=DEFAULT_MAX_BYTES):
    """Rotate one of THIS process's redirected log fds (no restart needed).

    Replaces the inherited cached-offset handle with a fresh O_APPEND one,
    keeps TAIL_KEEP bytes of history as <path>.1, truncates the file to 0.
    Returns an action string or None (fd not on `path` / under threshold).
    Never raises.
    """
    try:
        with _LOCK:
            size = os.fstat(fd).st_size
            if size <= max_bytes:
                return None
            # fresh in-CRT-append handle; dup2 releases the inherited one
            newfd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            os.dup2(newfd, fd)
            os.close(newfd)
            if os.name == "nt":
                # C libraries (ECCODES...) fetch stderr via GetStdHandle -
                # repoint the Win32 std slots so they follow the new handle
                import ctypes
                import msvcrt
                std = -11 if fd == 1 else -12        # STD_OUTPUT / STD_ERROR
                try:
                    ctypes.windll.kernel32.SetStdHandle(
                        std, msvcrt.get_osfhandle(fd))
                except Exception:                       # noqa: BLE001
                    pass
            _tail_to(path, size)
            with open(path, "r+b") as f:
                f.truncate()                            # at position 0
            return (f"fd {fd} rotated in place ({size // 1024} KB -> tail "
                    f"{min(size, TAIL_KEEP) // 1024} KB + .1)")
    except OSError as exc:
        return f"fd {fd} FAILED ({exc})"


def rotate_file(path, max_bytes=DEFAULT_MAX_BYTES):
    """Rename-rotate one unowned file if it is over max_bytes. Returns an
    action string or None (missing / under threshold / held open)."""
    try:
        with _LOCK:
            if not os.path.isfile(path):
                return None
            size = os.path.getsize(path)
            if size <= max_bytes:
                return None
            _shift_gens(path, GENS)
            return f"renamed (was {size // 1024} KB)"
    except OSError:
        # held open by a live process - and since we cannot know whether
        # that writer uses append semantics, leave it to its owner rather
        # than risk a NUL gap
        return None


def rotate_dir(directory, max_bytes=DEFAULT_MAX_BYTES, skip=PROC_OWNED):
    """Rotate every log-shaped file in `directory` over the threshold.

    Covers the launcher's naming (*.log, *.log.err, *.err.log, plain
    *.err), skips rotated generations AND the process-owned logs (see
    PROC_OWNED), and returns a list of human-readable actions.
    """
    actions = []
    try:
        names = os.listdir(directory)
    except OSError:
        return actions
    for name in sorted(names):
        if name in skip or _GEN_RE.search(name):
            continue
        if not (name.endswith(".log") or name.endswith(".log.err")
                or name.endswith(".err.log") or name.endswith(".err")):
            continue
        act = rotate_file(os.path.join(directory, name), max_bytes)
        if act:
            actions.append(f"{name}: {act}")
    return actions


def rotate_own_logs(log_path, max_bytes=DEFAULT_MAX_BYTES):
    """Rotate this process's stdout (fd 1) and stderr (fd 2) targets when
    they are over threshold. Returns a list of action strings."""
    out = []
    for fd in (1, 2):
        try:
            fd_path = os.path.abspath(log_path if fd == 1 else log_path + ".err")
            # only rotate an fd that actually points at the expected file
            try:
                fst, pst = os.fstat(fd), os.stat(fd_path)
                if (fst.st_dev, fst.st_ino) != (pst.st_dev, pst.st_ino):
                    continue
            except OSError:
                continue
            act = rotate_own_fd(fd, fd_path, max_bytes)
            if act:
                out.append(f"{os.path.basename(fd_path)}: {act}")
        except OSError:
            continue
    return out


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else ".freebuff"
    for act in rotate_dir(d):
        print("log rotation:", act)

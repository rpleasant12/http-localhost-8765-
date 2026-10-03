"""Publish the locally built site (docs/) to the gh-pages branch.

The weather center renders every model map, satellite frame and radar
product locally — CI runners cannot (GRIB decode + MetPy/Cartopy at 30-min
cadence is out of scope for a free runner). So the local build is the
source of truth: this script force-pushes docs/ as a single orphan commit
to gh-pages (branch never grows history), and CI mirrors that branch into
GitHub Pages / any free host (Netlify, Cloudflare, Vercel).

Usage:  python publish_site.py [--check]
  --check: exit 0 only when the local build is fresh enough to publish.
Run by site_updater after each successful repackage; best-effort always.
"""
import argparse
import base64
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

import urllib.request

REPO_API = "https://api.github.com/repos/rpleasant12/http-localhost-8765-"

# --- publish smoke test: every frame URL in data.json must exist in docs/ ---
# The wholesale ASSET_DIRS copy this used to back-stop was never wired in
# (defined in github_deploy.py, never imported): data.json frame references
# are the ONLY mechanism that ships imagery, so one silently-dropped frame
# dir = a broken feature on the live site (see the 2026-09-17 satellite
# incident in github_deploy._walk_json). Verify instead of assume.
_FRAME_URL_KEYS = ("pngUrl", "url")
_IMG_EXTS = (".png", ".gif", ".jpg", ".jpeg", ".webp")


def _smoke_walk(o, path, docs, misses, seen):
    """Collect local image URLs missing under docs/, with JSON paths."""
    if isinstance(o, dict):
        for k, v in o.items():
            _smoke_walk(v, f"{path}.{k}", docs, misses, seen)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            _smoke_walk(v, f"{path}[{i}]", docs, misses, seen)
    elif isinstance(o, str) and o.lower().endswith(_IMG_EXTS) \
            and "/" in o \
            and not o.startswith(("http://", "https://", "data:")):
        # bare filenames ("file": "sfc_wpc.gif") are fragments pages compose
        # client-side - same rule as github_deploy._frame_file, which returns
        # None for them. Only refs with a directory component are URLs.
        rel = o.replace("\\", "/").lstrip("/")
        if rel in seen:
            return
        seen.add(rel)
        if not os.path.isfile(os.path.join(docs, rel)):
            misses.append((path, rel))


def smoke_frames(docs="docs"):
    """True when every local frame URL in docs/data.json exists in docs/.

    Returns (ok, misses) with misses as (json_path, url) pairs, capped at
    25 reported so the log stays readable on a systemic failure.
    """
    dj = os.path.join(docs, "data.json")
    if not os.path.isfile(dj):
        return True, []          # no payload: nothing to verify here
    try:
        with open(dj, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return False, [("data.json", f"unparseable: {exc}")]
    misses, seen = [], set()
    for k, v in data.items():
        _smoke_walk(v, k, docs, misses, seen)
    return (not misses), misses

# CREATE_NO_WINDOW: a console-less publisher spawning git/tasklist would
# flash a visible console box per call - dozens per publish (2026-09-21).
NOWIN = 0x08000000 if os.name == "nt" else 0

MAX_AGE = 3600          # refuse to publish builds older than 1 hour
# Hard cap (GitHub Pages refuses sites over ~1 GB). Full loops on every
# model (2026-09-15) raised the worst-case build: model_maps budget 600 MB
# + every other cache at its ceiling sums to ~940 MB, so the guard sits at
# 950 - still under the Pages limit while never refusing a healthy build.
MAX_SIZE_MB = 950
# Each force-publish orphans the previous ~600 MB build in .git/objects;
# repack as soon as orphans pile past ~400 MB so the object store stays
# bounded (the 1.5 GB trigger let .git sit at 1+ GB - 2026-09-11).
REPACK_TRIGGER_KB = 2_000_000   # ~2 GB: with the gh-pages chain depth-capped,
                                # dead objects stay bounded between repacks, so
                                # repack when they pile up - not every publish
                                # (the old 390 MB trigger repacked 40+ GB every
                                # ~10 min, which is what cooked the disk)


def _pid_alive(pid):
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


LOCK_PATH = os.path.join(".freebuff", "publish.lock")


def _acquire_publish_lock():
    """Single-flight guard: only one publish's heavy git work at a time.

    Two concurrent publishes (updater main cycle + freshness-watchdog
    recovery + manual) interleave index/repack operations on the same
    object store and kill each other with empty 'git add failed' errors
    (2026-09-17: every publish after an 8h pause failed this way - the
    throttled main-cycle publish and the lag-recovery publish spawned
    together, and the old check-then-write lock let both through).
    Acquisition is now ATOMIC (O_CREAT|O_EXCL): exactly one caller wins.
    A lock older than 30 min or held by a dead pid is stale and removed.
    """
    os.makedirs(".freebuff", exist_ok=True)

    def _stale() -> bool:
        holder = ""
        try:
            with open(LOCK_PATH, encoding="utf-8") as f:
                holder = f.read().strip()
            holder_pid = int(holder.split()[0]) if holder.split() else 0
            age = time.time() - os.path.getmtime(LOCK_PATH)
            return not (holder_pid and _pid_alive(holder_pid) and age < 1800)
        except (OSError, ValueError):
            return True

    for attempt in range(2):
        try:
            fd = os.open(LOCK_PATH,
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{os.getpid()} "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            return True
        except FileExistsError:
            if attempt == 0 and _stale():
                print(f"publish: removing stale publish lock "
                      f"(holder {_lock_holder()!r})")
                try:
                    os.remove(LOCK_PATH)
                except OSError:
                    pass
                continue
            print("publish: another publish is in flight - skipping")
            return False
        except OSError:
            print("publish: cannot write publish lock - skipping")
            return False
    return False


def _lock_holder():
    try:
        with open(LOCK_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _clear_stale_index_locks():
    """Remove leftover git index locks from killed publishes (60s+ old)."""
    for p in (os.path.join(".git", "index.lock"),
              os.path.join(".git", "worktrees", "ghpages-wt", "index.lock")):
        try:
            if os.path.isfile(p) and time.time() - os.path.getmtime(p) > 60:
                os.remove(p)
                print(f"publish: removed stale lock {p}")
        except OSError:
            pass


def _run(args, **kw):
    # gc.auto=0: git's background geometric-repack spawned DURING our add/commit
    # collided with the publish's own index work and left it dead mid-commit
    # (2026-09-18 log: commits dying with rc=3221225786 while "executing git
    # geometric-repack"). All publishing git calls run gc-free; housekeeping()
    # below does the repacking explicitly, serialized under the publish lock.
    if args and args[0] == "git":
        args = ["git", "-c", "gc.auto=0"] + args[1:]
    if os.name == "nt":
        kw.setdefault("creationflags", NOWIN)
    try:
        return subprocess.run(args, capture_output=True, text=True, **kw)
    except subprocess.TimeoutExpired:
        # network git calls get explicit timeouts; surface them as rc=124
        class _R:
            returncode = 124
            stdout = ""
            stderr = "git call timed out"
        return _R()


def _push_once(cwd):
    """One push attempt, hard-capped at 15 min.

    2026-09-18: git push sat for 16+ minutes with ZERO network connections
    after GitHub's edge dropped the TLS upload mid-flight - the call had no
    timeout, so the publisher hung forever holding the publish lock while
    every later cycle just logged 'another publish is in flight'. The cap
    turns that hang into a normal retryable failure.
    """
    proc = subprocess.Popen(
        ["git", "-c", "gc.auto=0", "push", "origin", "HEAD:gh-pages", "--force"],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, **({"creationflags": NOWIN} if os.name == "nt" else {}))
    try:
        out, err = proc.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            out, err = proc.communicate(timeout=30)
        except Exception:  # noqa: BLE001 - kill is best-effort
            out, err = "", ""
        return 124, out or "", ((err or "") + "\npush timed out after 900s")
    return proc.returncode, out or "", err or ""


def _gh_token():
    """The push credential (git credential helper), for API dispatches."""
    try:
        r = subprocess.run(["git", "credential", "fill"],
                           input="protocol=https\nhost=github.com\n",
                           capture_output=True, text=True, timeout=20,
                           creationflags=NOWIN if os.name == "nt" else 0)
        for line in r.stdout.splitlines():
            if line.startswith("password="):
                return line[len("password="):].strip()
    except Exception:  # noqa: BLE001 - dispatch is best-effort
        pass
    return ""


def dispatch_mirror():
    """Nudge the Pages-mirror workflow to run NOW (best-effort).

    The */30 schedule is throttled/skipped on free accounts, which left the
    public site lagging the gh-pages branch by whole cycles. The local push
    credential may trigger workflow_dispatch, which updates Pages within
    ~2 minutes of every publish.
    """
    try:
        token = _gh_token()
        if not token:
            return False
        req = urllib.request.Request(
            f"{REPO_API}/actions/workflows/deploy-site.yml/dispatches",
            data=json.dumps({"ref": "main"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "tnwn-updater"},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 204
    except Exception:  # noqa: BLE001 - schedule remains as the fallback
        return False


# ---- Cloudflare Pages mirror (2026-10-02): keep the tnwn-weather.pages.dev
# copy in sync with the GitHub-published build. The project is DIRECT-UPLOAD
# (`wrangler pages deploy docs/`; a direct-upload Pages project cannot be
# converted to git-connected, and the free tier caps deployments at
# ~500/month), so sync runs on a >=2 h throttle (~12/day ~360/month with
# margin for manual redeploys). Best-effort and always OUTSIDE the publish
# lock (called from __main__ after publish() succeeds): any failure logs,
# retries (up to 3 attempts - see sync_cloudflare_pages) and moves on -
# github.io stays primary, this is the CDN mirror.
CFPAGES_PROJECT = "tnwn-weather"
CFPAGES_MIN_GAP = 2 * 3600
CFPAGES_STAMP = os.path.join(".freebuff", "cfpages.last")
CFPAGES_WRANGLER = os.path.join(os.path.expanduser("~"),
                                "AppData", "Roaming", "npm", "wrangler.cmd")
CFPAGES_ATTEMPTS = 3
CFPAGES_RETRY_WAIT = 30


def _cfpages_due():
    try:
        with open(CFPAGES_STAMP, encoding="utf-8") as f:
            return time.time() - float(f.read().strip()) >= CFPAGES_MIN_GAP
    except (OSError, ValueError):
        return True


# ---- Members-only page upload (2026-10-03): the four premium centers are
# HARD-gated at the worker (/p/<page> serves them from D1 to live premium/
# admin sessions only; the public site carries locked shells), so the built
# full pages must reach the worker's D1 after every publish. gzip + base64
# keeps each SQL statement well under D1's 100 KB limit. Best-effort: any
# failure logs and the next publish retries (sha state in
# .freebuff/pages-uploaded.json; the worker keeps serving the last good copy
# in the meantime).
PAGES_D1_DB = "tnwn-members"
PAGES_STATE = os.path.join(".freebuff", "pages-uploaded.json")
PAGES_FILES = ("severe.html", "storms.html", "tropical.html", "winter.html",
               "education.html", "fieldguide.html")
# plus every docs/storm_<id>.html detail page (globbed at upload time)


def upload_premium_pages():
    """Push changed docs/<page>.html into the worker's D1 `pages` table.
    Returns True when the table is known current (uploaded or unchanged)."""
    if not os.path.isdir("docs"):
        return False
    if not os.path.isfile(CFPAGES_WRANGLER):
        print("publish: premium pages upload skipped (wrangler not found)")
        return False
    state = {}
    try:
        with open(PAGES_STATE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    stmts = ["CREATE TABLE IF NOT EXISTS pages (name TEXT PRIMARY KEY, "
             "sha TEXT, html_b64 TEXT, updated INTEGER)"]
    now = int(time.time())
    changed = []
    # Source = static/premium_pages/ (the FULL pages the build stashed out
    # of the public tree). docs/ carries only the locked shells - uploading
    # those would make the worker serve the lock screen to paying members
    # (found live 2026-10-03: first run read docs/ and shipped shells).
    src_dir = os.path.join("static", "premium_pages")
    if not os.path.isdir(src_dir):
        return False
    fnames = [f for f in PAGES_FILES if os.path.isfile(os.path.join(src_dir, f))]
    try:
        # storm_<id> archive detail pages are hard-gated too: one row each
        fnames.extend(sorted(
            f for f in os.listdir(src_dir)
            if f.startswith("storm_") and f.endswith(".html")))
    except OSError:
        pass
    for fname in fnames:
        path = os.path.join(src_dir, fname)
        with open(path, "rb") as f:
            raw = f.read()
        digest = hashlib.sha256(raw).hexdigest()
        if state.get(fname) == digest:
            continue
        b64 = base64.b64encode(gzip.compress(raw, 9)).decode("ascii")
        safe = fname.replace(".html", "")
        stmts.append(
            f"INSERT INTO pages (name, sha, html_b64, updated) VALUES "
            f"('{safe}', '{digest}', '{b64}', {now}) "
            f"ON CONFLICT(name) DO UPDATE SET sha=excluded.sha, "
            f"html_b64=excluded.html_b64, updated=excluded.updated")
        changed.append(fname)
    if not changed:
        return True
    tmp_sql = os.path.join(".freebuff", "pages-upload.sql")
    try:
        with open(tmp_sql, "w", encoding="utf-8", newline="\n") as f:
            f.write(";\n".join(stmts) + ";\n")
        r = subprocess.run(
            ["cmd", "/c", CFPAGES_WRANGLER, "d1", "execute", PAGES_D1_DB,
             "--remote", "-y", "--file", tmp_sql],
            capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace",
            creationflags=NOWIN if os.name == "nt" else 0)
    finally:
        try:
            os.remove(tmp_sql)
        except OSError:
            pass
    if r.returncode != 0:
        detail = ((r.stderr or r.stdout or "")[-160:]).encode(
            "ascii", "replace").decode("ascii")
        print(f"publish: premium pages upload FAILED ({detail})")
        return False
    with open(PAGES_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)
    print(f"publish: premium pages uploaded to D1 ({', '.join(changed)})",
          flush=True)
    return True


def sync_cloudflare_pages():
    """Deploy docs/ to the Cloudflare Pages mirror (throttled, best-effort).

    KNOWN QUIRK (observed twice 2026-10-02): `wrangler pages deploy` can die
    SILENTLY mid-upload - the log just ends with no error and NO deployment
    record in the dashboard. A run is only trusted as success when its output
    contains wrangler's "Deployment complete" marker (or a deployed *.pages.dev
    URL, guarding against marker-text drift burning the deploy quota); rc==0
    alone is NOT enough. Anything else - weird rc, timeout, log-ends-early -
    is retried. Retries are cheap: wrangler content-hashes uploads, so a
    resumed attempt skips what already made it. If all attempts fail the
    stamp is left untouched, so the next heartbeat publish naturally retries
    the sync ~7 min later.
    """
    if not os.path.isdir("docs") or not _cfpages_due():
        return False
    if not os.path.isfile(CFPAGES_WRANGLER):
        print("publish: cloudflare pages mirror skipped (wrangler not found)")
        return False
    for attempt in range(1, CFPAGES_ATTEMPTS + 1):
        try:
            r = subprocess.run(
                ["cmd", "/c", CFPAGES_WRANGLER, "pages", "deploy", "docs/",
                 "--project-name", CFPAGES_PROJECT, "--branch", "main",
                 "--commit-dirty=true"],
                capture_output=True, text=True, timeout=540,
                encoding="utf-8", errors="replace",  # wrangler speaks UTF-8
                creationflags=NOWIN if os.name == "nt" else 0)
            combined = (r.stdout or "") + (r.stderr or "")
            low = combined.lower()
            tail = combined.strip().splitlines()
            # wrangler emits UTF-8 with emoji/box-drawing chars; a redirected
            # stdout on Windows is often cp1252 and print() would raise
            # UnicodeEncodeError (seen in sandbox test) - keep log lines ASCII.
            detail = (tail[-1][:160] if tail else "").encode(
                "ascii", "replace").decode("ascii")
            if (r.returncode == 0 and ("deployment complete" in low
                                       or "pages.dev" in low)):
                with open(CFPAGES_STAMP, "w", encoding="utf-8") as f:
                    f.write(str(time.time()))
                print(f"publish: cloudflare pages mirror deployed "
                      f"(attempt {attempt}/{CFPAGES_ATTEMPTS}, {detail})",
                      flush=True)
                return True
            print(f"publish: cloudflare pages mirror attempt "
                  f"{attempt}/{CFPAGES_ATTEMPTS} FAILED rc={r.returncode}: "
                  f"{detail}", flush=True)
        except Exception as exc:  # noqa: BLE001 - mirror must never break publish
            print(f"publish: cloudflare pages mirror attempt "
                  f"{attempt}/{CFPAGES_ATTEMPTS} skipped:", exc, flush=True)
        if attempt < CFPAGES_ATTEMPTS:
            time.sleep(CFPAGES_RETRY_WAIT)
    print("publish: cloudflare pages mirror gave up after "
          f"{CFPAGES_ATTEMPTS} attempts (github.io stays primary; next "
          "heartbeat publish retries the sync)", flush=True)
    return False


def _git_size_kb():
    """Packed object-store size in KB (from git count-objects -v)."""
    r = _run(["git", "count-objects", "-v"])
    for line in r.stdout.splitlines():
        if line.startswith("size-pack:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _push_in_flight():
    """True while any git push/recv-pack process is alive (Windows-safe)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq git.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=NOWIN).stdout or ""
        return "git.exe" in out.lower()
    except Exception:  # noqa: BLE001 - probe is best-effort
        return False


def housekeeping(full=None):
    """Drop git objects orphaned by force-published builds.

    Every publish creates a new orphan commit on gh-pages; the moment the
    next push replaces it, that commit's blobs become unreachable - but
    they stay in .git/objects forever unless pruned (this hit 6.3 GB in
    three days of 2-minute publishing). Loose orphans are pruned on every
    call; the expensive full repack runs only when the pack size crosses
    REPACK_TRIGGER_KB, or immediately with full=True (--cleanup flag).
    Reachable history (main) is never touched.

    2026-09-20: a full repack launched WHILE another publish's push was
    still uploading corrupted that push (git-for-Windows crashed with
    rc=3221225786 mid-add/repack, and the oversized 2.7 GB pack made both
    near-certain) - the public site went ~19 h stale. Repack is now
    skipped while any git push is alive, and retried on the next cycle.
    """
    _run(["git", "worktree", "prune"])   # a removed worktree must not pin its base
    _run(["git", "reflog", "expire", "--expire-unreachable=now", "--all"])
    _run(["git", "prune", "--expire=now"])
    size = _git_size_kb()
    if full is False:
        return
    need_full = bool(full) or (size is not None and size > REPACK_TRIGGER_KB)
    if need_full and _push_in_flight():
        print("publish: housekeeping repack deferred (push in flight)",
              flush=True)
        return
    if need_full:
        r = _run(["git", "repack", "-A", "-d", "--unpack-unreachable=now"])
        if r.returncode == 0:
            _run(["git", "prune", "--expire=now"])
        after = _git_size_kb()
        print(f"publish: housekeeping repack rc={r.returncode} "
              f"({(size or 0) // 1024} MB -> {(after or 0) // 1024} MB)",
              flush=True)


def _wait_remote_tip(sha, timeout=75):
    """Poll origin until gh-pages really shows `sha` (max ~75s)."""
    if not sha:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _run(["git", "ls-remote", "origin", "gh-pages"], timeout=60)
        if r.returncode == 0 and r.stdout.strip().startswith(sha):
            return True
        time.sleep(5)
    print("publish: remote tip never showed the pushed SHA; deploying anyway")
    return False


def build_age_seconds():
    """Age of the freshest file in docs/ (0 if the tree is missing)."""
    newest = 0.0
    for root, _dirs, files in os.walk("docs"):
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                continue
    if not newest:
        return None
    return max(0.0, time.time() - newest)


def publish(check_only=False):
    if not os.path.isfile(os.path.join("docs", "index.html")):
        print("publish: no docs/ build - run github_deploy.py first")
        return False
    age = build_age_seconds()
    if age is None or age > MAX_AGE:
        print(f"publish: build stale (age {age and int(age)}s > {MAX_AGE}s) - skipping")
        return False
    total = sum(os.path.getsize(os.path.join(r, f))
                for r, _d, fs in os.walk("docs") for f in fs) / 1e6
    if total > MAX_SIZE_MB:
        print(f"publish: build too large ({total:.0f} MB) - skipping")
        return False
    ok, misses = smoke_frames("docs")
    if not ok:
        print(f"publish: smoke test FAILED - {len(misses)} frame URL(s) in "
              f"data.json missing from docs/:")
        for path, rel in misses[:25]:
            print(f"  {path} -> {rel}")
        if len(misses) > 25:
            print(f"  ... and {len(misses) - 25} more")
        return False
    if check_only:
        print(f"publish: build ok ({total:.0f} MB, age {int(age)}s, "
              f"frame URLs verified)")
        return True
    if not _acquire_publish_lock():
        return False

    _run(["git", "worktree", "prune"])
    wt = os.path.join(".freebuff", "ghpages-wt")
    if os.path.isdir(wt):
        _run(["git", "worktree", "remove", "--force", wt])
        if os.path.isdir(wt):
            # Half-created leftover (crashed worktree add): git no longer
            # knows it, so 'remove' fails and a plain prune can't clear it
            # either - every later 'worktree add' then dies with "already
            # exists" and the public site goes stale (2026-09-19 15:56).
            # Nuke the folder by hand so the next add starts clean.
            shutil.rmtree(wt, ignore_errors=True)
    # Build each publish ON TOP OF the previous gh-pages commit. The old
    # flow parented every publish to main, i.e. an ORPHAN vs gh-pages: git
    # then re-uploaded the ENTIRE ~470 MB tree on every push (nothing to
    # delta against), pushes took 15+ min, GitHub's edge kept killing them
    # mid-upload, and the wedged git calls eventually hung the publisher
    # while it held the lock (2026-09-18). Chained, a push carries only the
    # frames that actually changed - a few MB - and lands in seconds.
    # Content is still complete: every tracked file is deleted and recopied
    # from docs/ below, so the pushed tree matches docs/ exactly. To keep
    # gh-pages history bounded ("branch never grows history"), a base older
    # than 2 h is ignored and that publish falls back to the old full
    # upload. (Was 12 h: the whole 12-h snapshot chain stayed reachable in
    # .git/objects the whole time, so housekeeping's repack could never
    # reclaim and the pack grew ~0.3 GB per publish to 80+ GB / disk-full
    # on 2026-09-26 14:00. 2 h bounds the pinned history while still
    # delta-compressing the common case.)
    _run(["git", "fetch", "origin", "gh-pages"], timeout=300)   # non-fatal

    def _usable(sha):
        sha = (sha or "").strip()
        if not sha:
            return ""
        if _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"]).returncode != 0:
            return ""
        bd = _run(["git", "show", "-s", "--format=%ct", sha]).stdout.strip()
        try:
            return sha if time.time() - int(bd) <= 2 * 3600 else ""
        except ValueError:
            return ""

    tip = _run(["git", "rev-parse", "--verify", "FETCH_HEAD"]).stdout.strip()
    base = _usable(tip) or _usable(
        _run(["git", "rev-parse", "--verify", "gh-pages"]).stdout.strip())
    if base:
        # Chain-depth cap: a chained push inherits the base's ancestry, so
        # origin/gh-pages becomes an unbroken chain of full-site snapshots
        # and EVERY one stays reachable forever - reflog expiry can't help,
        # and the pack grew to 80 GB / disk-full (2026-09-26). Once more
        # than 6 gh-pages-unique commits have piled up (snapshots not in
        # main's history), publish the next build as a fresh root (no-base
        # branch: parented on main, cutting the gh-pages chain) so the old
        # snapshots go unreachable and housekeeping's repack reclaims them.
        _depth = _run(["git", "rev-list", "--count", base, "--not", "main"])
        try:
            if int((_depth.stdout or "0").strip() or 0) > 6:
                print(f"publish: gh-pages chain depth "
                      f"{(_depth.stdout or '0').strip()} > 6 - publishing a "
                      f"fresh root to bound history", flush=True)
                base = ""
        except ValueError:
            pass
    if base:
        rc = _run(["git", "worktree", "add", "--detach", wt, base], timeout=180)
    else:
        rc = _run(["git", "worktree", "add", "--detach", wt], timeout=180)
    if rc.returncode != 0:
        # include git's own words: empty-stderr failures here are the
        # externally-killed pattern, and the detail matters (2026-09-18)
        print("publish: worktree add failed: "
              f"{(rc.stderr or '').strip()[:200] or (rc.stdout or '').strip()[:200] or 'no output (killed?)'}")
        return False
    try:
        for name in os.listdir(wt):
            if name == ".git":
                continue
            p = os.path.join(wt, name)
            if os.path.isdir(p):
                subprocess.run(["git", "rm", "-rf", "-q", name], cwd=wt,
                               creationflags=NOWIN)
            else:
                subprocess.run(["git", "rm", "-f", "-q", name], cwd=wt,
                               creationflags=NOWIN)
        for root, dirs, files in os.walk("docs"):
            rel = os.path.relpath(root, "docs")
            if rel == ".":
                rel = ""
            for d in dirs:
                os.makedirs(os.path.join(wt, rel, d), exist_ok=True)
            for f in files:
                src = os.path.join(root, f)
                dst = os.path.join(wt, rel, f)
                # The updater's own cycle REWRITES docs/ every ~2 min (fresh
                # data.json + packaged assets), so a file listed by os.walk
                # can legitimately vanish before we copy it. Previously one
                # such miss aborted the whole publish with FileNotFoundError
                # (2026-09-20 20:58: 'The system cannot find the file
                # specified' killed every publish for an hour). Skip the
                # vanished file - the next cycle's commit carries it.
                try:
                    if os.path.isfile(dst):
                        os.remove(dst)
                    shutil.copy2(src, dst)   # COPY: docs/ stays intact for serving
                except (FileNotFoundError, NotADirectoryError) as exc:
                    print(f"publish: skipped vanished file {rel and rel + '/'}{f}: {exc}")
                    continue
        _clear_stale_index_locks()
        r = _run(["git", "add", "-A", "."], cwd=wt)  # gc.auto=0 via _run
        if r.returncode != 0:
            # one retry after clearing any lock a killed publish left behind
            time.sleep(3)
            _clear_stale_index_locks()
            r = _run(["git", "add", "-A", "."], cwd=wt)
        if r.returncode != 0:
            print("publish: git add failed:", r.stderr[-300:])
            return False
        msg = f"Site build {time.strftime('%Y-%m-%d %H:%M')}"
        r = _run(["git", "commit", "-m", msg], cwd=wt)
        if r.returncode != 0:
            print("publish: nothing to commit?", r.stdout[-200:], r.stderr[-200:])
            return False
        # GitHub's edge hangs up on long (~450 MB) uploads intermittently
        # (2026-09-17: three consecutive publishes died with "remote end
        # hung up unexpectedly", leaving the public site 9 h stale). Retry
        # the push on transient transport failures - each attempt resumes
        # from the committed index, so retries are cheap.
        r = None
        for attempt in range(3):
            r_cd, r_out, r_err = _push_once(cwd=wt)
            class _P:  # shim so the existing rc/stderr checks keep working
                pass
            r = _P()
            r.returncode = r_cd
            r.stderr = r_err
            if r.returncode == 0:
                break
            err = (r_err or "").lower()
            transient = any(s in err for s in ("hung up", "timeout", "timed out",
                                               "connection", "could not resolve",
                                               "ssl", "unavailable"))
            if not transient:
                break
            print(f"publish: push attempt {attempt + 1} failed "
                  f"({r_err.strip().splitlines()[-1] if r_err.strip() else 'unknown'});"
                  f" retrying in 20s", flush=True)
            time.sleep(20)
        if r.returncode != 0:
            print("publish: push failed:", r.stderr[-300:])
            return False
        pushed_sha = _run(["git", "rev-parse", "HEAD"], cwd=wt).stdout.strip()
        # Re-point the local gh-pages ref at what we just pushed: left on an
        # old commit it pins that whole snapshot chain in .git/objects even
        # after housekeeping expires reflogs (pack grew to 80 GB this way).
        if pushed_sha:
            _run(["git", "update-ref", "refs/heads/gh-pages", pushed_sha])
        # Deploy race (cost the public site hours of staleness on 2026-09-11):
        # dispatching the mirror workflow ~2s after the push means its checkout
        # runs BEFORE GitHub's ref update is visible, so the deploy packages the
        # PREVIOUS build every time - while reporting success. Wait until
        # ls-remote actually shows the pushed SHA before nudging the deploy.
        _wait_remote_tip(pushed_sha)
        nudged = dispatch_mirror()
        print(f"publish: gh-pages updated ({total:.0f} MB, {msg});"
              f" pages mirror {'dispatched' if nudged else 'will follow on schedule'}")
        # completion marker for site_updater.spawn_publish() (best-effort)
        try:
            with open(os.path.join(".freebuff", "publish.done"), "w",
                      encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        except OSError:
            pass
        return True
    finally:
        _run(["git", "worktree", "remove", "--force", wt])
        _run(["git", "worktree", "prune"])
        # housekeeping MUST run after the worktree removal: while it exists,
        # its detached HEAD still anchors the just-pushed build's objects and
        # prune would keep ~1-2 builds (~100 MB each) alive forever.
        # AND while the lock is still held: a repack racing the next publish's
        # git add/commit on the same object store got killed every time
        # (rc=0xC000013A, 2026-09-15) and the orphaned packs never shrank.
        # Holding the lock through housekeeping serializes ALL git work; the
        # next publish skips and catches up on its next cycle.
        housekeeping()
        try:
            if os.path.isfile(LOCK_PATH):
                os.remove(LOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="validate only; do not push")
    ap.add_argument("--cleanup", action="store_true",
                    help="force a full git housekeeping repack, then exit")
    args = ap.parse_args()
    if args.cleanup:
        housekeeping(full=True)
        sys.exit(0)
    ok = publish(check_only=args.check)
    if ok and not args.check:
        # Hard-gated premium centers into the worker's D1 FIRST (premium
        # visitors hit /p/* the moment the new nav is live), then the
        # Cloudflare CDN mirror (deploy can take minutes on a big delta).
        try:
            upload_premium_pages()
        except Exception:  # noqa: BLE001 - never fail the CLI on upload issues
            pass
        try:
            sync_cloudflare_pages()
        except Exception:  # noqa: BLE001 - never fail the CLI on mirror issues
            pass
    sys.exit(0 if ok else 1)

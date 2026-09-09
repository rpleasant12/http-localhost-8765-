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
import json
import os
import shutil
import subprocess
import sys
import time

import urllib.request

REPO_API = "https://api.github.com/repos/rpleasant12/http-localhost-8765-"

MAX_AGE = 3600          # refuse to publish builds older than 1 hour
MAX_SIZE_MB = 780       # hard cap; full build sits around 700 MB


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def _gh_token():
    """The push credential (git credential helper), for API dispatches."""
    try:
        r = subprocess.run(["git", "credential", "fill"],
                           input="protocol=https\nhost=github.com\n",
                           capture_output=True, text=True, timeout=20)
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
    if check_only:
        print(f"publish: build ok ({total:.0f} MB, age {int(age)}s)")
        return True

    _run(["git", "worktree", "prune"])
    wt = os.path.join(".freebuff", "ghpages-wt")
    if os.path.isdir(wt):
        _run(["git", "worktree", "remove", "--force", wt])
    # ensure gh-pages exists remotely or locally before adding the worktree
    if _run(["git", "rev-parse", "--verify", "gh-pages"]).returncode != 0:
        r = _run(["git", "fetch", "origin", "gh-pages"])
        if r.returncode != 0:
            print("publish: creating fresh gh-pages branch")
            _run(["git", "branch", "gh-pages", "HEAD"])   # replaced below anyway
        else:
            _run(["git", "branch", "gh-pages", "origin/gh-pages"])
    if _run(["git", "rev-parse", "--verify", "gh-pages"]).returncode != 0:
        _run(["git", "branch", "gh-pages", "HEAD"])
    _run(["git", "branch", "-f", "gh-pages", "HEAD"])   # base only; content replaced
    if _run(["git", "worktree", "add", "--detach", wt]).returncode != 0:
        print("publish: worktree add failed")
        return False
    try:
        for name in os.listdir(wt):
            if name == ".git":
                continue
            p = os.path.join(wt, name)
            if os.path.isdir(p):
                subprocess.run(["git", "rm", "-rf", "-q", name], cwd=wt)
            else:
                subprocess.run(["git", "rm", "-f", "-q", name], cwd=wt)
        for root, dirs, files in os.walk("docs"):
            rel = os.path.relpath(root, "docs")
            if rel == ".":
                rel = ""
            for d in dirs:
                os.makedirs(os.path.join(wt, rel, d), exist_ok=True)
            for f in files:
                src = os.path.join(root, f)
                dst = os.path.join(wt, rel, f)
                if os.path.isfile(dst):
                    os.remove(dst)
                shutil.copy2(src, dst)   # COPY: docs/ stays intact for serving
        r = _run(["git", "add", "-A", "."], cwd=wt)
        if r.returncode != 0:
            print("publish: git add failed:", r.stderr[-300:])
            return False
        msg = f"Site build {time.strftime('%Y-%m-%d %H:%M')}"
        r = _run(["git", "commit", "-m", msg], cwd=wt)
        if r.returncode != 0:
            print("publish: nothing to commit?", r.stdout[-200:], r.stderr[-200:])
            return False
        r = _run(["git", "push", "origin", "HEAD:gh-pages", "--force"], cwd=wt)
        if r.returncode != 0:
            print("publish: push failed:", r.stderr[-300:])
            return False
        nudged = dispatch_mirror()
        print(f"publish: gh-pages updated ({total:.0f} MB, {msg});"
              f" pages mirror {'dispatched' if nudged else 'will follow on schedule'}")
        return True
    finally:
        _run(["git", "worktree", "remove", "--force", wt])
        _run(["git", "worktree", "prune"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="validate only; do not push")
    args = ap.parse_args()
    sys.exit(0 if publish(check_only=args.check) else 1)

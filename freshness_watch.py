"""PUBLIC-SITE + WORKER FRESHNESS MONITOR (the 24/7 self-check).

The existing watchdogs each cover one link in the update chain:
  site_updater.check_public_freshness  - gh-pages lag vs local build (+ recovery publish)
  site_updater.check_upstream_feeds    - NOAA model feeds (log warnings only)
  startup_task watchdog                - updater process liveness via heartbeat
This module closes the remaining gaps and adds a NOTIFICATION path (the
others only log, which nobody reads at 3 AM):

  1. local build    static/site/data.json age          (cold cycles run 25-45 min)
  2. gh-pages       public data.json age               (the page visitors actually load)
  3. gh-pages lag   local minus public dataEpochMs     (publish pipeline stuck)
  4. worker         GET /api/config answers 200        (members worker alive)
  5. D1 pages       sha of every premium page vs local (upload pipeline; AGE is
                    informational - pages pull live data client-side, so page
                    HTML only changes when site CODE changes)
  6. CF mirror      .freebuff/cfpages.last stamp age   (2 h throttle -> 12 h = failing)

D1 self-heal: when the sha check FAILs, re-run publish_site.upload_premium_pages(force=True)
(most 30 min) - the same recovery philosophy as check_public_freshness's recovery publish.

Escalation: WARN logs + files only. FAIL texts once per 6 h per failing
check via data.sms_alerts (Gmail SMTP -> carrier gateways - same free path
as the weather texter); recovery texts once per 12 h. If >= 2 REMOTE checks
fail at once while the LOCAL build is fresh, the fault is almost certainly
the machine's own network - log it, but no SMS (never text a false alarm;
observed 2026-10-05 when a mid-flight build raced a recycle).

Runs two ways:
  - inside the site updater's cycle (check_once(), own 5-min throttle), and
  - standalone:  python freshness_watch.py [--force] [--json]
    exit 0 = no FAIL, 2 = at least one FAIL (wire into Task Scheduler).
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ------------------------------------------------- constants (self-contained;
# no site_updater/publish_site imports - they chdir, install the net shim and
# would re-run module top-levels when the updater lazily imports us mid-cycle)
PUBLIC_DATA_URL = "https://rpleasant12.github.io/http-localhost-8765-/data.json"
WORKER_URL = "https://tnwn-members.nbasportstalk53.workers.dev/api/config"
WRANGLER = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "npm",
                        "wrangler.cmd")
LOCAL_DATA = os.path.join("static", "site", "data.json")
D1_SRC_DIR = os.path.join("static", "premium_pages")   # module const: tests patch it
D1_HEAL_EVERY = 1800           # D1 self-heal re-upload at most every 30 min
CFPAGES_STAMP = os.path.join(".freebuff", "cfpages.last")
STATE_PATH = os.path.join(".freebuff", "freshness_alerts.json")
STATUS_PATH = os.path.join(".freebuff", "freshness_status.json")

CHECK_EVERY = 300          # 5 min inside the updater cycle
SMS_REPEAT_EVERY = 6 * 3600    # re-text the same failing check at most 6-hourly
RECOVER_SMS_EVERY = 12 * 3600  # "recovered" texts, same cap
# Thresholds as {level: minutes}. Local build gets the cold-cycle grace
# (site_updater documents 25-45 min cold builds); the public site must lag
# no worse than the watchdog's own 30-min alert, and D1/mirror run on long
# windows because their cadence is genuinely slow (rotating windows + quota).
THRESHOLDS = {
    "local_build":  {"warn": 45, "fail": 90},
    "gh_pages_age": {"warn": 30, "fail": 60},
    "gh_pages_lag": {"warn": 30, "fail": 60},
    "worker":       {"warn": 5,  "fail": 15},   # minutes since last good probe
    "cf_mirror":    {"warn": 6 * 60, "fail": 12 * 60},
}
# Worker/D1 probe failures are sticky: if a probe fails we keep counting age
# from the last SUCCESS so a 10-min outage doesn't flap back to green.
_last_ok = {}   # check name -> epoch of last successful probe (process-local)


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def _fetch_json(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": "tnwn-freshness-watch", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _age_min(epoch_s):
    return (time.time() - epoch_s) / 60.0


def _lvl(name, minutes):
    t = THRESHOLDS[name]
    return "FAIL" if minutes >= t["fail"] else ("WARN" if minutes >= t["warn"] else "OK")


# ------------------------------------------------------------------ checks
# Each returns (level, detail, epoch_or_None). epoch = the freshness anchor
# (build stamp / last good probe) used for status snapshots and stickiness.

def check_local_build():
    try:
        with open(LOCAL_DATA, encoding="utf-8") as f:
            ms = json.load(f).get("dataEpochMs")
        if not ms:
            return "FAIL", "dataEpochMs missing from static/site/data.json", None
        age = _age_min(ms / 1000.0)
        return _lvl("local_build", age), f"local build {age:.0f} min old", ms / 1000.0
    except (OSError, ValueError) as exc:
        return "FAIL", f"local data.json unreadable: {exc}", None


def _remote_data_check(name, url):
    try:
        ms = _fetch_json(url).get("dataEpochMs")
        if not ms:
            return "FAIL", "public data.json has no dataEpochMs", None
        age = _age_min(ms / 1000.0)
        return _lvl(name, age), f"public data.json {age:.0f} min old", ms / 1000.0
    except Exception as exc:  # noqa: BLE001 - network/format = checked state
        return "WARN", f"fetch failed: {type(exc).__name__}", _last_ok.get(name)


def check_gh_pages():
    return _remote_data_check("gh_pages_age", PUBLIC_DATA_URL)


def check_gh_pages_lag(local_epoch, public_epoch):
    if not local_epoch or not public_epoch:
        return "WARN", "skipped (one side unknown)", None
    lag = (local_epoch - public_epoch) / 60.0
    return _lvl("gh_pages_lag", lag), f"public site {max(lag, 0):.0f} min behind local build", None


def check_worker():
    try:
        cfg = _fetch_json(WORKER_URL, timeout=20)
        if not isinstance(cfg, dict) or "billingReady" not in cfg:
            return "FAIL", "worker answered but payload unrecognized", None
        return "OK", "members worker responding", time.time()
    except Exception as exc:  # noqa: BLE001
        return "WARN", f"worker probe failed: {type(exc).__name__}", _last_ok.get("worker")


def _wrangler_json(sql, timeout=120):
    """Run a D1 query, returning the first ['results'] row list in wrangler's
    output. --json prints a single JSON array of statement results (older
    builds printed concatenated docs; both are handled)."""
    r = subprocess.run(
        ["cmd", "/c", WRANGLER, "d1", "execute", "tnwn-members", "--remote",
         "-y", "--json", "--command", sql],
        capture_output=True, text=True, timeout=timeout, encoding="utf-8",
        errors="replace", creationflags=0x08000000 if os.name == "nt" else 0)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "")[-140:])
    dec, out, pos = json.JSONDecoder(), (r.stdout or ""), 0
    best = None
    while pos < len(out):
        while pos < len(out) and out[pos] in " \t\r\n":
            pos += 1
        if pos >= len(out):
            break
        obj, end = dec.raw_decode(out, pos)
        pos = end
        items = obj if isinstance(obj, list) else [obj]
        for it in items:
            if isinstance(it, dict) and it.get("results"):
                best = it["results"]
                break
        if best:
            break
    if best is None:
        raise RuntimeError("no results doc in wrangler output")
    return best


def check_d1_pages(local_epoch=None):
    """Every premium page in D1 must byte-match the local build (sha column,
    written by publish_site.upload_premium_pages). Page HTML only changes
    when site CODE changes (pages pull live data client-side), so upload AGE
    is not a staleness signal - sha drift is: it means the D1 upload pipeline
    broke after that page was built. check_once() self-heals this by
    re-running the upload (force=True, 30-min cooldown)."""
    try:
        rows = _wrangler_json("SELECT name, sha, updated FROM pages")
        d1 = {r.get("name"): (r.get("sha") or "", int(r.get("updated") or 0))
              for r in rows}
        if not d1:
            return "FAIL", "D1 pages table empty", None
        newest = max(u for _, u in d1.values())
        detail = (f"D1 pages: {len(d1)} rows, newest upload "
                  f"{_age_min(newest) / 60:.1f} h old")
        if not os.path.isdir(D1_SRC_DIR):
            return "WARN", detail + " - no local build to compare yet", None
        drift = []
        for fn in sorted(f for f in os.listdir(D1_SRC_DIR)
                         if f.endswith(".html")):
            path = os.path.join(D1_SRC_DIR, fn)
            try:
                if time.time() - os.path.getmtime(path) < 600:
                    continue   # just built - its publish may still be in flight
                with open(path, "rb") as f:
                    local_sha = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                continue
            if d1.get(fn[:-5], ("", 0))[0] != local_sha:
                drift.append(fn)
        if drift:
            return "FAIL", (detail + f"; {len(drift)} differ from local build: "
                            + ", ".join(drift[:3]))[:160], None
        return "OK", detail + ", all shas current", float(newest)
    except Exception as exc:  # noqa: BLE001 - wrangler hiccup = sticky WARN
        return "WARN", f"D1 probe failed: {exc}"[:160], _last_ok.get("d1_pages_age")


def check_cf_mirror():
    try:
        with open(CFPAGES_STAMP, encoding="utf-8") as f:
            ts = float(f.read().strip())
        age = _age_min(ts)
        return _lvl("cf_mirror", age), f"Cloudflare mirror synced {age / 3600:.1f} h ago", ts
    except (OSError, ValueError):
        return "WARN", "no cfpages.last stamp (mirror never synced?)", None


# ------------------------------------------------------------- escalation

def _compose(results):
    fails = [f"{k}: {v['detail']}" for k, v in results.items() if v["level"] == "FAIL"]
    return "TNWN FRESHNESS FAIL - " + " | ".join(fails)[:280]


def _network_outage(results):
    """>=2 remote probes down while the local build is fresh -> the machine's
    own link is the likely fault; suppress SMS (log-only)."""
    remote_bad = sum(1 for k, v in results.items()
                     if k in ("gh_pages_age", "worker", "d1_pages_age")
                     and v["level"] in ("WARN", "FAIL"))
    local = results.get("local_build", {})
    return remote_bad >= 2 and local.get("level") == "OK"


def _notify(results, overall, state):
    """SMS on FAIL (once per 6 h per check) and on recovery (once per 12 h)."""
    now = time.time()
    if overall == "FAIL" and not _network_outage(results):
        for name, v in results.items():
            if v["level"] != "FAIL":
                continue
            st = state.setdefault(name, {})
            if now - st.get("lastSms", 0) >= SMS_REPEAT_EVERY:
                st["lastSms"] = now
                try:
                    from data.sms_alerts import send_sms
                    send_sms(_compose(results), subject="TNWN FRESHNESS")
                except Exception as exc:  # noqa: BLE001 - never break the cycle
                    print(f"freshness sms error: {exc}", flush=True)
            st["alertSince"] = st.get("alertSince") or now
    else:
        for name, st in list(state.items()):
            if name.startswith("_") or not isinstance(st, dict):
                continue                      # skip _overall/_lastRun metadata
            if st.get("alertSince") and results.get(name, {}).get("level") == "OK":
                if now - st.get("lastRecover", 0) >= RECOVER_SMS_EVERY:
                    st["lastRecover"] = now
                    try:
                        from data.sms_alerts import send_sms
                        send_sms(f"TNWN recovered: {name} fresh again "
                                 f"({time.strftime('%H:%M ET')})", subject="TNWN FRESHNESS")
                    except Exception as exc:  # noqa: BLE001
                        print(f"freshness sms error: {exc}", flush=True)
                st.pop("alertSince", None)


# ------------------------------------------------------------------- entry

def check_once(force=False):
    """Run all checks; log + SMS-escalate; write the status snapshot.
    Returns (overall, results). Throttled to CHECK_EVERY unless force."""
    state = _load(STATE_PATH, {})
    last_run = state.get("_lastRun", 0)
    if not force and time.time() - last_run < CHECK_EVERY:
        return state.get("_overall", "OK"), None

    results = {}
    lvl, det, ep = check_local_build()
    results["local_build"] = {"level": lvl, "detail": det, "epoch": ep}
    lvl, det, pep = check_gh_pages()
    results["gh_pages_age"] = {"level": lvl, "detail": det, "epoch": pep}
    lvl, det, _ = check_gh_pages_lag(ep, pep)
    results["gh_pages_lag"] = {"level": lvl, "detail": det}
    lvl, det, wep = check_worker()
    results["worker"] = {"level": lvl, "detail": det, "epoch": wep}
    lvl, det, dep = check_d1_pages(ep)
    results["d1_pages_age"] = {"level": lvl, "detail": det, "epoch": dep}
    lvl, det, cep = check_cf_mirror()
    results["cf_mirror"] = {"level": lvl, "detail": det, "epoch": cep}

    # Sticky anchors: remember the last GOOD probe time for remote checks so
    # a flapping probe keeps counting from the last success, not resetting.
    for name, res in results.items():
        if res.get("epoch"):
            _last_ok[name] = res["epoch"]

    # D1 self-heal: drifted premium pages -> re-run the upload directly
    # (force=True bypasses the sha state file - the 2026-10-03 incident had
    # state claiming success while D1 actually served the wrong content).
    # 30-min cooldown caps churn; re-probe so alerts reflect post-heal state.
    if (results["d1_pages_age"]["level"] == "FAIL"
            and not _network_outage(results)
            and time.time() - state.get("_d1Heal", 0) >= D1_HEAL_EVERY):
        state["_d1Heal"] = time.time()
        try:
            import publish_site
            _ok = publish_site.upload_premium_pages(force=True)
            print(f"freshness: D1 self-heal re-upload "
                  f"{'completed' if _ok else 'FAILED/skipped'}", flush=True)
        except Exception as exc:  # noqa: BLE001 - never break the cycle
            print(f"freshness: D1 self-heal error: {exc}", flush=True)
        lvl, det, dep = check_d1_pages()
        results["d1_pages_age"] = {"level": lvl, "detail": det, "epoch": dep}

    order = {"OK": 0, "WARN": 1, "FAIL": 2}
    overall = max((v["level"] for v in results.values()), key=lambda l: order[l])
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    # Log (quiet when all green - the updater log is read by humans).
    bad = {k: v for k, v in results.items() if v["level"] != "OK"}
    for name, v in bad.items():
        print(f"freshness {v['level']}: {name} - {v['detail']}", flush=True)
    if bad or overall != state.get("_overall"):
        print(f"freshness: overall {overall} ({len(bad)} of {len(results)} checks not OK)",
              flush=True)
    if _network_outage(results):
        print("freshness: multiple remote probes down but local build is "
              "fresh - likely this machine's network; SMS suppressed.", flush=True)

    _notify(results, overall, state)
    state["_overall"], state["_lastRun"] = overall, time.time()
    _save(STATE_PATH, state)
    _save(STATUS_PATH, {"checkedEpochS": time.time(), "checked": stamp,
                        "overall": overall, "checks": results})
    return overall, results


def main(argv=None):
    argv = argv or sys.argv[1:]
    force = "--force" in argv
    as_json = "--json" in argv
    overall, results = check_once(force=force)
    if as_json:
        print(json.dumps(results, indent=1))
        return 0
    print(f"TNWN freshness: overall {overall}")
    for name, v in (results or {}).items():
        print(f"  {v['level']:4} {name:14} {v['detail']}")
    return 2 if overall == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())

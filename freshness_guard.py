"""OUT-OF-BAND FRESHNESS GUARD - the layer that watches the watchers.

Every other watchdog lives INSIDE the updater (site_updater's freshness
checks, freshness_watch, the SMS weather texter). If the updater dies AND
the supervisor task is broken, they all go quiet together and the site
freezes with nobody the wiser. This module runs as its OWN scheduled task
(TNWN-FreshnessGuard, every 15 min) with no dependency on the updater
process, so it is the one alarm that still rings when the updater is dead:

  1. watch status    .freebuff/freshness_status.json older than 25 min means
                     the in-updater watch is dead -> run freshness_watch
                     here, standalone (its FAIL checks still SMS).
  2. updater heartbeat   .freebuff/updater.heartbeat stale past the
                     supervisor's own 50-min grace -> the whole restart chain
                     failed -> SMS + kick the supervisor task (schtasks /Run,
                     45-min kick cooldown, harmless when healthy).
  3. served build    docs/data.json older than 75 min -> build pipeline fully
                     stalled -> SMS.

PAUSED (.freebuff/PAUSED - the STOP button) always wins: stand down.

Own state: .freebuff/freshness_guard_state.json (per-incident SMS dedup,
60-min re-alert). Own log: .freebuff/freshness_guard.log (self-truncating).
Exit 0 = healthy, 2 = alert/action issued (visible in Task Scheduler's
Last Result).

Install (once, from the project root):
  schtasks /Create /F /TN TNWN-FreshnessGuard /SC MINUTE /MO 15 /
    TR "C:\\...\\pythonw.exe C:\\...\\freshness_guard.py"
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# Scheduled tasks start in System32 - every relative path below (and in
# freshness_watch) depends on the project root as cwd, like site_updater.
os.chdir(ROOT)

SUPERVISOR_TASK = "TNWN-WeatherCenter"
PAUSED = os.path.join(".freebuff", "PAUSED")
HEARTBEAT = os.path.join(".freebuff", "updater.heartbeat")
STATUS = os.path.join(".freebuff", "freshness_status.json")
DOCS_DATA = os.path.join("docs", "data.json")
STATE_PATH = os.path.join(".freebuff", "freshness_guard_state.json")
LOG_PATH = os.path.join(".freebuff", "freshness_guard.log")

# Thresholds vs the layers below us: the supervisor's own heartbeat grace is
# 50 min and its watchdog restarts hung updaters on a 30-min cooldown, so we
# alert at 55 min - if THIS fires, the supervisor chain itself has failed.
STATUS_STALE_MIN = 25
HEARTBEAT_STALE_MIN = 55
DOCS_STALE_MIN = 75
SMS_REPEAT = 60 * 60        # re-text the same incident hourly
KICK_COOLDOWN = 45 * 60     # supervisor kick at most every 45 min
LOG_CAP = 256 * 1024


def _age_min(path, fallback=1e9):
    try:
        return (time.time() - os.path.getmtime(path)) / 60.0
    except OSError:
        return fallback


def _log(msg):
    try:
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_CAP:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def _sms(text, state, kind):
    now = time.time()
    if now - state.get(kind, 0) < SMS_REPEAT:
        return False
    state[kind] = now
    try:
        from data.sms_alerts import send_sms
        send_sms(text[:320], subject="TNWN GUARD")
        _log(f"SMS sent ({kind}): {text[:80]}")
    except Exception as exc:  # noqa: BLE001 - alerting must not crash the guard
        _log(f"SMS error: {exc}")
    return True


def _kick_supervisor(state):
    """Wake the supervisor task - harmless when healthy (it exits immediately
    after confirming services). Cooldown-capped."""
    if time.time() - state.get("_lastKick", 0) < KICK_COOLDOWN:
        return False
    state["_lastKick"] = time.time()
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", SUPERVISOR_TASK],
                           capture_output=True, text=True, timeout=20,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        ok = r.returncode == 0
        _log(f"supervisor kick via schtasks: rc={r.returncode}")
        return ok
    except Exception as exc:  # noqa: BLE001
        _log(f"supervisor kick error: {exc}")
        return False


def run():
    if os.path.exists(PAUSED):
        return 0                        # STOP button: stale data is deliberate

    state = {}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        pass

    status_age = _age_min(STATUS)
    heart_age = _age_min(HEARTBEAT)
    docs_age = _age_min(DOCS_DATA)
    issues = []

    # 1. The in-updater watch has gone quiet -> BE the watch (standalone run
    #    covers the real FAIL checks; its dedup/notify logic handles SMS).
    if status_age > STATUS_STALE_MIN:
        issues.append(f"watch quiet {status_age:.0f} min")
        try:
            import freshness_watch
            overall, _ = freshness_watch.check_once(force=True)
            _log(f"standalone freshness check: overall {overall}")
        except Exception as exc:  # noqa: BLE001
            _log(f"standalone freshness check error: {exc}")

    # 2. Updater heartbeat stale past the supervisor's own grace: the restart
    #    chain failed. Alert AND kick - the kick is what heals.
    if heart_age > HEARTBEAT_STALE_MIN:
        issues.append(f"updater heartbeat {heart_age:.0f} min stale")
        if _kick_supervisor(state):
            issues.append("supervisor kick sent")

    # 3. The served build itself is old: everything above it failed to heal.
    if docs_age > DOCS_STALE_MIN:
        issues.append(f"served build {docs_age:.0f} min old")

    if issues:
        # dedup kind excludes the kick annotation - "kick sent" vs not is the
        # same incident, otherwise every post-kick run re-texts
        kind = " | ".join(i for i in issues if i != "supervisor kick sent")
        msg = "TNWN GUARD: " + "; ".join(issues) + " - check the weather PC"
        if _sms(msg, state, kind):
            issues.append("SMS sent")
    _log("guard: " + ("; ".join(issues) if issues else "all quiet"))

    state["_lastRun"] = time.time()
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass
    return 2 if issues else 0


if __name__ == "__main__":
    sys.exit(run())

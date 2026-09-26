import os
import sys
import json
import time
import atexit

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

# CREATE_NO_WINDOW: console tools spawned by a console-less parent allocate a
# VISIBLE console box on the user's screen (2026-09-21 complaint). Suppress it.
NOWIN = 0x08000000 if os.name == "nt" else 0

SITE_INTERVAL = 120
FB_INTERVAL = 300

PIDFILE = os.path.join(".freebuff", "site-updater.pid")
LOGFILE = os.path.join(".freebuff", "site-updater.log")

# ------------------------------------------------------------
# PUBLIC-SITE FRESHNESS WATCHDOG
# The public cadence is a publish every ~10 min + Pages CDN lag, so a
# healthy public site sits 0-15 min behind the local build. Past 30 min
# something is actually wrong (failed push, stuck Pages deploy, deploy
# pipeline regression - hit 2026-09-11 when two deploy pipelines raced).
# Alert once per incident to .freebuff/PUBLIC_STALE.alert and try one
# recovery publish, re-checked every FRESHNESS_CHECK_EVERY seconds.
# ------------------------------------------------------------
FRESHNESS_CHECK_EVERY = 300      # check every 5 min
PUBLIC_STALE_AFTER = 1800        # alert past 30 min of lag
RECOVERY_PUBLISH_EVERY = 1800    # at most one recovery publish / 30 min
PUBLIC_DATA_URL = "https://rpleasant12.github.io/http-localhost-8765-/data.json"
ALERT_PATH = os.path.join(".freebuff", "PUBLIC_STALE.alert")
_last_freshness_check = 0.0
_last_recovery_publish = 0.0


def check_public_freshness(force=False):
    """Alert + attempt recovery when the public site lags the local build.

    Compares dataEpochMs in the public data.json against the local one
    (both written by generate_site, same clock). Alerts once per incident
    by writing ALERT_PATH; clears it when freshness returns.
    """
    global _last_freshness_check, _last_recovery_publish
    now = time.time()
    if not force and now - _last_freshness_check < FRESHNESS_CHECK_EVERY:
        return
    _last_freshness_check = now

    import json as _json
    import urllib.request

    def _epoch_ms(path_or_url, remote=False):
        try:
            if remote:
                req = urllib.request.Request(
                    path_or_url, headers={"User-Agent": "tnwn-updater",
                                          "Cache-Control": "no-cache"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return _json.load(resp).get("dataEpochMs")
            with open(path_or_url, encoding="utf-8") as f:
                return _json.load(f).get("dataEpochMs")
        except Exception:  # noqa: BLE001 - network/format problems = skip cycle
            return None

    local_ms = _epoch_ms(os.path.join("static", "site", "data.json"))
    public_ms = _epoch_ms(PUBLIC_DATA_URL, remote=True)
    if not local_ms or not public_ms:
        return                      # own build missing or network hiccup

    lag_min = (local_ms - public_ms) / 60000.0
    if lag_min <= PUBLIC_STALE_AFTER / 60:
        if os.path.isfile(ALERT_PATH):
            os.remove(ALERT_PATH)
            log(f"Public site fresh again (lag {lag_min:.0f} min) - alert cleared.")
        return

    # Stale: write/refresh the alert marker.
    try:
        with open(ALERT_PATH, "w", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} public site lags "
                    f"local build by {lag_min:.0f} min\n")
    except OSError:
        pass
    log(f"WARNING: public site {lag_min:.0f} min behind local build "
        f"(alert filed; normal lag is 0-15 min).")

    # Recovery: one extra publish per half hour, on top of the throttle.
    if now - _last_recovery_publish >= RECOVERY_PUBLISH_EVERY:
        _last_recovery_publish = now
        try:
            import github_deploy      # local import: module-level import lives in main()
            github_deploy.package()
            if spawn_publish():
                log("Recovery publish started in background.")
        except Exception as exc:  # noqa: BLE001 - must never kill the updater
            log(f"Recovery publish error: {exc}")


# ------------------------------------------------------------
# NOAA UPSTREAM FEED WATCHDOG
# A stalled NOAA feed looks identical to a broken site from the outside
# (RRFS sat 4+ h at 21Z on 2026-09-25 and the page read "NOT UPDATING").
# Reuse website._upstream_status() - the same probe round the models-page
# status line ships - so the log and the page can never disagree. Warn
# once per incident per model; RRFS past RRFS_STALE_HOURS re-warns hourly
# (bounded) because RRFS staleness is what users notice first.
# ------------------------------------------------------------
UPSTREAM_CHECK_EVERY = 300       # check every 5 min
RRFS_STALE_HOURS = 2.0           # RRFS publishes hourly; 2 h = upstream gap
RRFS_REWARN_EVERY = 3600         # while stalled, re-warn hourly

_upstream_last_check = 0.0
_upstream_alerted = {}           # model -> True while its incident is open
_rrfs_last_warn = 0.0


def check_upstream_feeds(force=False):
    """Warn in the log when NOAA model feeds lag their publish rhythm.

    Calls website._upstream_status() (12-min probe cache shared with the
    models page) on a 5-min throttle. Any feed whose newest published
    cycle is older than its maxAge logs a WARNING once per incident and a
    "fresh again" line when it recovers; RRFS older than RRFS_STALE_HOURS
    additionally re-warns hourly for the duration of the gap. An empty
    probe round (network down) stays silent - never a false alarm.
    """
    global _upstream_last_check, _rrfs_last_warn
    now = time.time()
    if not force and now - _upstream_last_check < UPSTREAM_CHECK_EVERY:
        return
    _upstream_last_check = now

    try:
        import website as _website
        status = _website._upstream_status()
    except Exception as exc:  # noqa: BLE001 - must never break the update cycle
        log(f"upstream feed check error: {exc}")
        return
    if not status:
        return                      # probe round produced nothing - stay silent

    import datetime as _dt
    now_utc = _dt.datetime.now(_dt.timezone.utc)

    def _age_h(cyc):
        try:
            c = _dt.datetime.strptime(cyc, "%Y%m%d%H").replace(
                tzinfo=_dt.timezone.utc)
        except (ValueError, TypeError):
            return None
        return (now_utc - c).total_seconds() / 3600.0

    for model, info in sorted(status.items()):
        cyc = (info or {}).get("cycle")
        age = _age_h(cyc)
        if age is None:
            continue
        max_age = float((info or {}).get("maxAge") or 2.0)
        if age > max_age:
            if not _upstream_alerted.get(model):
                _upstream_alerted[model] = True
                log(f"WARNING: NOAA {model} feed lagging - newest published "
                    f"cycle {cyc} is {age:.1f} h old (normal <= {max_age:.1f} h); "
                    f"renders hold at that cycle until NOAA uploads.")
            if (model == "RRFS" and age > RRFS_STALE_HOURS
                    and now - _rrfs_last_warn >= RRFS_REWARN_EVERY):
                _rrfs_last_warn = now
                log(f"WARNING: RRFS newest cycle {cyc} is {age:.1f} h old "
                    f"(> {RRFS_STALE_HOURS:.0f} h) - RRFS maps and the "
                    f"HRRR-vs-RRFS card are holding at the old init. NOAA "
                    f"upstream gap, not a site fault.")
        elif _upstream_alerted.pop(model, None):
            log(f"NOAA {model} feed fresh again (newest cycle {cyc}).")


# ------------------------------------------------------------
# DETACHED PUBLISH
# ------------------------------------------------------------

PUBLISH_DONE = os.path.join(".freebuff", "publish.done")

# Local static/ frames are only a serving cache for the local preview - every
# published byte lives on GitHub (gh-pages, no history growth). After each
# confirmed publish, delete local frames older than their display window:
# the public site keeps serving the full history, the disk keeps only the
# fresh window. (User request 2026-09-14: "save maps to github not my disk".)
PRUNE_KEEP_SECONDS = {
    "model_maps": 30 * 3600,   # must outlive NOAA bucket stalls: during the
    # 09-14 NBM lag (newest live cycle 16 h old) an 8 h window deleted the
    # only renderable maps, so the models page showed zero NBM maps
    "hrrr": 8 * 3600,
    "sevmaps": 8 * 3600,
    "winter": 8 * 3600,
    "soundings": 8 * 3600,
    "aimodels": 14 * 3600,    # 6-hourly inits, keep two
    "meso": 6 * 3600,
    "nexrad_sites": 3 * 3600,
    "mrms": 3 * 3600,
    "nws_radar": 3 * 3600,
    "goes": 3 * 3600,
    "glm": 3 * 3600,
    "wbgt": 8 * 3600,
    "tropics": 24 * 3600,
    "climate": 24 * 3600,
    # herbie/ is the raw GRIB + index download cache: NEVER served (not in
    # ASSET_DIRS), consumed within the cycle that fetched it, and always
    # re-downloadable - so keep it only 1 h. It sat unpruned and grew to
    # 68 MB (2026-09-15).
    "herbie": 1 * 3600,
}


# The public site is what actually ships to GitHub Pages, which refuses
# sites over ~1 GB - publish_site.py blocks pushes above its 950 MB cap.
# docs/ is rebuilt EVERY cycle by github_deploy.package() as the payload-
# referenced subset of static/ (atomic swap), so the only durable lever is
# the SOURCE: prune static/ to "wire windows" sized just above each
# payload's reference horizon. docs/ grew to 1.1 GB (2026-09-22) because
# the only prune ran AFTER a successful publish: over-cap -> publish
# refused -> prune never ran -> deadlock, public site frozen while the
# local build kept updating. Windows: model_maps 21 h = 3 cycles of the
# slowest 6-hourly globals + margin (the NBM-stall lesson from 09-14:
# never below 3 cycles); aimodels 14 h = its "keep two 6-h inits" horizon;
# nexrad_sites 100 min = the player's 15 x 6-min frames + margin.
WIRE_KEEP_SECONDS = {
    "model_maps": 21 * 3600,
    "aimodels": 14 * 3600,
    "nexrad_sites": 100 * 60,
    "meso": 3 * 3600,
    "goes": 2 * 3600,
    "mrms": 2 * 3600,
    "glm": 2 * 3600,
    "nws_radar": 2 * 3600,
    "hrrr": 6 * 3600,
}
# Hard ceiling on docs/ image bytes - well under publish_site.py's 950 MB
# push gate. The window prune runs first; this cap only bites when the
# render catalog outgrows the windows (it will - the site adds walls
# constantly), evicting oldest frames so publishing can never deadlock
# against its own size gate again.
DOCS_CAP_MB = 880


def prune_wire_frames():
    """Enforce the wire windows on static/ AND docs/, every cycle.

    static/ is the durable source - package() mirrors its referenced files
    into docs/ via hardlinks each cycle, so shrinking static/ shrinks every
    future build. docs/ is pruned too: normally redundant (the swap rebuilds
    it), but it catches strays left by the merge-copy fallback path. Must
    run BEFORE the publish gate: publish_site.py measures docs/ size, so an
    over-grown tree blocks its own cleanup forever otherwise. Windows are
    each payload's reference horizon plus margin, so nothing a fresh payload
    points at is ever deleted.
    """
    import time as _t
    cutoffs = {k: _t.time() - v for k, v in WIRE_KEEP_SECONDS.items()}
    removed = saved = 0
    for base in ("static", "docs"):
        for sub, cutoff in cutoffs.items():
            root = os.path.join(base, sub)
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    if not fn.lower().endswith((".png", ".gif", ".jpg", ".webp")):
                        continue
                    p = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if st.st_mtime < cutoff:
                        try:
                            os.remove(p)
                            removed += 1
                            saved += st.st_size
                        except OSError:
                            continue
    if removed:
        log(f"wire prune: removed {removed} frames ({saved / 1_000_000:.0f} MB) "
            f"past their public display window")

    # Growth guarantee: the render catalog expands every week (new walls,
    # products, regions), so fixed windows alone would keep re-crossing the
    # publish cap. The census measures DOCS/ - the byte set publish_site.py
    # actually gates - not static/: static/ additionally holds always-fresh
    # serving caches (nexrad_sites oscillates 40->180 MB between its own
    # budget trims) that package() never mirrors, so counting them evicted
    # healthy model_maps history while docs/ sat comfortably under the cap
    # (walls starved 2026-09-23). Eviction removes the static/ ORIGINALS
    # oldest-first: docs/ is rebuilt from static/ every cycle, so trimming
    # the source trims every future build; the newest files (what the live
    # payload references) are naturally protected by mtime order.
    cap = DOCS_CAP_MB * 1_000_000
    docs_total = 0
    for sub in WIRE_KEEP_SECONDS:
        root = os.path.join("docs", sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith((".png", ".gif", ".jpg", ".webp")):
                    continue
                try:
                    docs_total += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    continue
    if docs_total <= cap:
        return removed
    cands = []
    for sub in WIRE_KEEP_SECONDS:
        root = os.path.join("static", sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith((".png", ".gif", ".jpg", ".webp")):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                cands.append((st.st_mtime, st.st_size, p))
    cands.sort()
    evicted = ev_bytes = 0
    for _mt, sz, p in cands:
        if docs_total <= cap:
            break
        try:
            os.remove(p)
            docs_total -= sz
            evicted += 1
            ev_bytes += sz
        except OSError:
            continue
    if evicted:
        log(f"wire prune: docs over {DOCS_CAP_MB} MB cap - evicted {evicted} "
            f"oldest source frames ({ev_bytes / 1_000_000:.0f} MB)")
    return removed + evicted


def prune_published_local():
    """Delete local cache frames older than PRUNE_KEEP_SECONDS (per folder).

    Runs ONLY right after a confirmed push, so anything deleted here is
    already on GitHub. Non-image files and renderer state are never touched
    (enforce_disk_budget's protected set lives in website.py).
    """
    import time as _t
    removed = saved = 0
    cutoffs = {k: _t.time() - v for k, v in PRUNE_KEEP_SECONDS.items()}
    for sub, cutoff in cutoffs.items():
        root = os.path.join("static", sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith((".png", ".gif", ".jpg", ".webp")):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    try:
                        os.remove(p)
                        removed += 1
                        saved += st.st_size
                    except OSError:
                        continue
    if removed:
        log(f"post-publish prune: removed {removed} local frames "
            f"({saved / 1_000_000:.0f} MB) - all already on GitHub")
    return removed


def spawn_publish():
    """Run publish_site.py as a DETACHED child process.

    The publish is the heaviest, crashiest part of the cycle (hundreds of MB
    of git object work). Running it in-process let one killed git child take
    the whole updater down (2026-09-12: updater died mid-publish, twice). A
    detached child that writes .freebuff/publish.done on success isolates it:
    the updater launches it, checks the marker next cycle, keeps cycling.
    """
    import subprocess
    import sys
    if os.path.isfile(PUBLISH_DONE):
        try:
            os.remove(PUBLISH_DONE)
        except OSError:
            pass
    # PRIMARY: write a request marker and fire the scheduled task. The task
    # instance (owned by the Task Scheduler service) picks the marker up in
    # startup_task.ensure_services() and starts the publisher from THERE, so
    # the publish cannot die with this updater's console when a session
    # tears down mid-push (that exact kill left the public site 9 h stale,
    # 2026-09-18). Triggering the task is harmless when healthy: the task's
    # own run exits immediately after adopting/confirming services.
    try:
        with open(os.path.join(".freebuff", "publish.request"), "w",
                  encoding="utf-8") as rf:
            rf.write("request\n")
        subprocess.run(["schtasks", "/Run", "/TN", "TNWN-WeatherCenter"],
                       capture_output=True, text=True, timeout=20, creationflags=NOWIN)
        log("Publish requested via scheduled task (teardown-safe path).")
        return True
    except Exception as exc:  # noqa: BLE001 - must never kill the updater
        log(f"Publish request failed ({exc}) - spawning directly.")
    # FALLBACK: previous direct detached spawn (also used when the task is
    # missing entirely). CREATE_BREAKAWAY_FROM_JOB matters: without it the
    # publisher stays in OUR job object, and an editor-session teardown
    # kills the whole tree mid-`git add` - empty stderr, rc=3221225786,
    # exactly the 2026-09-20 21:18-21:55 failure run. If breakaway is not
    # permitted by the job, retry without (old behavior).
    flags = 0
    if os.name == "nt":
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    try:
        with open(os.path.join(".freebuff", "publish.log"), "ab") as pf:
            subprocess.Popen([sys.executable, "publish_site.py"],
                             stdout=pf, stderr=subprocess.STDOUT,
                             creationflags=flags, close_fds=True)
        log("Publish started as detached process.")
        return True
    except OSError:
        if os.name == "nt":
            flags = (subprocess.CREATE_NEW_PROCESS_GROUP
                     | subprocess.DETACHED_PROCESS)
        try:
            with open(os.path.join(".freebuff", "publish.log"), "ab") as pf:
                subprocess.Popen([sys.executable, "publish_site.py"],
                                 stdout=pf, stderr=subprocess.STDOUT,
                                 creationflags=flags, close_fds=True)
            log("Publish started as detached process (no breakaway).")
            return True
        except Exception as exc:  # noqa: BLE001 - must never kill the updater
            log(f"Publish spawn failed: {exc}")
            return False
    except Exception as exc:  # noqa: BLE001 - must never kill the updater
        log(f"Publish spawn failed: {exc}")
        return False


def publish_finished():
    """True once a spawned publish completed since the last check."""
    if not os.path.isfile(PUBLISH_DONE):
        return False
    try:
        os.remove(PUBLISH_DONE)
    except OSError:
        pass
    return True


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)

    os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
    with open(LOGFILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _live_updater_pids():
    """All live site_updater.py pids, scanned from the process table.

    Pidfile-independent: an empty/stale pidfile once admitted a second
    updater, whose parallel publishes collided and whose stale 8 h prune
    deleted fresh model maps (2026-09-15). Costs ~1 s at startup only.
    """
    if os.name != "nt":
        return []
    import subprocess
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
             "Where-Object { $_.CommandLine -match 'site_updater' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=30, creationflags=NOWIN).stdout
    except Exception:
        return []
    pids = []
    for tok in out.split():
        try:
            pids.append(int(tok))
        except ValueError:
            pass
    return pids


def already_running():
    """True if ANY live site_updater.py process exists.

    Primary check is the process-table scan (pidfile-independent - an empty
    pidfile once admitted a second updater, 2026-09-15); the pidfile is only
    a fallback on non-Windows. Never start a second instance; startup_task's
    5-min self-heal is what kills strays.
    """
    if os.name == "nt":
        # exclude SELF: this process's own command line matches the filter,
        # and self-detection made every fresh start exit as "already running"
        if [p for p in _live_updater_pids() if p != os.getpid()]:
            return True
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            pid = int(f.read().strip())

        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(
                0x100000, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False

        os.kill(pid, 0)
        return True

    except (OSError, ValueError):
        return False


def cleanup():
    try:
        if os.path.isfile(PIDFILE):
            os.remove(PIDFILE)
    except OSError:
        pass


# ------------------------------------------------------------
# LIVE RADAR / SATELLITE RENDERERS
# ------------------------------------------------------------

def start_live_renderers():
    """
    Start the background renderers used by the TNWN website.

    These render actual image files into static/ so that
    github_deploy.py can package and publish them.
    """

    # NWS CONUS radar mosaic
    try:
        from data.nws_radar import start_nws_renderer
        start_nws_renderer()
        log("NWS live radar renderer started.")
    except Exception as exc:
        log(f"NWS radar renderer error: {exc}")

    # MRMS radar products
    try:
        from data.mrms import start_mrms_renderer
        start_mrms_renderer("cref")
        log("MRMS live radar renderer started.")
    except Exception as exc:
        log(f"MRMS renderer error: {exc}")

    # Future radar / model radar renderer (HRRR + NAM out to +48 h).
    # Without this the updater-driven pipeline never renders future frames:
    # the only other kickoff lives in app.py / github_deploy.warm_renderers.
    try:
        from data.radar_frames import start_future_renderer
        start_future_renderer(max_hours=48)
        log("Future radar renderer started (HRRR + NAM, 48 h).")
    except Exception as exc:
        log(f"Future radar renderer error: {exc}")

    # GOES satellite renderer
    try:
        from data.satellite_bands import (
            get_band_frames,
            BANDS,
            _render_band_frame,
            _merge_descriptors,
            _prune_registry,
            _save_registry,
        )

        rendered = 0

        for key in BANDS:
            try:
                frames = get_band_frames(key)

                if frames:
                    for frame in frames[-3:]:
                        try:
                            _render_band_frame(key, frame)
                            rendered += 1
                        except Exception as exc:
                            log(
                                f"Satellite frame error "
                                f"{key}: {str(exc)[:160]}"
                            )

            except Exception as exc:
                log(
                    f"Satellite band error "
                    f"{key}: {str(exc)[:160]}"
                )

        log(f"GOES satellite renderer initialized: {rendered} frames processed.")

    except Exception as exc:
        log(f"Satellite renderer startup error: {exc}")


def refresh_live_data():
    """
    Trigger live renderers once per update cycle.

    Background workers remain alive, while this function makes sure
    the current cycle actually touches radar/satellite data.
    """

    # NWS radar
    try:
        from data.nws_radar import get_nws_frames, _save_descriptors

        frames = get_nws_frames()
        _save_descriptors(frames)

        log(f"NWS radar: {len(frames)} current frame descriptors.")

    except Exception as exc:
        log(f"NWS radar refresh error: {exc}")

    # MRMS
    try:
        from data.mrms import mrms_bundle

        frames = mrms_bundle("cref")
        log(f"MRMS radar: {len(frames)} frames available.")

    except Exception as exc:
        log(f"MRMS refresh error: {exc}")

    # Satellite
    try:
        from data.satellite_bands import BANDS, get_band_frames

        total = 0

        for key in BANDS:
            try:
                frames = get_band_frames(key)
                total += len(frames or [])
            except Exception:
                continue

        log(f"GOES satellite: {total} frame descriptors available.")

    except Exception as exc:
        log(f"Satellite refresh error: {exc}")


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():
    if already_running():
        print("TNWN site updater is already running.")
        return

    os.makedirs(".freebuff", exist_ok=True)

    with open(PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    atexit.register(cleanup)

    from website import generate_site
    import github_deploy

    log("TNWN automatic updater started.")

    # Start the actual image renderers BEFORE the first site build.
    start_live_renderers()

    last_fb = 0
    last_publish = 0.0
    last_sms = 0.0
    last_fbpost = 0.0
    # The LOCAL site rebuilds every cycle (fresh data.json for the preview),
    # but each GitHub publish re-blobs ~300 MB of changed radar frames into
    # .git (and a Pages deployment) - publishing every cycle filled the disk
    # (1+ GB object store) and hammered Pages. 10 min keeps the public site
    # plenty fresh for weather; publish immediately on the first cycle.
    PUBLISH_EVERY = 600

    while True:
        started = time.time()

        try:
            # ------------------------------------------------
            # LIVE RADAR / SATELLITE
            # ------------------------------------------------
            log("Refreshing live radar and satellite data...")
            refresh_live_data()

            # ------------------------------------------------
            # WEBSITE
            # ------------------------------------------------
            log("Generating fresh weather data...")

            result = generate_site()

            if result is None:
                raise RuntimeError("generate_site() returned None")

            log("Weather site regenerated.")

            # Resumed after a STOP: the docs/ copy of PAUSED.json would keep
            # every page claiming the site is frozen forever. A fresh build
            # always publishes the truth, so clear a stale marker.
            try:
                _stale_marker = os.path.join("docs", "PAUSED.json")
                if os.path.exists(_stale_marker) and not os.path.exists(
                        os.path.join(".freebuff", "PAUSED")):
                    os.remove(_stale_marker)
                    log("Cleared stale docs/PAUSED.json (resumed).")
            except OSError:
                pass

            # A background publish from an earlier cycle may have finished.
            if publish_finished():
                last_publish = time.time()
                log("GitHub Pages published successfully.")
                check_public_freshness(force=True)   # clear any alert
                try:
                    prune_published_local()          # disk only keeps the live window
                except Exception as pexc:            # noqa: BLE001 - never break the cycle
                    log(f"post-publish prune error: {pexc}")

            # Enforce the wire windows every cycle, BEFORE the publish gate:
            # publish_site.py refuses pushes over its size cap, so an over-
            # grown tree must shrink first or it blocks its own cleanup (the
            # 09-22 deadlock: public site frozen ~40 min while docs/ sat at
            # 1.1 GB). Pruning static/ here also means package() hardlinks a
            # shrunken source into docs/ this same cycle.
            try:
                prune_wire_frames()
            except Exception as dexc:    # noqa: BLE001 - never break the cycle
                log(f"wire prune error: {dexc}")

            # ------------------------------------------------
            # LOCAL MIRROR: refresh docs/data.json every build
            # ------------------------------------------------
            # The preview server serves docs/, and docs/data.json only used
            # to change on the 10-min publish cycle (package() runs inside
            # the gate) - so locally the obs board sat up to ~10 min behind
            # every build ("observations not updating", 2026-09-23). Copy
            # the fresh payload + pages into docs/ each cycle; the full
            # payload-referenced swap still happens only on publishes.
            try:
                import shutil as _shutil
                from github_deploy import rewrite as _gd_rewrite, _walk_json as _gd_walk
                for _f in ("data.json", "index.html", "obs.html", "dashboard.html",
                           "meso.html", "radar.html", "satellite.html", "models.html",
                           "severe.html", "fieldguide.html"):
                    _src = os.path.join("static", "site", _f)
                    _dst = os.path.join("docs", _f)
                    if not os.path.isfile(_src):
                        continue
                    # Mirror the PACKAGED form, not the raw build: raw payload
                    # URLs ("../meso/..") only resolve from the app root; the
                    # packager rewrites them to docs/relative ("meso/.."). The
                    # original copy2 shipped raw URLs, so every cycle the
                    # served data.json flipped back to 404-refs between
                    # publishes - meso fields, wall tiles and any page
                    # live-refresh broke until the next 10-min package()
                    # (found 2026-09-24 via the ET meso EHI field).
                    if _f.endswith(".html"):
                        _copy = set()
                        _text = open(_src, encoding="utf-8").read()
                        with open(_dst, "w", encoding="utf-8") as _out:
                            _out.write(_gd_rewrite(_text, _copy))
                    else:
                        _copy = set()
                        with open(_src, encoding="utf-8") as _in:
                            _payload = json.load(_in)
                        with open(_dst, "w", encoding="utf-8") as _out:
                            json.dump(_gd_walk(_payload, _copy), _out)
                    # rewrite() only RECORDS asset refs - copy the referenced
                    # graphics the packager would have hardlinked
                    for _rel in _copy:
                        _s = os.path.join("static", _rel.replace("/", os.sep))
                        _d = os.path.join("docs", _rel.replace("/", os.sep))
                        if os.path.isfile(_s):
                            os.makedirs(os.path.dirname(_d), exist_ok=True)
                            if not os.path.exists(_d):
                                try:
                                    os.link(_s, _d)
                                except OSError:
                                    try:
                                        _shutil.copy2(_s, _d)
                                    except OSError:
                                        pass
                # mesoanalysis overlays re-render every cycle; the meso dir is
                # hardlink-mirrored by package(), so re-linking here keeps the
                # served overlays current between publishes (radar/warns loops).
                # model_maps too: newly-rendered tiles (a model's first frame
                # of a new product) must not 404 on the local wall until the
                # next 10-min publish (SCP/STP walls 2026-09-23). hail_ed too:
                # the radar page's annotated hail-core lesson cut rebuilds on
                # its own 12 h cache and must not 404 on the served page.
                for _pair in (("meso", "meso"), ("model_maps", "model_maps"),
                              ("hail_ed", "hail_ed")):
                    _sdir, _ddir = os.path.join("static", _pair[0]), os.path.join("docs", _pair[1])
                    for _root, _dirs, _files in os.walk(_sdir):
                        _rel = os.path.relpath(_root, _sdir)
                        _out = os.path.join(_ddir, _rel) if _rel != "." else _ddir
                        os.makedirs(_out, exist_ok=True)
                        for _fn in _files:
                            _s = os.path.join(_root, _fn)
                            _d = os.path.join(_out, _fn)
                            if not os.path.exists(_d) or os.path.getmtime(_s) > os.path.getmtime(_d) + 5:
                                try:
                                    if os.path.exists(_d):
                                        os.remove(_d)
                                    os.link(_s, _d)
                                except OSError:
                                    try:
                                        _shutil.copy2(_s, _d)
                                    except OSError:
                                        pass
            except OSError:
                pass

            # ------------------------------------------------
            # GITHUB PAGES (throttled - see PUBLISH_EVERY)
            # ------------------------------------------------
            if time.time() - last_publish >= PUBLISH_EVERY:
                log("Packaging GitHub Pages docs...")

                count = github_deploy.package()

                log(f"GitHub package complete: {count} assets.")

                log("Publishing to GitHub Pages...")

                spawn_publish()
                log("GitHub publish running in background; "
                    "will confirm next cycle.")
            else:
                log(f"Skipping GitHub publish (next in "
                    f"{int(PUBLISH_EVERY - (time.time() - last_publish))}s).")

            # Freshness watchdog (own 5-min throttle inside).
            check_public_freshness()

            # NOAA upstream feed watchdog (own 5-min throttle inside;
            # shares website's 12-min probe cache with the models page).
            check_upstream_feeds()

            # ------------------------------------------------
            # SMS WEATHER ALERTS (texts new NWS warnings/alerts)
            # ------------------------------------------------
            if time.time() - last_sms >= 120:
                try:
                    from data.sms_alerts import check_once
                    res = check_once()
                    if res:
                        log(f"SMS alerts: {res}")
                except Exception as sexc:   # noqa: BLE001 - never break the cycle
                    log(f"SMS alert check error: {sexc}")
                last_sms = time.time()

            # ------------------------------------------------
            # FACEBOOK AUTO-POST (season graphic, Dec 1-7 window;
            # cheap no-op outside the window - a date compare only)
            # ------------------------------------------------
            if time.time() - last_fbpost >= 21600:
                try:
                    from data.fb_autopost import check_once as fb_post_once
                    res = fb_post_once()
                    if res:
                        log(f"FB auto-post: {res}")
                except Exception as fexc:   # noqa: BLE001 - never break the cycle
                    log(f"FB auto-post error: {fexc}")
                last_fbpost = time.time()

        except Exception as exc:
            log(f"UPDATE ERROR: {exc}")

        # ----------------------------------------------------
        # FACEBOOK
        # ----------------------------------------------------
        if time.time() - last_fb >= FB_INTERVAL:
            try:
                from fb_page import regenerate

                regenerate()

                last_fb = time.time()

                log("Facebook page updated.")

            except Exception as exc:
                log(f"Facebook update error: {exc}")

        # ----------------------------------------------------
        # WAIT
        # ----------------------------------------------------
        elapsed = time.time() - started
        wait = max(1, SITE_INTERVAL - elapsed)

        log(f"Next weather update in {int(wait)} seconds.")

        time.sleep(wait)


if __name__ == "__main__":
    main()
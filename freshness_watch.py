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

Also rebuilds the tiny ops dashboard (docs/ops.html) after every check:
snapshot baked in at write time (the no-JS fallback) plus a live JS poller
that re-fetches data.json every 60 s, so an open tab updates itself between
publishes. Shipped to gh-pages by the normal publish cycle. Even when the
updater is down, freshness_guard's standalone check_once keeps baking it.

Runs two ways:
  - inside the site updater's cycle (check_once(), own 5-min throttle), and
  - standalone:  python freshness_watch.py [--force] [--json] [--ops]
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


# ------------------------------------------------------------ ops dashboard

OPS_PAGE = os.path.join("docs", "ops.html")

# Live-update script for the ops page (plain string: real braces, no f-string
# escaping). Re-renders from window.MONITOR - the baked snapshot on load,
# then a fresh data.json fetch every 60 s - so an open tab tracks the site's
# continuous republishing instead of freezing at the last bake.
_OPS_JS = """
(function () {
  var BADGE = { OK: ['ok', 'OK'], WARN: ['warn', 'WARN'], FAIL: ['fail', 'FAIL'] };
  var BAKED = window.MONITOR;   // polled data.json replaces MONITOR; keep base
  function el(id) { return document.getElementById(id); }
  function chip(lv) {
    var b = BADGE[lv] || ['warn', String(lv || '?').toUpperCase()];
    return '<span class="chip ' + b[0] + '">' + b[1] + '</span>';
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function age(sec) {
    if (!sec || sec < 0) return '-';
    var m = sec / 60;
    return m < 120 ? Math.round(m) + ' min' : (m / 60).toFixed(1) + ' h';
  }
  function epSec(ep) { return ep ? (ep > 1e12 ? ep / 1000 : ep) : 0; }
  function fmt() {
    // Polled data.json overlays the baked snapshot per key: an old-shape or
    // partial monitor block (details/epochs arrive with the next build after
    // deploy) falls back to the baked copy instead of blanking the page.
    var src = window.MONITOR || BAKED || {};
    var pm = src.monitor || {}, bm = (BAKED || {}).monitor || {};
    var s = src.lastUpdate || pm.stats || bm.stats || {};
    var now = Date.now() / 1000;
    var lv = pm.levels || bm.levels || {},
        det = pm.details || bm.details || {},
        ep = pm.epochs || bm.epochs || {};
    var chk = pm.checkedEpochS || bm.checkedEpochS || 0;
    var stale = chk && (now - chk) > 900;
    el('ovChip').innerHTML = chip(pm.overall || bm.overall) +
      (stale ? ' <span class="chip fail">STALE</span>' : '');
    el('when').textContent = chk ? age(now - chk) + ' ago' : 'no snapshot';
    var rows = [
      ['local build', lv.local_build, det.local_build, ep.local_build],
      ['public site', lv.gh_pages_age, det.gh_pages_age, ep.gh_pages_age],
      ['publish lag', lv.gh_pages_lag, det.gh_pages_lag, 0],
      ['members worker', lv.worker, det.worker, ep.worker],
      ['D1 pages', lv.d1_pages_age, det.d1_pages_age, ep.d1_pages_age],
      ['CF mirror', lv.cf_mirror, det.cf_mirror, ep.cf_mirror]
    ];
    var h = '';
    for (var i = 0; i < rows.length; i++) {
      var e = epSec(rows[i][3]);
      h += '<tr><td>' + esc(rows[i][0]) + '</td><td>' + chip(rows[i][1]) + '</td><td>' +
           esc(rows[i][2] || '-') + '</td><td class="num">' +
           (e ? age(now - e) : '-') + '</td></tr>';
    }
    el('rows').innerHTML = h;
    var lb = epSec(ep.local_build), pb = epSec(ep.gh_pages_age);
    el('cLocal').textContent = lb ? age(now - lb) : '-';
    el('cLocalSub').textContent = det.local_build || '';
    el('cPublic').textContent = pb ? age(now - pb) : '-';
    el('cPublicSub').textContent = det.gh_pages_age || '';
    var lagdet = det.gh_pages_lag || '';
    el('cLag').textContent = lagdet ?
      lagdet.replace('public site ', '').replace(' behind local build', '') : '-';
    el('cLagSub').textContent = lagdet;
    el('cCycle').textContent = s.lastDurationS != null ? s.lastDurationS + ' s' : '-';
    el('cCycleSub').textContent = 'finished ' +
      (s.finishedEpochS ? age(now - s.finishedEpochS) + ' ago' : '?') +
      (s.ok === false ? ' - FAILED' : '');
    el('cAvg').textContent = s.avgDurationS != null ? s.avgDurationS + ' s' : '-';
    el('cAvgSub').textContent = (s.cycles || 0) + ' cycles in window';
  }
  function poll(first) {
    var x = new XMLHttpRequest();
    x.open('GET', 'data.json?_=' + Date.now(), true);
    x.onload = function () {
      if (x.status !== 200) return;
      try { window.MONITOR = JSON.parse(x.responseText); } catch (e) { return; }
      fmt();
      if (!first) el('stamp').textContent = 'live - refreshed ' +
        new Date().toLocaleTimeString() + ' - re-fetches data.json every 60 s';
    };
    x.send();
  }
  fmt();
  poll(true);
  setInterval(function () { poll(false); }, 60000);
})();
"""


def _age_str(seconds):
    if not seconds or seconds < 0:
        return "-"
    m = seconds / 60.0
    return f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h"


def render_ops_dashboard(snap=None):
    """Tiny ops dashboard: docs/ops.html rebuilt from the freshness snapshot
    + update_stats.json after every check (and via --ops). The snapshot is
    baked in at write time (no-JS/first-paint fallback) while inline JS
    re-fetches data.json every 60 s and re-renders in place - the page tracks
    the site's continuous republishing, not just the last bake. The normal
    publish cycle ships it to gh-pages. Best-effort: a dashboard glitch must
    never break check_once()."""
    try:
        import html as _h
        if snap is None:
            snap = _load(STATUS_PATH, {})
        stats = _load(os.path.join(".freebuff", "update_stats.json"), {})
        checks = snap.get("checks") or {}
        now = time.time()

        def chip(level):
            cls = {"OK": "ok", "WARN": "warn", "FAIL": "fail"}.get(level, "warn")
            return f'<span class="chip {cls}">{_h.escape(str(level))}</span>'

        def card(cid, label, value, sub=""):
            s = (f'<div class="sub" id="c{cid}Sub">{_h.escape(str(sub))}</div>'
                 if sub else "")
            return (f'<div class="card"><div class="label">{_h.escape(label)}</div>'
                    f'<div class="value" id="c{cid}">{_h.escape(str(value))}</div>'
                    f'{s}</div>')

        local = checks.get("local_build") or {}
        pub = checks.get("gh_pages_age") or {}
        lag = checks.get("gh_pages_lag") or {}
        le, pe = local.get("epoch"), pub.get("epoch")
        lag_min = max((le - pe) / 60.0, 0) if (le and pe) else None
        lag_txt = (f"{lag_min:.0f} min" if lag_min is not None
                   else str(lag.get("detail", "-")).replace("public site ", "").strip())

        cards = "".join([
            card("Local", "local build",
                 _age_str(now - le) if le else local.get("detail", "-"),
                 str(local.get("detail", ""))),
            card("Public", "public site",
                 _age_str(now - pe) if pe else pub.get("detail", "-"),
                 str(pub.get("detail", ""))),
            card("Lag", "publish lag", lag_txt, str(lag.get("detail", ""))),
            card("Cycle", "last cycle",
                 f"{stats.get('lastDurationS', '-')} s"
                 + ("" if stats.get("ok", True) else " (FAILED)"),
                 (f"finished {_age_str(now - stats.get('finishedEpochS', 0))} ago"
                  if stats.get("finishedEpochS") else "no stats yet")),
            card("Avg", "updater avg", f"{stats.get('avgDurationS', '-')} s",
                 f"{stats.get('cycles', 0)} cycles in window"),
        ])

        disp = [("local build", "local_build"), ("public site", "gh_pages_age"),
                ("publish lag", "gh_pages_lag"), ("members worker", "worker"),
                ("D1 pages", "d1_pages_age"), ("CF mirror", "cf_mirror")]
        rows = []
        for label, name in disp:
            c = checks.get(name)
            if not c:
                rows.append(f'<tr><td>{label}</td><td>{chip("-")}</td>'
                            f'<td colspan="2">no data</td></tr>')
                continue
            ep = c.get("epoch")
            ep_s = (ep / 1000.0) if (ep and ep > 1e12) else ep
            age = _age_str(now - ep_s) if ep_s else "-"
            rows.append(f'<tr><td>{label}</td><td>{chip(c.get("level", "?"))}</td>'
                        f'<td>{_h.escape(str(c.get("detail", "")))}</td>'
                        f'<td class="num">{age}</td></tr>')
        rows_html = "".join(rows)

        overall = snap.get("overall") or "?"
        checked = snap.get("checkedEpochS")
        stale = bool(checked) and (now - checked) > 900
        head_chip = chip(overall) + (' <span class="chip warn">STALE</span>'
                                     if stale else "")
        when = (f'{_age_str(now - checked)} ago ({_h.escape(snap.get("checked", ""))})'
                if checked else "no snapshot yet")
        # Baked MONITOR blob: what the live JS renders on load (and what
        # no-JS visitors effectively see). Prefer data.json's enriched
        # monitor block when the snapshot carries one (--ops after a build);
        # check_once's fresh status_doc derives it from the raw results.
        mon = snap.get("monitor")
        if not isinstance(mon, dict) or not mon.get("levels"):
            mon = {"overall": overall, "checkedEpochS": int(checked or 0),
                   "levels": {k: (v or {}).get("level") for k, v in checks.items()},
                   "details": {k: (v or {}).get("detail") or "" for k, v in checks.items()},
                   "epochs": {k: (v or {}).get("epoch") for k, v in checks.items()}}
        mon["stats"] = {k: stats.get(k) for k in
                        ("lastDurationS", "ok", "avgDurationS", "cycles", "finishedEpochS")}
        ops_blob = json.dumps({"monitor": mon}, separators=(",", ":")).replace(
            "</", "<\\/")
        baked_stamp = time.strftime("%Y-%m-%d %H:%M")
        doc = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta http-equiv="refresh" content="60"/>
<meta name="robots" content="noindex"/>
<title>TNWN Ops</title>
<style>
body{{margin:0;background:#0e1117;color:#d7dde6;font:14px/1.45 -apple-system,'Segoe UI',Roboto,sans-serif;padding:18px}}
h1{{font-size:19px;margin:0 0 2px}}
h2{{font-size:13px;color:#8b96a5;margin:18px 0 6px;text-transform:uppercase;letter-spacing:.06em}}
.stamp{{color:#66707e;font-size:12px;margin:0 0 14px}}
.chip{{display:inline-block;padding:1px 9px;border-radius:10px;font-size:12px;font-weight:600}}
.ok{{background:#123c22;color:#4ade80}}
.warn{{background:#3f3208;color:#fbbf24}}
.fail{{background:#451717;color:#f87171}}
.cards{{display:flex;flex-wrap:wrap;gap:8px}}
.card{{background:#161b22;border:1px solid #232a33;border-radius:8px;padding:8px 12px;min-width:130px}}
.label{{color:#8b96a5;font-size:11px;text-transform:uppercase;letter-spacing:.05em}}
.value{{font-size:17px;font-weight:600;margin-top:2px}}
.sub{{color:#66707e;font-size:11px;margin-top:2px;max-width:210px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
table{{border-collapse:collapse;width:100%;max-width:780px}}
td,th{{text-align:left;padding:4px 10px 4px 0;border-bottom:1px solid #1c232c;vertical-align:top}}
th{{color:#8b96a5;font-size:11px;text-transform:uppercase;letter-spacing:.05em}}
.num{{text-align:right;white-space:nowrap;color:#aab4c0}}
</style></head><body>
<h1>TNWN ops <span id="ovChip">{head_chip}</span></h1>
<p class="stamp" id="stamp">checked <span id="when">{when}</span> &middot; live view - re-fetches data.json every 60 s</p>
<div class="cards">{cards}</div>
<h2>freshness checks</h2>
<table><thead><tr><th>check</th><th>level</th><th>detail</th><th class="num">anchor age</th></tr></thead>
<tbody id="rows">{rows_html}</tbody>
</table>
<p class="stamp" id="src">freshness_watch.py &rarr; docs/ops.html &middot; baked {baked_stamp} &middot; js re-renders from data.json (monitor + lastUpdate) every 60 s</p>
<script>window.MONITOR = {ops_blob};</script>
<script>{_OPS_JS}</script>
</body></html>
"""
        os.makedirs("docs", exist_ok=True)
        tmp = OPS_PAGE + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(doc)
        os.replace(tmp, OPS_PAGE)
        return True
    except Exception as exc:  # noqa: BLE001 - dashboard must never break checks
        print(f"freshness: ops dashboard write failed: {exc}", flush=True)
        return False


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
    status_doc = {"checkedEpochS": time.time(), "checked": stamp,
                  "overall": overall, "checks": results}
    _save(STATUS_PATH, status_doc)
    render_ops_dashboard(status_doc)
    return overall, results


def main(argv=None):
    argv = argv or sys.argv[1:]
    force = "--force" in argv
    as_json = "--json" in argv
    if "--ops" in argv:
        ok = render_ops_dashboard()
        print(("ops dashboard written: " + OPS_PAGE) if ok
              else "ops dashboard write FAILED (see log)")
        return 0 if ok else 1
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

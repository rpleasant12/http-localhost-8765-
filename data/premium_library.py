"""Premium member library refresher.

Keeps the members-only library (premium/ tree, gitignored) current with
the LIVE render pipelines and mirrors changed files into the Cloudflare
R2 bucket `tnwn-premium`, so member.html serves fresh AI-model maps and
CFSv2 long-range GIFs without hand-running DEPLOY_MEMBERSHIP.md §5.
Called from the site_updater cycle (throttled there); runs after
generate_site(), so the CFSv2 GIFs in static/cfsv2/ are already fresh.

Layout (matches the worker proxy + member.html catalog exactly):
  premium/index.json                    catalog served by GET /api/premium
  premium/models/<stable>.png           5 AI-model maps (per AIWP init)
  premium/longrange/cfsv2/mo_*.gif      4 CFSv2 monthly-mean GIFs

Dormant by design until R2 is enabled (the dashboard card step): with
no bucket - or any upload failure - the R2 side backs off for an hour,
logs once, and moves on, while the LOCAL premium/ tree still refreshes
(it also feeds the rclone dev stand-in on :8790 and DEPLOY_MEMBERSHIP
§5's manual fill, if ever needed).

Uploads use `wrangler r2 object put tnwn-premium/<key> --file <path>
--remote` - the same wrangler OAuth the Cloudflare Pages sync uses, no
extra secrets. File names are STABLE (the member.html catalog links
them), so puts are in-place overwrites with no bucket garbage, and only
files whose sha256 changed since the last successful upload
(.freebuff/premium-uploaded.json) are put - an unchanged cycle costs
zero R2 operations. index.json is uploaded LAST so it never references
a file that is not in the bucket yet.

Render cost: the AI maps render into premium/_cache, which is also
render_product_map's disk cache, so re-runs within an AIWP init are
free; only a new 00Z/12Z init (2x/day) pays a render, same as the walls.
Dated cache files from older inits are pruned after a successful run.
"""

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import time

NOWIN = 0x08000000 if os.name == "nt" else 0

R2_BUCKET = "tnwn-premium"
CACHE_DIR = os.path.join("premium", "_cache")
STATE_FILE = os.path.join(".freebuff", "premium-uploaded.json")
WRANGLER = os.path.join(os.path.expanduser("~"),
                        "AppData", "Roaming", "npm", "wrangler.cmd")
R2_BACKOFF_S = 3600          # after any upload failure, retry next hour
R2_PUT_TIMEOUT = 180         # per-object put

PREMIUM_FH = 24              # library lead: day-1 maps for every AI model
PREMIUM_REGION = "us"        # national view (DEFAULT_REGION is the etn wall)

# (model, product, stable filename, title, desc) - member.html catalog.
# Product/level validity per PRODUCTS_BY_MODEL: only AI-GraphCast carries
# omega (700_w) and pwat (2026-10-02 NetCDF header probe).
AI_ITEMS = [
    ("AI-GraphCast", "700_w", "graphcast_700_w.png",
     "AI-GraphCast - 700_w", "700mb omega - rising/sinking air (ascent bands)"),
    ("AI-GraphCast", "pwat", "graphcast_pwat.png",
     "AI-GraphCast - pwat", "Precipitable water - atmospheric rivers"),
    ("AI-Pangu", "500_tmp", "pangu_500_tmp.png",
     "AI-Pangu - 500_tmp", "500mb temperature anomalies - upper lows/warm ridges"),
    ("AI-Aurora", "sfc_mslp", "aurora_sfc_mslp.png",
     "AI-Aurora - sfc_mslp", "Surface analysis - fronts & pressure centers"),
    ("AI-FourCastNet", "500_vort", "fourcastnet_500_vort.png",
     "AI-FourCastNet - 500_vort", "500mb vorticity - spinning disturbances"),
]

CFSV2_ITEMS = [
    ("mo_prec.gif", "CFSv2 monthly - prec"),
    ("mo_prec_prob.gif", "CFSv2 monthly - prec_prob"),
    ("mo_t2m.gif", "CFSv2 monthly - t2m"),
    ("mo_t2m_prob.gif", "CFSv2 monthly - t2m_prob"),
]


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            st.setdefault("uploaded", {})
            st.setdefault("r2_retry_after", 0)
            return st
    except (OSError, ValueError):
        pass
    return {"uploaded": {}, "r2_retry_after": 0}


def _save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def _copy_if_changed(src, dst):
    """Copy src -> dst unless the bytes already match. True if copied."""
    if os.path.isfile(dst) and _sha256(src) == _sha256(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    return True


def _prune_cache(prefix, keep=2):
    """Keep only the newest `keep` dated renders per model/product."""
    try:
        files = [os.path.join(CACHE_DIR, n) for n in os.listdir(CACHE_DIR)
                 if n.startswith(prefix) and n.endswith(".png")]
        files.sort(key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            os.remove(old)
    except OSError:
        pass


def _refresh_ai(built):
    """Render the AI-model maps for the newest live AIWP init."""
    from data.model_maps import find_cycle, render_product_map
    out = []
    for model, product, stable, _title, _desc in AI_ITEMS:
        try:
            cycle = find_cycle(model)
            if not cycle:
                out.append(f"{stable}: no live {model} cycle")
                continue
            png, _meta = render_product_map(
                model, cycle, PREMIUM_FH, product,
                out_dir=CACHE_DIR, region=PREMIUM_REGION)
            if not png or not os.path.isfile(png):
                out.append(f"{stable}: render produced no file")
                continue
            if _copy_if_changed(png, os.path.join("premium", "models", stable)):
                built.append(f"models/{stable}")
            _prune_cache(f"{model}_{product}_")
        except Exception as exc:      # noqa: BLE001 - one model never blocks the rest
            out.append(f"{stable}: {type(exc).__name__}: {exc}")
    return out


def _refresh_cfsv2(built):
    """Copy the freshest CFSv2 monthly GIFs (site build refreshed them)."""
    out = []
    for fn, _title in CFSV2_ITEMS:
        src = os.path.join("static", "cfsv2", fn)
        if not os.path.isfile(src):
            out.append(f"cfsv2/{fn}: not built yet")
            continue
        if _copy_if_changed(src, os.path.join("premium", "longrange", "cfsv2", fn)):
            built.append(f"longrange/cfsv2/{fn}")
    return out


def _write_index(built):
    """Regenerate premium/index.json - only when its images changed."""
    idx_path = os.path.join("premium", "index.json")
    if not built and os.path.isfile(idx_path):
        return False
    from data._tz import ET
    stamp = dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    doc = {
        "generated": stamp,
        "models": [
            {"title": title, "file": stable, "desc": desc}
            for _m, _p, stable, title, desc in AI_ITEMS
        ],
        "longrange": [
            {"title": title, "file": f"cfsv2/{fn}"}
            for fn, title in CFSV2_ITEMS
        ],
    }
    os.makedirs("premium", exist_ok=True)
    tmp = idx_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, idx_path)
    return True


def _r2_put(rel):
    """Upload one premium/<rel> file to R2. Returns (ok, detail)."""
    r = subprocess.run(
        ["cmd", "/c", WRANGLER, "r2", "object", "put",
         f"{R2_BUCKET}/premium/{rel}", "--file", os.path.join("premium", rel),
         "--remote"],
        capture_output=True, text=True, timeout=R2_PUT_TIMEOUT,
        encoding="utf-8", errors="replace",
        creationflags=NOWIN)
    if r.returncode == 0:
        return True, ""
    tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    detail = (tail[-1][:200] if tail else f"rc={r.returncode}")
    return False, detail.encode("ascii", "replace").decode("ascii")


def _mirror_to_r2(state, log):
    """Upload changed files. Returns list of uploaded relpaths."""
    if not os.path.isfile(WRANGLER):
        log("wrangler not found - R2 mirror skipped")
        return []
    if time.time() < state.get("r2_retry_after", 0):
        return []

    # Only files whose bytes changed since the last SUCCESSFUL upload.
    pending = []
    for root in ("models", os.path.join("longrange", "cfsv2")):
        d = os.path.join("premium", root)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            rel = f"{root.replace(os.sep, '/')}/{name}"
            sha = _sha256(os.path.join(d, name))
            if state["uploaded"].get(rel) != sha:
                pending.append(rel)
    if os.path.isfile(os.path.join("premium", "index.json")):
        rel = "index.json"
        sha = _sha256(os.path.join("premium", "index.json"))
        if state["uploaded"].get(rel) != sha:
            pending.append(rel)      # index LAST: never reference an un-uploaded file
    if not pending:
        return []

    uploaded = []
    for rel in pending:
        try:
            ok, detail = _r2_put(rel)
        except Exception as exc:      # noqa: BLE001 - back off, retry next cycle
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            state["r2_retry_after"] = time.time() + R2_BACKOFF_S
            log(f"R2 upload failed ({rel}): {detail} - backing off 1 h "
                "(is the tnwn-premium bucket enabled? DEPLOY_MEMBERSHIP §4)")
            break
        state["uploaded"][rel] = _sha256(os.path.join("premium", rel))
        uploaded.append(rel)
    if uploaded:
        state["r2_retry_after"] = 0
    return uploaded


def refresh(log=print):
    """Refresh premium/ from live pipelines, then mirror deltas to R2.

    Returns a one-line summary for the updater log (always something).
    Never raises for expected conditions - R2 being disabled is dormancy,
    not an error.
    """
    built, notes = [], []
    try:
        notes += _refresh_ai(built)
    except Exception as exc:          # noqa: BLE001 - catalog/import failure
        notes.append(f"AI renders: {type(exc).__name__}: {exc}")
    try:
        notes += _refresh_cfsv2(built)
    except Exception as exc:          # noqa: BLE001
        notes.append(f"CFSv2 copy: {type(exc).__name__}: {exc}")
    try:
        if _write_index(built):
            built.append("index.json")
    except Exception as exc:          # noqa: BLE001
        notes.append(f"index.json: {type(exc).__name__}: {exc}")

    state = _load_state()
    try:
        uploaded = _mirror_to_r2(state, log)
        _save_state(state)
    except Exception as exc:          # noqa: BLE001 - mirror must never break the cycle
        uploaded = []
        notes.append(f"R2 mirror: {type(exc).__name__}: {exc}")

    parts = []
    if built:
        parts.append(f"{len(built)} rebuilt: " + ", ".join(built[:6]))
    else:
        parts.append("library up to date")
    if uploaded:
        parts.append(f"{len(uploaded)} uploaded to R2: " + ", ".join(uploaded[:6]))
    elif time.time() < state.get("r2_retry_after", 0):
        parts.append("R2 backoff (bucket missing or error; retrying ~1 h)")
    elif notes:
        parts.append("; ".join(n[:120] for n in notes[:3]))
    else:
        parts.append("R2 in sync")
    return "; ".join(parts)


if __name__ == "__main__":
    print(refresh())

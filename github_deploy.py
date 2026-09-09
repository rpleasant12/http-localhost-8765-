"""Package the TNWN website for GitHub Pages into ./docs/.

GitHub Pages serves a repo folder verbatim, so the site must be
self-contained: every graphic the pages and data.json reference
(../model_maps/..., /app/static/hrrr/...) is copied into docs/<dir>/
and every URL rewritten to the flat Pages form ("model_maps/...").

Modes:
  python github_deploy.py            package the current static/site build
  python github_deploy.py --pages    regenerate fresh (site_updater path),
                                     then package - used by CI workflow
Missing graphics are skipped with a warning; the pages tolerate absent
frames, so a partial build still renders.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SITE_DIR = os.path.join("static", "site")
DOCS_DIR = "docs"

# asset dirs the pages may reference; anything found is copied wholesale
ASSET_DIRS = ["model_maps", "hrrr", "nam", "mrms", "nws", "goes",
              "star", "psu_hrrr", "satellite", "meso"]
COPY_EXT = (".png", ".gif", ".jpg", ".jpeg", ".webp")

# ../hrrr/x.png  ../../hrrr/x.png  /app/static/hrrr/x.png  static/hrrr/x.png
_REF = re.compile(
    r"(?:(?:\.\./)+|/app/static/|static/)([A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*/[A-Za-z0-9_\-.]+\.(?:png|gif|jpe?g|webp))"
)

# windows-reserved device names would break the git checkout on Windows
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | \
            {f"lpt{i}" for i in range(1, 10)}


def _safe_name(relpath):
    """docs/-relative path with any reserved stem defused."""
    d, base = os.path.split(relpath)
    stem, ext = os.path.splitext(base)
    if stem.lower() in _RESERVED:
        base = "f_" + base
    return os.path.join(d, base)


def rewrite(text, copy_log):
    """Rewrite every local asset URL to Pages form; record files to copy."""
    def sub(m):
        rel = m.group(1).replace("\\", "/")
        src = os.path.join("static", rel)
        if os.path.isfile(src):
            copy_log.add(rel)
            return _safe_name(rel).replace("\\", "/")
        print(f"  ! missing (left as-is): {src}")
        return m.group(0)
    return _REF.sub(sub, text)


def _walk_json(o, copy_log):
    if isinstance(o, dict):
        return {k: _walk_json(v, copy_log) for k, v in o.items()}
    if isinstance(o, list):
        out = [_walk_json(v, copy_log) for v in o]
        # drop animation frames whose graphic no longer exists (pruned cache)
        if out and all(isinstance(v, dict) and "pngUrl" in v for v in out):
            out = [v for v in out if not re.search(r"(?:\.\./|/app/static/)", v["pngUrl"])]
        return out
    if isinstance(o, str) and ("../" in o or "/app/static/" in o or "static/" in o):
        return rewrite(o, copy_log)
    return o


def package():
    """Build docs/ from the current static/site build. Returns count."""
    if not os.path.isfile(os.path.join(SITE_DIR, "index.html")):
        print(f"No site build found in {SITE_DIR} - run the app or "
              f"'python website.py' first."); return 0
    if os.path.isdir(DOCS_DIR):
        shutil.rmtree(DOCS_DIR)
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(os.path.join(DOCS_DIR, ".nojekyll"), "w") as f:
        f.write("")

    copy_log = set()
    # pages: rewrite relative asset refs, keep nav/CDN links untouched
    for name in sorted(os.listdir(SITE_DIR)):
        if not name.endswith(".html"):
            continue
        text = open(os.path.join(SITE_DIR, name), encoding="utf-8").read()
        out = rewrite(text, copy_log)
        with open(os.path.join(DOCS_DIR, name), "w", encoding="utf-8") as f:
            f.write(out)

    # data.json: rewrite string values structurally
    data = json.load(open(os.path.join(SITE_DIR, "data.json"), encoding="utf-8"))
    data = _walk_json(data, copy_log)
    with open(os.path.join(DOCS_DIR, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)

    # the stand-alone Facebook post page lives outside static/site
    fb_src = os.path.join("static", "fb_page.html")
    if os.path.isfile(fb_src):
        out = rewrite(open(fb_src, encoding="utf-8").read(), copy_log)
        with open(os.path.join(DOCS_DIR, "fb_page.html"), "w", encoding="utf-8") as f:
            f.write(out)

    # copy every referenced graphic into docs/<dir>/
    n = 0
    for rel in sorted(copy_log):
        src = os.path.join("static", rel)
        if not os.path.isfile(src):
            continue  # source vanished between generation and packaging
        dst = os.path.join(DOCS_DIR, _safe_name(rel))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except OSError:
            continue  # writer replaced/locked the file mid-copy
        n += 1

    # meso history frames are referenced by JS URL-templates (field_yymmddhh),
    # not literally in the payload - copy them wholesale
    meso_src = os.path.join("static", "meso")
    if os.path.isdir(meso_src):
        for dirpath, _, files in os.walk(meso_src):
            for fn in files:
                if not (fn.endswith(".gif") or fn.endswith(".png")):
                    continue   # .gif = native SPC sectors, .png = East TN zoom crops
                src = os.path.join(dirpath, fn)
                rel = os.path.relpath(src, "static")
                dst = os.path.join(DOCS_DIR, _safe_name(rel))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)
                    n += 1
    return n


def warm_renderers(max_wait=420):
    """Kick off the background frame renderers and wait until each radar
    family has usable frames (CI machines start with empty caches)."""
    try:
        from data.radar_frames import (future_bundle, start_future_renderer)
        from data.mrms import mrms_bundle
        from data.nws_radar import nws_bundle
        from data.satellite_bands import band_bundle
        start_future_renderer(max_hours=48)
        deadline = time.time() + max_wait
        while time.time() < deadline:
            f = future_bundle(max_hours=48)
            m = (mrms_bundle("cref") or {}).get("frames") or []
            n = nws_bundle() or []
            sat_ready = 0
            for bk in ("wvh", "ir", "c02", "c07"):
                try:
                    b = band_bundle(bk)
                    if (b.get("ready") or 0) >= 2:
                        sat_ready += 1
                except Exception:  # noqa: BLE001
                    pass
            if (len(m) >= 3 and len(n) >= 2 and f.get("ready", 0) >= 6
                    and sat_ready >= 2):
                print(f"renderers warm: mrms={len(m)} nws={len(n)} "
                      f"future={f.get('ready')}/{f.get('total')} sat={sat_ready}/4")
                return True
            print(f"warming renderers: mrms={len(m)} nws={len(n)} "
                  f"future={f.get('ready')}/{f.get('total')} sat={sat_ready}/4")
            time.sleep(20)
        print("warm-up deadline reached; generating with what is ready")
        return False
    except Exception as exc:  # noqa: BLE001 - warm-up is best-effort
        print(f"warm-up skipped: {exc}")
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pages", action="store_true",
                    help="regenerate the site fresh before packaging (CI mode)")
    ap.add_argument("--warm-seconds", type=int, default=420,
                    help="max seconds to wait for frame renderers in --pages mode")
    args = ap.parse_args()
    if args.pages:
        warm_renderers(max_wait=args.warm_seconds)
        try:
            import website
            website._seed_model_maps()   # best-effort gallery renders
        except Exception as exc:  # noqa: BLE001
            print(f"model seeding skipped: {exc}")
        import website
        site = website.generate_site()
        if site is None:
            print("Site regeneration failed - packaging last build instead.")
        try:
            import fb_page
            p = fb_page.regenerate()
            print(f"fb page: {p}")
        except Exception as exc:  # noqa: BLE001
            print(f"fb page skipped: {exc}")

    n = package()
    files = sum(len(fs) for _, _, fs in os.walk(DOCS_DIR))
    size = sum(os.path.getsize(os.path.join(r, f))
               for r, _, fs in os.walk(DOCS_DIR) for f in fs) / 1e6
    print(f"docs/ built: {files} files, {size:.1f} MB ({n} graphics copied)")
    print("Enable GitHub Pages: Settings > Pages > Source: GitHub Actions")


if __name__ == "__main__":
    main()

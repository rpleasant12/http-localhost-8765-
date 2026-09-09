"""Standalone updater for the TNWN public site + Facebook share page.

Runs as its own DETACHED process (not inside Streamlit), so the site keeps
updating no matter what the app is doing - restarts, reruns, tab closures.
Loop: regenerate static/site/ + static/fb_page.html, then sleep. Writes a
heartbeat to .freebuff/site-updater.log; a pidfile guards double-starts.
Start with:  python site_updater.py  (or use start_site_updater.ps1)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SITE_INTERVAL = 120   # regenerate the site every 2 min (data fetch ~5 s)
FB_INTERVAL = 300     # regenerate the Facebook page every 5 min
PIDFILE = os.path.join(".freebuff", "site-updater.pid")
LOGFILE = os.path.join(".freebuff", "site-updater.log")


def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _already_running():
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            pid = int(f.read().strip())
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def main():
    if _already_running():
        print("site updater already running; exiting", flush=True)
        return
    os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
    with open(PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    import warnings
    warnings.filterwarnings("ignore")

    from website import generate_site, _seed_model_maps, _render_index
    from fb_page import regenerate

    def _future_radar_step(batch=8):
        """Advance the future-radar renderer (HRRR 0-18 h + NAM nest 19-48 h).

        The Streamlit app also runs this in a thread, but that dies with the
        app process and its exceptions are swallowed - so the updater owns it
        too: each cycle discovers the newest cycles and renders a batch,
        keeping the future loop fresh even when the app is closed.
        """
        try:
            import warnings
            warnings.filterwarnings("ignore")
            from data.radar_frames import (get_future_frames_cached,
                                           _save_descriptors,
                                           render_future_frames)
            descriptors = get_future_frames_cached(max_hours=48)
            if descriptors:
                _save_descriptors(descriptors)
                # descriptors run nearest-valid-hour first, so the HEAD is
                # where new HRRR hours appear; render_future_frames' own
                # max_hours slices the tail, so cap by slicing here instead
                render_future_frames(descriptors[:batch])
        except Exception as exc:  # noqa: BLE001
            _log(f"future radar step failed: {exc}")

    def _render_queue_step(per_cycle=3):
        """Render the next un-rendered (model, product, region) combos so the
        static site's catalog explorer fills out over time."""
        try:
            from data.model_maps import PRODUCTS_BY_MODEL, find_cycle, render_product_map
            have = {(c["model"], c["product"], c["region"]) for c in _render_index()}
            fh_pick = {"HRRR": 18, "RRFS": 12, "HREF": 12, "REFS": 12, "RAP": 6,
                       "NAM": 24, "GFS": 24, "NBM": 24, "CFS": 48}
            done = 0
            for model, prods in PRODUCTS_BY_MODEL.items():
                for prod in prods:
                    for region in ("etn", "us"):
                        if (model, prod, region) in have:
                            continue
                        try:
                            cyc = find_cycle(model)
                            if cyc is None:
                                continue
                            render_product_map(model, cyc, fh_pick.get(model, 24), prod, region=region)
                            _log(f"rendered {model} {prod} {region}")
                            done += 1
                        except Exception as exc:  # noqa: BLE001
                            _log(f"render {model}/{prod}/{region} failed: {exc}")
                            done += 1  # do not retry-spin the same combo forever
                        if done >= per_cycle:
                            return
        except Exception as exc:  # noqa: BLE001
            _log(f"render queue failed: {exc}")

    def _satellite_step(bands=("ir", "wvh", "wvm", "wvl", "c02", "c01")):
        """Advance the GOES band renderers (one render pass per band).

        Like future radar: the app's daemon threads die with the app and
        swallow errors, so the updater drives a synchronous pass each cycle
        to keep every satellite band's frame loop fresh.
        """
        try:
            from data.satellite_bands import get_band_frames, _render_band_frame
            import data.satellite_bands as sb
            for k in bands:
                try:
                    frames = get_band_frames(k)
                    if not frames:
                        continue
                    sb._merge_descriptors(k, frames)
                    with sb._REG_LOCK:
                        reg = sb._prune_registry(sb._load_registry())
                        sb._save_registry(reg)
                    pending = [fr for fr in frames if reg.get(fr["id"], {}).get("status") != "done"]
                    for fr in pending[:2]:   # newest first-ish; 2 per band per cycle
                        sb._render_band_frame(k, fr)
                except Exception as exc:  # noqa: BLE001
                    _log(f"satellite {k} failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            _log(f"satellite step failed: {exc}")

    _log(f"site updater started (pid {os.getpid()}); site every {SITE_INTERVAL}s, fb every {FB_INTERVAL}s")
    last_fb = 0.0
    seeded = False
    fails = 0
    while True:
        t0 = time.time()
        try:
            out = generate_site()
            if out:
                fails = 0
                _log(f"site regenerated in {time.time() - t0:.1f}s")
                if not seeded:
                    seeded = True
                    _log("seeding model map renders (first cycle)")
                    _seed_model_maps()   # fills the models-page gallery
                    generate_site()
                _render_queue_step()  # progressively render the full catalog
                _future_radar_step()  # keep future radar (HRRR+NAM) fresh
                _satellite_step()     # keep GOES bands (IR/WV/visible) fresh
                if os.path.isdir("docs"):   # keep the Pages package current
                    try:
                        import github_deploy
                        github_deploy.package()
                        _log("docs/ repackaged")
                        try:
                            import publish_site
                            if publish_site.publish():
                                _log("gh-pages published")
                        except Exception as exc:  # noqa: BLE001
                            _log(f"gh-pages publish failed: {exc}")
                    except Exception as exc:  # noqa: BLE001
                        _log(f"docs repackage failed: {exc}")
            else:
                raise RuntimeError("generate_site returned None")
        except Exception as exc:  # noqa: BLE001 - updater must never exit
            fails += 1
            _log(f"site generation FAILED ({fails} in a row): {exc}")
        if time.time() - last_fb >= FB_INTERVAL:
            try:
                regenerate()
                last_fb = time.time()
                _log("facebook page regenerated")
            except Exception as exc:  # noqa: BLE001
                _log(f"fb page FAILED: {exc}")
        time.sleep(SITE_INTERVAL)


if __name__ == "__main__":
    main()

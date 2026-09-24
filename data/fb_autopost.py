"""Facebook Page auto-posting for the TNWN storm archive.

check_once() runs inside the site updater's cycle (throttled to a few
times a day by the caller) and publishes the season-in-review graphic to
the Tennessee Weather Network Page on December 1st each year.

Auth: a Page access token with pages_manage_posts + pages_read_engagement.
Credentials follow the project's token-file convention - the FIRST line of
gmail_alert_token.txt may be reused, but the dedicated file is
fb_autopost_token.txt in the project root (gitignored), one item per line:
    line 1: Page ID (numeric, or the page's vanity handle)
    line 2: Page access token
Optional env fallback: FB_PAGE_ID / FB_PAGE_TOKEN.

State: .freebuff/fb_autopost_state.json records the last posted year, so
the post happens exactly once per season even though the check runs many
times during the posting window. The window stays open through Dec 7 so a
multi-day machine outage still posts (late, but never skipped and never
duplicated). Never raises.
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOKEN_FILE = os.path.join(ROOT, "fb_autopost_token.txt")
STATE_PATH = os.path.join(ROOT, ".freebuff", "fb_autopost_state.json")
LOG_PATH = os.path.join(ROOT, ".freebuff", "fb-autopost.log")

POST_MONTH, POST_DAY = 12, 1       # start of the posting window
WINDOW_DAYS = 7                    # keep trying through Dec 7
GRAPH = "https://graph.facebook.com/v19.0"


def _cfg():
    cfg = {"page_id": os.environ.get("FB_PAGE_ID", ""),
           "token": os.environ.get("FB_PAGE_TOKEN", "")}
    try:
        if os.path.isfile(TOKEN_FILE):
            lines = [ln.strip() for ln in open(TOKEN_FILE, encoding="utf-8")
                     if ln.strip() and not ln.startswith("#")]
            if len(lines) >= 1:
                cfg["page_id"] = lines[0]
            if len(lines) >= 2:
                cfg["token"] = lines[1]
    except OSError:
        pass
    return cfg


def _enabled():
    c = _cfg()
    return bool(c["page_id"] and c["token"])


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"posted_year": None, "posted_at": None, "post_id": None}


def _save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_PATH)


def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return line


def _in_window(now=None):
    """True Dec 1 through Dec 7 (UTC - the archive runs on UTC)."""
    now = time.gmtime() if now is None else (
        now if hasattr(now, "tm_mon") else time.gmtime(now))
    return (now.tm_mon == POST_MONTH
            and POST_DAY <= now.tm_mday <= POST_DAY + WINDOW_DAYS - 1)


def _post_photo(cfg, png_path, message):
    """Multipart photo upload to the Page. Returns (ok, post_id_or_error)."""
    import requests
    try:
        with open(png_path, "rb") as fh:
            r = requests.post(
                f"{GRAPH}/{cfg['page_id']}/photos",
                data={"access_token": cfg["token"], "caption": message},
                files={"source": (os.path.basename(png_path), fh,
                                  "image/png")},
                timeout=60)
    except requests.RequestException as exc:
        return False, f"network: {exc}"
    if r.status_code == 200:
        return True, (r.json() or {}).get("post_id") or "ok"
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def check_once(force_test=False):
    """One scheduler cycle. Returns a short status string for the log."""
    cfg = _cfg()
    if not _enabled():
        return ("disabled: create fb_autopost_token.txt (Page ID + Page "
                "access token) or set FB_PAGE_ID / FB_PAGE_TOKEN")
    st = _load_state()
    year = time.gmtime().tm_year
    if not force_test:
        if st.get("posted_year") == year:
            return f"already posted for {year}"
        if not _in_window():
            return "not in posting window (Dec 1-7)"
    png = os.path.join(ROOT, "static", "share", f"season_{year}.png")
    if not os.path.isfile(png):
        # graphic not rendered yet - build it from the live archive
        try:
            import storm_detail
            png = storm_detail.season_summary_png(
                {"stormArchive": _archive_now(), "generated": ""}) or ""
        except Exception:  # noqa: BLE001
            png = ""
        if not png or not os.path.isfile(png):
            return f"season graphic missing for {year}"
    storm_names = _storm_names(year)
    message = (
        f"\U0001f32f {year} hurricane season in review\n\n"
        f"Every storm the Tennessee Weather Network archive tracked this "
        f"season{(' - ' + storm_names) if storm_names else ''} - tracks and "
        "peak intensity from official NHC advisories.\n"
        "Full storm history + every advisory cone:\n"
        "https://rpleasant12.github.io/http-localhost-8765-/storms.html\n"
        "#tnwx #hurricaneseason #weather")
    ok, result = _post_photo(cfg, png, message)
    if not ok:
        _log(f"POST FAILED ({year}): {result}")
        return f"post failed: {result[:80]}"
    st.update({"posted_year": year, "posted_at": time.strftime(
        "%Y-%m-%d %H:%M:%S"), "post_id": result})
    _save_state(st)
    _log(f"posted {year} graphic (post_id={result})")
    return f"posted {year} season graphic"


def _archive_now():
    """Best-effort live archive for an on-demand render inside the cycle."""
    try:
        import website
        return website._storm_archive_list()
    except Exception:  # noqa: BLE001
        return []


def _storm_names(year):
    """Comma-separated archived-storm names for the caption."""
    try:
        import website
        names = [b["name"] for b in (website._storm_archive_list() or [])
                 if (b.get("advisories")
                     and b["advisories"][0].get("stamp", "").startswith(
                         str(year)[:4]))]
        return ", ".join(names[:8])
    except Exception:  # noqa: BLE001
        return ""


if __name__ == "__main__":
    import sys
    print(check_once(force_test="--test" in sys.argv))

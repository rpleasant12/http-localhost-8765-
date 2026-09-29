"""NWS Area Forecast Discussion (AFD) - the forecaster's own reasoning.

Every NWS office issues an AFD several times a day: plain-language notes on
WHY the forecast is what it is (pattern drivers, model agreement, uncertainty).
Served via the keyless api.weather.gov products API (verified live
2026-09-20). The bundle keeps the KEY MESSAGES section separate - it is the
skim-readable part - plus every other section for the full read.

The site serves East Tennessee, so the home office is Morristown TN (MRX).
"""
import datetime as dt
import json
import os
import re

import requests

OFFICE = "MRX"          # NWS Morristown TN (East Tennessee)
OFFICE_NAME = "NWS Morristown TN"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_CACHE = os.path.join(".freebuff", "afd-cache.json")
_CACHE_TTL = 600        # 10 min - the AFD changes a few times a day


def _parse_sections(text):
    """Split the raw AFD into [{title, text}] on `.TITLE...` headers."""
    sections = []
    cur_title, cur = "Overview", []
    for line in text.splitlines():
        m = re.match(r"^\.([A-Z0-9][A-Z0-9 /&().,'-]*)\.\.\.", line)
        if m:
            if cur_title and cur:
                sections.append({"title": cur_title, "text": "\n".join(cur).strip()})
            cur_title, cur = m.group(1).strip(), []
        elif cur_title:
            cur.append(line)
    if cur_title and cur:
        sections.append({"title": cur_title, "text": "\n".join(cur).strip()})
    return sections


def afd_bundle():
    """{ok, issued, issuedEpoch, office, keyMessages: [..], sections: [{title, text}]}"""
    now = time.time() if (time := __import__("time")) else 0
    # small disk cache - 2-minute builds should not hammer api.weather.gov
    try:
        st = os.stat(_CACHE)
        if now - st.st_mtime < _CACHE_TTL:
            with open(_CACHE, encoding="utf-8") as f:
                b = json.load(f)
            if b.get("ok"):
                return b
    except (OSError, ValueError):
        pass

    out = {"ok": False, "office": OFFICE, "officeName": OFFICE_NAME,
           "issued": "", "issuedEpoch": 0, "keyMessages": [], "sections": []}
    try:
        s = requests.Session()
        s.headers.update(UA)
        lst = s.get(
            f"https://api.weather.gov/products/types/AFD/locations/{OFFICE}",
            timeout=25).json()
        graph = lst.get("@graph") or []
        if not graph:
            raise ValueError("no AFD products listed")
        newest = graph[0]                      # list is newest-first
        prod = s.get(newest["@id"], timeout=25).json()
        text = prod.get("productText") or ""
        if not text:
            raise ValueError("empty product text")

        mIss = re.search(r"(\d{1,2}:?\d{2}) (AM|PM) (EDT|EST) \w+ (\w+ \d{1,2},? \d{4})",
                         text)
        issued = f"{mIss.group(1)} {mIss.group(2)} {mIss.group(3)}, {mIss.group(4)}" if mIss else ""
        try:
            issued_epoch = dt.datetime.strptime(
                newest.get("issuanceTime", ""), "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except ValueError:
            issued_epoch = 0

        sections = _parse_sections(text)
        key = []
        for sec in sections:
            if sec["title"].upper().startswith("KEY MESSAGES"):
                # Bullets wrap across lines - keep each bullet's continuation
                # lines joined until the next bullet or blank line.
                cur = None
                for ln in sec["text"].splitlines():
                    if ln.strip().startswith("-"):
                        if cur:
                            key.append(cur)
                        cur = ln.lstrip("- ").strip()
                    elif cur is not None and ln.strip():
                        cur = f"{cur} {ln.strip()}"
                if cur:
                    key.append(cur)
                break
        # clip very long sections for the payload (full text stays on NWS)
        for sec in sections:
            if len(sec["text"]) > 2400:
                sec["text"] = sec["text"][:2400] + "\n[... continued on weather.gov]"
        out = {"ok": True, "office": OFFICE, "officeName": OFFICE_NAME,
               "issued": issued, "issuedEpoch": int(issued_epoch),
               "keyMessages": key[:8], "sections": sections[:12]}

        os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
        with open(_CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f)
    except Exception:                          # noqa: BLE001 - never break the build
        return out
    return out


if __name__ == "__main__":
    b = afd_bundle()
    print("ok:", b["ok"], "| issued:", b["issued"] or "-")
    print("key messages:", len(b["keyMessages"]))
    for k in b["keyMessages"][:4]:
        print(" -", k[:90])
    print("sections:", [s["title"] for s in b["sections"]])

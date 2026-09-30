"""Winter storm desk: school-closing signals + power-outage directory.

No site ships a machine-readable "closings" feed (TV-station pages render
client-side behind bot walls; utilities sit behind Cloudflare/TLS walls).
What IS machine-readable and predictive: NWS alerts. A Winter Storm Warning
in a county means closings are near-certain; an Ice Storm Warning means the
same plus power-line failures; High Wind means outages without cold. So:

  1. CLOSURE SIGNALS - api.weather.gov/alerts/active?area=TN, filtered to
     closure-relevant events, mapped to counties, rolled into a per-county
     worst-status ("closings likely" / "delays possible" / "outage risk" /
     "cold watch").
  2. DISTRICT + UTILITY DIRECTORY - curated, static, honest: every East
     Tennessee district and the electric utilities serving the region, each
     linking to a live search (never a guessed deep-URL that 404s the week
     after launch) plus verified site links where known-good.

Everything fails safe: a dead NWS fetch degrades to "signals unavailable",
never a broken page.
"""
import datetime
import re
import urllib.request

UA = {"User-Agent": "tnwx-site/1.0 (weather site; contact via site feedback)"}

# The 34 East Tennessee counties (East Tennessee Historical Society region).
EAST_TN = [
    "Anderson", "Bledsoe", "Blount", "Bradley", "Campbell", "Carter",
    "Claiborne", "Cocke", "Cumberland", "Fentress", "Grainger", "Greene",
    "Hamblen", "Hamilton", "Hancock", "Hawkins", "Jefferson", "Knox",
    "Loudon", "Marion", "McMinn", "Meigs", "Monroe", "Morgan", "Polk",
    "Rhea", "Roane", "Scott", "Sequatchie", "Sevier", "Sullivan", "Unicoi",
    "Union", "Washington",
]

# NWS event -> (status key, label, color). Ordered worst-first; a county
# takes the status of its most severe active event.
EVENT_STATUS = [
    ("Ice Storm Warning", "outages", "Ice storm - outages likely", "#ff1744"),
    ("Winter Storm Warning", "closing", "Closings likely", "#ff5252"),
    ("Blizzard Warning", "closing", "Closings likely", "#ff5252"),
    ("Winter Storm Watch", "closing", "Closings possible", "#ffd54f"),
    ("Winter Weather Advisory", "delay", "Delays possible", "#ff9f43"),
    ("Freezing Rain Advisory", "delay", "Delays possible", "#ff9f43"),
    ("Wind Chill Warning", "cold", "Cold - bus/fuel watch", "#4da3ff"),
    ("Extreme Cold Warning", "cold", "Cold - bus/fuel watch", "#4da3ff"),
    ("Wind Chill Advisory", "delay", "Cold delay watch", "#81c784"),
    ("Extreme Cold Watch", "delay", "Cold delay watch", "#81c784"),
    ("High Wind Warning", "outages", "Wind - outage risk", "#ff9f43"),
    ("Wind Advisory", "outages", "Gusty - minor outage risk", "#81c784"),
    ("Flood Warning", "delay", "Road flooding - delays", "#ff9f43"),
    ("Flash Flood Warning", "delay", "Road flooding - delays", "#ff9f43"),
]
_EVENT_RANK = {e: i for i, (e, _s, _l, _c) in enumerate(EVENT_STATUS)}

# Principal public school district per county (name, site if verified this
# build season; no guessed deep links - the search link does the rest).
# Extra city districts for the metros follow the county list.
DISTRICTS = [
    ("Anderson", "Anderson County Schools", None),
    ("Anderson", "Oak Ridge Schools", "https://www.ortn.edu/"),
    ("Bledsoe", "Bledsoe County Schools", None),
    ("Blount", "Blount County Schools", None),
    ("Blount", "Maryville City Schools", "https://www.maryville-schools.org/"),
    ("Blount", "Alcoa City Schools", "https://www.alcoaschools.net/"),
    ("Bradley", "Bradley County Schools", "https://www.bradleyschools.net/"),
    ("Bradley", "Cleveland City Schools", "https://www.clevelandschools.org/"),
    ("Campbell", "Campbell County Schools", None),
    ("Carter", "Carter County Schools", None),
    ("Carter", "Elizabethton City Schools", "https://www.ecschools.net/"),
    ("Claiborne", "Claiborne County Schools", None),
    ("Cocke", "Cocke County Schools", None),
    ("Cocke", "Newport Grammar (city)", None),
    ("Cumberland", "Cumberland County Schools", "https://ccschools.k12tn.net/"),
    ("Fentress", "Fentress County Schools", None),
    ("Grainger", "Grainger County Schools", None),
    ("Greene", "Greene County Schools", None),
    ("Greene", "Greeneville City Schools", "https://www.gcschools.net/"),
    ("Hamblen", "Hamblen County Schools", None),
    ("Hamblen", "Morristown-Hamblen (city)", None),
    ("Hamilton", "Hamilton County Schools", "https://www.hcde.org/"),
    ("Hancock", "Hancock County Schools", None),
    ("Hawkins", "Hawkins County Schools", None),
    ("Hawkins", "Kingsport City Schools", "https://www.k12k.com/"),
    ("Jefferson", "Jefferson County Schools", "https://www.jc-schools.net/"),
    ("Knox", "Knox County Schools", "https://www.knoxschools.org/"),
    ("Loudon", "Loudon County Schools", None),
    ("Loudon", "Lenoir City Schools", None),
    ("Marion", "Marion County Schools", None),
    ("McMinn", "McMinn County Schools", None),
    ("McMinn", "Athens City Schools", None),
    ("Meigs", "Meigs County Schools", None),
    ("Monroe", "Monroe County Schools", None),
    ("Morgan", "Morgan County Schools", None),
    ("Polk", "Polk County Schools", None),
    ("Rhea", "Rhea County Schools", None),
    ("Roane", "Roane County Schools", None),
    ("Scott", "Scott County Schools", None),
    ("Sequatchie", "Sequatchie County Schools", None),
    ("Sevier", "Sevier County Schools", "https://www.sevier.org/"),
    ("Sullivan", "Sullivan County Schools", None),
    ("Sullivan", "Bristol TN City Schools", None),
    ("Unicoi", "Unicoi County Schools", None),
    ("Union", "Union County Schools", None),
    ("Washington", "Washington County Schools", None),
    ("Washington", "Johnson City Schools", "https://www.jcschools.org/"),
]

# Electric utilities serving East Tennessee, with the counties where they
# are the primary supplier. "q" is the exact search that finds their live
# outage map / report line - deliberately a search, not a guessed URL.
UTILITIES = [
    ("KUB - Knoxville Utilities Board", "Knox and parts of Anderson, Blount, Sevier, Union",
     "https://www.kub.org/", "KUB Knoxville outage map"),
    ("EPB Chattanooga", "Hamilton (Chattanooga)",
     "https://epbnet.us/", "EPB Chattanooga power outage"),
    ("Sevier County Electric System", "Sevier",
     None, "Sevier County Electric System outage"),
    ("Greeneville Light & Power", "Greene",
     None, "Greeneville Light and Power outage"),
    ("Johnson City Power Board (JCPB)", "Washington, Unicoi, Carter, Greene (parts)",
     "https://www.jcpb.com/", "JCPB outage map"),
    ("Bristol Tennessee Essential Services", "Sullivan (Bristol)",
     "https://www.btes.net/", "BTES outage"),
    ("Appalachian Power (AEP)", "Sullivan, Hawkins, Washington (parts)",
     "https://outagemap.appalachianpower.com/", "Appalachian Power Tennessee outage map"),
    ("Fort Loudoun Electric Co-op", "Loudon, Monroe, Blount, McMinn (parts)",
     None, "Fort Loudoun Electric Cooperative outage"),
    ("Volunteer Energy Cooperative", "Rhea, Meigs, Hamilton (north), McMinn, Bradley (parts)",
     "https://www.vec.org/", "Volunteer Energy Cooperative outage"),
    ("Sequachee Valley Electric Co-op", "Marion, Sequatchie, Bledsoe",
     None, "Sequachee Valley Electric outage"),
    ("Plateau Electric Co-op", "Cumberland (plateau), Morgan, Scott (parts)",
     None, "Plateau Electric Cooperative outage"),
    ("Athens Utilities Board", "McMinn (Athens)",
     None, "Athens Utilities Board Tennessee outage"),
    ("Newport Utilities", "Cocke (Newport)",
     None, "Newport Utilities Tennessee outage"),
    ("TVA", "Regional grid coordination",
     "https://www.tva.com/", "TVA power system status"),
]

_ALERTS_URL = "https://api.weather.gov/alerts/active?area=TN"


def _fetch_alerts(timeout=10):
    req = urllib.request.Request(_ALERTS_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json_loads(r.read())


def json_loads(b):
    import json
    return json.loads(b.decode("utf-8", "replace"))


def _counties_from_area(area):
    """'Scott County, TN; Campbell County, TN' -> ['Scott', 'Campbell']."""
    out = []
    for part in re.split(r";", area or ""):
        m = re.match(r"\s*([A-Za-z ]+?)\s+County,\s*TN\.?\s*$", part.strip())
        if m and m.group(1).strip() in EAST_TN:
            out.append(m.group(1).strip())
    return out


def _short_event(ev):
    return (ev.replace(" Warning", "").replace(" Advisory", "")
              .replace(" Watch", "").replace(" Statement", ""))


def collect_closings():
    """Build the winter-desk bundle. Never raises."""
    now = datetime.datetime.now().astimezone()
    bundle = {
        "ok": True,
        "updated": now.strftime("%a %b %d, %I:%M %p ET"),
        "counties": {},       # county -> {status,label,color,event,until}
        "signals": [],        # flat list for the chips row
        "districts": DISTRICTS,
        "utilities": UTILITIES,
        "note": ("Signals come from active National Weather Service alerts "
                 "(the same trigger superintendents watch); final calls are "
                 "made by each district, usually 5:30-7:00 am."),
    }
    try:
        data = _fetch_alerts()
        feats = data.get("features") or []
    except Exception:                                    # noqa: BLE001
        bundle["ok"] = False
        bundle["note"] = ("NWS alert feed unavailable this cycle - check "
                          "district pages directly; final calls are made "
                          "there regardless.")
        return bundle

    per_county = {}
    for f in feats:
        props = (f or {}).get("properties") or {}
        ev = props.get("event") or ""
        rank = _EVENT_RANK.get(ev)
        if rank is None:
            continue
        counties = _counties_from_area(props.get("areaDesc") or "")
        sig = {
            "event": ev, "short": _short_event(ev), "rank": rank,
            "label": EVENT_STATUS[rank][2], "color": EVENT_STATUS[rank][3],
            "status": EVENT_STATUS[rank][1],
            "until": (props.get("ends") or props.get("expires") or ""),
            "headline": (props.get("headline") or "")[:140],
            "counties": counties,
        }
        bundle["signals"].append(sig)
        for c in counties:
            cur = per_county.get(c)
            if cur is None or rank < cur["rank"]:
                per_county[c] = sig

    bundle["counties"] = {
        c: {"status": s["status"], "label": s["label"], "color": s["color"],
            "event": s["event"], "until": s["until"]}
        for c, s in sorted(per_county.items(), key=lambda kv: kv[1]["rank"])
    }
    return bundle


if __name__ == "__main__":
    import json
    print(json.dumps(collect_closings(), indent=1)[:1500])

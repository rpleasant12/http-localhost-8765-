"""Standalone Tennessee Weather Network share page (for Facebook).

Generates static/fb_page.html from live NWS + RainViewer data: current
conditions, 3-day forecast, active alerts, SPC threat banner, and an
embedded animated radar map (Leaflet + public RainViewer tiles - no keys,
works on desktop and mobile). The app regenerates it every few minutes so
the posted link always serves fresh data; `python fb_page.py` rebuilds it
on demand.
"""
import datetime as dt
import html
import json
import os
import re
import threading
import time

import requests

import config

OUT_PATH = os.path.join("static", "fb_page.html")
UA = {"User-Agent": "tennessee-weather-network/1.0"}
RAINVIEWER_API = "https://api.rainviewer.com/public/weather-maps.json"


# ---------------------------------------------------------------- data
def _f(celsius):
    return None if celsius is None else celsius * 9 / 5 + 32


def _mph(kmh):
    return None if kmh is None else kmh * 0.621371


def _icon(short_forecast, daytime=True):
    f = (short_forecast or "").lower()
    if "thunder" in f:
        return "⛈️"
    if "snow" in f or "flurr" in f:
        return "❄️"
    if "rain" in f or "shower" in f or "drizzle" in f:
        return "🌧️"
    if "fog" in f:
        return "🌫️"
    if "sunny" in f or "clear" in f:
        return "☀️" if daytime else "🌙"
    if "cloud" in f or "cloudy" in f:
        return "☁️"
    return "🌤️" if daytime else "☁️"


def spc_day1_at_home():
    """SPC Day-1 outlook category at the home point (or None)."""
    try:
        from data.severe import spc_outlooks, spc_risk_at
        outlooks = spc_outlooks()
        risk = spc_risk_at(config.LATITUDE, config.LONGITUDE, outlooks)
        cat = (risk or {}).get("cat") or {}
        return {
            "label": cat.get("label") or cat.get("label2") or "NONE",
            "fill": cat.get("fill") or "#c1e9c1",
        }
    except Exception:  # noqa: BLE001 - page must render even if SPC is down
        return None


def collect_weather():
    """Everything the page shows, from the same keyless feeds as the app."""
    from data.nws import get_active_alerts, get_current_conditions, get_forecast

    lat, lon = config.LATITUDE, config.LONGITUDE
    cur = get_current_conditions(lat, lon) or {}
    forecast = get_forecast(lat, lon) or []
    alerts = get_active_alerts(lat, lon) or []

    temp_c = (cur.get("temperature") or {}).get("value")
    dew_c = (cur.get("dewpoint") or {}).get("value")
    current = {
        "text": cur.get("textDescription") or "",
        "tempF": round(_f(temp_c)) if temp_c is not None else None,
        "dewF": round(_f(dew_c)) if dew_c is not None else None,
        "rh": round((cur.get("relativeHumidity") or {}).get("value") or 0),
        "windDir": cur.get("windDirection", {}).get("value") if cur.get("windDirection") else None,
        "windMph": (round(_mph(cur["windSpeed"].get("value")))
                    if cur.get("windSpeed") and cur["windSpeed"].get("value") is not None else None),
        "time": (cur.get("timestamp") or "")[:16].replace("T", " ") + "Z",
    }

    days = []
    seen = set()
    for period in forecast:
        name = period.get("name", "")
        if name in seen or "Night" in name:
            continue
        seen.add(name)
        days.append({
            "name": name,
            "hi": period.get("temperature"),
            "icon": _icon(period.get("shortForecast"), True),
            "text": period.get("shortForecast") or "",
            "wind": period.get("windSpeed") or "",
            "pop": (period.get("probabilityOfPrecipitation") or {}).get("value") or 0,
        })
        if len(days) == 3:
            break

    return {
        "place": config.DEFAULT_LOCATION_NAME,
        "pageName": config.PAGE_NAME,
        "pageUrl": config.PAGE_URL,
        "lat": lat,
        "lon": lon,
        "current": current,
        "days": days,
        "alerts": [{
            "event": a.get("event") or "Alert",
            "severity": a.get("severity") or "Unknown",
            "areaDesc": (a.get("areaDesc") or "")[:120],
            "expires": (a.get("expires") or "")[:16].replace("T", " ") + "Z" if a.get("expires") else "",
        } for a in alerts],
        "spc": spc_day1_at_home(),
        "generated": dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
    }


# ------------------------------------------------------------- model maps
_MS_RX = re.compile(r"(mpas|shield)_(\w+)_(\w+)_f(\d+)_(\d{10})\.jpg$")
_MAP_RX = re.compile(r"([A-Za-z0-9\-]+)_(\w+)_f(\d+)_(\d{10})_(\w+)\.png$")


def _model_data():
    """Everything the share page's model explorer needs (all local, fast).

    catalog: model -> [{key, label}] product lists (same source as the
             models page, incl. MPAS + FV3/SHiELD).
    rend:    every pre-rendered (model, product, region) combo from
             static/model_maps - latest cycle, newest 8 frames.
    ms:      MPAS/SHiELD official frame loops already on disk.
    """
    out = {"catalog": {}, "rend": [], "ms": {"mpas": {}, "shield": {}}}
    try:
        from data.model_maps import PRODUCTS_BY_MODEL, PRODUCTS
        for model, prods in PRODUCTS_BY_MODEL.items():
            out["catalog"][model] = [
                {"key": p, "label": PRODUCTS.get(p, {}).get("label", p)} for p in prods]
    except Exception:  # noqa: BLE001 - catalog must not break the page
        pass
    try:
        from data.shield_mpas import MPAS_PRODUCTS, SHIELD_PRODUCTS
        out["catalog"]["MPAS"] = [
            {"key": k, "label": v["label"]} for k, v in MPAS_PRODUCTS.items()]
        out["catalog"]["FV3 (SHiELD)"] = [
            {"key": k, "label": v["label"]} for k, v in SHIELD_PRODUCTS.items()]
    except Exception:  # noqa: BLE001
        pass

    combos = {}
    try:
        names = os.listdir(os.path.join("static", "model_maps"))
    except OSError:
        names = []
    for fn in names:
        m = _MAP_RX.match(fn)
        if not m:
            continue
        model, prod, fh, cyc, region = m.groups()
        combos.setdefault((model, prod, region), []).append((cyc, int(fh), fn))
    for (model, prod, region), items in combos.items():
        newest = max(c for c, _f, _n in items)
        frames = sorted((f, fn) for c, f, fn in items if c == newest)[-8:]
        out["rend"].append({
            "model": model, "product": prod, "region": region, "cycle": newest,
            "frames": [{"fh": fh, "url": f"../model_maps/{fn}"} for fh, fn in frames],
        })

    try:
        ainames = os.listdir(os.path.join("static", "aimodels"))
    except OSError:
        ainames = []
    groups = {}
    for fn in ainames:
        m = _MS_RX.match(fn)
        if not m:
            continue
        kind, key, _dom, fh, init = m.groups()
        groups.setdefault((kind, key, init), []).append((int(fh), fn))
    for (kind, key, init), items in groups.items():
        frames = sorted(items)[:12]
        bucket = out["ms"][kind].setdefault(key, {"init": init, "frames": []})
        if len(frames) > len(bucket["frames"]):
            bucket["init"] = init
            bucket["frames"] = [
                {"fh": fh, "url": f"../aimodels/{fn}"} for fh, fn in frames]
    return out


# ---------------------------------------------------------------- html
def _alert_color(sev, event):
    e = (event or "").lower()
    if "tornado warning" in e:
        return "#ff1744"
    if "severe thunderstorm warning" in e or "flash flood warning" in e:
        return "#ff5252"
    if "warning" in e:
        return "#ff9f43"
    if "watch" in e:
        return "#ffd54f"
    if "advisory" in e:
        return "#ffe0b2"
    return "#c1e9c1" if (sev or "").lower() != "severe" else "#ff5252"


def render_html(d):
    cur = d["current"]
    alerts = d["alerts"]
    spc = d.get("spc")
    og_desc = "Live radar, forecast and alerts for East Tennessee"
    if cur["tempF"] is not None:
        og_desc = f"{cur['tempF']}°F - {cur['text']} - {og_desc}"
    if alerts:
        og_desc = f"{len(alerts)} active alert(s) - " + og_desc

    alert_html = ""
    if alerts:
        rows = "".join(
            f'<div class="alert" style="border-left:6px solid {_alert_color(a["severity"], a["event"])}">'
            f'<b>{html.escape(a["event"])}</b><span>{html.escape(a["areaDesc"])}</span>'
            f'<i>until {a["expires"] or "further notice"}</i></div>'
            for a in alerts[:8]
        )
        alert_html = f'<h2>⚠️ Active alerts ({len(alerts)})</h2><div class="alerts">{rows}</div>'
    else:
        alert_html = '<h2>⚠️ Active alerts</h2><div class="alert ok">No active alerts for this area.</div>'

    spc_html = ""
    if spc:
        spc_html = (f'<div class="spc" style="background:{spc["fill"]}">'
                    f'<span>SPC Day 1 risk</span><b>{html.escape(spc["label"])}</b></div>')

    days_html = "".join(
        f'<div class="day"><div class="dname">{html.escape(day["name"])}</div>'
        f'<div class="dicon">{day["icon"]}</div><div class="dhi">{day["hi"]}°F</div>'
        f'<div class="dtext">{html.escape(day["text"])}</div>'
        f'<div class="dmeta">💨 {html.escape(day["wind"])} · 💧 {day["pop"]}%</div></div>'
        for day in d["days"]
    )

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta property="og:title" content="TNWX_PAGE_NAME - Live Radar & Forecast"/>
<meta property="og:description" content="OG_DESC"/>
<meta property="og:type" content="website"/>
<meta property="og:image" content="OG_SITE/og.png?v=OG_STAMP"/>
<meta property="og:image:width" content="1200"/>
<meta property="og:image:height" content="630"/>
<meta property="og:url" content="OG_SITE/"/>
<meta name="twitter:card" content="summary_large_image"/>
<meta name="twitter:image" content="OG_SITE/og.png?v=OG_STAMP"/>
<meta name="description" content="OG_DESC"/>
<title>TNWX_PAGE_NAME - Live Radar & Forecast</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: "Segoe UI", system-ui, sans-serif; background: #0e1117; color: #e8edf4; }
  .wrap { max-width: 1000px; margin: 0 auto; padding: 16px; }
  header { text-align: center; padding: 18px 8px 6px; }
  header h1 { margin: 0; font-size: clamp(22px, 5vw, 34px); }
  header h1 span { color: #4da3ff; }
  header .place { color: #9aa4b2; margin-top: 4px; font-size: 14px; }
  header a { color: #4da3ff; text-decoration: none; }
  .card { background: #161b26; border: 1px solid rgba(255,255,255,.08); border-radius: 14px; padding: 16px; margin: 14px 0; }
  h2 { font-size: 17px; margin: 0 0 10px; color: #cdd7e4; }
  .now { display: flex; flex-wrap: wrap; gap: 18px; align-items: center; }
  .now .big { font-size: clamp(40px, 10vw, 64px); font-weight: 800; }
  .now .desc { font-size: 18px; color: #cdd7e4; }
  .now .meta { color: #9aa4b2; font-size: 14px; line-height: 1.7; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
  .day { background: #10151f; border: 1px solid rgba(255,255,255,.07); border-radius: 12px; padding: 12px; text-align: center; }
  .dname { font-weight: 700; color: #cdd7e4; }
  .dicon { font-size: 34px; margin: 6px 0; }
  .dhi { font-size: 26px; font-weight: 800; }
  .dtext { color: #9aa4b2; font-size: 13px; min-height: 32px; }
  .dmeta { color: #7d8794; font-size: 12px; margin-top: 4px; }
  .alerts { display: grid; gap: 8px; }
  .alert { background: #10151f; border-radius: 10px; padding: 10px 12px; display: grid; gap: 2px; }
  .alert b { color: #fff; }
  .alert span { color: #9aa4b2; font-size: 13px; }
  .alert i { color: #7d8794; font-size: 12px; font-style: normal; }
  .alert.ok { border-left: 6px solid #2e7d32 !important; color: #a5d6a7; }
  .spc { display: inline-flex; flex-direction: column; align-items: center; border-radius: 12px; padding: 10px 26px; color: #102015; }
  .spc span { font-size: 11px; font-weight: 700; letter-spacing: .4px; }
  .spc b { font-size: 26px; font-weight: 900; }
  #map { height: clamp(300px, 45vh, 460px); border-radius: 12px; z-index: 0; }
  .mapctl { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 10px; }
  .mapctl button { background: #2b80ff; color: #fff; border: none; border-radius: 8px; padding: 9px 20px; font-size: 16px; cursor: pointer; }
  .mapctl button:active { transform: scale(.97); }
  .mapctl select { background: #1b1f27; color: #eee; border: 1px solid rgba(255,255,255,.2); border-radius: 6px; padding: 7px 8px; font-size: 14px; }
  .mapctl input[type=range] { flex: 1; min-width: 110px; accent-color: #2b80ff; height: 26px; }
  .frame { min-width: 84px; text-align: center; font-weight: 700; font-size: 16px; color: #ffd54f; }
  nav.models { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; padding: 0 0 6px; }
  nav.models a { background: #1b2230; border: 1px solid rgba(255,255,255,.1); color: #cdd7e4; border-radius: 999px; padding: 7px 14px; font-size: 13.5px; text-decoration: none; }
  .mctl { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: 10px 0; }
  .mctl select, .mctl button { background: #1b1f27; color: #eee; border: 1px solid rgba(255,255,255,.2); border-radius: 8px; padding: 8px 10px; font-size: 14px; }
  .mctl button { background: #2b80ff; border: none; color: #fff; cursor: pointer; font-size: 16px; padding: 8px 18px; }
  #modelImg { width: 100%; border-radius: 12px; background: #0e1117; min-height: 220px; }
  .stepper { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
  .stepper button { background: #1b1f27; color: #eee; border: 1px solid rgba(255,255,255,.2); border-radius: 8px; padding: 8px 14px; font-size: 14px; cursor: pointer; }
  .stepper button:active { transform: scale(.97); }
  .src { color: #7d8794; font-size: 12px; }
  footer { text-align: center; color: #7d8794; font-size: 12.5px; padding: 18px 8px 26px; line-height: 1.8; }
  footer a { color: #4da3ff; text-decoration: none; }
  .leaflet-container { background: #0e1117; }
  /* OSM tiles + CSS invert = keyless dark basemap (CARTO now requires API keys) */
  .map-dark .leaflet-tile-pane { filter: invert(1) hue-rotate(180deg) brightness(.92) contrast(1.05); }
  .leaflet-control-attribution { background: rgba(14,17,23,.8) !important; color: #9aa4b2 !important; }
  .leaflet-control-attribution a { color: #4da3ff !important; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>🌧️ <span>TNWX_PAGE_NAME</span></h1>
    <div class="place">PLACE &nbsp;·&nbsp; <a href="OG_URL" target="_blank">Facebook page</a> &nbsp;·&nbsp; updated GEN_TIME</div>
    <a id="bigShare" href="#" target="_blank" rel="noopener"
       style="display:inline-block;margin:10px 0 2px;background:#1877f2;color:#fff;font-weight:700;font-size:17px;
              padding:12px 30px;border-radius:12px;text-decoration:none">📘 Share this page on Facebook</a>
  </header>

  <div class="card">
    <div class="now">
      <div class="big">CUR_TEMP</div>
      <div>
        <div class="desc">CUR_TEXT</div>
        <div class="meta">💧 Dew point CUR_DEW · 💨 Wind CUR_WIND · Humidity CUR_RH%<br/>Observation CUR_TIME</div>
      </div>
      SPC_BLOCK
    </div>
  </div>

  ALERT_BLOCK

  <div class="card">
    <h2>📅 3-day forecast</h2>
    <div class="grid">DAYS_BLOCK</div>
  </div>

  <div class="card">
    <h2>📡 Live radar</h2>
    <div id="map"></div>
    <div class="mapctl">
      <button id="back" title="Previous frame">⏮</button>
      <button id="play">⏸</button>
      <button id="fwd" title="Next frame">⏭</button>
      <span class="frame" id="frame">--:--</span>
      <select id="layer">
        <option value="radar">Radar</option>
        <option value="satellite">Satellite IR</option>
      </select>
      <select id="base">
        <option value="dark">Dark map</option>
        <option value="light">Light map</option>
      </select>
      <input type="range" id="opacity" min="20" max="100" value="80"/>
    </div>
    <div class="src">⏮ ⏸ ⏭ step through frames · Radar: RainViewer global NEXRAD composite (last 2 h + 30 min nowcast) · Map: CARTO/OSM · Drag to pan, pinch or scroll to zoom.</div>
  </div>

  <nav class="models">
    <a href="OG_SITE/index.html">🏠 Home</a>
    <a href="OG_SITE/radar.html">📡 Radar + future</a>
    <a href="OG_SITE/models.html">🧮 All model maps</a>
    <a href="OG_SITE/satellite.html">🛰️ Satellite</a>
    <a href="OG_SITE/tropical.html">🌀 NHC tropical</a>
    <a href="OG_SITE/severe.html">🚨 Severe storms</a>
    <a href="OG_SITE/meso.html">🗺️ Mesoanalysis</a>
    <a href="OG_SITE/obs.html">🌡️ Obs + Skew-T</a>
    <a href="OG_SITE/charts.html">📈 Charts + MOS</a>
  </nav>

  <div class="card">
    <h2>🧮 Forecast model maps — all models</h2>
    <div class="mctl">
      <select id="mModel"></select>
      <select id="mProd"></select>
      <select id="mRegion">
        <option value="etn">East Tennessee</option>
        <option value="us">US (CONUS)</option>
      </select>
      <button id="mGo">Render</button>
      <button id="mPlay">⏸</button>
    </div>
    <img id="modelImg" loading="lazy" alt="model map"/>
    <div class="stepper">
      <button id="mBack" title="Previous frame">◀</button>
      <select id="mFrame"></select>
      <button id="mFwd" title="Next frame">▶</button>
      <span class="src" id="mInfo"></span>
    </div>
    <div class="src" id="mMsg">Pick any model + product (500 mb, 850 mb, composite radar, jet levels, severe fields &amp; more) and hit Render — every model the network runs is here, including AI models and MPAS/FV3.</div>
  </div>

  <footer>
    Data: National Weather Service · NOAA · RainViewer — no APIs harmed, no keys used.<br/>
    Auto-regenerated every few minutes by the TNWN weather center. <a href="OG_URL" target="_blank">Tennessee Weather Network on Facebook</a>
  </footer>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function () {
  var pub = "OG_SITE/";
  var here = location.origin + location.pathname;
  var target = (location.hostname === "localhost" || location.hostname === "127.0.0.1") && pub !== "/" ? pub : here;
  var u = "https://www.facebook.com/sharer/sharer.php?u=" + encodeURIComponent(target);
  ["bigShare"].forEach(function (id) { var a = document.getElementById(id); if (a) a.href = u; });
})();
const HOME = [HOME_LAT, HOME_LON];
const map = L.map("map", { zoomSnap: 0.5, maxZoom: 21 }).setView(HOME, 6);
const OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTR = "© OpenStreetMap contributors";
const mapEl = document.getElementById("map");
const bases = {
  dark: L.tileLayer(OSM_URL, { maxNativeZoom: 19, maxZoom: 21, attribution: OSM_ATTR }),
  light: L.tileLayer(OSM_URL, { maxNativeZoom: 19, maxZoom: 21, attribution: OSM_ATTR }),
};
let baseKind = "dark";
bases.dark.addTo(map);
mapEl.classList.add("map-dark");
L.circleMarker(HOME, { radius: 7, color: "#fff", weight: 2, fillColor: "#ff5252", fillOpacity: 1 }).addTo(map)
  .bindTooltip("PLACE", { permanent: false });

let frames = [], idx = 0, playing = true, timer = null, layerKind = "radar", curLayer = null;
const frameEl = document.getElementById("frame");
const playBtn = document.getElementById("play");
const opacityEl = document.getElementById("opacity");

function tileUrl(path, ts) {
  return "https://tilecache.rainviewer.com" + path + "/256/{z}/{x}/{y}/2/1_1_" + ts + ".png";
}
function fmt(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}
function clearLayers() {
  Object.values(bases).forEach(b => { if (b && map.hasLayer(b)) map.removeLayer(b); });
  if (curLayer) { map.removeLayer(curLayer); curLayer = null; }
}
function setBase(kind) {
  const zoom = map.getZoom(), center = map.getCenter();
  clearLayers();
  baseKind = kind;
  bases[kind].addTo(map);
  mapEl.classList.toggle("map-dark", kind === "dark");
  map.setView(center, zoom);
  if (curLayer) show(idx);
}
function show(i) {
  idx = i;
  if (curLayer) map.removeLayer(curLayer);
  const f = frames[i];
  if (!f) return;
  curLayer = L.tileLayer(tileUrl(f.path, f.time + ""), { opacity: opacityEl.value / 100, maxNativeZoom: 10, maxZoom: 21 }).addTo(map);
  frameEl.textContent = fmt(f.time) + (f.kind === "nowcast" ? "+" : "");
}
function play() {
  playing = true; playBtn.textContent = "⏸";
  timer = setInterval(() => show((idx + 1) % frames.length), 700);
}
function pause() {
  playing = false; playBtn.textContent = "▶";
  clearInterval(timer);
}
let userPaused = false;
playBtn.onclick = () => { userPaused = playing; playing ? pause() : play(); };
const backBtn = document.getElementById("back");
const fwdBtn = document.getElementById("fwd");
function stepFrame(d) {
  if (!frames.length) return;
  userPaused = true;          // manual step stops the auto-advance
  pause();
  show((idx + d + frames.length) % frames.length);
}
backBtn.onclick = () => stepFrame(-1);
fwdBtn.onclick = () => stepFrame(1);
document.getElementById("layer").onchange = (e) => { layerKind = e.target.value; build(); };
document.getElementById("base").onchange = (e) => setBase(e.target.value);
opacityEl.oninput = () => { if (curLayer) curLayer.setOpacity(opacityEl.value / 100); };

async function build() {
  try {
    const r = await fetch("https://api.rainviewer.com/public/weather-maps.json");
    const j = await r.json();
    const list = [];
    ((j.radar || {}).past || []).slice(-12).forEach(f => list.push({ ...f, kind: "past" }));
    if (layerKind === "radar") ((j.radar || {}).nowcast || []).slice(0, 3).forEach(f => list.push({ ...f, kind: "nowcast" }));
    if (layerKind === "satellite") {
      list.length = 0;
      ((j.satellite || {}).infrared || []).slice(-12).forEach(f => list.push({ ...f, kind: "past" }));
    }
    if (!list.length) return;
    frames = list;
    show(frames.length - 1);
    if (!userPaused && !timer) play();
  } catch (e) { frameEl.textContent = "offline"; }
}
build();
setInterval(build, 5 * 60 * 1000);  // fresh frames every 5 min
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { pause(); }
  else if (!userPaused) { play(); }
});

/* ---------------- model map explorer (every model x product) ---------------- */
const MD = MODEL_DATA;
const mModel = document.getElementById("mModel"), mProd = document.getElementById("mProd");
const mGo = document.getElementById("mGo"), mPlay = document.getElementById("mPlay");
const mFrame = document.getElementById("mFrame"), mImg = document.getElementById("modelImg");
const mMsg = document.getElementById("mMsg"), mInfo = document.getElementById("mInfo");
const mRegion = document.getElementById("mRegion");

function fillMProds() {
  const prods = MD.catalog[mModel.value] || [];
  mProd.innerHTML = prods.map(p => `<option value="${p.key}">${p.label}</option>`).join("")
    || `<option value="">(no products)</option>`;
}
(function initModels() {
  if (!Object.keys(MD.catalog).length) {
    mMsg.textContent = "Model catalog unavailable in this build.";
    mGo.disabled = true;
    return;
  }
  const names = Object.keys(MD.catalog).sort((a, b) => {
    const pri = m => (m.startsWith("AI-") ? 2 : (m === "HRRR" ? 0 : 1));
    return pri(a) - pri(b) || a.localeCompare(b);
  });
  mModel.innerHTML = names.map(m =>
    `<option value="${m}">${m === "MPAS" ? "NCAR MPAS (global 3.75 km)" : m === "FV3 (SHiELD)" ? "GFDL FV3 (SHiELD)" : m}</option>`).join("");
  fillMProds();
})();
mModel.onchange = fillMProds;

let mFrames = [], mIdx = 0, mTimer = null, mPlaying = false;
function mShow(i) {
  mIdx = i;
  const f = mFrames[i];
  if (!f) return;
  mImg.src = f.url;
  mFrame.value = String(f.fh);
  mInfo.textContent = `F${String(f.fh).padStart(3, "0")} · ${i + 1}/${mFrames.length}`;
}
function mStep() { mShow((mIdx + 1) % mFrames.length); }
function mStart() {
  if (mFrames.length < 2) return;
  mPlaying = true; mPlay.textContent = "⏸";
  mTimer = setInterval(mStep, 800);
}
function mStop() {
  mPlaying = false; mPlay.textContent = "▶";
  clearInterval(mTimer);
}
mPlay.onclick = () => (mPlaying ? mStop() : mStart());
mFrame.onchange = () => { const f = mFrames.find(x => String(x.fh) === mFrame.value); if (f) mShow(mFrames.indexOf(f)); };
const mBack = document.getElementById("mBack");
const mFwd = document.getElementById("mFwd");
function mStepBtn(d) {
  if (!mFrames.length) return;
  mStop();
  mShow((mIdx + d + mFrames.length) % mFrames.length);
}
mBack.onclick = () => mStepBtn(-1);
mFwd.onclick = () => mStepBtn(1);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) mStop(); else if (mPlaying) mStop(), mStart();
});

function msFramesFor(model, prod) {
  const key = model === "MPAS" ? "mpas" : (model.indexOf("FV3") === 0 ? "shield" : null);
  if (!key) return [];
  return ((MD.ms[key] || {})[prod] || {}).frames || [];
}
mGo.onclick = () => {
  const m = mModel.value, p = mProd.value, r = mRegion.value;
  if (!m || !p) return;
  mStop();
  const msF = msFramesFor(m, p).slice().sort((a, b) => a.fh - b.fh);
  const c = MD.rend.find(x => x.model === m && x.product === p && x.region === r);
  mFrames = (msF.length ? msF : (c ? c.frames : [])).slice();
  if (mFrames.length) {
    const cyc = c ? c.cycle : "";
    mMsg.textContent = `${m} · ${mProd.options[mProd.selectedIndex] ? mProd.options[mProd.selectedIndex].text : p}` +
      (cyc ? ` · init ${cyc.slice(-6, -2)}Z ${cyc.slice(-2)}Z` : "") +
      ` · ${mFrames.length} frame(s) — ${msF.length ? "official pre-rendered loop" : (msF.length || (c && c.frames.length > 1) ? "playing" : "single frame")}`;
    mFrame.innerHTML = mFrames.map(f =>
      `<option value="${f.fh}">F${String(f.fh).padStart(3, "0")}</option>`).join("");
    mShow(mFrames.length - 1);
    mStart();
  } else {
    mMsg.textContent = `${m} ${p} (${r}) is not pre-rendered in this build yet — more combos are added every update cycle. Try another product or check back soon.`;
    mImg.removeAttribute("src"); mInfo.textContent = "";
  }
};
/* open on HRRR composite radar, East TN - falls back to whatever is on disk */
(function autoOpen() {
  const find = (m, p, r) => MD.rend.find(x => x.model === m && x.product === p && x.region === r);
  if (find("HRRR", "refc", "etn")) {
    mModel.value = "HRRR"; fillMProds(); mProd.value = "refc"; mRegion.value = "etn";
  } else if (MD.rend.length) {
    const f = MD.rend[0];
    mModel.value = f.model; fillMProds(); mProd.value = f.product; mRegion.value = f.region;
  } else {
    return;
  }
  mGo.click();
})();
</script>
</body>
</html>"""


def _fill(template, d):
    cur = d["current"]
    alerts = d["alerts"]
    spc = d.get("spc")
    og_desc = "Live radar, forecast and alerts for East Tennessee"
    if cur["tempF"] is not None:
        og_desc = f"{cur['tempF']}F - {cur['text']} - {og_desc}"
    if alerts:
        og_desc = f"{len(alerts)} active alert(s) - {og_desc}"

    spc_block = ""
    if spc:
        spc_block = (f'<div class="spc" style="background:{spc["fill"]}">'
                     f'<span>SPC DAY 1 RISK</span><b>{html.escape(spc["label"])}</b></div>')

    if alerts:
        rows = "".join(
            f'<div class="alert" style="border-left:6px solid {_alert_color(a["severity"], a["event"])}">'
            f'<b>{html.escape(a["event"])}</b><span>{html.escape(a["areaDesc"])}</span>'
            f'<i>until {a["expires"] or "further notice"}</i></div>'
            for a in alerts[:8]
        )
        alert_block = f'<div class="card"><h2>⚠️ Active alerts ({len(alerts)})</h2><div class="alerts">{rows}</div></div>'
    else:
        alert_block = '<div class="card"><h2>⚠️ Active alerts</h2><div class="alert ok">No active alerts for this area.</div></div>'

    days_block = "".join(
        f'<div class="day"><div class="dname">{html.escape(day["name"])}</div>'
        f'<div class="dicon">{day["icon"]}</div><div class="dhi">{day["hi"]}°F</div>'
        f'<div class="dtext">{html.escape(day["text"])}</div>'
        f'<div class="dmeta">💨 {html.escape(day["wind"])} · 💧 {day["pop"]}%</div></div>'
        for day in d["days"]
    )

    cur_temp = f'{cur["tempF"]}°F' if cur["tempF"] is not None else "--°F"
    cur_text = html.escape(cur["text"] or "Conditions unavailable")
    cur_dew = f'{cur["dewF"]}°F' if cur["dewF"] is not None else "n/a"
    wind = f'{cur["windMph"]} mph' if cur["windMph"] is not None else "calm"
    if cur["windDir"] is not None and cur["windMph"]:
        wind = f'{_compass(cur["windDir"])} {wind}'

    out = template
    for key, val in {
        "TNWX_PAGE_NAME": d["pageName"],
        "OG_URL": d["pageUrl"],
        "OG_SITE": (getattr(config, "PUBLIC_SITE_URL", "") or "").rstrip("/"),
        "OG_STAMP": (d.get("generated") or "").replace("-", "").replace(":", "").replace(" ", ""),
        "OG_DESC": html.escape(og_desc, quote=True),
        "PLACE": html.escape(d["place"]),
        "GEN_TIME": d["generated"],
        "CUR_TEMP": cur_temp,
        "CUR_TEXT": cur_text,
        "CUR_DEW": cur_dew,
        "CUR_WIND": wind,
        "CUR_RH": cur["rh"],
        "CUR_TIME": cur["time"],
        "SPC_BLOCK": spc_block,
        "ALERT_BLOCK": alert_block,
        "DAYS_BLOCK": days_block,
        "HOME_LAT": f'{d["lat"]:.4f}',
        "HOME_LON": f'{d["lon"]:.4f}',
        "MODEL_DATA": json.dumps(_model_data(), separators=(",", ":")),
    }.items():
        out = out.replace(key, str(val))
    return out


def _compass(deg):
    try:
        pts = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        return pts[int((float(deg) + 22.5) // 45) % 8]
    except (TypeError, ValueError):
        return ""


# The template above contains literal placeholders; render_html assembles
# everything through _fill. Keep a single public entry point:
def render_page(d):
    return _fill(render_html(d), d)


def _og_image(d):
    """Render the 1200x630 Facebook share card (static/fb_og.png).

    Branded card with live temp/conditions/SPC risk so shared links show a
    real weather snapshot. Best-effort: returns the path or None."""
    try:
        import os
        from PIL import Image, ImageDraw, ImageFont

        def _font(size, bold=False):
            try:
                from matplotlib import font_manager
                path = font_manager.findfont("DejaVu Sans")
                if bold:
                    b = os.path.join(os.path.dirname(path), "DejaVuSans-Bold.ttf")
                    if os.path.isfile(b):
                        path = b
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                try:
                    return ImageFont.truetype("arialbd.ttf" if bold else "arial.ttf", size)
                except Exception:  # noqa: BLE001
                    return ImageFont.load_default()

        cur = d.get("current") or {}
        w, h = 1200, 630
        im = Image.new("RGB", (w, h), (14, 17, 23))
        dr = ImageDraw.Draw(im)
        dr.rectangle([0, 0, 14, h], fill=(77, 163, 255))
        f_title = _font(52, bold=True)
        f_temp = _font(210, bold=True)
        f_desc = _font(46)
        f_meta = _font(30)
        f_url = _font(28)

        dr.text((70, 56), "Tennessee Weather Network", font=f_title, fill=(77, 163, 255))
        temp = cur.get("tempF")
        t_txt = f"{round(temp)}\u00b0F" if temp is not None else "--\u00b0F"
        dr.text((60, 170), t_txt, font=f_temp, fill=(255, 255, 255))
        desc = cur.get("text") or "Live East Tennessee weather"
        dr.text((620, 260), desc[:34], font=f_desc, fill=(205, 215, 228))
        spc = d.get("spc") or {}
        if spc.get("label"):
            dr.rounded_rectangle([620, 340, 620 + 560, 420], 14, fill=spc.get("fill") or (30, 40, 55))
            dr.text((640, 352), "SPC Day 1", font=_font(24), fill=(14, 17, 23))
            dr.text((640, 380), str(spc["label"])[:38], font=_font(30, bold=True), fill=(14, 17, 23))
        alerts = d.get("alerts") or []
        meta2 = f"{len(alerts)} active alert(s)" if alerts else "No active alerts"
        dr.text((70, 470), meta2, font=_font(34, bold=True),
                fill=(255, 120, 120) if alerts else (130, 200, 130))
        dr.text((70, 528), f"{d.get('place', '')}  \u00b7  updated {d.get('generated', '')}",
                font=f_meta, fill=(154, 164, 178))
        dr.text((70, 572), "rpleasant12.github.io/http-localhost-8765-",
                font=f_url, fill=(120, 140, 165))
        os.makedirs("static", exist_ok=True)
        path = os.path.join("static", "fb_og.png")
        tmp = path + ".tmp"
        im.save(tmp, "PNG")
        os.replace(tmp, path)
        return path
    except Exception:  # noqa: BLE001 - share card must never break the page
        return None


def regenerate():
    """Collect live data and write static/fb_page.html. Returns the path."""
    try:
        data = collect_weather()
        page = render_page(data)
        _og_image(data)
    except Exception:  # noqa: BLE001 - never kill the app over the share page
        return None
    os.makedirs("static", exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(tmp, OUT_PATH)
    return OUT_PATH


_REWRITER_THREAD = None


def start_bg_rewriter(interval=300):
    """Regenerate the page every `interval` seconds in a daemon thread (once)."""
    global _REWRITER_THREAD
    if _REWRITER_THREAD and _REWRITER_THREAD.is_alive():
        return _REWRITER_THREAD

    def _worker():
        while True:
            try:
                regenerate()
            except Exception:  # noqa: BLE001 - keep updating even after failures
                pass
            time.sleep(interval)

    _REWRITER_THREAD = threading.Thread(target=_worker, daemon=True, name="fb-page-rewriter")
    _REWRITER_THREAD.start()
    return _REWRITER_THREAD


if __name__ == "__main__":
    path = regenerate()
    print(f"written: {path}" if path else "generation failed")

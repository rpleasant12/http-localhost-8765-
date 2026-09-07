"""Freebuff Weather Center - radar, satellite, HRRR model, NWS data, and AI storm tracking.

Mapbox basemaps are enabled by saving a token (sidebar) - it is stored in
.freebuff/mapbox_token.txt so it survives restarts.

Data sources (all free, no keys):
- Past radar: RainViewer NEXRAD composite tiles
- Satellite: RainViewer IR when populated, else NASA GIBS GOES-East GeoColor
- Future radar + model: NOAA HRRR (AWS open data) simulated reflectivity f01-f18
- Observations/forecast/alerts: api.weather.gov
- Search: OpenStreetMap Nominatim
"""
import os
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

from ai.threat_engine import assess_threat, score_alerts, score_forecast, total_threat
from data import nws
from data.geocode import search_location
from data.nws import parse_wind_mph, city_forecasts
from data.observations import nearby_observations, city_observations
from data.radar_frames import (
    future_bundle,
    get_nowcast_frames,
    get_past_frames,
    get_satellite_frames,
    satellite_source,
    sample_future_dbz,
    start_future_renderer,
)
from data.severe import SPC_COLORS, hrrr_severe, spc_outlooks, spc_risk_at, tn_alerts
from data.mrms import mrms_bundle
from data.nws_radar import nws_bundle
from data.national import nhc_storms, wpc_catalog, wpc_qpf, wpc_sigwx
from data.satellite_bands import BANDS as SAT_BANDS, band_bundle
from data.star_sat import PRODUCTS as STAR_PRODUCTS, star_bundle
from data.sounding import build_sounding
from data.model_maps import (
    MAP_MODELS,
    PRODUCTS as MAP_PRODUCTS,
    PRODUCTS_BY_MODEL,
    clear_map_cache,
    find_cycle,
    render_product_map,
)
from data.models import clear_model_cache, get_ensemble_fan, get_series, grouped_model_list, model_info
from maps.radar_component import animated_radar
import config

# optional: gentle auto-refresh so background-rendered frames pop in live
try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:  # pragma: no cover
    st_autorefresh = None

st.set_page_config(page_title="Tennessee Weather Network", page_icon="\U0001f326\ufe0f", layout="wide")

# ---------------------------------------------------------------- session state
defaults = {
    "name": config.DEFAULT_LOCATION_NAME,
    "lat": config.LATITUDE,
    "lon": config.LONGITUDE,
    "past_only": False,
    "auto_play": True,
}
for key, value in defaults.items():
    st.session_state.setdefault(key, value)


# ---------------------------------------------------------------- cached data
@st.cache_data(ttl=600, show_spinner=False)
def cached_search(query):
    return search_location(query)


@st.cache_data(ttl=600, show_spinner=False)
def cached_forecast(lat, lon):
    return nws.get_forecast(lat, lon)


@st.cache_data(ttl=1800, show_spinner=False)
def cached_hourly(lat, lon):
    return nws.get_hourly(lat, lon)


@st.cache_data(ttl=300, show_spinner=False)
def cached_current(lat, lon):
    return nws.get_current_conditions(lat, lon)


@st.cache_data(ttl=300, show_spinner=False)
def cached_alerts(lat, lon):
    return nws.get_active_alerts(lat, lon)


@st.cache_data(ttl=180, show_spinner=False)
def cached_past_frames():
    return get_past_frames()


@st.cache_data(ttl=300, show_spinner=False)
def cached_nowcast_frames():
    return get_nowcast_frames()


@st.cache_data(ttl=180, show_spinner=False)
def cached_satellite_frames():
    return get_satellite_frames()


@st.cache_data(ttl=3600, show_spinner=False)
def cached_series(model, var_key, lat, lon, max_hours):
    """Point time series from a forecast model (disk-cached per cycle inside)."""
    return get_series(model, var_key, lat, lon, max_hours=max_hours)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_fan(lat, lon):
    """Multi-model temperature comparison at a point (disk-cached per cycle inside)."""
    return get_ensemble_fan(lat, lon, var_key="temp", max_hours=48)


@st.cache_data(ttl=300, show_spinner=False)
def cached_spc():
    return spc_outlooks()


@st.cache_data(ttl=120, show_spinner=False)
def cached_tn_alerts():
    return tn_alerts()


@st.cache_data(ttl=600, show_spinner=False)
def cached_severe(lat, lon):
    return hrrr_severe(lat, lon, hours=4)


@st.cache_data(ttl=900, show_spinner=False)
def cached_nhc():
    return nhc_storms()


@st.cache_data(ttl=900, show_spinner=False)
def cached_wpc_qpf(day):
    return wpc_qpf(day)


@st.cache_data(ttl=1800, show_spinner=False)
def cached_wpc_sigwx():
    return wpc_sigwx()


@st.cache_data(ttl=1800, show_spinner=False)
def cached_sounding(lat, lon, fh):
    """RAP Skew-T at a point (disk-cached PNG inside)."""
    return build_sounding(lat, lon, fh=fh, place=NAME)


@st.cache_data(ttl=600, show_spinner=False)
def cached_city_obs():
    return city_observations()


@st.cache_data(ttl=900, show_spinner=False)
def cached_city_forecasts():
    return city_forecasts()


@st.cache_data(ttl=300, show_spinner=False)
def cached_observations(lat, lon, limit=18):
    """Latest METAR/AWOS obs from stations near this point (NWS, no key)."""
    return nearby_observations(lat, lon, limit=limit)


def refresh():
    """Drop cached weather data so the next rerun refetches everything."""
    for fn in (cached_forecast, cached_hourly, cached_current, cached_alerts,
               cached_past_frames, cached_satellite_frames, cached_series, cached_fan,
               cached_spc, cached_tn_alerts, cached_severe, cached_nhc, cached_sounding,
               cached_wpc_qpf, cached_wpc_sigwx):
        fn.clear()
    clear_model_cache()
    clear_map_cache()
    for base in ("maps_model", "maps_prod", "maps_fh", "maps_model_prev"):
        for suf in ("", "_v2"):  # widget keys are versioned for stale-state recovery
            st.session_state.pop(f"{base}{suf}", None)
    st.session_state.pop("maps_rendered", None)
    for k in [k for k in list(st.session_state) if k.startswith("fan_")]:  # reset fan
        st.session_state.pop(k, None)


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("\U0001f30d Location")
    query = st.text_input(
        "Search for a place",
        placeholder="e.g. Asheville, NC",
        help="Enter a city, ZIP code, or landmark",
    )
    if st.button("Search", width="stretch") and query:
        results = cached_search(query)
        st.session_state.search_results = results
        st.session_state.search_warning = not results
        st.rerun()

    if st.session_state.get("search_warning"):
        st.warning("No matches found.")
    if st.session_state.get("search_results"):
        results = st.session_state.search_results
        options = {r["name"]: r for r in results}
        pick = st.selectbox("Results", list(options.keys()))
        if st.button("Use this location", width="stretch"):
            chosen = options[pick]
            st.session_state.name = chosen["name"]
            st.session_state.lat = chosen["lat"]
            st.session_state.lon = chosen["lon"]
            st.session_state.search_results = None
            st.session_state.search_warning = False
            refresh()
            st.rerun()

    st.divider()
    st.markdown("**\U0001f5fa Basemap**")

    def _mapbox_token_file():
        return os.path.join(".freebuff", "mapbox_token.txt")

    def _load_saved_token():
        try:
            with open(_mapbox_token_file(), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _save_token(tok):
        try:
            os.makedirs(".freebuff", exist_ok=True)
            if tok:
                with open(_mapbox_token_file(), "w", encoding="utf-8") as f:
                    f.write(tok)
            else:
                try:
                    os.remove(_mapbox_token_file())
                except OSError:
                    pass
        except OSError:
            pass

    saved_token = st.session_state.get("mapbox_saved", None)
    if saved_token is None:
        saved_token = _load_saved_token()
        st.session_state.mapbox_saved = saved_token
    mapbox_token = st.text_input(
        "Mapbox access token",
        value=saved_token,
        type="password",
        placeholder="pk.eyJ1Ijoi\u2026 (optional)",
        help="Paste a Mapbox token to unlock Mapbox Streets / Satellite / Dark / "
             "Outdoors basemaps at street-level zoom. Free at account.mapbox.com. "
             "Saved locally so it survives restarts. Leave empty for OpenStreetMap.",
    )
    if mapbox_token != saved_token:
        st.session_state.mapbox_saved = mapbox_token
        _save_token(mapbox_token)
        st.rerun()
    if mapbox_token:
        st.caption("\u2705 Mapbox basemaps enabled.")
    else:
        st.caption("Using OpenStreetMap. Add a Mapbox token above for satellite/street maps.")

    st.divider()
    st.markdown("**\u2699\ufe0f Map options**")
    map_opacity = st.slider("Overlay opacity", 30, 100, 85, key="map_opacity",
                            help="Opacity of radar / satellite / future-frame overlays")
    show_stations = st.checkbox("Station observations on map", value=True, key="show_stations",
                                help="Temp-colored dots for nearby NWS stations "
                                     "(in-map layer menu: Station Observations)")
    show_alerts_opt = st.checkbox("Alert polygons on map", value=True, key="show_alerts_opt")

    st.divider()
    past_only = st.toggle("Show only past radar", value=st.session_state.past_only)
    if past_only != st.session_state.past_only:
        st.session_state.past_only = past_only
        st.rerun()
    auto_play = st.toggle("Auto-play animation", value=st.session_state.auto_play)
    if auto_play != st.session_state.auto_play:
        st.session_state.auto_play = auto_play
        st.rerun()
    st.caption("Past: 2 h NEXRAD + official NOAA MRMS mosaic. Future: 18 h HRRR + 48 h NAM 3 km model radar.")

    st.divider()
    st.caption("Click the map to jump anywhere (US).")
    if st.button("\U0001f504 Refresh data", width="stretch"):
        refresh()
        st.rerun()

    st.caption(f"Last updated: {datetime.now().strftime('%I:%M %p')}")

NAME = st.session_state.name
LAT = st.session_state.lat
LON = st.session_state.lon

# display options + observations shared by every map
MAP_OPTS = {"overlayOpacity": map_opacity / 100.0, "showAlerts": show_alerts_opt}
obs_stations = cached_observations(LAT, LON) if show_stations else []

# ---------------------------------------------------------------- header
if st.session_state.pop("outside_us", False):
    st.warning("That spot is outside NWS coverage (US only) - staying on the current location.")

st.title("\U0001f326\ufe0f Tennessee Weather Network")
st.caption(f"{NAME} \u00b7 [Facebook page]({config.PAGE_URL})")


# ---------------------------------------------------------------- helpers (before use)
def render_alerts_column():
    """Active-alert list used by two tabs."""
    st.subheader("Active alerts")
    if alerts:
        for alert in alerts:
            sev_icon = {"Extreme": "\U0001f7e5", "Severe": "\U0001f7e0", "Moderate": "\U0001f7e1"}.get(alert["severity"], "\U0001f535")
            with st.expander(f"{sev_icon} {alert['event']} - {alert['areaDesc'][:48]}"):
                st.markdown(f"**Severity:** {alert['severity']}  \u00b7  **Urgency:** {alert['urgency']}")
                st.markdown(f"**Expires:** {alert['expires'] or 'unknown'}")
                st.markdown(alert["description"].split("*")[0].strip())
                if alert["instruction"]:
                    st.info(alert["instruction"])
    else:
        st.info("No active alerts - polygons appear on the map when issued.")


def handle_map_click(map_result):
    """Apply a map click / satellite-band change to session state; True if rerun."""
    if map_result and map_result.get("sat_mode"):
        picked = "gibs" if map_result["sat_mode"] == "satellite" else map_result["sat_mode"]
        if picked != st.session_state.get("sat_band"):
            st.session_state.sat_band = picked
            st.rerun()
            return True
    if map_result and map_result.get("last_clicked"):
        clicked = map_result["last_clicked"]
        clat, clng = clicked["lat"], clicked["lng"]
        if 24 <= clat <= 50 and -125 <= clng <= -66:
            st.session_state.lat = clat
            st.session_state.lon = clng
            st.session_state.name = f"{clat:.3f}, {clng:.3f}"
            refresh()
        else:
            st.session_state.outside_us = True
        st.rerun()
        return True
    return False


def _model_charts(hrrr_bundle):
    """Charts for the HRRR tab: modeled dBZ curve at the selected location."""
    import altair as alt

    samples = sample_future_dbz(LAT, LON)
    if not samples:
        st.info("No rendered model hours yet - charts populate as frames render.")
        return
    df = pd.DataFrame(samples)
    df["valid"] = pd.to_datetime(df["time"])
    df = df.sort_values("valid")
    chart = (
        alt.Chart(df)
        .mark_area(color="#7aa6ff", opacity=0.6)
        .encode(
            x=alt.X("valid:T", title="Valid time (UTC)"),
            y=alt.Y("dbz:Q", title="Modeled dBZ within 15 km", scale=alt.Scale(domain=[0, 75])),
        )
        .properties(title="HRRR simulated reflectivity at this location", height=260)
    )
    rule = (
        alt.Chart(pd.DataFrame({"y": [40]}))
        .mark_rule(color="#ff5252", strokeDash=[6, 4])
        .encode(y="y:Q")
    )
    st.altair_chart(chart + rule, use_container_width=True)
    st.caption("Red line: 40 dBZ - storm-strength threshold used by the AI tracker.")


# ---------------------------------------------------------------- fetch core data
with st.spinner("Fetching weather data..."):
    alerts = cached_alerts(LAT, LON)
    forecast = cached_forecast(LAT, LON)
    current = cached_current(LAT, LON)
    past_frames = cached_past_frames()
    sat_frames = cached_satellite_frames()
    spc = cached_spc()
    tn_all = cached_tn_alerts()
    mrms = mrms_bundle()          # official MRMS mosaic (background renderer)
    nws_radar = nws_bundle()      # official NWS WMS mosaic (background renderer)
nhc = cached_nhc()

# satellite band selection shared between the picker and the map component
st.session_state.setdefault("sat_band", "gibs")
sat_band = st.session_state.sat_band
# in-map satellite picker: GeoColor + every ABI channel the backend can render
# + the NOAA STAR sectorized products (star.nesdis.noaa.gov viewer data)
SAT_MODE_OPTS = {"gibs": "GeoColor"}
SAT_MODE_OPTS.update({k: v["label"] for k, v in SAT_BANDS.items()})
SAT_MODE_OPTS.update({k: v[1] for k, v in STAR_PRODUCTS.items()})
if sat_band in SAT_BANDS:
    band_bundle(sat_band)         # start the GOES-19 band renderer threads
elif sat_band in STAR_PRODUCTS:
    star_bundle(sat_band)         # start the NOAA STAR renderer threads
spc_day1_features = (spc.get("day1") or {}).get("features", [])
sev = cached_severe(LAT, LON)
severe_payload = {
    "hailPoints": sev.get("hail_points", []),
    "rotPoints": sev.get("rot_points", []),
}

# Kick off the background renderers (render pending frames to static/ dirs)
start_future_renderer(max_hours=48)
hrrr = future_bundle(max_hours=48)

# gentle page auto-refresh so freshly rendered frames appear without manual reload
if st_autorefresh is not None:
    st_autorefresh(interval=90_000, key="tnwx_autorefresh")

# ---------------------------------------------------------------- threat banner
alert_score = score_alerts(alerts)
forecast_score = score_forecast(forecast)
overall = total_threat(alert_score, forecast_score)
level = assess_threat(overall)

level_colors = {
    "LOW": "green",
    "ELEVATED": "yellow",
    "MODERATE": "orange",
    "HIGH": "red",
    "EXTREME": "red",
}
st.markdown(
    f"**Overall storm threat: :{level_colors.get(level, 'blue')}[{level}]** "
    f"(score {overall}/100 - alerts {alert_score}, forecast {forecast_score})"
)

# ---------------------------------------------------------------- alert strip
if alerts:
    worst = max(alerts, key=lambda a: {"Extreme": 3, "Severe": 2, "Moderate": 1}.get(a["severity"], 0))
    st.error(
        f"\u26a0\ufe0f {len(alerts)} active alert(s) - worst: {worst['event']} ({worst['severity']})",
        icon="\U0001f6a8",
    )
else:
    st.success("No active alerts for this location.")

st.divider()

MB = st.session_state.get("mapbox_token") or ""

alert_geoms = [
    {"event": a["event"], "severity": a["severity"], "areaDesc": a["areaDesc"], "geometry": a.get("geometry")}
    for a in alerts
]

# ---------------------------------------------------------------- site-wide ticker
# official event colors (kept in sync with the map component)
EVENT_HEX = {
    "Tornado Warning": "#ff0000", "Severe Thunderstorm Warning": "#ffa500",
    "Flash Flood Warning": "#8b0000", "Flood Warning": "#00ff00",
    "Flash Flood Watch": "#2e8b57", "Flood Advisory": "#00ff7f",
    "Tornado Watch": "#ffff00", "Severe Thunderstorm Watch": "#db74bc",
    "Special Weather Statement": "#ffe4b5", "Winter Storm Warning": "#ff8fab",
    "Winter Weather Advisory": "#766ec8", "Wind Advisory": "#d8bfd8",
    "Dense Fog Advisory": "#b9b9b9", "Heat Advisory": "#ff7f50",
    "Extreme Heat Warning": "#c71585", "Red Flag Warning": "#ff1493",
    "Freeze Warning": "#48d1cc", "Frost Advisory": "#87ceeb",
    "Air Quality Alert": "#808080",
}

ticker_items = ['<span style="color:#4fc3f7;font-weight:800">\U0001f31e TENNESSEE WEATHER NETWORK \u2014 LIVE</span>']

# current conditions at the saved location
_c_t = current.get("temperature", {}).get("value")
_c_txt = current.get("textDescription") or ""
_c_ws = current.get("windSpeed", {}).get("value")
_c_rh = current.get("relativeHumidity", {}).get("value")
if _c_t is not None:
    _bits = [f"{_c_t * 9 / 5 + 32:.0f}\u00b0F"]
    if _c_txt:
        _bits.append(_c_txt)
    if _c_ws is not None:
        _bits.append(f"wind {(_c_ws or 0) * 0.621371:.0f} mph")
    if _c_rh is not None:
        _bits.append(f"humidity {_c_rh:.0f}%")
    ticker_items.append(
        f'<span style="color:#7ee787">\U0001f321 {NAME}: ' + " \u00b7 ".join(_bits) + "</span>"
    )

# next two forecast periods
for p in forecast[:2]:
    _pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
    _seg = f"\U0001f4c5 {p.get('name', 'Next')}: {p.get('temperature', '?')}\u00b0{p.get('temperatureUnit', 'F')}"
    if _pop:
        _seg += f", {_pop}% precip"
    if p.get("shortForecast"):
        _seg += f" \u2014 {p['shortForecast']}"
    ticker_items.append(_seg)

# warning for this location (front of the alert queue)
if alerts:
    _worst_a = max(alerts, key=lambda a: {"Extreme": 3, "Severe": 2, "Moderate": 1}.get(a["severity"], 0))
    _color = EVENT_HEX.get(_worst_a["event"], "#ff5252")
    ticker_items.append(
        f'<span style="color:{_color};font-weight:700">\u26a0\ufe0f {_worst_a["event"].upper()} HERE '
        f'\u2014 {(_worst_a.get("areaDesc") or "")[:60]}</span>'
    )

# statewide Tennessee alerts
for a in tn_all[:10]:
    color = EVENT_HEX.get(a["event"], "#4fc3f7")
    area = (a["areaDesc"].split(";")[0] if a["areaDesc"] else "Tennessee")
    ticker_items.append(
        f'<span style="color:{color};font-weight:700">{a["event"].upper()}</span>'
        f" \u2014 {area[:60]}"
        + (f" <small style=\"color:#9aa4b2\">until {a['expires']}</small>" if a["expires"] else "")
    )
if not tn_all and not alerts:
    ticker_items.append('<span style="color:#7ee787">No active alerts \u2014 all clear across Tennessee</span>')

# SPC day-1 worst category nationally
if spc_day1_features:
    _CAT_RANK = {"TSTM": 0, "MRGL": 1, "SLGT": 2, "ENH": 3, "MDT": 4, "HIGH": 5}
    _worst = max(spc_day1_features, key=lambda x: _CAT_RANK.get(x["label"], -1), default=None)
    if _worst:
        ticker_items.append(
            f'<span style="color:{_worst["fill"]};font-weight:700">\U0001f3a9 SPC DAY 1: {_worst["label2"].upper()}</span>'
        )

# HRRR hail + AI storm-tracker summary
if sev.get("hail_max_mm", 0) and sev["hail_max_mm"] >= 13 and sev.get("hail_time"):
    ticker_items.append(
        f'<span style="color:#00e676;font-weight:700">\U0001f32a HRRR HAIL: {sev["hail_max_mm"]:.0f} mm '
        f'possible @ {sev["hail_time"][11:16]}Z</span>'
    )
if hrrr.get("summary"):
    ticker_items.append(f"\U0001f9e0 Storm tracker: {hrrr['summary']}")

# active tropical cyclones
for s in nhc:
    ticker_items.append(
        f'<span style="color:#ce93d8;font-weight:700">\U0001f300 {(s.get("name") or "Storm").upper()}</span>'
        f" ({s.get('classification', '')}) \u2014 {s.get('intensity', '?')} kt \u00b7 {s.get('pressure', '?')} mb"
        f" \u00b7 moving {s.get('movement', '')}"
    )

ticker_items.append(f'<span style="color:#9aa4b2">Updated {datetime.now().strftime("%I:%M %p")}</span>')

items_html = " \u2022 ".join(ticker_items) + " \u2022 "
st.markdown(
    f'''<style>
.twx-ticker {{ overflow: hidden; background: #14161c; border: 1px solid rgba(255,255,255,.15);
  border-radius: 10px; margin-bottom: 6px; }}
.twx-ticker div {{ display: inline-block; white-space: nowrap; padding: 13px 0 13px 14px;
  animation: twxscroll 45s linear infinite; color: #fafafa; font-size: 19px; font-weight: 600;
  font-family: "Source Sans Pro", sans-serif; }}
.twx-ticker small {{ font-size: 15px; }}
.twx-ticker:hover div {{ animation-play-state: paused; }}
@keyframes twxscroll {{ 0% {{ transform: translateX(0); }} 100% {{ transform: translateX(-50%); }} }}
</style>
<div class="twx-ticker"><div>{items_html}{items_html}</div></div>''',
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- main menu
tabs = st.tabs([
    "\U0001f6a8 Severe Weather",
    "\U0001f4e1 Radar & AI",
    "\U0001f6f0\ufe0f Satellite",
    "\U0001f9ee NWS Models",
    "\U0001f30e National",
    "\U0001f4cb Forecast & Alerts",
    "\U0001f321\ufe0f Current",
])

# ============================================================ Tab 0: Severe Weather
with tabs[0]:

    # ---------------- SPC outlook + risk at location ----------------
    risk = spc_risk_at(LAT, LON, spc)
    cat_hex = risk["cat"]["fill"] if risk["cat"] else "#2e7d32"
    cat_name = (risk["cat"]["label2"] if risk["cat"] else "No severe risk area")
    l1, l2 = st.columns([1, 3])
    l1.markdown(
        f'''<div style="background:{cat_hex};color:#111;border-radius:10px;padding:14px;text-align:center">
<div style="font-size:12px;font-weight:600;opacity:.75">SPC DAY 1 RISK HERE</div>
<div style="font-size:22px;font-weight:800">{risk["cat"]["label"] if risk["cat"] else "NONE"}</div>
<div style="font-size:12px">{cat_name}</div></div>''',
        unsafe_allow_html=True,
    )
    l2.markdown(
        f"**{risk['summary']}**  \n\n"
        f"Hail risk: **{risk['hail']['label'] + '%' if risk['hail'] else 'none drawn'}** \u00b7 "
        f"Tornado risk: **{risk['torn']['label'] if risk['torn'] else 'none drawn'}** \u00b7 "
        f"HRRR hail max: **{sev.get('hail_max_mm', 0):.1f} mm** \u00b7 "
        f"Rotation (UPHL) max: **{sev.get('uphl_max', 0):.0f} m\u00b2/s\u00b2**"
    )

    # ---------------- map: SPC polygons + warning polys + signatures ----------------
    map_result = animated_radar(
        LAT, LON,
        past_frames=past_frames,
        future_frames=[],
        alerts_geojson=[
            {"event": a["event"], "severity": a["severity"], "areaDesc": a["areaDesc"],
             "expires": a["expires"], "geometry": a.get("geometry")}
            for a in tn_all
        ],
        satellite_frames=[],
        sat_modes=SAT_MODE_OPTS,
        nws_frames=nws_radar,
        future_pending=False,
        threat=level,
        threat_score=overall,
        ai_summary=None,
        auto_play=False,
        initial_mode="radar",
        zoom=6,
        height=520,
        key="severe_map",
        severe=severe_payload,
        spc_features=spc_day1_features,
        mapbox_token=MB,
        map_options=MAP_OPTS,
    )
    handle_map_click(map_result)
    d1 = spc.get("day1") or {}
    d2 = spc.get("day2") or {}
    st.caption(
        "Map: SPC Day 1 outlook (filled), NWS watches/warnings with official colors + codes "
        "(TOR/SVR/FFW\u2026), HRRR hail (green) and rotation (purple) signatures. "
        "Fullscreen button top-right; double-click or scroll to zoom to street level."
        + (f" \u00b7 Outlook valid {(d1.get('meta') or {}).get('valid', '')}Z expiring {(d1.get('meta') or {}).get('expire', '')}Z" if d1.get("meta") else "")
    )

    # ---------------- storm tracks + signatures + outlook details ----------------
    tc1, tc2, tc3 = st.columns(3)
    with tc1:
        st.subheader("Storm tracks (AI)")
        track_frames = [f for f in hrrr["frames"] if f.get("tracks") and (f["tracks"].get("30") or f["tracks"].get("60"))]
        if track_frames:
            f = track_frames[-1]
            rows = []
            for c in (f.get("cells") or [])[:10]:
                t30 = next((p for p in f["tracks"].get("30", []) if abs(p["lat"] - c["lat"]) < 1.2 and abs(p["lon"] - c["lon"]) < 1.2), None)
                t60 = next((p for p in f["tracks"].get("60", []) if abs(p["lat"] - c["lat"]) < 1.8 and abs(p["lon"] - c["lon"]) < 1.8), None)
                rows.append({
                    "Cell": f"{c['dbz_max']:.0f} dBZ",
                    "Now": f"{c['lat']:.2f}, {c['lon']:.2f}",
                    "+30m": f"{t30['lat']:.2f}, {t30['lon']:.2f}" if t30 else "\u2014",
                    "+60m": f"{t60['lat']:.2f}, {t60['lon']:.2f}" if t60 else "\u2014",
                })
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            st.caption(f"Valid {f['time']} \u00b7 projections from AI motion vectors")
        else:
            st.info("No tracked cells this cycle.")
    with tc2:
        st.subheader("Hail signatures")
        if sev.get("hail_points"):
            for p in sev["hail_points"][:5]:
                st.markdown(f"**{p['peak']:.0f} mm** hail at {p['lat']:.2f}, {p['lon']:.2f} \u2014 {p['time'][11:16]}Z ({p['cells']} px)")
        else:
            st.info("No hail signatures above 25 mm this cycle.")
        st.subheader("Rotation signatures")
        if sev.get("rot_points"):
            for p in sev["rot_points"][:5]:
                st.markdown(f"**{p['peak']:.0f} m\u00b2/s\u00b2** rotation at {p['lat']:.2f}, {p['lon']:.2f} \u2014 {p['time'][11:16]}Z")
        else:
            st.info("No rotation signatures above 130 m\u00b2/s\u00b2 this cycle.")
    with tc3:
        st.subheader("SPC outlooks")
        for label, src in (("Day 1", spc.get("day1")), ("Day 2", spc.get("day2")),
                            ("Day 1 hail", spc.get("day1_hail")), ("Day 1 tornado", spc.get("day1_torn"))):
            if not src:
                st.caption(f"{label}: unavailable")
                continue
            chips = " ".join(
                f'<span style="background:{f["fill"]};color:#111;border-radius:4px;padding:1px 6px;font-size:12px;font-weight:700">{f["label"]}</span>'
                for f in src["features"]
            )
            st.markdown(f"**{label}:** " + chips, unsafe_allow_html=True)
        st.subheader("Tennessee alerts")
        if tn_all:
            for a in tn_all[:8]:
                color = EVENT_HEX.get(a["event"], "#4fc3f7")
                st.markdown(
                    f'<span style="color:{color};font-weight:700">\u25a0</span> **{a["event"]}** '
                    f"\u2014 {a['areaDesc'][:60]}",
                    unsafe_allow_html=True,
                )
        else:
            st.caption("No active alerts statewide.")

# ============================================================ Tab 1: Radar & AI
with tabs[1]:
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Animated radar - past + future")
        future_frames = [] if st.session_state.past_only else cached_nowcast_frames() + hrrr["frames"]
        ai_summary = hrrr["summary"]
        if hrrr["total"] and hrrr["ready"] < hrrr["total"] and not st.session_state.past_only:
            st.info(
                f"\u23f3 Rendering future radar: {hrrr['ready']}/{hrrr['total']} hours ready. "
                "New hours appear automatically every few seconds.",
                icon=None,
            )
        if ai_summary:
            st.info(f"\U0001f9e0 {ai_summary}", icon=None)
        map_result = animated_radar(
            LAT, LON,
            past_frames=past_frames,
            future_frames=future_frames,
            alerts_geojson=alert_geoms,
            satellite_frames=(band_bundle(sat_band)["frames"] if sat_band in SAT_BANDS
                              else (star_bundle(sat_band)["frames"] if sat_band in STAR_PRODUCTS
                                    else sat_frames)),
            sat_mode=sat_band,
            sat_modes=SAT_MODE_OPTS,
            obs_stations=obs_stations,
            map_options=MAP_OPTS,
            mrms_frames=mrms["frames"],
            nws_frames=nws_radar,
            future_pending=bool(future_frames) and hrrr["ready"] < hrrr["total"],
            threat=level,
            threat_score=overall,
            ai_summary=ai_summary,
            auto_play=st.session_state.auto_play and not st.session_state.past_only,
            initial_mode="radar",
            zoom=7,
            height=560,
            key="radar_map",
            severe=severe_payload,
            spc_features=spc_day1_features,
            mapbox_token=MB,
        )
        clicked = handle_map_click(map_result)
        if not clicked:
            st.subheader("Current conditions")
            if current:
                temp_f = current.get("temperature", {}).get("value")
                st.metric("Temperature", f"{temp_f * 9 / 5 + 32:.0f} \u00b0F" if temp_f is not None else "n/a")
                col1, col2, col3 = st.columns(3)
                wind_kmh = current.get("windSpeed", {}).get("value")
                col1.metric("Wind", f"{wind_kmh * 0.621371:.0f} mph" if wind_kmh is not None else "n/a")
                col2.metric("Humidity", f"{current.get('relativeHumidity', {}).get('value') or 0:.0f}%")
                col3.metric("Dewpoint",
                            f"{(current.get('dewpoint', {}).get('value') or 0) * 9 / 5 + 32:.0f} \u00b0F")
                st.caption(current.get("textDescription", ""))
            else:
                st.info("No nearby observation station reporting right now.")
    with right:
        render_alerts_column()

# ============================================================ Tab 2: Satellite
with tabs[2]:
    st.subheader("GOES-19 (GOES-East) satellite")
    band_opts = {"gibs": "GeoColor daylight/night composite (GIBS)"}
    band_opts.update({k: f"{v['label']} (C{v['band']:02d})" for k, v in SAT_BANDS.items()})
    band_opts.update({k: f"{v[1]} (STAR CDN)" for k, v in STAR_PRODUCTS.items()})
    band_pick = st.selectbox(
        "Satellite product",
        list(band_opts.keys()),
        format_func=lambda k: band_opts[k],
        index=list(band_opts.keys()).index(sat_band) if sat_band in band_opts else 0,
        key="sat_band_pick",
    )
    if band_pick != sat_band:
        st.session_state.sat_band = band_pick
        st.rerun()

    if sat_band == "gibs":
        src = satellite_source(sat_frames)
        src_label = {"rainviewer": "RainViewer IR composite",
                     "gibs": "NASA GIBS / NOAA GOES-East ABI GeoColor"}.get(src, src)
        st.caption(f"Source: {src_label} \u00b7 {len(sat_frames)} frames \u00b7 animated cloud loop.")
        shown_frames = sat_frames
        initial_mode = "satellite"
    elif sat_band in STAR_PRODUCTS:
        bb = star_bundle(sat_band)
        st.caption(
            f"NOAA STAR sectorized imagery (star.nesdis.noaa.gov CDN) \u00b7 "
            f"{bb['ready']}/{bb['total']} frames ready \u00b7 new scans appear automatically."
        )
        if bb["ready"] < bb["total"]:
            st.info("\u23f3 Fetching NOAA STAR frames - they appear automatically as the "
                    "background fetcher finishes (first frame ~5 s).", icon=None)
        shown_frames = bb["frames"]
        initial_mode = sat_band
    else:
        bb = band_bundle(sat_band)
        st.caption(
            f"GOES-19 ABI band C{SAT_BANDS[sat_band]['band']:02d} decoded locally from NOAA's "
            f"open-data bucket \u00b7 {bb['ready']}/{bb['total']} scans rendered \u00b7 new scans appear automatically."
        )
        if bb["ready"] < bb["total"]:
            st.info("\u23f3 Rendering GOES-19 band frames - they appear automatically as the "
                    "background renderer finishes (first scan ~10 s).", icon=None)
        shown_frames = bb["frames"]
        initial_mode = sat_band
    if not shown_frames:
        st.warning("This satellite product is still rendering - switch bands or wait a moment.")
    map_result = animated_radar(
        LAT, LON,
        past_frames=past_frames,
        future_frames=[],
        alerts_geojson=alert_geoms,
        satellite_frames=shown_frames,
        sat_mode=sat_band,
        sat_modes=SAT_MODE_OPTS,
        nws_frames=nws_radar,
        future_pending=False,
        threat=level,
        threat_score=overall,
        ai_summary=None,
        auto_play=st.session_state.auto_play,
        initial_mode=initial_mode,
        zoom=6,
        height=600,
        key="satellite_map",
        mapbox_token=MB,
        map_options=MAP_OPTS,
    )
    handle_map_click(map_result)

# ============================================================ Tab 3: NWS Models
with tabs[3]:
    st.subheader("NWS models")

    # --- Section A: classic model MAPS (MetPy + Cartopy)
    st.markdown("**Forecast model maps** \u00b7 MetPy \u00d7 Cartopy, straight from NOAA GRIB2")
    with st.expander("\U0001f5fa\ufe0f Open model map viewer", expanded=True):
        from data.model_maps import MAP_MODELS as _MAP_MODELS

        # CAM first, then global + blends, then ensembles
        MAP_MODEL_ORDER = ["RRFS", "HREF", "REFS", "HRRR", "RAP", "NAM", "GFS", "AI-GraphCast", "AI-Pangu", "AI-FourCastNet", "ECMWF", "AIFS", "AIFS-ENS", "NBM", "CFS", "GEFS", "GEFS-Spread"]
        MAP_MODEL_ORDER = [m for m in MAP_MODEL_ORDER if m in _MAP_MODELS]
        mv1, mv2, mv3, mv4 = st.columns([2, 3, 2, 1])
        # versioned keys: stale replayed state dies here instead of crashing
        mk = "_v2" if st.session_state.get("maps_keys_v2") else ""
        try:
            maps_model = mv1.selectbox(
                "Model", MAP_MODEL_ORDER,
                format_func=lambda k: _MAP_MODELS[k]["label"],
                key=f"maps_model{mk}",
            )
            # switching models invalidates the stored product/hour pickers
            if st.session_state.get(f"maps_model_prev{mk}") != maps_model:
                st.session_state[f"maps_model_prev{mk}"] = maps_model
                for k in (f"maps_prod{mk}", f"maps_fh{mk}", "maps_rendered"):
                    st.session_state.pop(k, None)
            # drop any stored picks that are no longer valid for this model
            if st.session_state.get(f"maps_prod{mk}") not in PRODUCTS_BY_MODEL[maps_model]:
                st.session_state.pop(f"maps_prod{mk}", None)
            mm_def = _MAP_MODELS[maps_model]
            fh_opts = list(range(0, mm_def["max_hour"] + 1, mm_def["hour_step"]))
            if st.session_state.get(f"maps_fh{mk}") not in fh_opts:
                st.session_state.pop(f"maps_fh{mk}", None)
            maps_prod = mv2.selectbox(
                "Map product",
                PRODUCTS_BY_MODEL[maps_model],
                format_func=lambda k: MAP_PRODUCTS[k]["label"],
                key=f"maps_prod{mk}",
            )
            maps_fh = mv3.selectbox("Forecast hour", fh_opts,
                                    index=min(2, len(fh_opts) - 1), key=f"maps_fh{mk}")
        except KeyError:
            if not st.session_state.get("maps_keys_v2"):
                st.session_state.maps_keys_v2 = True
                st.rerun()
            maps_model, maps_prod, maps_fh = "GFS", "500_vort", 6
        show = mv4.button("Render", use_container_width=True, key="maps_render")

        # remember last selection so the map persists across reruns
        if show:
            st.session_state["maps_rendered"] = (maps_model, maps_prod, maps_fh)
        cur = st.session_state.get("maps_rendered")
        if cur:
            cm, cp, cf = cur
            try:
                with st.spinner(f"Rendering {cm} {MAP_PRODUCTS[cp]['label']} F{cf:03d} (first render ~1 min, then cached)..."):
                    cyc = find_cycle(cm)
                    if cyc is None:
                        st.warning(f"{cm} unavailable right now (NOAA bucket unreachable?).")
                    else:
                        png, meta = render_product_map(cm, cyc, cf, cp)
                        st.image(png, use_container_width=True)
                        st.caption(
                            f"{MAP_PRODUCTS[cp]['desc']} \u00b7 init {meta['cycle']} \u00b7 valid {meta['valid']} \u00b7 "
                            "rendered locally with MetPy from NOAA/NCEP GRIB2."
                        )
            except Exception as exc:
                st.warning(f"Map render failed: {exc}")
        else:
            st.info("Pick a model, map product, and forecast hour, then hit **Render**. "
                    "Each map is rendered locally with MetPy (vorticity, heights, isotachs) "
                    "from NOAA GRIB2 and cached on disk.")

    # --- Section A1: animated model loops + side-by-side comparison
    import streamlit.components.v1 as components
    from data.model_loops import cached_cycle, ensure_loop, loop_bundle

    with st.expander("\U0001f39e\ufe0f Animated model loop", expanded=False):
        lv1, lv2, lv3, lv4 = st.columns([2, 3, 2, 1])
        try:
            loop_model = lv1.selectbox(
                "Model", MAP_MODEL_ORDER,
                format_func=lambda k: _MAP_MODELS[k]["label"], key="loop_model")
            if st.session_state.get("loop_model_prev") != loop_model:
                st.session_state["loop_model_prev"] = loop_model
                st.session_state.pop("loop_prod", None)
            if st.session_state.get("loop_prod") not in PRODUCTS_BY_MODEL[loop_model]:
                st.session_state.pop("loop_prod", None)
            loop_prod = lv2.selectbox(
                "Product", PRODUCTS_BY_MODEL[loop_model],
                format_func=lambda k: MAP_PRODUCTS[k]["label"], key="loop_prod")
            lm_def = _MAP_MODELS[loop_model]
            loop_max = min(lm_def["max_hour"], 48)
            loop_len = lv3.select_slider(
                "Loop length",
                options=list(range(lm_def["hour_step"], loop_max + 1, lm_def["hour_step"])),
                value=min(24, loop_max), key="loop_len")
            speed_ms = lv4.selectbox("Speed", [1200, 800, 500, 300], index=1, key="loop_speed")
        except KeyError:
            loop_model, loop_prod, loop_len, speed_ms = "GFS", "500_vort", 24, 800
            lm_def = _MAP_MODELS[loop_model]
        hours = list(range(lm_def["hour_step"], loop_len + 1, lm_def["hour_step"]))
        ensure_loop(loop_model, loop_prod, hours)
        lb = loop_bundle(loop_model, loop_prod, hours)
        if lb["cycle"] is None:
            st.warning(f"{loop_model} unavailable right now (NOAA/ECMWF bucket unreachable?).")
        elif not lb["frames"]:
            st.info(f"\u23f3 Rendering the loop: 0/{lb['total']} hours ready - frames appear "
                    "here automatically (first hour ~1 min).", icon=None)
        else:
            if lb["ready"] < lb["total"]:
                st.caption(f"\u23f3 {lb['ready']}/{lb['total']} hours rendered - the loop grows automatically.")
            urls = [f["url"] for f in lb["frames"]]
            labels = [f["label"] for f in lb["frames"]]
            import json as _json
            components.html(
                f'''<div style="position:relative;background:#0e1117;border-radius:10px;overflow:hidden">
  <img id="lf" src="{urls[-1]}" style="width:100%;display:block"/>
  <div id="llb" style="position:absolute;top:10px;left:12px;color:#fff;font:600 15px 'Source Sans Pro',sans-serif;
    background:rgba(13,17,26,.78);padding:3px 12px;border-radius:6px">{labels[-1]}</div>
  <div style="position:absolute;top:10px;right:12px;color:#ffd54f;font:700 12px 'Source Sans Pro',sans-serif;
    background:rgba(13,17,26,.78);padding:3px 10px;border-radius:6px">{_MAP_MODELS[loop_model]['label'].split(' (')[0]}</div>
</div>
<script>
const urls = {_json.dumps(urls)};
const labels = {_json.dumps(labels)};
let i = urls.length - 1;
const img = document.getElementById('lf'), lb = document.getElementById('llb');
setInterval(() => {{
  i = (i + 1) % urls.length;
  const pre = new Image();
  pre.onload = () => {{ img.src = urls[i]; lb.textContent = labels[i]; }};
  pre.src = urls[i];
}}, {speed_ms});
</script>''',
                height=430,
            )
            st.caption(f"Init {lb['cycle']:%Y-%m-%d %HZ} \u00b7 valid times on each frame \u00b7 "
                       f"{speed_ms} ms/frame \u00b7 rendered with MetPy from NOAA GRIB2.")

    with st.expander("\U0001f503 Compare models side by side", expanded=False):
        _support = {}
        for _m, _ps in PRODUCTS_BY_MODEL.items():
            if _m in _MAP_MODELS:
                for _p in _ps:
                    _support.setdefault(_p, []).append(_m)
        comp_products = [p for p, ms in _support.items() if len(ms) >= 2]
        cv1, cv2, cv3 = st.columns([3, 3, 2])
        comp_prod = cv1.selectbox(
            "Product", comp_products,
            format_func=lambda k: MAP_PRODUCTS[k]["label"], key="comp_prod")
        avail = [m for m in MAP_MODEL_ORDER if m in _support[comp_prod]]
        comp_models = cv2.multiselect(
            "Models (2-4)", avail,
            default=[m for m in ("RRFS", "HRRR", "GFS") if m in avail][:3],
            max_selections=4, key="comp_models")
        comp_hour = cv3.select_slider("Target hour", options=list(range(0, 49, 3)), value=24,
                                      key="comp_hour")
        if cv2.button("Render comparison", width="stretch", key="comp_go"):
            st.session_state["comp_show"] = (comp_prod, tuple(comp_models), comp_hour)
        cs = st.session_state.get("comp_show")
        if cs:
            cp2, cm2, ch2 = cs
            cols = st.columns(max(1, len(cm2)))
            for col, m in zip(cols, cm2):
                with col:
                    try:
                        mm_def2 = _MAP_MODELS[m]
                        fh_opts2 = [h for h in range(0, mm_def2["max_hour"] + 1, mm_def2["hour_step"])
                                    if h > 0 or m not in ("HREF", "REFS")]  # HREF/REFS start at f01
                        fh2 = min(fh_opts2, key=lambda h: abs(h - ch2))
                        with st.spinner(f"{m} F{fh2:03d}..."):
                            cyc2 = cached_cycle(m)
                            if cyc2 is None:
                                st.warning(f"{m} unavailable right now.")
                                continue
                            png2, meta2 = render_product_map(m, cyc2, fh2, cp2)
                        st.image(png2, use_container_width=True)
                        st.caption(f"**{m}** \u00b7 F{fh2:03d} \u00b7 valid {meta2['valid']}")
                    except Exception as exc:  # noqa: BLE001 - one model failing must not kill the row
                        st.warning(f"{m}: {exc}")
            st.caption("Nearest available forecast hour per model \u00b7 first render of each "
                       "panel can take a couple of minutes, then it's disk-cached.")

    # --- Section A2: Skew-T soundings (RAP 13 km + MetPy)
    st.markdown("**Skew-T / Log-P sounding - RAP 13 km at this location**")
    st.caption(
        "Full isobaric profile decoded from NOAA RAP GRIB2 and plotted with MetPy: "
        "temperature/dewpoint traces, parcel path, CAPE/CIN shading, hodograph-grade winds."
    )
    skew_c1, skew_c2 = st.columns([1, 3])
    skew_fh = skew_c1.slider("Forecast hour", 0, 18, 0, key="skew_fh")
    if skew_c1.button("Render Skew-T", width="stretch", key="skew_go"):
        st.session_state["skew_show"] = True
    if st.session_state.get("skew_show"):
        try:
            with st.spinner("Decoding RAP pressure levels and rendering the Skew-T (~30 s first time)..."):
                snd = cached_sounding(LAT, LON, skew_fh)
            if snd.get("png"):
                st.image(snd["png"], use_container_width=True)
                m2 = snd.get("meta", {})
                st.caption(
                    f"RAP {m2.get('cycle', '')} \u00b7 valid {m2.get('valid', '')} \u00b7 "
                    f"SBCAPE {m2.get('cape', 'n/a')} J/kg \u00b7 SBCIN {m2.get('cin', 'n/a')} J/kg"
                )
            else:
                st.warning(f"Sounding unavailable: {snd.get('error', 'unknown error')}")
        except Exception as exc:  # noqa: BLE001 - soundings are best-effort
            st.warning(f"Sounding failed: {exc}")

    st.divider()

    # --- Section A: future radar outlook (HRRR + NAM to 48 h)
    st.markdown("**Future radar outlook - next 48 hours (HRRR + NAM 3 km)**")
    st.caption(
        "HRRR ~3 km simulated composite reflectivity (REFC) for hours 1-18, extended to "
        "+48 h with the NAM CONUS 3 km nest. Frames render progressively in the background; "
        "charts update as hours finish."
    )
    if not hrrr["labels"]:
        st.warning("HRRR descriptors unavailable (NOAA bucket unreachable?).")
    col_chart, col_info = st.columns([3, 2], gap="large")
    with col_chart:
        _model_charts(hrrr)
    with col_info:
        ready, total = hrrr["ready"], hrrr["total"]
        st.metric("Hours rendered", f"{ready}/{total}")
        st.progress(min(1.0, ready / total) if total else 0.0)
        st.caption("Frame-by-frame status:")
        ready_labels = {f["label"] for f in hrrr["frames"]}
        for lbl in hrrr["labels"]:
            st.markdown(
                ("\u2705" if lbl in ready_labels else "\u23f3") + f" **{lbl}** - "
                + ("rendered" if lbl in ready_labels else "queued/rendering")
            )
        if hrrr["summary"]:
            st.info(f"\U0001f9e0 {hrrr['summary']}", icon=None)

    st.divider()

    # --- Section B: ensemble fan - every model side by side
    st.markdown("**Multi-model comparison - temperature fan chart (next 48 h)**")
    st.caption(
        "HRRR, RAP, NAM, GFS, GEFS ensemble mean, ECMWF Euro, and the NWS National "
        "Blend plotted together, with the GEFS ensemble spread band showing "
        "forecast uncertainty. Tight lines = high confidence; wide = uncertain."
    )
    fan_btn_col, fan_info_col = st.columns([1, 3])
    if fan_btn_col.button("Build fan chart", width="stretch", key="fan_build"):
        st.session_state["fan_show"] = True
    if st.session_state.get("fan_show"):
        try:
            with st.spinner("Reading every model at this location (first build ~1-2 min, then cached)..."):
                fan = cached_fan(LAT, LON)
            if not fan["models"]:
                st.warning("No model data available right now.")
            else:
                import altair as alt

                frames = []
                for series in fan["models"]:
                    df = pd.DataFrame(series["points"])
                    if df.empty:
                        continue
                    df["t"] = pd.to_datetime(df["t"])
                    df["Model"] = series["model_key"]
                    df["value"] = df["v"]
                    frames.append(df[["t", "value", "Model"]])
                if fan["spread"]:
                    ds = pd.DataFrame(fan["spread"]["points"])
                    if not ds.empty:
                        base = pd.DataFrame(fan["models"][0]["points"])
                        base["t"] = pd.to_datetime(base["t"])
                        ds = ds.rename(columns={"v": "delta"})
                        ds["t"] = pd.to_datetime(ds["t"])
                        merged = base[["t", "v"]].merge(ds[["t", "delta"]], on="t", how="inner")
                        if not merged.empty:
                            spread_frames = pd.DataFrame({
                                "t": pd.concat([merged["t"], merged["t"][::-1]]),
                                "value": pd.concat([merged["v"] + merged["delta"],
                                                    merged["v"] - merged["delta"]]),
                                "Model": "GEFS spread",
                            })
                            frames.append(spread_frames)
                if frames:
                    all_df = pd.concat(frames, ignore_index=True)
                    spread_band = alt.Chart(all_df[all_df["Model"] == "GEFS spread"]).mark_area(
                        opacity=0.25, color="#888888"
                    ).encode(x="t:T", y="value:Q")
                    model_lines = alt.Chart(all_df[all_df["Model"] != "GEFS spread"]).mark_line(
                        point=True
                    ).encode(
                        x=alt.X("t:T", title="Valid time (UTC)"),
                        y=alt.Y("value:Q", title="Temperature (\u00b0F)"),
                        color="Model:N",
                        tooltip=["Model:N", "value:Q", "t:T"],
                    ).properties(title="Model temperature consensus at " + NAME, height=320)
                    st.altair_chart((spread_band + model_lines).interactive(), use_container_width=True)
                    if fan["errors"]:
                        st.caption("Unavailable this cycle: " + ", ".join(sorted(fan["errors"])) + ".")
                    else:
                        st.caption("All 7 sources fetched successfully this cycle.")
                else:
                    st.warning("Models returned no usable points this cycle.")
        except Exception as exc:  # noqa: BLE001 - fan is best-effort
            st.warning(f"Fan chart failed: {exc}")
    else:
        st.info("The fan chart fetches 7 model runs at once - hit **Build fan chart** to run it.")

    st.divider()

    # --- Section C: model point-series explorer
    st.markdown("**Model data at this location**")
    st.caption(
        "Point forecasts read directly from NOAA/ECMWF AWS open-data GRIB2, RRFS via "
        "NOMADS, and NBM COGs (no keys, no middleman). HRRR/RRFS/RAP hourly, "
        "NAM/GFS 6-hourly cycles, GEFS 3-hourly, Euro 3-hourly to 144 h, "
        "NBM hourly blend of all models."
    )
    from data.models import MODELS

    mx_col, var_col, hr_col, go_col = st.columns([2, 2, 2, 1])
    # widget keys get versioned if stale browser state ever crashes them
    ks = "_v2" if st.session_state.get("model_keys_v2") else ""
    # family-ordered menu: CAM, Regional, Global, Ensemble, Blend
    fam_of = {}
    for fam, names in grouped_model_list():
        for n in names:
            fam_of[n] = fam.split(" -")[0]
    ordered_models = [n for _, names in grouped_model_list() for n in names]
    if set(ordered_models) != set(MODELS.keys()):
        ordered_models = list(MODELS.keys())
    try:
        model_pick = mx_col.selectbox(
            "Model", ordered_models,
            format_func=lambda k: f"{k}  \u2014 {fam_of[k]}" if k in fam_of else k,
            key=f"model_pick{ks}",
        )
        vars_for_model = model_info(model_pick)["vars"]
        var_pick = var_col.selectbox(
            "Variable",
            list(vars_for_model.keys()),
            format_func=lambda k: vars_for_model[k][3],
            key=f"var_pick{ks}",
        )
        m_def = model_info(model_pick)
        horizon = hr_col.slider(
            "Hours ahead", 3, m_def["max_hours"],
            value=min(m_def["max_hours"], 24), step=1, key=f"horizon_pick{ks}",
        )
    except KeyError:
        # stale widget state replayed by the browser - bump to fresh keys and rerun once
        if not st.session_state.get("model_keys_v2"):
            st.session_state.model_keys_v2 = True
            st.rerun()
        model_pick, var_pick, horizon = "GFS", "temp", 24
    go = go_col.button("Fetch", use_container_width=True, key="model_fetch")

    if go or f"series_{model_pick}_{var_pick}_{horizon}" in st.session_state:
        with st.spinner(f"Reading {model_pick} GRIB2 from NOAA..."):
            series = cached_series(model_pick, var_pick, LAT, LON, horizon)
        st.session_state[f"series_{model_pick}_{var_pick}_{horizon}"] = series
        if "error" in series:
            st.warning(f"{model_pick}: {series['error']}")
        else:
            import altair as alt

            df = pd.DataFrame(series["points"])
            df["t"] = pd.to_datetime(df["t"])
            ycol = "value"
            df[ycol] = df["v"]
            chart = (
                alt.Chart(df)
                .mark_line(point=True, color="#2b80ff")
                .encode(
                    x=alt.X("t:T", title="Valid time (UTC)"),
                    y=alt.Y(f"{ycol}:Q", title=series["unit"]),
                )
                .properties(
                    title=f"{series['model']} - {series['variable']} at {NAME}",
                    height=280,
                )
            )
            if var_pick == "temp":
                freezing = alt.Chart(pd.DataFrame({"y": [32]})).mark_rule(
                    color="#7aa6ff", strokeDash=[4, 4]
                ).encode(y="y:Q")
                chart = chart + freezing
            st.altair_chart(chart, use_container_width=True)
            first, last = series["points"][0], series["points"][-1]
            peak = max(series["points"], key=lambda p: p["v"])
            st.caption(
                f"Cycle {series['cycle']} \u00b7 {len(series['points'])} steps \u00b7 "
                f"start {first['v']}{series['unit']} \u2192 end {last['v']}{series['unit']} \u00b7 "
                f"peak {peak['v']}{series['unit']} at {peak['t']}"
            )

# ============================================================ Tab 4: National
with tabs[4]:
    st.subheader("National centers - SPC outlooks, NHC tropics, WPC charts")

    storm_alerts = []
    for s in nhc:
        for ft in s["features"]:
            storm_alerts.append({
                "event": ft.get("name") or f'{s.get("classification", "")} {s.get("name", "")}'.strip(),
                "severity": "Severe",
                "areaDesc": f'{s.get("name", "Tropical")} \u2014 {(ft.get("desc") or "")[:140]}',
                "expires": None,
                "geometry": ft["geometry"],
            })

    n_left, n_right = st.columns([3, 2], gap="large")
    with n_left:
        nat_product = st.radio(
            "National map product",
            ["SPC Outlook", "WPC QPF (rain)", "WPC Hazards"],
            horizontal=True,
            key="nat_product_pick",
        )
        if nat_product == "SPC Outlook":
            spc_day_pick = st.radio(
                "SPC convective outlook",
                ["Day 1", "Day 2", "Day 3", "Day 1 Hail", "Day 1 Tornado"],
                horizontal=True,
                key="nat_spc_day",
            )
            spc_key = {"Day 1": "day1", "Day 2": "day2", "Day 3": "day3",
                       "Day 1 Hail": "day1_hail", "Day 1 Tornado": "day1_torn"}[spc_day_pick]
            nat_feats = (spc.get(spc_key) or {}).get("features", [])
        else:
            nat_feats = []
        wpc_polys = []
        wpc_overlays = []
        if nat_product == "WPC QPF (rain)":
            qpf_day = st.select_slider("QPF period", ["Day 1", "Day 2", "Day 3"], value="Day 1", key="nat_qpf_day")
            wpc_polys = cached_wpc_qpf(["Day 1", "Day 2", "Day 3"].index(qpf_day) + 1)["features"]
        elif nat_product == "WPC Hazards":
            wpc_overlays = cached_wpc_sigwx()
        map_result = animated_radar(
            LAT, LON,
            past_frames=past_frames,
            future_frames=[],
            alerts_geojson=alert_geoms + storm_alerts,
            satellite_frames=[],
            nws_frames=nws_radar,
            future_pending=False,
            threat=level,
            threat_score=overall,
            ai_summary=None,
            auto_play=False,
            initial_mode="radar",
            zoom=4,
            height=520,
            key="national_map",
            wpc_polygons=wpc_polys,
            wpc_overlays=wpc_overlays,
            spc_features=nat_feats,
            mapbox_token=MB,
            map_options=MAP_OPTS,
        )
        handle_map_click(map_result)
        st.caption(
            "SPC outlook polygons (official category colors) \u00b7 NHC cone of uncertainty + "
            "forecast track for every active storm \u00b7 NWS watches/warnings."
        )
    with n_right:
        st.subheader("Active tropical systems")
        if nhc:
            for s in nhc:
                st.markdown(
                    f"**{s.get('classification', '')} {s.get('name', '')}** \u00b7 "
                    f"{s.get('intensity', '')} kt \u00b7 {s.get('pressure', '')} mb \u00b7 "
                    f"moving {s.get('movement', '')}"
                )
                st.caption(f"{s.get('lat', '')}, {s.get('lon', '')} \u00b7 advisory {s.get('lastUpdate', '')}")
                if s.get("advisoryUrl"):
                    st.markdown(f"[Public advisory]({s['advisoryUrl']})")
        else:
            st.info("No active tropical storms right now.")

        st.subheader("WPC forecast graphics")
        wpc_pick = st.selectbox(
            "Chart", wpc_catalog(),
            format_func=lambda w: w["title"],
            key="nat_wpc_pick",
        )
        try:
            wr = requests.get(wpc_pick["url"], headers={"User-Agent": "tennessee-weather-network/1.0"}, timeout=20)
            if wr.ok:
                st.image(wr.content, caption=wpc_pick["desc"], use_container_width=True)
            else:
                st.warning(f"WPC image unavailable (HTTP {wr.status_code}).")
        except requests.RequestException as exc:
            st.warning(f"WPC image failed: {exc}")

# ============================================================ Tab 5: Forecast & Alerts
with tabs[5]:
    fc_left, fc_right = st.columns(2, gap="large")
    with fc_left:
        st.subheader("Next 24 hours")
        hourly = cached_hourly(LAT, LON)
        if hourly:
            rows = []
            for period in hourly[:12]:
                t = pd.to_datetime(period["startTime"])
                rows.append({
                    "Hour": t.strftime("%I %p").lstrip("0"),
                    "Temp (\u00b0F)": period["temperature"],
                    "Wind (mph)": parse_wind_mph(period.get("windSpeed")),
                    "Sky": period["shortForecast"],
                    "Precip %": period.get("probabilityOfPrecipitation", {}).get("value") or 0,
                })
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        else:
            st.caption("No hourly forecast available for this location.")

        st.subheader("5-day outlook")
        if forecast:
            rows = []
            seen = set()
            for period in forecast:
                day = period.get("name", "")
                if day in seen or "Night" in day:
                    continue
                seen.add(day)
                rows.append({
                    "Day": day,
                    "Hi/Low (\u00b0F)": period["temperature"],
                    "Wind (mph)": parse_wind_mph(period.get("windSpeed")),
                    "Outlook": period["shortForecast"],
                })
                if len(rows) == 5:
                    break
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        else:
            st.warning("Forecast unavailable for this location.")
    with fc_right:
        render_alerts_column()

# ============================================================ Tab 6: Current
with tabs[6]:
    st.subheader("Current conditions")
    if current:
        temp_f = current.get("temperature", {}).get("value")
        st.metric("Temperature", f"{temp_f * 9 / 5 + 32:.0f} \u00b0F" if temp_f is not None else "n/a")
        col1, col2, col3, col4 = st.columns(4)
        wind_kmh = current.get("windSpeed", {}).get("value")
        col1.metric("Wind", f"{wind_kmh * 0.621371:.0f} mph" if wind_kmh is not None else "n/a")
        col2.metric("Humidity", f"{current.get('relativeHumidity', {}).get('value') or 0:.0f}%")
        col3.metric("Dewpoint", f"{(current.get('dewpoint', {}).get('value') or 0) * 9 / 5 + 32:.0f} \u00b0F")
        col4.metric("Wind direction", f"{current.get('windDirection', {}).get('value') or '-'}\u00b0")
        st.caption(current.get("textDescription", ""))
    else:
        st.info("No nearby observation station reporting right now.")

    st.subheader("Nearby station observations")
    st.caption(
        "Latest METAR/AWOS reports from NWS stations near this location (sorted by distance). "
        "Also on the map: Radar & AI \u2192 layer menu \u2192 Station Observations."
    )
    if obs_stations:
        st.dataframe(
            pd.DataFrame([
                {"Station": o["id"], "Temp (\u00b0F)": o["tempF"], "Dew (\u00b0F)": o["dewF"],
                 "Wind": ((f"{o['windDir']} {o['windMph']}".strip()
                           + (f" G{o['gustMph']}" if o["gustMph"] else ""))
                          if o["windMph"] is not None else "-"),
                 "RH %": o["rh"], "Conditions": o["desc"], "Obs at": o["time"]}
                for o in obs_stations
            ]).set_index("Station"),
            use_container_width=True, height=400,
        )
    else:
        st.caption("No station observations available right now.")

    # ---------------- East Tennessee city board: observations + forecast ----
    st.subheader("East Tennessee cities - observations & forecast")
    st.caption(
        "Latest NWS station report for each city (nearest METAR/AWOS/ASOS), "
        "plus the NWS point forecast for the next periods. Refreshes every 10 min."
    )
    etn_obs = cached_city_obs()
    etn_fc = {f["city"]: f["periods"] for f in cached_city_forecasts()}
    if etn_obs:
        st.dataframe(
            pd.DataFrame([{
                "City": o["city"],
                "Temp (\u00b0F)": o["tempF"] if o["tempF"] is not None else "-",
                "Dew (\u00b0F)": o["dewF"] if o["dewF"] is not None else "-",
                "Wind": ((f"{o['windDir']} {o['windMph']}".strip()
                          + (f" G{o['gustMph']}" if o["gustMph"] else ""))
                         if o["windMph"] is not None else "-"),
                "RH %": o["rh"] if o["rh"] is not None else "-",
                "Conditions": o["desc"] or "-",
                "Station": f"{o['station']} ({o['miles']} mi)" if o["miles"] is not None else "-",
                "Obs at": o["time"] or "-",
            } for o in etn_obs]).set_index("City"),
            use_container_width=True, height=430,
        )
        # per-city forecast grid: next two periods side by side
        rows = []
        for o in etn_obs:
            periods = etn_fc.get(o["city"], [])
            p1 = periods[0] if periods else {}
            p2 = periods[1] if len(periods) > 1 else {}
            rows.append({
                "City": o["city"],
                "Now": p1.get("name", "-"),
                "Temp": (f"{p1.get('tempF')} \u00b0F" if p1.get("tempF") is not None else "-"),
                "Sky": p1.get("short", "-") or "-",
                "Next": p2.get("name", "-"),
                "Next temp": (f"{p2.get('tempF')} \u00b0F" if p2.get("tempF") is not None else "-"),
                "Next sky": p2.get("short", "-") or "-",
            })
        st.dataframe(pd.DataFrame(rows).set_index("City"), use_container_width=True, height=430)
        # full 4-period detail for a chosen city
        pick = st.selectbox("City forecast detail", [o["city"] for o in etn_obs])
        detail = etn_fc.get(pick, [])
        if detail:
            cols = st.columns(len(detail))
            for c, p in zip(cols, detail):
                c.metric(p.get("name", ""),
                         f"{p.get('tempF')} \u00b0F" if p.get("tempF") is not None else "-",
                         p.get("wind", ""))
                c.caption(p.get("short", ""))
    else:
        st.caption("No East TN city observations available right now.")

    st.subheader("Hourly temperature & precip (next 24 h)")
    hourly = cached_hourly(LAT, LON)
    if hourly:
        hrs = pd.DataFrame([
            {
                "time": pd.to_datetime(p["startTime"]),
                "Temp (\u00b0F)": p["temperature"],
                "Precip %": p.get("probabilityOfPrecipitation", {}).get("value") or 0,
            }
            for p in hourly[:24]
        ]).set_index("time")
        st.bar_chart(hrs[["Temp (\u00b0F)", "Precip %"]])
    else:
        st.caption("No hourly data available.")

st.divider()
st.caption(
    "Data: National Weather Service (api.weather.gov) \u00b7 Radar: RainViewer NEXRAD + NOAA MRMS "
    "mosaic \u00b7 Satellite: GOES-19 ABI bands decoded locally + NASA GIBS GeoColor \u00b7 "
    "Models: NOAA HRRR, RRFS, RAP, NAM, GFS, GEFS, CFS \u00b7 ECMWF IFS (Euro) \u00b7 NWS NBM \u00b7 "
    "Skew-T: RAP 13 km + MetPy \u00b7 National: SPC / NHC / WPC \u00b7 "
    "Search: OpenStreetMap Nominatim. All sources free - no API keys."
)
st.markdown(
    f"\U0001f5f3\ufe0f [Tennessee Weather Network on Facebook]({config.PAGE_URL})",
)

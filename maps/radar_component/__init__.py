"""Weather center map: a Streamlit custom component wrapping Leaflet with:
- Past radar: RainViewer NEXRAD composite tiles (~2 h of frames)
- Satellite: RainViewer IR or NASA GIBS GOES-East GeoColor tiles
- Future radar: HRRR simulated reflectivity frames rendered server-side,
  loaded lazily by URL as the background renderer finishes them
- AI storm-cell tracking: detected cells + projected 30/60-min tracks
- NWS alert polygons, threat badge, location marker, map-click picking
"""
import pathlib

import streamlit.components.v1 as components

_COMPONENT_DIR = pathlib.Path(__file__).parent / "radar_component"

_component_func = components.declare_component(
    "animated_radar",
    path=str(_COMPONENT_DIR),
)


def animated_radar(
    lat,
    lon,
    past_frames,
    future_frames,
    alerts_geojson,
    satellite_frames=None,
    sat_mode="gibs",
    sat_modes=None,
    obs_stations=None,
    map_options=None,
    mrms_frames=None,
    nws_frames=None,
    future_pending=False,
    threat=None,
    threat_score=None,
    ai_summary=None,
    auto_play=True,
    initial_mode="radar",
    height=560,
    zoom=7,
    key=None,
    severe=None,
    spc_features=None,
    mapbox_token=None,
    wpc_polygons=None,
    wpc_overlays=None,
):
    """Render the animated weather-center map.

    past_frames:      [{'time': unix_s, 'path': '/v2/radar/<hash>', 'label': '-30m'}]
    satellite_frames: tile frames ({'label','path'|'time'}) or rendered band
                      overlays ({'label','pngUrl','bounds'})
    sat_mode:         which satellite product satellite_frames is for
                      ('gibs' | 'ir' | 'wvh' | 'wvm' | 'wvl' | 'c01'..'c16')
    sat_modes:        {mode_key: label} for the in-map satellite picker
                      (GeoColor + every ABI band the backend can render)
    obs_stations:     NWS station observations [{'id','name','lat','lon','tempF',
                      'dewF','windDir','windMph','gustMph','rh','desc','time'}]
                      - adds the 'Station Observations' in-map mode
    map_options:      {'overlayOpacity': 0-1, 'showAlerts': bool} display options
    mrms_frames:      official MRMS mosaic overlays [{'id','label','time',
                      'pngUrl','bounds'}]
    nws_frames:       official NWS WMS mosaic overlays (same shape as mrms_frames)
    future_frames:    [{'id', 'label', 'time', 'model', 'pngUrl' (or None while
                        pending), 'bounds': [S,W,N,E], 'cells', 'tracks'}]
    alerts_geojson:   list of {'event', 'severity', 'areaDesc', 'geometry', 'expires'} dicts
    severe:           {'hailPoints': [...], 'rotPoints': [...]} or None
    spc_features:     SPC outlook polygons [{'label','label2','fill','geometry'}]
    mapbox_token:     Mapbox access token - enables Mapbox basemaps when set
    wpc_polygons:     WPC map features [{'name','fill','color','geometry'}] (QPF contours)
    wpc_overlays:     WPC hazard images [{'name','href','bounds'}] (SigWx day overlays)
    """
    payload = {
        "lat": lat,
        "lon": lon,
        "zoom": zoom,
        "pastFrames": past_frames,
        "satelliteFrames": satellite_frames or [],
        "satMode": sat_mode,
        "satModes": sat_modes or {},
        "obsStations": obs_stations or [],
        "mapOptions": map_options or {},
        "mrmsFrames": mrms_frames or [],
        "nwsFrames": nws_frames or [],
        "futureFrames": future_frames,
        "futurePending": future_pending,
        "alerts": alerts_geojson,
        "threat": threat,
        "threatScore": threat_score,
        "aiSummary": ai_summary,
        "autoPlay": auto_play,
        "initialMode": initial_mode,
        "severe": severe,
        "spcPolygons": spc_features or [],
        "mapboxToken": mapbox_token or "",
        "wpcPolygons": wpc_polygons or [],
        "wpcOverlays": wpc_overlays or [],
    }
    return _component_func(
        **payload,
        height=height,
        key=key,
        default=None,
    )

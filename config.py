# config.py

# Branding
PAGE_NAME = "Tennessee Weather Network"
PAGE_URL = "https://www.facebook.com/tennesseeweathernetwork"
# Public site URL (GitHub Pages). Used as the share target when the site is
# viewed locally - sharing a localhost link on Facebook is useless.
PUBLIC_SITE_URL = "https://rpleasant12.github.io/http-localhost-8765-/"

# Initial location: Greeneville, East Tennessee
DEFAULT_LOCATION_NAME = "Greeneville, TN"
LATITUDE = 36.1627
LONGITUDE = -82.8332

# RainViewer tiles URL template
RAINVIEWER_TILE_URL = "https://tilecache.rainviewer.com/weather-tile/{z}/{x}/{y}/0/0/1.png"

# NWS API base
NWS_API_URL = "https://api.weather.gov/alerts/active"
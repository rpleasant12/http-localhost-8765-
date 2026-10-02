# config.py

# Branding
PAGE_NAME = "Tennessee Weather Network"
PAGE_URL = "https://www.facebook.com/tennesseeweathernetwork"
# Public site URL (GitHub Pages). Used as the share target when the site is
# viewed locally - sharing a localhost link on Facebook is useless.
PUBLIC_SITE_URL = "https://rpleasant12.github.io/http-localhost-8765-/"

# Visitor analytics (optional). Set to your GoatCounter site URL, e.g.
# "https://MYSITE.goatcounter.com" (free for non-commercial sites, no
# cookies, no personal data, localhost views ignored). Empty string =
# no tracking script is emitted on any page.
ANALYTICS_SITE = ""

# Initial location: Greeneville, East Tennessee
DEFAULT_LOCATION_NAME = "Greeneville, TN"
LATITUDE = 36.1627
LONGITUDE = -82.8332

# RainViewer tiles URL template
RAINVIEWER_TILE_URL = "https://tilecache.rainviewer.com/weather-tile/{z}/{x}/{y}/0/0/1.png"

# NWS API base
NWS_API_URL = "https://api.weather.gov/alerts/active"
# ---------------------------------------------------------------- membership
# Cloudflare Worker backing accounts/billing/members-only content. Empty =
# member features dormant (no member.js, no strip, premium pages show a
# setup notice). NEVER put secrets here - only the public worker origin.
# Flip to the deployed worker URL (https://tnwn-members.<your-subdomain>.workers.dev)
# AFTER 'wrangler deploy' - see DEPLOY_MEMBERSHIP.md.
MEMBER_WORKER_URL = ""
# Free-vs-premium content policy mirrored by the worker proxy (docs only).
PREMIUM_PRICE_LABEL = "$4.99/mo"

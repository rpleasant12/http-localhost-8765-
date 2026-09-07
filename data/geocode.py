"""Location search via the public Nominatim (OpenStreetMap) API."""
import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "freebuff-weather-app/1.0 (Streamlit demo)"}


def search_location(query):
    """Return a list of {name, lat, lon} results for a free-text place search."""
    if not query or not query.strip():
        return []
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "q": query.strip(),
                "format": "jsonv2",
                "limit": 5,
                "addressdetails": 0,
            },
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []
    results = []
    for item in resp.json():
        try:
            results.append({
                "name": item.get("display_name", "Unknown"),
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
            })
        except (KeyError, ValueError):
            continue
    return results

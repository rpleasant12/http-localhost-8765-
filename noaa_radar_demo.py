import streamlit as st
import requests
import rasterio
import numpy as np
import matplotlib.pyplot as plt
import io
import datetime
import time

st.title("🌩️ NOAA NEXRAD Level 3 Radar with Futurecast")
st.write("Real-time radar data fetched from NOAA AWS. This demo fetches the latest images and animates them.")

# NOAA NEXRAD Level 3 radar image URL (latest)
# Replace with actual URL pattern for recent images
# For the demo, we'll fetch a few recent images from NOAA AWS

# NOAA Level 3 radar images are stored in a predictable URL pattern
# Example: https://noaa-nexrad-level3.s3.amazonaws.com/20191120/00/level3_Radar_Reflectivity_20191120_0000.png

# For simplicity, let's list recent image timestamps (latest 3 hours)
# Here we generate URLs for the last 3 hours
def get_radar_urls():
    base_url = "https://noaa-nexrad-level3.s3.amazonaws.com"
    today = datetime.datetime.utcnow()
    urls = []
    for hours_ago in range(0, 3):
        dt = today - datetime.timedelta(hours=hours_ago)
        date_str = dt.strftime('%Y%m%d')
        hour_str = dt.strftime('%H')
        filename = f"level3_Radar_Reflectivity_{date_str}_{hour_str}00.png"
        url = f"{base_url}/{date_str}/{hour_str}/{filename}"
        urls.append(url)
    return urls

# Fetch images from NOAA
def fetch_image(url):
    try:
        response = requests.get(url)
        if response.status_code == 200:
            return plt.imread(io.BytesIO(response.content))
        else:
            return None
    except Exception as e:
        return None

urls = get_radar_urls()
images = [fetch_image(url) for url in urls]

# Filter out failed fetches
images = [img for img in images if img is not None]

if not images:
    st.error("Failed to fetch radar images. Check URLs or internet connection.")
else:
    # Animate images
    st.write("Recent Radar Reflectivity")
    for img in images:
        st.image(img, use_column_width=True)
        time.sleep(1)  # pause for animation effect

# Optional: Add a slider to select frame manually
if len(images) > 1:
    selected_idx = st.slider("Select radar frame", 0, len(images)-1, len(images)-1)
    st.image(images[selected_idx], caption=f"Radar Frame {selected_idx+1}", use_column_width=True)

st.write("Note: For real-time, automate URL fetching and update periodically.")
import requests
import gzip
import io
import xarray as xr
import matplotlib.pyplot as plt
import datetime
import re

# NOAA AWS bucket URL (list of files)
bucket_url = "https://noaa-nexrad-level2.s3.amazonaws.com/"

# Function to list files from the bucket
def list_files():
    # NOAA's AWS buckets support listing via HTML
    response = requests.get(bucket_url)
    if response.status_code != 200:
        print("Failed to access NOAA bucket")
        return []

    # Extract file links using regex
    pattern = r'href="([^"/]+\.gz)"'
    files = re.findall(pattern, response.text)
    # Prepend URL
    full_urls = [f"{bucket_url}{file}" for file in files]
    # Sort by date (latest first)
    full_urls_sorted = sorted(full_urls, reverse=True)
    return full_urls_sorted

# Download latest file
def download_latest():
    files = list_files()
    if not files:
        print("No files found.")
        return None
    latest_url = files[0]
    print(f"Downloading: {latest_url}")
    response = requests.get(latest_url)
    if response.status_code != 200:
        print("Failed to download file.")
        return None

    # Decompress gzip
    with gzip.GzipFile(fileobj=io.BytesIO(response.content)) as f:
        data_bytes = f.read()

    filename = 'latest_radar.nc'
    with open(filename, 'wb') as f:
        f.write(data_bytes)
    return filename

# Run in Streamlit
import streamlit as st

st.title("NOAA NEXRAD Level 2 Data from AWS")
st.write("Fetching latest radar data...")

filename = download_latest()
if filename:
    try:
        ds = xr.open_dataset(filename, engine='h5netcdf')
        st.write("Dataset info")
        st.write(ds)

        # Find reflectivity variable
        reflect_vars = [v for v in ds.variables if 'reflectivity' in v.lower()]
        if reflect_vars:
            vname = reflect_vars[0]
            refl = ds[vname]
            if 'height' in refl.dims:
                refl = refl.mean(dim='height')
            plt.figure(figsize=(10,6))
            plt.imshow(refl, origin='lower', cmap='pyart_NWSRef')
            plt.colorbar(label='Reflectivity (dBZ)')
            plt.title("Latest Radar Reflectivity")
            st.pyplot(plt)
        else:
            st.write("Reflectivity variable not found.")
    except Exception as e:
        st.write(f"Error reading dataset: {e}")
else:
    st.write("No data available.")
import streamlit as st
import boto3
import xarray as xr
import numpy as np
import matplotlib.pyplot as plt
import io
import gzip
import datetime

# Initialize S3 client
s3 = boto3.client('s3', region_name='us-east-1')

# Define the bucket
bucket_name = 'noaa-nexrad-level2'

# List recent files from NOAA AWS bucket
def list_latest_files():
    # List objects in the bucket
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=bucket_name)
    files = []

    for page in pages:
        for obj in page.get('Contents', []):
            key = obj['Key']
            # Filter for recent files (e.g., last 24 hours)
            if key.endswith('.gz'):
                files.append(key)
    # Sort files by date in filename
    files_sorted = sorted(files, reverse=True)
    return files_sorted

# Download latest file
def download_latest():
    files = list_latest_files()
    if not files:
        st.error("No files found.")
        return None
    latest_file = files[0]
    s3_response = s3.get_object(Bucket=bucket_name, Key=latest_file)
    gz_content = s3_response['Body'].read()

    # Decompress gzip
    with gzip.GzipFile(fileobj=io.BytesIO(gz_content)) as f:
        data_bytes = f.read()

    # Save to disk or load directly into xarray
    filename = 'latest_radar.nc'
    with open(filename, 'wb') as f:
        f.write(data_bytes)
    return filename

st.title("NOAA NEXRAD Level 2 Radar Data Viewer")
st.write("Fetching the latest radar data from NOAA AWS...")

# Fetch latest data
filename = download_latest()

if filename:
    # Read dataset
    ds = xr.open_dataset(filename, engine='h5netcdf')
    # Explore dataset structure
    st.write("Dataset Info:")
    st.write(ds)

    # Extract reflectivity data (example, depends on dataset structure)
    # The variable name may vary, e.g., 'reflectivity'
    # Here, find the variable containing reflectivity
    var_name = None
    for v in ds.variables:
        if 'reflectivity' in v.lower():
            var_name = v
            break

    if var_name:
        reflectivity = ds[var_name]
        # Average over vertical levels if needed
        if 'height' in reflectivity.dims:
            reflectivity = reflectivity.mean(dim='height')

        # Plot reflectivity
        plt.figure(figsize=(10,6))
        plt.imshow(reflectivity, origin='lower', cmap='pyart_NWSRef')
        plt.colorbar(label='Reflectivity (dBZ)')
        plt.title('Recent Radar Reflectivity')
        st.pyplot(plt)
    else:
        st.error("Reflectivity variable not found in dataset.")
else:
    st.error("Could not fetch radar data.")
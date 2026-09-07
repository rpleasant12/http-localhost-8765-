import streamlit as st
import boto3
import gzip
import io
import xarray as xr
import matplotlib.pyplot as plt
import datetime

# Initialize S3 client
s3 = boto3.client('s3', region_name='us-east-1')

bucket_name = 'noaa-nexrad-level2'

def list_files():
    # List objects in the bucket
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=bucket_name)
    files = []
    for page in pages:
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('.gz'):
                files.append(key)
    # Sort by filename timestamp (latest first)
    files_sorted = sorted(files, reverse=True)
    return files_sorted

def download_latest():
    files = list_files()
    if not files:
        st.error("No files found in the NOAA AWS bucket.")
        return None
    latest_file = files[0]
    s3_response = s3.get_object(Bucket=bucket_name, Key=latest_file)
    gz_content = s3_response['Body'].read()

    # Decompress gzip
    with gzip.GzipFile(fileobj=io.BytesIO(gz_content)) as f:
        data_bytes = f.read()

    # Save to disk
    filename = 'latest_radar.nc'
    with open(filename, 'wb') as f:
        f.write(data_bytes)
    return filename

st.title("NOAA NEXRAD Level 2 Radar Data Viewer")
st.write("Fetching the latest radar data from NOAA AWS...")

filename = download_latest()

if filename:
    try:
        ds = xr.open_dataset(filename, engine='h5netcdf')
        st.write("Dataset info:")
        st.write(ds)

        # Find reflectivity variable
        reflect_vars = [v for v in ds.variables if 'reflectivity' in v.lower()]
        if reflect_vars:
            var_name = reflect_vars[0]
            reflectivity = ds[var_name]
            # Average over height if available
            if 'height' in reflectivity.dims:
                reflectivity = reflectivity.mean(dim='height')
            # Plot
            plt.figure(figsize=(10,6))
            plt.imshow(reflectivity, origin='lower', cmap='pyart_NWSRef')
            plt.colorbar(label='Reflectivity (dBZ)')
            plt.title('Recent Radar Reflectivity')
            st.pyplot(plt)
        else:
            st.error("Reflectivity variable not found.")
    except Exception as e:
        st.error(f"Error reading dataset: {e}")
else:
    st.write("No data fetched.")
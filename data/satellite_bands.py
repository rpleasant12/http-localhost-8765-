"""GOES-19 (GOES-East) single-band satellite imagery, decoded locally.

Source: NOAA's open-data bucket noaa-goes19 (AWS) - ABI L1b RadC CONUS files
(NetCDF4/HDF5), one scan every 5 min per band. No API key.

GINI-style products: raw GINI (the NOAAPort broadcast format for remapped
GOES imagery) has no keyless public HTTP mirror as of 2026 - Unidata's classic
GINI THREDDS collections were retired and the format is dish-only today. What
GINI delivered was the NWS-standard sectorized display, so this module
reproduces that experience from the same live GOES-19 feed:
  'wvg'  = CONUS water vapor in the NOAAPort grayscale display
  'wvfd' = full-disk water vapor (the classic GINI full-disk sector)
both grayscale, dry = dark, moist = bright, like the broadcast product.

Bands (all 16 ABI channels):
  C08  6.2 um  -> water vapor, HIGH layer (upper troposphere)
  C09  6.9 um  -> water vapor, MID layer  (mid troposphere)
  C10  7.3 um  -> water vapor, LOW layer  (lower troposphere)
  C13 10.3 um  -> clean infrared window (cloud tops)
  plus C01-C07, C12, C14-C16 rendered from the L2 Cloud & Moisture
  Imagery product (CMI): pre-calibrated brightness temperature (K)
  for emissive bands and reflectance (0-1) for solar bands.
  Solar channels are day-only - black outside the daylight disk.
  C07 doubles as the fire / hot-spot channel (enhanced hot palette).

GOES-19 L1b files ship without the Planck coefficient attributes (verified:
no planck_* attrs on the file or Rad variable), so the radiance -> BT
conversion computes the Planck coefficients from each band's central
wavelength (GOES-R PUG): fk1 = c1*v^3, fk2 = c2*v with v = 1e4/wave_um.
"""

import datetime as dt
import json
import math
import os
import re
import threading
import time

import numpy as np
import requests


UA = {
    "User-Agent": "tennessee-weather-network/1.0 (local demo)"
}

BUCKET = "https://noaa-goes19.s3.amazonaws.com"


# ---------------------------------------------------------------------------
# BAND DEFINITIONS
# ---------------------------------------------------------------------------

BANDS = {
    "ir": {
        "band": 13,
        "label": "Infrared - Cloud Tops",
        "product": "l1b",
        "kind": "bt",
    },
    "wvh": {
        "band": 8,
        "label": "Water Vapor - High",
        "product": "l1b",
        "kind": "bt",
    },
    "wvm": {
        "band": 9,
        "label": "Water Vapor - Mid",
        "product": "l1b",
        "kind": "bt",
    },
    "wvl": {
        "band": 10,
        "label": "Water Vapor - Low",
        "product": "l1b",
        "kind": "bt",
    },
    "wvg": {
        "band": 8,
        "label": "Water Vapor - GINI Style (CONUS)",
        "product": "l1b",
        "kind": "gini",
    },
    "wvfd": {
        "band": 8,
        "label": "Water Vapor - GINI Style (Full Disk)",
        "product": "l1b",
        "kind": "gini",
        "sector": "F",
        "count": 4,
        "max_px": 1400,
    },
    "c02": {
        "band": 2,
        "label": "Red Visible (day)",
        "product": "l2",
        "kind": "refl",
        "count": 5,
    },
    "c01": {
        "band": 1,
        "label": "Blue Visible (day)",
        "product": "l2",
        "kind": "refl",
    },
    "c03": {
        "band": 3,
        "label": "Near-IR Veggie (day)",
        "product": "l2",
        "kind": "refl",
    },
    "c04": {
        "band": 4,
        "label": "Cirrus (day)",
        "product": "l2",
        "kind": "refl",
    },
    "c05": {
        "band": 5,
        "label": "Snow / Ice (day)",
        "product": "l2",
        "kind": "refl",
    },
    "c06": {
        "band": 6,
        "label": "Cloud Particle Size (day)",
        "product": "l2",
        "kind": "refl",
    },
    "c07": {
        "band": 7,
        "label": "Shortwave IR - Fire / Hot Spots",
        "product": "l2",
        "kind": "bt",
    },
    "c12": {
        "band": 12,
        "label": "Ozone Channel",
        "product": "l2",
        "kind": "bt",
    },
    "c14": {
        "band": 14,
        "label": "Infrared Longwave (11.2)",
        "product": "l2",
        "kind": "bt",
    },
    "c15": {
        "band": 15,
        "label": "Dirty IR (12.3)",
        "product": "l2",
        "kind": "bt",
    },
    "c16": {
        "band": 16,
        "label": "CO2 - Cloud Top Height",
        "product": "l2",
        "kind": "bt",
    },
}


FRAME_COUNT = 8


# ---------------------------------------------------------------------------
# PLANCK COEFFICIENTS
# ---------------------------------------------------------------------------

_BAND_WAVELENGTH = {
    8: 6.17,
    9: 6.94,
    10: 7.42,
    13: 10.33,
}

_C1 = 1.191042972e-5
_C2 = 1.43877735

PLANCK = {
    b: (
        _C1 * (1e4 / wl) ** 3,
        _C2 * (1e4 / wl),
        1.0,
        0.0,
    )
    for b, wl in _BAND_WAVELENGTH.items()
}


# ---------------------------------------------------------------------------
# COLOR PALETTES
# ---------------------------------------------------------------------------

_IR_STOPS = [
    (32, 0, 0, 0, 0),
    (25, 176, 190, 197, 70),
    (10, 144, 164, 174, 110),
    (-10, 96, 125, 139, 130),
    (-30, 255, 255, 255, 175),
    (-45, 255, 255, 255, 215),
    (-55, 255, 213, 79, 230),
    (-62, 255, 143, 0, 240),
    (-70, 229, 57, 53, 245),
    (-80, 244, 143, 177, 250),
]

_WV_STOPS = [
    (35, 0, 0, 0, 0),
    (20, 237, 222, 176, 90),
    (0, 245, 245, 245, 140),
    (-20, 127, 200, 248, 175),
    (-40, 41, 98, 255, 210),
    (-60, 124, 77, 255, 240),
    (-80, 240, 80, 220, 250),
]

_GINI_GRAY_STOPS = [
    (40, 0, 0, 0, 235),
    (10, 30, 30, 30, 235),
    (-10, 80, 80, 80, 235),
    (-25, 140, 140, 140, 235),
    (-40, 200, 200, 200, 235),
    (-55, 245, 245, 245, 235),
    (-70, 255, 255, 255, 235),
    (-90, 255, 255, 255, 235),
]

_GRAY_STOPS = [
    (100, 255, 255, 255, 235),
    (75, 248, 248, 250, 215),
    (45, 214, 218, 224, 175),
    (20, 128, 134, 142, 120),
    (8, 56, 60, 66, 70),
    (0, 0, 0, 0, 0),
]

_FIRE_STOPS = [
    (450, 255, 255, 220, 250),
    (300, 255, 242, 120, 250),
    (150, 255, 160, 60, 240),
    (90, 255, 80, 40, 225),
    (60, 220, 45, 45, 190),
    (30, 0, 0, 0, 0),
    (-40, 220, 228, 240, 165),
    (-70, 255, 255, 255, 215),
]


def _expand(stops, top=45):
    """Interpolate RGBA stops to a 1-unit lookup covering -90..top."""
    temps = np.array(
        [s[0] for s in stops],
        dtype=float,
    )

    rgba = np.array(
        [s[1:] for s in stops],
        dtype=float,
    )

    grid = np.arange(
        top,
        -91,
        -1,
        dtype=float,
    )

    out = np.zeros(
        (len(grid), 4),
        dtype=np.uint8,
    )

    for c in range(4):
        out[:, c] = np.clip(
            np.interp(
                grid,
                temps[::-1],
                rgba[::-1, c],
            ),
            0,
            255,
        )

    return grid, out


def _palette_for(key, band_num, kind):
    if kind == "gini":
        return _expand(
            _GINI_GRAY_STOPS,
            top=50,
        )

    if kind == "refl":
        return _expand(
            _GRAY_STOPS,
            top=100,
        )

    if band_num == 7:
        return _expand(
            _FIRE_STOPS,
            top=450,
        )

    if band_num in (8, 9, 10) or key == "c12":
        return _expand(_WV_STOPS)

    return _expand(_IR_STOPS)


_PALETTES = {
    k: _palette_for(
        k,
        v["band"],
        v.get("kind", "bt"),
    )
    for k, v in BANDS.items()
}


# ---------------------------------------------------------------------------
# PIXEL -> RGBA
# ---------------------------------------------------------------------------

def _to_rgba(band_key, vals):
    """Convert calibrated satellite values to RGBA without NaN cast warnings."""

    grid, lut = _PALETTES[band_key]

    # Determine valid pixels BEFORE converting values to integer indexes.
    valid = np.isfinite(vals)

    # Replace NaN/inf values with a safe value temporarily.
    # These pixels are later left transparent because of the valid mask.
    safe_vals = np.where(
        valid,
        vals,
        grid[0],
    )

    idx = np.clip(
        np.round(
            grid[0] - safe_vals
        ).astype(np.int32),
        0,
        len(grid) - 1,
    )

    rgba = np.zeros(
        vals.shape + (4,),
        dtype=np.uint8,
    )

    rgba[valid] = lut[idx[valid]]

    return rgba


# ---------------------------------------------------------------------------
# DISCOVERY
# ---------------------------------------------------------------------------

_LIST_CACHE = {}


def _latest_scans(
    ts,
    band_num,
    count=FRAME_COUNT,
    product="l1b",
    root=None,
):
    """Latest count scan keys for a band, oldest first."""

    root = root or (
        "ABI-L2-CMIPC"
        if product == "l2"
        else "ABI-L1b-RadC"
    )

    out = []

    hour = ts.replace(
        minute=0,
        second=0,
        microsecond=0,
    )

    for _ in range(3):

        ck = (
            hour.strftime("%Y%j%H"),
            band_num,
            product,
            root,
        )

        if (
            ck not in _LIST_CACHE
            or time.time() - _LIST_CACHE[ck][1] > 120
        ):
            prefix = (
                f"{root}/{hour:%Y}/{hour:%j}/{hour:%H}/"
            )

            url = (
                f"{BUCKET}/"
                f"?list-type=2"
                f"&prefix={prefix}"
                f"&max-keys=400"
            )

            try:
                r = requests.get(
                    url,
                    headers=UA,
                    timeout=20,
                )

                keys = [
                    k
                    for k in re.findall(
                        r"<Key>([^<]+)</Key>",
                        r.text,
                    )
                    if f"-M6C{band_num:02d}_" in k
                ]

            except requests.RequestException:
                keys = []

            _LIST_CACHE[ck] = (
                keys,
                time.time(),
            )

        out.extend(
            _LIST_CACHE[ck][0]
        )

        hour -= dt.timedelta(hours=1)

        if len(out) >= count:
            break

    out.sort()

    return out[-count:]


def get_band_frames(band_key):
    """Return satellite frame descriptors, oldest first."""

    b = BANDS[band_key]

    now = dt.datetime.now(
        dt.timezone.utc
    )

    frames = []

    count = b.get(
        "count",
        FRAME_COUNT,
    )

    sector = b.get(
        "sector",
        "C",
    )

    root = (
        f"ABI-L1b-Rad{sector}"
        if b.get("product", "l1b") == "l1b"
        else None
    )

    for key in _latest_scans(
        now,
        b["band"],
        count=count,
        product=b.get(
            "product",
            "l1b",
        ),
        root=root,
    ):

        m = re.search(
            r"_s(\d{14})_",
            key,
        )

        if not m:
            continue

        # GOES filenames contain a 14-digit scan token:
        #
        # YYYYDDDHHMMSSx
        #
        # The final digit is the GOES sub-second component.
        tok = m.group(1)

        # The actual timestamp is the first 13 digits:
        #
        # YYYYDDDHHMMSS
        #
        scan_token = tok[:13]

        try:
            scan = dt.datetime.strptime(
                scan_token,
                "%Y%j%H%M%S",
            ).replace(
                tzinfo=dt.timezone.utc
            )
        except ValueError:
            continue

        mins = int(
            max(
                0,
                (
                    now - scan
                ).total_seconds()
                // 60,
            )
        )

        frames.append(
            {
                # IMPORTANT:
                # Use the real 13-digit timestamp rather than the
                # 14-digit GOES filename token.
                "id": f"{band_key}_{scan_token}",

                "label": (
                    "Now"
                    if mins < 6
                    else f"-{mins}m"
                ),

                "time": scan.strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),

                # Keep the original 14-digit token for the S3 source.
                "scan": tok,

                "key": key,
            }
        )

    return frames


# ---------------------------------------------------------------------------
# RENDERING
# ---------------------------------------------------------------------------

FRAME_DIR = os.path.join(
    "static",
    "goes",
)

REGISTRY_PATH = os.path.join(
    FRAME_DIR,
    "registry.json",
)

DESCRIPTORS_PATH = os.path.join(
    FRAME_DIR,
    "descriptors.json",
)

_REGISTRY = {}


def _load_registry():
    if not _REGISTRY.get("loaded"):

        try:
            with open(
                REGISTRY_PATH,
                encoding="utf-8",
            ) as f:
                _REGISTRY["data"] = json.load(f)

        except (
            OSError,
            ValueError,
        ):
            _REGISTRY["data"] = {}

        _REGISTRY["loaded"] = True

    return _REGISTRY["data"]


def _save_registry(reg):
    os.makedirs(
        FRAME_DIR,
        exist_ok=True,
    )

    tmp = REGISTRY_PATH + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            reg,
            f,
        )

    os.replace(
        tmp,
        REGISTRY_PATH,
    )

    _REGISTRY["data"] = reg
    _REGISTRY["loaded"] = True


def _save_descriptors(all_desc):
    os.makedirs(
        FRAME_DIR,
        exist_ok=True,
    )

    tmp = DESCRIPTORS_PATH + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            all_desc,
            f,
        )

    os.replace(
        tmp,
        DESCRIPTORS_PATH,
    )


def _merge_descriptors(
    band_key,
    frames,
):
    """Persist slim frame descriptors so the UI never blocks on S3."""

    os.makedirs(
        FRAME_DIR,
        exist_ok=True,
    )

    try:
        with open(
            DESCRIPTORS_PATH,
            encoding="utf-8",
        ) as f:
            all_desc = json.load(f)

    except (
        OSError,
        ValueError,
    ):
        all_desc = {}

    all_desc[band_key] = [
        {
            "id": d["id"],
            "label": d["label"],
            "time": d["time"],
        }
        for d in frames
    ]

    tmp = DESCRIPTORS_PATH + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            all_desc,
            f,
        )

    os.replace(
        tmp,
        DESCRIPTORS_PATH,
    )


def _prune_registry(reg):
    """Remove stale renders and old malformed satellite IDs."""

    now = dt.datetime.now(
        dt.timezone.utc
    )

    # Remove stale per-process download temp files.
    try:
        for fn in os.listdir(
            FRAME_DIR
        ):
            if not fn.endswith(
                ".nc.tmp"
            ):
                continue

            path = os.path.join(
                FRAME_DIR,
                fn,
            )

            try:
                age = (
                    now.timestamp()
                    - os.path.getmtime(path)
                )
            except OSError:
                continue

            if age > 900:
                try:
                    os.remove(path)
                except OSError:
                    pass

    except OSError:
        pass

    keep = {}

    for fid, entry in reg.items():

        # Correct satellite frame IDs use:
        #
        # band_YYYYDDDHHMMSS
        #
        # which is 13 timestamp digits.
        m = re.match(
            r"^\w+_(\d{13})$",
            fid,
        )

        if m:

            try:
                t = dt.datetime.strptime(
                    m.group(1),
                    "%Y%j%H%M%S",
                ).replace(
                    tzinfo=dt.timezone.utc
                )

                if (
                    now - t
                ).total_seconds() < 24 * 3600:

                    keep[fid] = entry
                    continue

            except ValueError:
                pass

        # Anything that does not match the corrected format is stale.
        # This also removes the previously generated malformed IDs such as:
        #
        # c16_20262531356179
        #
        for suffix in (
            ".png",
            ".json",
        ):
            try:
                os.remove(
                    os.path.join(
                        FRAME_DIR,
                        fid + suffix,
                    )
                )
            except OSError:
                pass

    return keep


# ---------------------------------------------------------------------------
# PROJECTION
# ---------------------------------------------------------------------------

def _mercator_bounds(
    proj,
    xb,
    yb,
):
    """GOES fixed-grid scan-angle bounds -> lat/lon corners."""

    from pyproj import Transformer

    H = float(
        proj[
            "perspective_point_height"
        ]
    )

    tr = Transformer.from_crs(
        (
            f"+proj=geos "
            f"+h={H} "
            f"+lon_0="
            f"{proj['longitude_of_projection_origin']} "
            f"+sweep=x "
            f"+a={proj['semi_major_axis']} "
            f"+b={proj['semi_minor_axis']}"
        ),
        "EPSG:4326",
        always_xy=True,
    )

    x0, x1 = (
        float(xb[0]) * H,
        float(xb[1]) * H,
    )

    y0, y1 = (
        float(yb[0]) * H,
        float(yb[1]) * H,
    )

    lon0, lat0 = tr.transform(
        x0,
        min(y0, y1),
    )

    lon1, lat1 = tr.transform(
        x1,
        max(y0, y1),
    )

    return [
        float(lat0),
        float(lon0),
        float(lat1),
        float(lon1),
    ]


# ---------------------------------------------------------------------------
# FRAME RENDERER
# ---------------------------------------------------------------------------

def _render_band_frame(
    band_key,
    desc,
    max_px=1100,
):
    """Download, decode and render one GOES scan."""

    b = BANDS[band_key]

    band_num = b["band"]

    max_px = b.get(
        "max_px",
        max_px,
    )

    fid = desc["id"]

    entry = {
        "id": fid,
        "status": "rendering",
        "label": desc["label"],
        "time": desc["time"],
    }

    png_path = os.path.join(
        FRAME_DIR,
        fid + ".png",
    )

    os.makedirs(
        FRAME_DIR,
        exist_ok=True,
    )

    try:

        import h5py

        url = (
            f"{BUCKET}/"
            f"{desc['key']}"
        )

        tmp = os.path.join(
            FRAME_DIR,
            f"{fid}.{os.getpid()}.nc.tmp",
        )

        response = requests.get(
            url,
            headers=UA,
            timeout=120,
        )

        response.raise_for_status()

        with open(
            tmp,
            "wb",
        ) as f:
            f.write(
                response.content
            )

        with h5py.File(
            tmp,
            "r",
        ) as f:

            var = (
                "CMI"
                if b.get("product") == "l2"
                else "Rad"
            )

            dv = f[var]

            scale = float(
                np.asarray(
                    dv.attrs.get(
                        "scale_factor",
                        1.0,
                    )
                ).reshape(-1)[0]
            )

            offset = float(
                np.asarray(
                    dv.attrs.get(
                        "add_offset",
                        0.0,
                    )
                ).reshape(-1)[0]
            )

            shape = dv.shape

            step = max(
                1,
                int(
                    np.ceil(
                        max(shape)
                        / max_px
                    )
                ),
            )

            raw = np.asarray(
                dv[
                    ::step,
                    ::step
                ],
                dtype=float,
            )

            fill = dv.attrs.get(
                "_FillValue"
            )

            if fill is not None:
                raw[
                    np.isclose(
                        raw,
                        float(
                            np.asarray(
                                fill
                            ).reshape(-1)[0]
                        ),
                    )
                ] = np.nan

            D = (
                raw * scale
                + offset
            )

            pa = f[
                "goes_imager_projection"
            ].attrs

            proj = {
                k: float(
                    np.asarray(
                        pa[k]
                    ).reshape(-1)[0]
                )
                for k in (
                    "perspective_point_height",
                    "longitude_of_projection_origin",
                    "semi_major_axis",
                    "semi_minor_axis",
                )
                if k in pa
            }

            if "x_image_bounds" in f:

                xb = np.atleast_1d(
                    f[
                        "x_image_bounds"
                    ][()]
                ).astype(float)

                yb = np.atleast_1d(
                    f[
                        "y_image_bounds"
                    ][()]
                ).astype(float)

            else:

                xb = np.asarray(
                    [
                        f["x"][0],
                        f["x"][-1],
                    ],
                    dtype=float,
                )

                yb = np.asarray(
                    [
                        f["y"][0],
                        f["y"][-1],
                    ],
                    dtype=float,
                )

        try:
            os.remove(tmp)
        except OSError:
            pass

        # ---------------------------------------------------------------
        # Calibration
        # ---------------------------------------------------------------

        if b.get("kind") == "refl":

            vals = np.clip(
                D * 100.0,
                0,
                120,
            )

        elif b.get("product") == "l2":

            vals = (
                D - 273.15
            )

        else:

            fk1, fk2, bc1, bc2 = PLANCK[
                band_num
            ]

            with np.errstate(
                divide="ignore",
                invalid="ignore",
            ):
                vals = (
                    fk2
                    / np.log(
                        fk1
                        / np.where(
                            D > 0,
                            D,
                            np.nan,
                        )
                        + bc1
                    )
                    - bc2
                    - 273.15
                )

        # ---------------------------------------------------------------
        # RGBA conversion
        # ---------------------------------------------------------------

        rgba = _to_rgba(
            band_key,
            vals,
        )

        from PIL import Image

        Image.fromarray(
            rgba,
            "RGBA",
        ).save(
            png_path,
            "PNG",
            optimize=True,
        )

        # ---------------------------------------------------------------
        # Geographic bounds
        # ---------------------------------------------------------------

        bounds = _mercator_bounds(
            proj,
            xb,
            yb,
        )

        if not all(
            math.isfinite(v)
            for v in bounds
        ):

            # Full disk can fall outside the ellipsoid.
            c = math.degrees(
                math.acos(
                    proj[
                        "semi_major_axis"
                    ]
                    / (
                        proj[
                            "semi_major_axis"
                        ]
                        + proj[
                            "perspective_point_height"
                        ]
                    )
                )
            )

            lon0 = proj[
                "longitude_of_projection_origin"
            ]

            bounds = [
                -c,
                lon0 - c,
                c,
                lon0 + c,
            ]

        meta = {
            "bounds": bounds
        }

        with open(
            os.path.join(
                FRAME_DIR,
                fid + ".json",
            ),
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                meta,
                f,
            )

        entry.update(
            {
                "status": "done",
                **meta,
            }
        )

    except Exception as exc:

        entry["status"] = "error"

        entry["error"] = str(
            exc
        )[:200]

    with _REG_LOCK:

        reg = _load_registry()

        reg[fid] = entry

        _save_registry(
            reg
        )

    return entry


# ---------------------------------------------------------------------------
# BACKGROUND RENDERERS
# ---------------------------------------------------------------------------

_RENDERER_THREADS = {}

_REG_LOCK = threading.Lock()


def _ensure_threads(
    band_key=None,
):
    """Start one renderer thread per requested band."""

    keys = (
        [band_key]
        if band_key
        else list(BANDS)
    )

    for key in keys:

        t = _RENDERER_THREADS.get(
            key
        )

        if t and t.is_alive():
            continue

        def _worker(
            k=key
        ):

            while True:

                try:

                    frames = get_band_frames(
                        k
                    )

                    with _REG_LOCK:

                        reg = _prune_registry(
                            _load_registry()
                        )

                        _save_registry(
                            reg
                        )

                    _merge_descriptors(
                        k,
                        frames
                    )

                    # Truth is the DISK, not the registry: the budget
                    # sweeper can delete PNGs the registry still calls
                    # "done" (same 2026-09-18 lesson as the HRRR renderer).
                    # Re-render anything whose PNG is missing so a sweep or
                    # a restart never leaves the band page with dead links.
                    pending = [
                        fr
                        for fr in frames
                        if reg.get(
                            fr["id"],
                            {}
                        ).get(
                            "status"
                        ) != "done"
                        or not os.path.isfile(
                            os.path.join(
                                FRAME_DIR,
                                fr["id"] + ".png",
                            )
                        )
                    ]

                    for fr in pending:

                        _render_band_frame(
                            k,
                            fr,
                        )

                except Exception:
                    # Worker must never die permanently because of one
                    # bad download or malformed satellite frame.
                    pass

                time.sleep(
                    300
                )

        _RENDERER_THREADS[key] = threading.Thread(
            target=_worker,
            daemon=True,
            name=f"goes-{key}",
        )

        _RENDERER_THREADS[
            key
        ].start()


# ---------------------------------------------------------------------------
# UI BUNDLE
# ---------------------------------------------------------------------------

def band_bundle(
    band_key,
):
    """Return UI snapshot for one satellite band."""

    _ensure_threads(
        band_key
    )

    b = BANDS[
        band_key
    ]

    try:

        with open(
            DESCRIPTORS_PATH,
            encoding="utf-8",
        ) as f:
            all_desc = json.load(
                f
            )

    except (
        OSError,
        ValueError,
    ):
        all_desc = {}

    desc = (
        all_desc.get(
            band_key
        )
        or []
    )

    reg = _load_registry()

    frames = []

    for d in desc:

        entry = reg.get(
            d["id"]
        )

        if (
            entry
            and entry.get(
                "status"
            ) == "done"
        ):

            frames.append(
                {
                    "kind": "sat",
                    "label": d["label"],
                    "time": d["time"],
                    "pngUrl": (
                        f"/app/static/goes/"
                        f"{d['id']}.png"
                    ),
                    "bounds": entry.get(
                        "bounds"
                    ),
                }
            )

    return {
        "key": band_key,
        "label": b["label"],
        "total": len(desc),
        "ready": len(frames),
        "frames": frames,
    }


def all_bundles():
    """Return bundles for every satellite band."""

    return [
        band_bundle(k)
        for k in BANDS
    ]
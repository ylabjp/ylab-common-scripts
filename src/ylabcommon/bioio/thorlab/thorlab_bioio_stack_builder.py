from pathlib import Path
from typing import List, Tuple
from collections import defaultdict
import os
import xarray as xr
import numpy as np
import dask.array as da
import xml.etree.ElementTree as ET
from bioio_tifffile import Reader as TiffReader
#from bioio.readers import Reader as TiffReader
from bioio import BioImage
from ylabcommon.utils.normalize_bioImage import normalize_to_tczyx
from ylabcommon.utils.outfile_name import extract_dimensions, is_mosaic


# ---------------------------------------------------------
# The Metadata-Aware Universal Stacker
#Need to extract the physical coordinates from the xml 
#"Z-step" or "Time Interval" is already set correctly.
#Ensures that the final file in software like Fiji/ImageJ/Analysis, 
# ---------------------------------------------------------

def get_channel_names_index(xml_path):
    """Return the Thorlabs channel (wavelength) names from an Experiment.xml.

    Accepts a path to Experiment.xml. Each ``<Wavelength name="...">`` under
    ``<Wavelengths>`` becomes one channel name. Falls back to ``["Channel 0"]``
    if the file cannot be parsed.

    (Previously this expected an xmltodict-style dict but was called with a Path,
    so the subscript access always raised and it silently returned the fallback.)
    """
    try:
        root = ET.parse(str(xml_path)).getroot()
        names = [w.get("name") for w in root.findall(".//Wavelength") if w.get("name")]
        return names if names else ["Channel 0"]
    except (ET.ParseError, OSError, TypeError, ValueError):
        return ["Channel 0"]

def _thorlabs_channel_key(path):
    """Channel identifier parsed from a Thorlabs TIFF filename.

    Thorlabs raw files look like ``ChanA_00001_00002_00003_00004.tif`` — the
    channel token (``ChanA`` / ``ChanB`` / ... or ``CH1``) is what distinguishes
    channels. Returns that token, or the whole stem if none is found (so all files
    collapse into a single channel). Mirrors the convention used by
    ``outfile_name.build_output_name`` and ``file_selection``.
    """
    stem = Path(path).stem
    for tok in stem.split("_"):
        if "Chan" in tok or "CH" in tok:
            return tok
    return stem


def stack_thorlab_with_bioio_calibrated(tiff_files: list, xml_path: str, get_thorlabs_params, min_kb: int = 100):
    params = get_thorlabs_params
    print("PARMS : ", params)
    mode = params["mode"]
    target_total = params.get("SizeZ" if mode == "Z" else "SizeT", 0)

    print(f"DEBUG: XML says Target {mode} is {target_total}")

    # Axis index of the stacking dimension within TCZYX.
    axis = {"T": 0, "C": 1, "Z": 2, "Y": 3, "X": 4}[mode]

    # Size filter (a stat() per file, NOT a pixel read). ``tiff_files`` is already
    # sorted by collect_valid_tiffs; honor the ``min_kb`` parameter.
    filtered_files = sorted(f for f in tiff_files if os.path.getsize(f) > min_kb * 1024)

    # Mosaic (multiple XY stage positions) is NOT supported here: each tile would
    # be collapsed into Z/T. Detect it from the filenames and fail loudly rather
    # than produce a silently wrong stack.
    try:
        _, _dims = extract_dimensions(filtered_files)
        _is_mosaic = is_mosaic(_dims)
    except Exception:
        _dims, _is_mosaic = {}, False
    if _is_mosaic:
        raise RuntimeError(
            "Multiple XY stage positions (mosaic) detected — not supported by "
            "stack_thorlab_with_bioio_calibrated (tiles would collapse into Z/T). "
            f"XY={sorted(_dims.get('XY', [])) or None}, "
            f"X={sorted(_dims.get('X', [])) or None}, "
            f"Y={sorted(_dims.get('Y', [])) or None}. "
            "Process one stage position at a time, or stitch the tiles first."
        )

    # Group files by channel so channels land on the C axis instead of being
    # collapsed into Z/T. Within a channel the (filename-sorted) order is the plane
    # order — Thorlabs zero-pads the numeric Z/T fields, so lexical order is correct.
    by_channel = defaultdict(list)
    for f in filtered_files:
        by_channel[_thorlabs_channel_key(f)].append(f)

    channel_stacks = []
    for ch in sorted(by_channel):
        # Read each file for this channel lazily as a 5D TCZYX dask array; bioio
        # expands/reorders axes for us (no custom TCZYX normalization needed).
        arrs = [BioImage(f, reader=TiffReader).get_image_dask_data("TCZYX")
                for f in by_channel[ch]]

        # Prefer a single multi-page file (mode axis already > 1) if the channel
        # has one; otherwise concatenate the individual planes along the mode axis.
        multi = [a for a in arrs if a.shape[axis] > 1]
        if multi:
            ch_stack = max(multi, key=lambda a: a.shape[axis])
            print(f"DEBUG: Channel {ch}: multi-page file with {ch_stack.shape[axis]} {mode} slices")
        else:
            ch_stack = arrs[0] if len(arrs) == 1 else da.concatenate(arrs, axis=axis)
            print(f"DEBUG: Channel {ch}: {len(arrs)} plane(s) stacked along {mode}")

        channel_stacks.append(ch_stack)

    # Stack channels along C (axis 1). A single channel still yields a full 5D
    # TCZYX array.
    if len(channel_stacks) == 1:
        stacked = channel_stacks[0]
    else:
        stacked = da.concatenate(channel_stacks, axis=1)

    t, c, zz, yy, xx = stacked.shape
    print(f"DEBUG: Final stack (TCZYX) = ({t}, {c}, {zz}, {yy}, {xx})")

    # Physical calibration is written from the XML params at save time via the
    # OME-TIFF writer's `physical_pixel_sizes`; no xarray coordinates are needed
    # (they were never read back into the output).
    dx = params.get("PixelSizeX", 1.0)
    dy = params.get("PixelSizeY", dx)
    dz = params.get("PixelSizeZ", 1.0)
    print(f"[Coordinates] Pixel size (Z, Y, X) um = ({dz}, {dy}, {dx})")

    return stacked, filtered_files

# ---------------------------------------------------------
# Nuclear stacking using BioIO ONLY
# ---------------------------------------------------------
def stack_with_bioio(tiff_files: list, min_kb: int = 100):
    all_planes = []
    valid_files = []
    base_shape = None

    sorted_files = sorted(tiff_files)

    for f in sorted_files:
        if os.path.getsize(f) < min_kb * 1024:
            print(f"[Skip] {Path(f).name} is too small (likely metadata).")
            continue

        try:
            img = BioImage(f, reader=TiffReader)
            data = normalize_to_tczyx(img)

            current_shape = {
                "C": data.sizes["C"],
                "Y": data.sizes["Y"],
                "X": data.sizes["X"]
            }
           
            if base_shape is None:
                base_shape = current_shape
            else:
                # If resolution or channel count changed, we must stop
                if current_shape != base_shape:
                    raise ValueError(
                    f"Mismatched dimensions in {Path(f).name}. "
                    f"Expected {base_shape}, got {current_shape}"
                  ) 
            # ---------------------------------------------
            # Deconstruct Z into individual planes
            # ----------------------------------------------
            if data.sizes["Z"] > 1:
                print(f"[Extract] {Path(f).name} has {data.sizes['Z']} slices. Expanding...")
                for i in range(data.sizes["Z"]):
                    plane = data.isel(Z=slice(i, i+1)) 
                    all_planes.append(plane)
            else:
                all_planes.append(data)
            
            valid_files.append(f)

        except Exception as e:
            print(f"[Error] Skipping  {Path(f).name}: {e}")

    if not all_planes:
        raise RuntimeError("No valid image data found in provided files.")

    #Final Stack
    stacked = xr.concat(all_planes, dim="Z")

    print("--- Stack Complete ---")
    print(f"Final Shape (TCZYX): {stacked.shape}")
    
    return stacked, valid_files

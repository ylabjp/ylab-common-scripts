from pathlib import Path
from typing import List, Tuple
import os
import xarray as xr
import numpy as np
import dask.array as da
import xml.etree.ElementTree as ET
from bioio_tifffile import Reader as TiffReader
#from bioio.readers import Reader as TiffReader
from bioio import BioImage
from ylabcommon.utils.normalize_bioImage import normalize_to_tczyx


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

def stack_thorlab_with_bioio_calibrated(tiff_files: list, xml_path: str, get_thorlabs_params, min_kb: int = 100):
    params = get_thorlabs_params
    print("PARMS : ", params)
    mode = params["mode"]
    target_total = params.get("SizeZ" if mode == "Z" else "SizeT", 0)

    print(f"DEBUG: XML says Target {mode} is {target_total}")

    # Axis index of the stacking dimension within TCZYX.
    axis = {"T": 0, "C": 1, "Z": 2, "Y": 3, "X": 4}[mode]

    # Size filter (a stat() on each file, NOT a pixel read). ``tiff_files`` is
    # already sorted by collect_valid_tiffs; honor the ``min_kb`` parameter.
    filtered_files = sorted(f for f in tiff_files if os.path.getsize(f) > min_kb * 1024)

    multi_page_list = []   # (n_slices, dask_array)
    single_page_list = []  # dask_array

    # Categorize files by their dimensions ONLY. bioio's get_image_dask_data
    # expands/reorders axes to TCZYX for us and stays lazy, so this loop reads
    # TIFF headers/metadata, not pixel arrays — files we later discard cost no
    # decode, and no custom TCZYX normalization is needed.
    for f in filtered_files:
        img = BioImage(f, reader=TiffReader)
        arr = img.get_image_dask_data("TCZYX")
        n_slices = arr.shape[axis]

        if n_slices > 1:
            multi_page_list.append((n_slices, arr))
            print(f"DEBUG: Identified Multi-page file: {os.path.basename(f)} ({n_slices} slices)")
        else:
            single_page_list.append(arr)

    # DECISION: Use the big multi-page file OR the individual planes (never both).
    if multi_page_list:
        # The largest multi-page file already IS the full stack along `mode`.
        multi_page_list.sort(key=lambda t: t[0], reverse=True)
        stacked = multi_page_list[0][1]
        print(f"DEBUG: Priority Path -> Using 1 multi-page file with {stacked.shape[axis]} slices.")
    else:
        # single_page_list is already in filename order (filtered_files is sorted).
        print(f"DEBUG: Standard Path -> Collecting {len(single_page_list)} individual planes.")
        stacked = da.concatenate(single_page_list, axis=axis)

    print(f"DEBUG: Final {mode} size: {stacked.shape[axis]}")

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

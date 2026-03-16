from pathlib import Path
from typing import List, Tuple
import os
import xarray as xr
import numpy as np
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

def get_channel_names_index(xml_dict):
    try:
        # Thorlabs usually nests this under Experiment -> Wavelengths
        wavelengths = xml_dict['ThorImageExperiment']['Wavelengths']['Wavelength']
        
        # If there's only one wavelength, xmltodict might return a dict instead of a list
        if isinstance(wavelengths, dict):
            wavelengths = [wavelengths]
            
        return [w.get('@name', f"Channel {i}") for i, w in enumerate(wavelengths)]
    except (KeyError, TypeError):
        return ["Channel 0"]

def stack_thorlab_with_bioio_calibrated(tiff_files: list, xml_path: str, get_thorlabs_params, min_kb: int = 100):
    params = get_thorlabs_params
    print("PARMS : ", params)
    mode = params["mode"]
    target_total = params.get("SizeZ" if mode == "Z" else "SizeT", 0)

    print(f"DEBUG: XML says Target {mode} is {target_total}")

    all_chunks = []
    filtered_files = sorted([f for f in tiff_files if os.path.getsize(f) > 100 * 1024])

    multi_page_list = []
    single_page_list = []

    # Categorize all files first
    for f in filtered_files:
        img = BioImage(f, reader=TiffReader)
        data = normalize_to_tczyx(img)
    
        if data.sizes[mode] > 1:
            multi_page_list.append(data)
            print(f"DEBUG: Identified Multi-page file: {os.path.basename(f)} ({data.sizes[mode]} slices)")
        else:
            single_page_list.append(data)

    # DECISION: Use the big file OR the individual planes (Never both)
    if multi_page_list:
        # Sort by number of slices and take the biggest one
        multi_page_list.sort(key=lambda d: d.sizes[mode], reverse=True)
        best_data = multi_page_list[0]
    
        print(f"DEBUG: Priority Path -> Using 1 multi-page file with {best_data.sizes[mode]} slices.")
        for i in range(best_data.sizes[mode]):
            all_chunks.append(best_data.isel({mode: slice(i, i+1)}))
    else:
        single_page_list.sort(key=lambda x: str(x)) # Simple sort
        
        print(f"DEBUG: Standard Path -> Collecting {len(single_page_list)} individual planes.")
        all_chunks = single_page_list

    print(f"DEBUG: Final unique chunk count: {len(all_chunks)}")

    # Final Stack
    stacked = xr.concat(all_chunks, dim=mode)

    # ATTACH CALIBRATED COORDINATES

    dx = params.get("PixelSizeX", 1.0)
    dy = params.get("PixelSizeY", dx)      # Usually X and Y are the same
    dz = params.get("PixelSizeZ", 1.0)
    dt = params.get("TimelapseInterval", 1.0)

    # This turns index [0, 1, 2...] into physical units [0um, 1.2um, 2.4um...]
    stacked = stacked.assign_coords({
        "X": [i * dx for i in range(stacked.sizes["X"])],
        "Y": [i * dy for i in range(stacked.sizes["Y"])],
        "Z": [i * dz for i in range(stacked.sizes["Z"])],
        "T": [i * dt for i in range(stacked.sizes["T"])]
    })
    # These are used by OME-TIFF writers to set the header correctly
    stacked.attrs["units"] = "micrometers"
    stacked.attrs["time_units"] = "seconds"

    # Add a summary attribute for easy debugging
    stacked.attrs["pixel_size_xyz"] = (dz, dy, dx)

    print(f"[Coordinates] Applied spatial scaling: X/Y={dx}um, Z={dz}um")
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

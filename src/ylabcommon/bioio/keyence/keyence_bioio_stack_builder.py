from collections import defaultdict
from pathlib import Path
import os
import re
import xarray as xr
from bioio import BioImage
from bioio_tifffile import Reader as TiffReader
from ylabcommon.utils.normalize_bioImage import normalize_to_tczyx


def file_pattern():
    pattern = re.compile(
        r".*XY(?P<xy>\d+)_Z(?P<z>\d+)_CH(?P<ch>\d+)\.tif",
        re.IGNORECASE
    )
    return pattern

def parse_keyence_filename(filename):

    name = Path(filename).name.replace(".tif", "")

    parts = name.split("_")

    xy = None
    z = None
    ch = None

    for p in parts:

        if p.startswith("XY"):
            xy = int(p.replace("XY", ""))

        elif p.startswith("Z"):
            z = int(p.replace("Z", ""))

        elif p.startswith("CH"):
            ch = int(p.replace("CH", ""))

        elif p.isdigit() and z is None:
            z = int(p)
            #print(f"Non-standard filename detected (missing Z label)")

    if xy is None or z is None or ch is None:
        raise RuntimeError(f"Cannot parse filename")

    return {"xy": xy, "z": z, "ch": ch}

def normalize_keyence_filename(filename):

    name = Path(filename).name

    # detect missing Z label
    m = re.match(r"(.*XY\d+)_(\d+)_CH(\d+)\.tif$", name, re.IGNORECASE)

    if m:
        prefix = m.group(1)
        z_val  = m.group(2)
        ch_val = m.group(3)

        new_name = f"{prefix}_Z{z_val}_CH{ch_val}.tif"
    
    return new_name, z_val

    #return name

def parse_keyence_name(name):

    #name = Path(path).name
    pattern = file_pattern()
 
    #name = normalize_keyence_filename(name)   
    m = pattern.match(name)
    z_val = 0
    m_type = True
    
    if m is None:
        m_type = m
        name, z_val = normalize_keyence_filename(name)
        m = pattern.match(name)
        print(f"[INFO] Non-standard filename detected: Assuming Z index → {name}")

    #if m is None:
        #raise ValueError(f"Filename does not match Keyence pattern: {name}")
    tile = int(m.group("xy"))
    z  = int(m.group("z"))
    ch = int(m.group("ch"))
    """
    if m:
        tile = int(m.group("xy"))
        z  = int(m.group("z"))
        ch = int(m.group("ch"))
     
    else: 
        parsed = parse_keyence_filename(name)
        tile = parsed["xy"]
        z  = parsed["z"]
        ch = parsed["ch"]
    #print(f"Non-standard Keyence filename detected")
    """ 

    return tile, z, ch, z_val, m_type

def get_channel_names(channels):
    
    channel_names = [f"CH{c}" for c in channels] 
    return channel_names


def stack_keyence_with_bioio_calibrated(tiff_files, min_kb: int = 100):

    grouped = defaultdict(lambda: defaultdict(list))

    # -----------------------------------
    # index dataset
    # -----------------------------------
    sorted_files = sorted(tiff_files)
    z_arry = []
    for f in sorted_files:

        tile, z, ch, z_val, m_type = parse_keyence_name(f)

        grouped[tile][ch].append((z, f))
        z_arry.append(z_val)

    tiles = {}

    z_max_min = []
    z_max_min.append(z_arry[0]) 
    z_max_min.append(z_arry[-1]) 
    z_max_min.append(m_type)
    print(f"[DEBUG ZVALUES:], {z_max_min}")

    # -----------------------------------
    # stack each tile
    # -----------------------------------

    for tile, channels in grouped.items():

        channel_stacks = []

        for ch in sorted(channels):

            z_files = sorted(channels[ch], key=lambda p: p[0])

            planes = []

            for z, f in z_files:

                img = BioImage(f, reader=TiffReader)

                data = normalize_to_tczyx(img)

                planes.append(data)

            z_stack = xr.concat(planes, dim="Z")

            channel_stacks.append(z_stack)

        stacked = xr.concat(channel_stacks, dim="C")

        tiles[tile] = stacked

        channels_keys = sorted(channels.keys())
        channels = get_channel_names(channels)

        print(f"DEBUG: Tiles: {len(grouped)}" )
        print(f"DEBUG: channels: {channels}")
        print(f"DEBUG: Z slices: {len(z_files)}")
        print(f"DEBUG: Z order:, {[z for z, _ in z_files]}")
        print(f"DEBUG: {stacked.dtype}")
        if stacked is not None:
            dims = stacked.sizes
            print("Stack Complete: (TCZYX) : "
            f"(T={dims['T']}, C={dims['C']}, Z={dims['Z']}, "
            f"Y={dims['Y']}, X={dims['X']})"
            ) 
    print(f"DEBUG: (XY, Z, C) Group Index")
    #for f in tiff_files[:10]:
        #print(f"    {parse_keyence_name(f)}    ")
   
    if len(tiles) == 1:
        data = next(iter(tiles.values()))
    else:
        data = tiles

    return data, sorted_files, channels, z_max_min 


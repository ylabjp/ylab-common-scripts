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

def parse_keyence_name(name):

    #name = Path(path).name
    pattern = file_pattern()
    m = pattern.match(name)

    if m is None:
        raise ValueError(f"Filename does not match Keyence pattern: {name}")

    tile = int(m.group("xy"))
    z  = int(m.group("z"))
    ch = int(m.group("ch"))

    return tile, z, ch

def get_channel_names(channels):
    
    channel_names = [f"CH{c}" for c in channels] 
    return channel_names


def stack_keyence_with_bioio_calibrated(tiff_files, min_kb: int = 100):

    grouped = defaultdict(lambda: defaultdict(list))

    # -----------------------------------
    # index dataset
    # -----------------------------------
    sorted_files = sorted(tiff_files)

    for f in sorted_files:

        tile, z, ch = parse_keyence_name(f)

        grouped[tile][ch].append((z, f))

    tiles = {}

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
    for f in tiff_files[:10]:
        print(f"    {parse_keyence_name(f)}    ")
   
    if len(tiles) == 1:
        data = next(iter(tiles.values()))
    else:
        data = tiles

    return data, sorted_files, channels


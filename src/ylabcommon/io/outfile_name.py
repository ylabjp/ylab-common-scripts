from pathlib import Path
from datetime import datetime
import pandas as pd
import re
from collections import defaultdict

from pathlib import Path
from collections import defaultdict


def extract_dimensions(sorted_tiffs):

    dims = defaultdict(set)
    image_name = None

    for f in sorted_tiffs:

        name = Path(f).stem
        tokens = name.split("_")

        if image_name is None:
            image_name = tokens[0]

        # Case 1: labeled format
        for token in tokens:

            if token.startswith("XY"):
                dims["XY"].add(int(token[2:]))

            elif token.startswith("X") and token[1:].isdigit():
                dims["X"].add(int(token[1:]))

            elif token.startswith("Y") and token[1:].isdigit():
                dims["Y"].add(int(token[1:]))

            elif token.startswith("Z"):
                dims["Z"].add(int(token[1:]))

            elif token.startswith("CH"):
                dims["CH"].add(int(token[2:]))

            elif token.startswith("T"):
                dims["T"].add(int(token[1:]))

        # Case 2: Thorlab numeric format
        if len(tokens) == 5 and tokens[1].isdigit():

            dims["X"].add(int(tokens[1]))
            dims["Y"].add(int(tokens[2]))
            dims["Z"].add(int(tokens[3]))
            dims["T"].add(int(tokens[4]))

    return image_name, dims

def format_range(prefix, values):

    values = sorted(values)

    if len(values) == 1:
        return f"{prefix}{values[0]:03d}"

    return f"{prefix}{values[0]:03d}_to_{prefix}{values[-1]:03d}"

def is_mosaic(dims):
    if "XY" in dims and len(dims["XY"]) > 1:
        return True
    if "X" in dims and "Y" in dims:
        if len(dims["X"]) > 1 or len(dims["Y"]) > 1:
            return True
    return False

def build_stack_filename(output_dir: Path, image_name, dims, ext=".tiff"):

    parts = [image_name]

    # XY dimension
    if "XY" in dims:
        parts.append(format_range("XY", dims["XY"]))

    # X Y dimension (Thorlabs)
    if "X" in dims:
        parts.append(format_range("X", dims["X"]))

    if "Y" in dims:
        parts.append(format_range("Y", dims["Y"]))

    # channel dimension
    if "CH" in dims:
        parts.append(format_range("CH", dims["CH"]))

    # Z dimension
    if "Z" in dims:
        parts.append(format_range("Z", dims["Z"]))

    # detect mosaic automatically
    if is_mosaic(dims):
        parts.append("stitched") 

    parts.append("stack")

    # time dimension
    if "T" in dims:
        parts.append(format_range("T", dims["T"]))

    filename = "_".join(parts) + ext
    return output_dir / filename


def build_output_name(output_dir: Path, tiff_files, Z_stack_val, T_stack_val):

    records = []

    for f in tiff_files:

        name = Path(f).name

        parts = name.replace(".tif","").replace(".tiff","").split("_")
        print(f"DEBUG : {parts}")

        channel = None
        stageX = None
        stageY = None
        z = None
        t = None

        for p in parts:
            if "Chan" in p or "CH" in p:
                channel = p

        try:
            stageX = int(parts[1])
            stageY = int(parts[2])
            z = int(parts[3])
        except:
            pass

        records.append({
            "path": f,
            "filename": name,
            "channel": channel,
            "stageX": stageX,
            "stageY": stageY,
            "z": z,
            "t": t,
        })

    df = pd.DataFrame(records)

    if df is None or len(df) == 0:
        raise RuntimeError("Metadata dataframe empty — cannot build output name")

    ch = df["channel"].dropna().iloc[0]
    ''''
    zvals = df["z"].dropna().astype(int).sort_values().unique()
    num_z = len(zvals)

    if num_z == 0:
        zpart = "Zsingle"
    elif num_z == 1:
        zpart = f"Z{zvals[0]:03d}_stack"
    else:
        z_min = zvals.min()
        z_max = zvals.max()
        zpart = f"Zstack_{z_min:03d}to{z_max:03d}"

    stageX = df["stageX"].dropna().iloc[0] if not df["stageX"].empty else 0
    stageY = df["stageY"].dropna().iloc[0] if not df["stageY"].empty else 0
    tval = df["t"].dropna().iloc[0] if df["t"].notna().any() else 1

    filename = f"Output_File_{ch}_X{stageX:03d}_Y{stageY:03d}_{zpart}_T{tval:03d}"
    '''

    total_z = Z_stack_val
    z_start = int(df["z"].dropna().min()) if not df.get("z", pd.Series()).empty else 1
    
    if total_z > 1:
        zpart = f"Z{z_start:03d}_to_{total_z}_stack"
    else:
        zpart = f"Z{z_start:03d}"

    total_t = T_stack_val
    t_start = int(df["t"].dropna().min()) if "t" in df.columns and not df["t"].dropna().empty else 1
    
    if total_t > 1:
        tpart = f"T{t_start:03d}_series{total_t}"
    else:
        tpart = f"T{t_start:03d}"

    filename = f"Output_{ch}_X{stageX:03d}_Y{stageY:03d}_{zpart}_{tpart}"

    return output_dir / filename


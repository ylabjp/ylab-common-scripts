from pathlib import Path
from datetime import datetime
import pandas as pd
import re
from collections import defaultdict

from pathlib import Path
from collections import defaultdict


#: ラベル付き形式の接頭辞 → 次元名。長い接頭辞を先に見る (``XY`` を ``X`` より先、
#: ``CH`` を ``C`` より先)。そうしないと ``XY01`` が X=Y01 として拾われる。
_LABELLED_PREFIXES = (("XY", "XY"), ("CH", "CH"),
                      ("X", "X"), ("Y", "Y"), ("Z", "Z"), ("T", "T"))


def extract_dimensions(sorted_tiffs):
    """ファイル名から次元ごとの連番の集合を取り出す。

    **この関数は例外を投げない。** 読めないトークンは黙って飛ばす。

    以前は ``Z`` / ``T`` / ``XY`` / ``CH`` の分岐だけ ``isdigit()`` の確認が無く、
    ``Timelapse`` や ``Zstack`` のようなトークンを含む名前で ``ValueError`` に
    なっていた。呼び出し側 (mosaic 判定) はそれを ``except Exception`` で受けて
    「mosaic ではない」として続行していたため、**タイル取得が黙って Z/T 軸へ
    潰される** 経路になっていた。安全確認が、確認できなかったときに素通りする形に
    なっていたということである。

    そこで X/Y と同じように全分岐で数字を確認する。読めないものは次元として
    数えないだけなので、呼び出し側は「投げない」ことだけ前提にできる。
    """
    dims = defaultdict(set)
    image_name = None

    for f in sorted_tiffs:

        name = Path(f).stem
        tokens = name.split("_")

        if image_name is None:
            image_name = tokens[0]

        # Case 1: labeled format (image_XY01_Z02_CH1_T03)
        for token in tokens:
            for prefix, dim in _LABELLED_PREFIXES:
                if token.startswith(prefix) and token[len(prefix):].isdigit():
                    dims[dim].add(int(token[len(prefix):]))
                    break

        # Case 2: Thorlab numeric format (ChanA_<X>_<Y>_<Z>_<T>)
        if len(tokens) == 5 and all(t.isdigit() for t in tokens[1:]):
            for dim, value in zip(("X", "Y", "Z", "T"), tokens[1:]):
                dims[dim].add(int(value))

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

def build_stack_filename(output_dir: Path, image_name, dims, z_mx_min_re, ext=".tiff"):

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

    elif z_mx_min_re[-1] is None:
        z_mx_min_re = [z for z in z_mx_min_re if z is not None]
        z_mx_min_re = [int(v) for v in z_mx_min_re]
        z_mx_min_re = sorted(set(z_mx_min_re))
        
        parts.append(format_range("Z", z_mx_min_re))
        
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
        #print(f"DEBUG : {parts}")

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


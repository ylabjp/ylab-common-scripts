"""OME-TIFF の「複数ファイルにまたがるデータセット」を作るための共通部品。

先頭ファイルの OME XML が ``<TiffData><UUID FileName="...">`` で兄弟ファイルを名前で
指すと、tifffile はその名前を追って同じフォルダのファイルを開き、1つの配列に組み上げる。
実データの ``ChanA_001_001_001_004.tif`` がこの形だった。
"""
import numpy as np
import tifffile


def write_ome_master(path, sibling_names, size_t):
    """``path`` を「兄弟 ``sibling_names`` 全部を束ねる」OME マスターとして書く。

    ``metadata=None`` が要る。付けないと tifffile が自前の shaped 記述子を足し、
    そちらが先に効いて OME として読まれない (この取り違えで再現に手間取った)。
    """
    uuids = [f"urn:uuid:{i:08d}-0000-0000-0000-000000000000"
             for i in range(len(sibling_names))]
    own = sibling_names.index(path.name)
    tiff_data = "".join(
        f'<TiffData FirstT="{i}" FirstC="0" FirstZ="0" IFD="0" PlaneCount="1">'
        f'<UUID FileName="{n}">{u}</UUID></TiffData>'
        for i, (n, u) in enumerate(zip(sibling_names, uuids)))
    ome = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06" UUID="{uuids[own]}">'
        '<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" Type="uint16" '
        f'SizeX="8" SizeY="8" SizeC="1" SizeZ="1" SizeT="{size_t}" '
        'Interleaved="false" BigEndian="false" SignificantBits="16">'
        '<Channel ID="Channel:0:0" SamplesPerPixel="1"/>'
        + tiff_data + '</Pixels></Image></OME>')
    tifffile.imwrite(path, np.full((8, 8), own + 1, np.uint16),
                     description=ome, metadata=None)

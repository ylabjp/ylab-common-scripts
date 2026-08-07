"""ThorLabs の生 TIFF 群を遅延 (dask) の TCZYX スタックへ組み立てる。

設計方針:

**入力側は BioImage を使わない。** 生データの形は Experiment.xml とファイル名で
決まっており、画素の型と1ファイルあたりの面数だけが XML から分からない。したがって
必要なヘッダ読みは「取得ごとに1枚」で足りる。以前は ``BioImage(f)`` を全ファイルに対して
呼んでいたが、遅延なのは画素だけでヘッダは1ファイルずつ読むため、数万ファイルを
ネットワークドライブ越しに開くことになり取り込みの中で最も長い工程になっていた。

**組み立ては最初から最後まで dask。** ``dask.delayed`` した読み取りを
:func:`dask.array.from_delayed` で束ねるので、グラフ構築の時点では I/O が一切起きない。
画素が実際に読まれるのは書き出し時 (または解析側が compute したとき) の1回だけ。

出力は従来どおり bioio (OME-TIFF/Zarr) が担当する。
"""

from pathlib import Path
from collections import defaultdict, namedtuple
import os
import warnings

import dask
import dask.array as da
import numpy as np
import tifffile

from ylabcommon.utils.outfile_name import extract_dimensions, is_mosaic
from ylabcommon.utils.perf import timed_step
from ylabcommon.utils.utils import natural_sort_key
from ylabcommon.bioio.thorlab.xml_parser import ExperimentXMLParser


# ---------------------------------------------------------
# The Metadata-Aware Universal Stacker
# Need to extract the physical coordinates from the xml
# "Z-step" or "Time Interval" is already set correctly.
# Ensures that the final file in software like Fiji/ImageJ/Analysis,
# ---------------------------------------------------------

#: 1ファイルのヘッダから分かること。XML には無い情報だけを持つ。
PlaneLayout = namedtuple("PlaneLayout", "n_pages height width dtype")


def get_channel_names_index(xml_path):
    """Return the Thorlabs channel (wavelength) names from an Experiment.xml.

    Accepts a path to Experiment.xml. Each ``<Wavelength name="...">`` becomes one
    channel name. Falls back to ``["Channel 0"]`` if the file cannot be parsed.
    """
    try:
        names = ExperimentXMLParser(xml_path).extract_metadata()["Channels"]
    except Exception:
        return ["Channel 0"]
    return list(names) if names else ["Channel 0"]


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


def probe_plane_layout(path) -> PlaneLayout:
    """1ファイルのヘッダだけを読んで、面数・縦横・画素の型を返す。

    XML から分からないのはこの3つだけなので、同一取得であれば1枚読めば全ファイルに
    ついて分かる。画素は読まない。
    """
    with tifffile.TiffFile(str(path)) as tf:
        page = tf.pages[0]
        shape = tuple(page.shape)
        if len(shape) < 2:
            raise RuntimeError(
                f"Unexpected TIFF page shape {shape} in {path}; expected at least 2D."
            )
        return PlaneLayout(len(tf.pages), int(shape[-2]), int(shape[-1]),
                           np.dtype(page.dtype))


def _read_file_planes(path, n_pages, height, width, dtype):
    """遅延読みの実体。1ファイルを ``(n_pages, Y, X)`` として返す。

    ヘッダを1枚しか読んでいない以上、残りのファイルが本当に同じ形かはここで初めて
    分かる。食い違いは黙って通さず、**どのファイルか** を添えて落とす
    (dask の shape 不一致エラーはファイル名を持たないため)。
    """
    try:
        arr = tifffile.imread(str(path))
    except OSError as e:
        # ネットワークドライブが落ちたときに、どのファイルで落ちたかを残す。
        raise OSError(e.errno, "%s (while reading %s)" % (e.strerror, path)) from e

    arr = np.asarray(arr)
    if arr.shape[-2:] != (height, width):
        raise ValueError(
            "Frame size mismatch in %s: expected (Y=%d, X=%d), got %s. "
            "All files in one acquisition must share the same frame size."
            % (path, height, width, arr.shape[-2:])
        )

    arr = arr.reshape((-1, height, width))
    if arr.shape[0] != n_pages:
        raise ValueError(
            "Page count mismatch in %s: expected %d page(s), got %d."
            % (path, n_pages, arr.shape[0])
        )
    return arr.astype(dtype, copy=False)


def _page_counts(files, layout, target):
    """各ファイルの面数を返す。

    ほぼすべての取得は「全ファイルが同じ面数」なので、先頭と末尾の2枚だけ確かめて
    済ませる (取得が途中で終わったときに欠けるのは末尾のファイルなので、この2枚で
    現実的な不揃いは捕まえられる)。それでも食い違ったときだけ全ファイルを開く。
    """
    if len(files) == 1:
        return [layout.n_pages]

    tail = probe_plane_layout(files[-1])
    if tail.n_pages == layout.n_pages:
        return [layout.n_pages] * len(files)

    # 不揃いが確定した。ここだけは1ファイルずつ確かめるしかない (それでも
    # 読むのはヘッダだけで、BioImage を通すより桁で軽い)。
    print(f"[Stack] Page count differs between first ({layout.n_pages}) and last "
          f"({tail.n_pages}) file; probing every file's header.")
    with timed_step("thorlab.probe_tiffs", total=len(files), target=target) as step:
        counts = []
        for f in files:
            step.advance(item=f)
            counts.append(probe_plane_layout(f).n_pages)
    return counts


def stack_thorlab_with_bioio_calibrated(tiff_files: list, xml_path: str, get_thorlabs_params, min_kb: int = 100):
    params = get_thorlabs_params
    print("PARMS : ", params)
    mode = params["mode"]
    target_total = params.get("SizeZ" if mode == "Z" else "SizeT", 0)

    print(f"DEBUG: XML says Target {mode} is {target_total}")

    # 計測ログの target は取得ディレクトリ (img*) に揃える。sorter 側の load_image と
    # 同じ値になるので、Better Stack 上で工程をまたいで突き合わせられる。
    target = str(Path(xml_path).parent)

    # Size filter (a stat() per file, NOT a pixel read). ``tiff_files`` is already
    # sorted by collect_valid_tiffs; honor the ``min_kb`` parameter.
    #
    # 画素は読まないが stat は1ファイルにつき1回走る。生データはネットワークドライブ
    # (V:) 上にあるため、数万ファイルになるとこの往復だけで数分かかり、接続が切れると
    # ここで止まる。何件目のどのファイルを見ているかを進捗として送る。
    with timed_step("thorlab.filter_by_size", total=len(tiff_files),
                    target=target) as step:
        filtered_files = []
        for f in tiff_files:
            step.advance(item=f)
            if os.path.getsize(f) > min_kb * 1024:
                filtered_files.append(f)

    if not filtered_files:
        raise RuntimeError(
            "No TIFF file larger than %d KB was found; nothing to stack." % min_kb
        )

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

    # チャンネルの並びは自然順にする。素の sorted() だと CH1 < CH10 < CH2 となり、
    # XML の Wavelength 順と対応が崩れてチャンネル名がずれる。
    channel_keys = sorted(by_channel, key=natural_sort_key)

    # ヘッダを読むのは「取得ごとに1枚」で足りる。XML が面の縦横 (pixelX/pixelY) を、
    # ファイル名が並び順を決めており、ここでしか分からないのは画素の型と
    # 1ファイルあたりの面数だけだから。
    with timed_step("thorlab.probe_layout", target=target) as step:
        step.advance(item=filtered_files[0])
        layout = probe_plane_layout(filtered_files[0])

    print(f"DEBUG: Probed layout from 1 file: {layout.n_pages} page(s) per file, "
          f"Y={layout.height} X={layout.width}, dtype={layout.dtype}")

    # XML の宣言と実データが食い違っていたら、実データを採用したうえで知らせる
    # (XML は取得前の設定、ファイルは取得の結果なので、後者が事実)。
    xml_y, xml_x = params.get("SizeY"), params.get("SizeX")
    if xml_y and xml_x and (int(xml_y), int(xml_x)) != (layout.height, layout.width):
        warnings.warn(
            "[thorlab] XML declares (Y=%s, X=%s) but the TIFF header says "
            "(Y=%d, X=%d). Using the values from the image files."
            % (xml_y, xml_x, layout.height, layout.width),
            stacklevel=2,
        )

    # 1ファイル = 1面 が圧倒的多数。そうでないときだけ面数を確かめに行く。
    read_planes = dask.delayed(_read_file_planes, pure=True)

    channel_stacks = []
    for ch in channel_keys:
        ch_files = by_channel[ch]
        counts = _page_counts(ch_files, layout, target)

        # ここは遅延グラフを組み立てるだけで I/O は起きない。以前はこのループが
        # 1ファイルずつ BioImage を開いており、取り込みで最も長い工程だった。
        with timed_step("thorlab.open_tiffs", total=len(ch_files),
                        channel=ch, target=target) as step:
            blocks = []
            for f, n_pages in zip(ch_files, counts):
                step.advance(item=f)
                blocks.append(
                    da.from_delayed(
                        read_planes(f, n_pages, layout.height, layout.width,
                                    layout.dtype),
                        shape=(n_pages, layout.height, layout.width),
                        dtype=layout.dtype,
                    )
                )

        # 多ページのファイルはその全ページが面として並ぶ。以前はチャンネル内に
        # 多ページのファイルが1つでもあると、それ1つだけを残して他のファイルを
        # 黙って捨てていた (40面あるのに Z=10 のスタックが出来ていた)。
        ch_planes = blocks[0] if len(blocks) == 1 else da.concatenate(blocks, axis=0)
        channel_stacks.append(ch_planes)
        print(f"DEBUG: Channel {ch}: {len(ch_files)} file(s) → "
              f"{ch_planes.shape[0]} {mode} plane(s)")

    # 取得が途中で終わると、チャンネルによって面数が1つ違うことがある。バッチ全体を
    # 落とさず、揃っている分だけを使う (XML より短い T を許容しているのと同じ考え方)。
    plane_counts = [int(s.shape[0]) for s in channel_stacks]
    if len(set(plane_counts)) > 1:
        keep = min(plane_counts)
        warnings.warn(
            "[thorlab] チャンネルごとの面数が揃っていません (%s)。取得が途中で終了した"
            "可能性があります。共通する %d 面までで続行します。"
            % (dict(zip(channel_keys, plane_counts)), keep),
            stacklevel=2,
        )
        channel_stacks = [s[:keep] for s in channel_stacks]

    # 面を TCZYX の該当軸へ置く。mode が Z ならファイル1枚が1つの Z 面、
    # T ならファイル1枚が1つの時点。
    if mode == "Z":
        channel_stacks = [s[np.newaxis, np.newaxis] for s in channel_stacks]
    else:
        channel_stacks = [s[:, np.newaxis, np.newaxis] for s in channel_stacks]

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

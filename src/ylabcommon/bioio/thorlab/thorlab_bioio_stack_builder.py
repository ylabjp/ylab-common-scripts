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
from ylabcommon.utils.utils import natural_sort_key, sizes_from_dir_scan
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


def _note_file(exc: BaseException, text: str) -> None:
    """例外の型を変えずに「どのファイルか」だけを足す。

    ``tifffile`` の例外はファイル名を持たないものがある (``TiffFileError:
    not a TIFF file: header=b''`` / ``ValueError: failed to read 2048 bytes,
    got 896``)。3001 枚を数時間流したあとにこれだけ出ても調べようがない。
    型を包み替えると呼び出し側の ``except`` の意味が変わるので、PEP 678 の
    note として添えるだけにする。
    """
    try:
        exc.add_note(text)
    except AttributeError:      # Python < 3.11
        pass


def _thorlabs_zt(path):
    """``ChanA_<X>_<Y>_<Z>_<T>.tif`` から ``(z, t)`` を取り出す。読めなければ None。

    ThorLabs は取得の実際の次元をファイル名の連番で持っている。XML は **取得前の
    設定** なので、Z スタックを組んだまま T 連続撮影に切り替えた、途中で止めた、
    といった場合に実データとずれる。ファイルは取得の結果なので、こちらが事実。
    """
    tokens = Path(path).stem.split("_")
    if len(tokens) == 5 and tokens[3].isdigit() and tokens[4].isdigit():
        return int(tokens[3]), int(tokens[4])
    return None


def _group_by_timepoint(files):
    """``[(t, [ファイル, ...]), ...]`` を t 昇順・各時点内は z 昇順で返す。

    ファイル名が 5 トークンの数値形式でなければ None (呼び出し側が XML の mode へ
    退避する)。
    """
    indexed = []
    for f in files:
        zt = _thorlabs_zt(f)
        if zt is None:
            return None
        indexed.append((zt[1], zt[0], f))       # (t, z, path)
    indexed.sort()

    groups = []
    for t, _z, f in indexed:
        if not groups or groups[-1][0] != t:
            groups.append((t, []))
        groups[-1][1].append(f)
    return groups


def probe_plane_layout(path) -> PlaneLayout:
    """1ファイルのヘッダだけを読んで、面数・縦横・画素の型を返す。

    XML から分からないのはこの3つだけなので、同一取得であれば1枚読めば全ファイルに
    ついて分かる。画素は読まない。
    """
    try:
        with tifffile.TiffFile(str(path)) as tf:
            page = tf.pages[0]
            shape = tuple(page.shape)
            if len(shape) < 2:
                raise RuntimeError(
                    f"Unexpected TIFF page shape {shape} in {path}; expected at least 2D."
                )
            return PlaneLayout(len(tf.pages), int(shape[-2]), int(shape[-1]),
                               np.dtype(page.dtype))
    except BaseException as e:
        _note_file(e, "while reading the TIFF header of: %s" % path)
        raise


def _read_file_planes(path, n_pages, height, width, dtype):
    """遅延読みの実体。1ファイルを ``(n_pages, Y, X)`` として返す。

    ヘッダを1枚しか読んでいない以上、残りのファイルが本当に同じ形かはここで初めて
    分かる。食い違いは黙って通さず、**どのファイルか** を添えて落とす
    (dask の shape 不一致エラーはファイル名を持たないため)。
    """
    try:
        arr = tifffile.imread(str(path))
    except OSError as e:
        # ネットワークドライブが落ちたときに、どのファイルで落ちたかを残す。読むのは
        # compute 時 (書き出しや解析側) なので、呼び出し側の try では捕まえきれない。
        # errno はそのまま引き継ぐ (sorter 側が EIO かどうかで分岐するため)。
        raise OSError(e.errno, "%s (while reading %s)"
                      % (e.strerror or type(e).__name__, path)) from e
    except BaseException as e:
        # 途中で切れたファイルは OSError ではなく ``ValueError: failed to read
        # 2048 bytes, got 896`` で落ちる。errno を持たないので包み替えられないが、
        # ファイル名が無いのは同じように困るので note で添える。
        _note_file(e, "while reading the pixels of: %s" % path)
        raise

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

    # Size filter (NOT a pixel read). ``tiff_files`` is already sorted by
    # collect_valid_tiffs; honor the ``min_kb`` parameter.
    #
    # 以前は ``os.path.getsize(f)`` を1ファイルずつ呼んでいた。画素は読まないが往復は
    # 1件1回で、生データがネットワークドライブ (V:) 上にあるため数万ファイルでは
    # この stat だけで数分かかり、接続が切れるとここで止まっていた。
    #
    # サイズはディレクトリ列挙の応答に最初から入っているので、ディレクトリごとに1回
    # 列挙すれば全件分が追加の往復なしで揃う (:func:`sizes_from_dir_scan`)。
    # 3001 回の stat が 1 回の列挙になる。
    #
    # ただし列挙が返すサイズは、書き込み中のファイルに対して古い値になりうる
    # (Windows のメタデータ更新は遅延する)。そこで **捨てる判断だけ** は
    # ``os.path.getsize`` で裏を取る。正常系では 0 回、取りこぼしかけた件数ぶんしか
    # 追加の往復が発生せず、「取得と並行して走らせたら数枚落ちた」を防げる。
    # 列挙で止まったときに「どのディレクトリを見ているか」を名指しできるよう、
    # ディレクトリに入る直前に item を差し替える (ファイル単位のループと同じ読み方)。
    with timed_step("thorlab.scan_sizes", total=len(tiff_files),
                    target=target) as scan_step:
        scan_step.advance(n=0, item=target)
        scanned_sizes = sizes_from_dir_scan(
            tiff_files, on_directory=lambda d: scan_step.advance(n=0, item=d))
        scan_step.advance(n=len(scanned_sizes), item=target)

    with timed_step("thorlab.filter_by_size", total=len(tiff_files),
                    target=target, scanned=len(scanned_sizes)) as step:
        filtered_files = []
        threshold = min_kb * 1024
        for f in tiff_files:
            step.advance(item=f)
            size = scanned_sizes.get(f)
            if size is None or size <= threshold:
                # 列挙で拾えなかった / 小さすぎるように見えるものだけ個別に確認する。
                size = os.path.getsize(f)
            if size > threshold:
                filtered_files.append(f)

    # ``tiff_files`` は collect_valid_tiffs が自然順に並べたものなので、ここで
    # 並べ直さない。素の sorted() は ChanA_00002 < ChanA_00010 を保証しないため、
    # ゼロ埋めが崩れた取得で面の順序が入れ替わりうる。
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
            blocks = {}
            for f, n_pages in zip(ch_files, counts):
                step.advance(item=f)
                blocks[f] = da.from_delayed(
                    read_planes(f, n_pages, layout.height, layout.width,
                                layout.dtype),
                    shape=(n_pages, layout.height, layout.width),
                    dtype=layout.dtype,
                )

        # 多ページのファイルはその全ページが面として並ぶ。以前はチャンネル内に
        # 多ページのファイルが1つでもあると、それ1つだけを残して他のファイルを
        # 黙って捨てていた (40面あるのに Z=10 のスタックが出来ていた)。
        # ファイル名の Z / T 連番が両方とも動いているなら 4D 取得 (Z スタックの
        # タイムラプス)。この形は mode がどちらであっても1軸には収まらないので、
        # ファイル名の通りに組む。片方しか動いていない取得は従来どおり mode に従う
        # (どちらの軸に積むかはファイル名だけからは決められないため)。
        groups = _group_by_timepoint(ch_files)
        if groups is not None:
            n_t = len(groups)
            n_z = max(len(fs) for _t, fs in groups)
            print(f"DEBUG: Channel {ch}: file names give Z={n_z} x T={n_t} "
                  f"(XML says SizeZ={params.get('SizeZ')} SizeT={params.get('SizeT')}, "
                  f"mode={mode})")
            if n_t < 2 or n_z < 2:
                groups = None                   # 片方しか動いていない = 4D ではない
        if groups is None:
            planes = da.concatenate([blocks[f] for f in ch_files], axis=0)
            vol = planes[np.newaxis] if mode == "Z" else planes[:, np.newaxis]
        else:
            per_t = []
            for _t, fs in groups:
                zs = [blocks[f] for f in fs]
                per_t.append(zs[0] if len(zs) == 1 else da.concatenate(zs, axis=0))

            # 取得が途中で終わると、最後の時点だけ Z が欠ける。欠けた時点を捨てて
            # 揃っている分で続ける (XML より短い T を許容しているのと同じ考え方)。
            depths = [int(v.shape[0]) for v in per_t]
            full = max(set(depths), key=depths.count)
            if len(set(depths)) > 1:
                dropped = [t for (t, _), d in zip(groups, depths) if d != full]
                warnings.warn(
                    "[thorlab] %d of %d timepoints have an incomplete Z stack "
                    "(expected Z=%d, got %s); dropping them. The acquisition was "
                    "probably stopped mid-stack. Dropped timepoints: %s"
                    % (len(dropped), len(depths), full,
                       sorted({d for d in depths if d != full}),
                       dropped[:10] + (["..."] if len(dropped) > 10 else [])),
                    stacklevel=2,
                )
                per_t = [v for v, d in zip(per_t, depths) if d == full]
            vol = da.stack(per_t, axis=0)               # (T, Z, Y, X)

        channel_stacks.append(vol)
        print(f"DEBUG: Channel {ch}: {len(ch_files)} file(s) → "
              f"T={vol.shape[0]} Z={vol.shape[1]}")

    # 取得が途中で終わると、チャンネルによって時点数 (または面数) が1つ違うことがある。
    # バッチ全体を落とさず、揃っている分だけを使う (XML より短い T を許容しているのと
    # 同じ考え方)。T 側と Z 側のどちらがずれるかは取得の切れ方で変わるので両方見る。
    for axis, label in ((0, "時点数"), (1, "面数")):
        counts = [int(s.shape[axis]) for s in channel_stacks]
        if len(set(counts)) > 1:
            keep = min(counts)
            warnings.warn(
                "[thorlab] チャンネルごとの%sが揃っていません (%s)。取得が途中で終了した"
                "可能性があります。共通する %d までで続行します。"
                % (label, dict(zip(channel_keys, counts)), keep),
                stacklevel=2,
            )
            channel_stacks = [s[:keep] if axis == 0 else s[:, :keep]
                              for s in channel_stacks]

    # (T, Z, Y, X) へ C を挿して TCZYX にする。単一チャンネルでも 5D になる。
    channel_stacks = [s[:, np.newaxis] for s in channel_stacks]

    # Stack channels along C (axis 1). A single channel still yields a full 5D
    # TCZYX array.
    if len(channel_stacks) == 1:
        stacked = channel_stacks[0]
    else:
        stacked = da.concatenate(channel_stacks, axis=1)

    # XML の宣言と実データが食い違っていたら、実データを採用したうえで知らせる。
    # 「Z スタックのつもりで組んだが T 連続撮影になっていた」ときにここが黙ると、
    # 3001 時点が Z 軸へ潰れて深さ 1500 um のスタックが静かに出来上がる。
    xml_z, xml_t = params.get("SizeZ"), params.get("SizeT")
    got_t, got_z = int(stacked.shape[0]), int(stacked.shape[2])
    if mode == "Z" and xml_z and int(xml_z) != got_z:
        warnings.warn(
            "[thorlab] The XML was configured for a Z stack of SizeZ=%s (SizeT=%s), "
            "but this build produced Z=%d over T=%d. Every plane has been put on the "
            "Z axis because the file names show only one varying index. If this "
            "acquisition is really a time series, the Z axis is wrong (it would make "
            "the stack %.1f um deep) — check the file name layout before using the "
            "result."
            % (xml_z, xml_t, got_z, got_t, got_z * params.get("PixelSizeZ", 1.0)),
            stacklevel=2,
        )

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

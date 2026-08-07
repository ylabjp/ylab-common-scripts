from pathlib import Path
from typing import List, Tuple
from collections import defaultdict
import os
import tifffile
import xarray as xr
import numpy as np
import dask.array as da
import xml.etree.ElementTree as ET
from dask import delayed
from bioio_tifffile import Reader as TiffReader
from bioio_tifffile.reader import UNKNOWN_DIM_CHARS
#from bioio.readers import Reader as TiffReader
from bioio import BioImage
from bioio_base.dimensions import DEFAULT_CHUNK_DIMS, REQUIRED_CHUNK_DIMS
from bioio_base.exceptions import UnsupportedFileFormatError
from bioio_base.transforms import reshape_data
from ylabcommon.utils.normalize_bioImage import normalize_to_tczyx
from ylabcommon.utils.outfile_name import extract_dimensions, is_mosaic
from ylabcommon.utils.parallel import ordered_bounded_map
from ylabcommon.utils.perf import timed_step
from ylabcommon.utils.utils import sizes_from_dir_scan


# 1ファイルのヘッダを読むのに BioImage は open() を 3 回行う (検証用・メタデータ用・
# シーン数の数え上げ用)。ローカル SSD では 1 件 2.5 ms で終わるが、SMB 越しでは
# 1 往復 30 ms として 1 件 90 ms、3001 件で 4 分半かかる。この待ちはほぼ全部が
# ソケット待ちで GIL を手放しているので、ワーカースレッドで往復を重ねられる。
#
# 既定値は控えめの 4。共有側が弱っているときは同時要求を増やすほど1件あたりが
# 遅くなるため、大きくしても効かない (実測: 負荷で劣化する共有では W=4 で 1.7x、
# W=64 まで増やしても 2.1x で頭打ち)。ローカルディスクではヘッダ読みが
# GIL 保持の純 Python 処理なので、並行化しても速くならず僅かに遅くなる。
HEADER_WORKERS_ENV = "YLAB_TIFF_HEADER_WORKERS"
DEFAULT_HEADER_WORKERS = 4


def _header_workers() -> int:
    """ヘッダ読みの並行数。``YLAB_TIFF_HEADER_WORKERS`` で上書きでき、1 で逐次に戻る。"""
    raw = os.environ.get(HEADER_WORKERS_ENV)
    if not raw:
        return DEFAULT_HEADER_WORKERS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_HEADER_WORKERS


# bioio がチャンクにまとめる次元。これ以外の次元 (T/C など) を持つファイルは
# bioio 側で次元ごとに別チャンクへ切り分けられるため、後述の高速経路には乗せない。
# 集合は bioio の定数から作るので、上流が変えれば追従する。
_BIOIO_CHUNKED_DIMS = (frozenset(d.upper() for d in DEFAULT_CHUNK_DIMS)
                       | frozenset(d.upper() for d in REQUIRED_CHUNK_DIMS))


def _note_file(exc: BaseException, text: str) -> None:
    """例外の型を変えずに「どのファイルか」だけ traceback へ添える。"""
    add_note = getattr(exc, "add_note", None)
    if add_note is not None:
        add_note(text)


def _read_tiff_pixels(path: str):
    """遅延チャンクの実体。``compute()`` されたときに初めて画素を読む。

    ``tifffile.imread(..., series=0, level=0, is_mmstack=False)`` は
    ``bioio_tifffile.Reader._get_image_data`` が全チャンクを要求されたときと
    同じ読み方で、open は1回。

    例外にファイル名を添えるのが本関数の存在理由でもある。ストリーミング書き出しの
    最中に壊れたファイルへ当たると、tifffile が投げるのは
    ``failed to read 2097152 bytes, got 999744`` のように **どのファイルか分からない**
    メッセージで、数時間流したあとの調査ではほぼ役に立たなかった。
    """
    try:
        return tifffile.imread(path, series=0, level=0, is_mmstack=False)
    except BaseException as e:
        _note_file(e, "while reading pixels from: %s" % path)
        raise


def _tiff_dims(series) -> str:
    """``bioio_tifffile.Reader._guess_tiff_dim_order`` と同じ次元文字列を返す。

    判定そのものは bioio の static メソッド (``_merge_dim_guesses`` /
    ``_guess_dim_order``) をそのまま呼ぶので、次元推定の規則は bioio と一致し続ける。
    """
    axes = "".join(series.pages.axes)
    if all(c not in UNKNOWN_DIM_CHARS for c in axes):
        return axes
    return "".join(TiffReader._merge_dim_guesses(
        axes, TiffReader._guess_dim_order(tuple(series.shape))))


def _read_tiff_header(f: str) -> Tuple[Tuple[int, ...], np.dtype, str]:
    """``(shape, dtype, dims)`` を **open 1回** で読む。画素は読まない。

    ネットワークへ触るのはこの関数だけなので、止まる/失敗する箇所を差し替えたい
    テストはここを monkeypatch すればよい (以前の ``BioImage`` に相当する継ぎ目)。

    失敗時の例外型は ``BioImage`` に合わせて ``UnsupportedFileFormatError`` に
    そろえる。``bioio_tifffile.Reader._is_supported_image`` が
    ``except Exception`` で同じ包み方をしていたので、そのままだと開けない
    ファイル (0 バイト / TIFF でない / パーミッション) の型が静かに変わり、
    呼び出し側の except 節をすり抜ける。元の例外文はそのまま残す。

    Note:
        この包み方は bioio 由来の難点も一緒に引き継いでいる。共有が落ちた
        ときの I/O エラーまで「対応していない形式」として報告されてしまう。
        直すなら ``OSError`` を素通しする形にすべきだが、それは例外契約の変更
        なので本変更の範囲外にしてある。
    """
    try:
        with tifffile.TiffFile(f, is_mmstack=False) as tf:
            series = tf.series[0]
            return (tuple(int(s) for s in series.shape), series.dtype,
                    _tiff_dims(series))
    except Exception as e:
        raise UnsupportedFileFormatError("bioio-tifffile", str(f), str(e)) from e


def _open_lazy_tczyx(f: str):
    """1ファイルを TCZYX の遅延 dask 配列として開く (画素は読まない)。

    **ファイルを開く回数が 3 回から 1 回になる。** ``BioImage(f, reader=TiffReader)``
    は1ファイルにつき open を 3 回行うが、そのうち使うのは1回分だけである
    (実測: 10 ファイルで openat 30 回。内訳は
    ``Reader._is_supported_image`` の「本当に TIFF か」の検証 — 結果は捨てられる、
    ``_read_delayed`` の本命のヘッダ読み、``Reader.scenes`` の
    ``len(tiff.series)`` を数えるためだけの再解析)。ローカル SSD では 1 件 2.5 ms
    なので誰も気付かないが、SMB 越しでは 1 open が 30 ms 級になり、3001 件で
    9003 回 = 4 分半が丸ごとここに乗る。

    そこで ``tifffile.TiffFile`` を **1回だけ** 開いて shape/dtype/次元を読み、
    画素は ``da.from_delayed`` の中に閉じ込める。**ファイルごとに開くことは
    やめていない**ので、形が揃っている前提は一切置いていない。プレーンごとに
    解像度が違えば従来どおりグラフ構築時に ``Shapes do not align`` で落ちる。

    Fallback:
        bioio はチャンク対象外の次元 (T/C) を持つファイルを次元ごとに別チャンクへ
        分ける。同じチャンク分割を手で再現すると bioio の内部実装を写経することに
        なるので、そういうファイルは **従来どおり ``BioImage`` で開く**。
        Thorlabs の生データ (単一プレーン YX / 多ページ ZYX) は必ず高速経路に乗り、
        混ざり込んだ OME/ImageJ 由来のファイルだけが 3 open のままになる。
        高速経路の出力が従来と shape/chunks/dtype/画素まで一致することは
        ``tests/test_tiff_header_roundtrips.py`` で固定している。

    ワーカースレッドから呼ばれる。``BioImage`` はモジュール属性として参照するので、
    テストからの monkeypatch がそのまま効く。

    ``chunk_dims`` を明示的に**新しいリスト**で渡している点が並行化に伴う要点。
    既定値 ``bioio_base.dimensions.DEFAULT_CHUNK_DIMS`` はモジュールレベルの
    *可変* リストで、各 Reader インスタンスはそれを参照で共有したうえ
    ``bioio_tifffile/reader.py:374`` が ``self.chunk_dims.append(...)`` で書き換える。
    現行のバージョンでは追加すべき次元が既に入っているため発火しないが、逐次実行では
    無害だったこの共有可変状態は、複数スレッドから同時に触ると壊れうる。
    1インスタンス1リストにしておけば、上流の定数が変わっても競合しない。
    """
    try:
        shape, dtype, dims = _read_tiff_header(f)

        if len(dims) != len(shape) or not set(dims) <= _BIOIO_CHUNKED_DIMS:
            # 高速経路でチャンク分割を再現できない形。従来の経路へ落とす。
            return BioImage(
                f, reader=TiffReader, chunk_dims=list(DEFAULT_CHUNK_DIMS),
            ).get_image_dask_data("TCZYX")

        arr = da.from_delayed(
            delayed(_read_tiff_pixels, pure=True)(f), shape=shape, dtype=dtype,
        )
        return reshape_data(arr, given_dims=dims, return_dims="TCZYX")
    except BaseException as e:
        # 例外の型は変えない (呼び出し側の except OSError などをそのまま通す)。
        # どのファイルで落ちたかだけを traceback に添える。
        _note_file(e, "while reading the TIFF header of: %s" % f)
        raise


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
        kept = []
        threshold = min_kb * 1024
        for f in tiff_files:
            step.advance(item=f)
            size = scanned_sizes.get(f)
            if size is None or size <= threshold:
                # 列挙で拾えなかった / 小さすぎるように見えるものだけ個別に確認する。
                size = os.path.getsize(f)
            if size > threshold:
                kept.append(f)
        filtered_files = sorted(kept)

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
        # Read each file for this channel lazily as a 5D TCZYX dask array.
        #
        # 遅延なのは「画素」だけで、ヘッダを読むために1ファイルずつ開くこと自体は
        # 避けられない (形が揃っている前提を置かないため)。数千ファイルをネットワーク
        # ドライブ越しに開くので取り込みの中で最も長く、途中で止まるとしても最有力の箇所。
        # 1件ずつ進捗を刻んでおけば、沈黙しても heartbeat が「どのファイルを掴んだまま
        # 止まっているか」を名指しできる。
        ch_files = by_channel[ch]
        workers = _header_workers()
        with timed_step("thorlab.open_tiffs", total=len(ch_files),
                        channel=ch, target=target, workers=workers) as step:
            # 投入は先行させるが回収は入力順。したがって done / item の意味は逐次
            # ループのときと同じままで、「done 件目まで着手済み、いま item を掴んで
            # 止まっている」と読める。ordered_bounded_map の docstring を参照。
            arrs = ordered_bounded_map(
                _open_lazy_tczyx, ch_files, max_workers=workers, step=step,
                thread_name_prefix="ylab-tiff-header",
            )

        # Prefer a single multi-page file (mode axis already > 1) if the channel
        # has one; otherwise concatenate the individual planes along the mode axis.
        multi = [a for a in arrs if a.shape[axis] > 1]
        if multi and len(arrs) > 1:
            # ここは以前 max(multi, key=...) で「最も大きい1ファイル」だけを採用しており、
            # 同じチャンネルの残りのファイルを黙って捨てていた (10ページ x 3ファイルなら
            # 30 プレーンのうち 10 しか残らず、しかも警告も出ない)。
            # 複数の多ページファイルが「1つのスタックの連続した一部」なのか「同じものの
            # 別コピー」なのかはファイル名からは決められないので、取り違えたスタックを
            # 黙って作らず、mosaic 判定と同じく明示的に失敗させる。
            raise RuntimeError(
                "Channel %s has %d files and %d of them contain multiple %s slices "
                "(%s sizes: %s). It is ambiguous whether these files are consecutive "
                "parts of one stack (which should be concatenated) or alternative copies "
                "of the same stack (one of which should be chosen), so the stack cannot be "
                "built safely. Note the previous behaviour silently kept only the largest "
                "file and discarded the rest, which lost data. Either arrange the "
                "acquisition so each channel has a single multi-page file or only "
                "single-plane files, or decide the intended rule and update "
                "stack_thorlab_with_bioio_calibrated."
                % (ch, len(arrs), len(multi), mode, mode,
                   [int(a.shape[axis]) for a in arrs])
            )
        if multi:
            ch_stack = multi[0]
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

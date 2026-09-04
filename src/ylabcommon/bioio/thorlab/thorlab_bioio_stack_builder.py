"""ThorLabs の生 TIFF 群を遅延 (dask) の TCZYX スタックへ組み立てる。

設計方針:

**入力側は BioImage を使わない。** 生データの形は Experiment.xml とファイル名で
決まっており、画素の型と1ファイルあたりの面数だけが XML から分からない。読むのは
その2つを得るための TIFF ヘッダだけで、画素には触れない。以前は ``BioImage(f)`` を
全ファイルに対して呼んでいたが、遅延なのは画素だけでヘッダは1ファイルずつ順に読むため、
数万ファイルをネットワークドライブ越しに開くことになり取り込みの中で最も長い工程に
なっていた。面数は取得ごとに1枚では決まらない (:func:`_page_counts`) ので全件読むが、
読むのはヘッダだけで、しかも並行して読む。

**組み立ては最初から最後まで dask。** ``dask.delayed`` した読み取りを
:func:`dask.array.from_delayed` で束ねるので、グラフ構築の時点では I/O が一切起きない。
画素が実際に読まれるのは書き出し時 (または解析側が compute したとき) の1回だけ。

出力は従来どおり bioio (OME-TIFF/Zarr) が担当する。
"""

from typing import Any, Optional, Sequence
from pathlib import Path
from collections import defaultdict, namedtuple
from concurrent.futures import ThreadPoolExecutor
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


def get_channel_names_index(xml_path: Any) -> list[str]:
    """Return the Thorlabs channel (wavelength) names from an Experiment.xml.

    Accepts a path to Experiment.xml. Each ``<Wavelength name="...">`` becomes one
    channel name. Falls back to ``["Channel 0"]`` if the file cannot be parsed.
    """
    try:
        names = ExperimentXMLParser(xml_path).extract_metadata()["Channels"]
    except Exception:
        return ["Channel 0"]
    return list(names) if names else ["Channel 0"]


def _thorlabs_channel_key(path: Any) -> str:
    """Channel identifier parsed from a Thorlabs TIFF filename.

    Thorlabs raw files look like ``ChanA_00001_00002_00003_00004.tif`` — the
    channel token (``ChanA`` / ``ChanB`` / ... or ``CH1``) is what distinguishes
    channels. Returns that token, or the whole stem if none is found (so all files
    collapse into a single channel). Mirrors the convention used by
    ``file_selection``.
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


#: 枠を埋めた結果。``kept`` が実際に使う (t, z) の並び、``cut`` は理由ごとの件数。
FilledFrame = namedtuple("FilledFrame", "slots t_keep z_keep unreadable outside ragged")


def _thorlabs_zt(path: Any) -> Optional[tuple[int, int]]:
    """ファイル名の末尾2つの数値連番を ``(z, t)`` として返す。読めなければ None。

    ThorLabs は取得の実際の次元をファイル名の連番で持っている
    (``ChanA_<X>_<Y>_<Z>_<T>.tif``)。「ちょうど5トークン」ではなく **末尾の2つ**
    を見るのは、接頭辞が増えた形 (``Image_ChanA_001_001_001_0001.tif``) でも
    同じ規約が続くため。
    """
    nums = [t for t in Path(path).stem.split("_") if t.isdigit()]
    if len(nums) < 2:
        return None
    return int(nums[-2]), int(nums[-1])


def _is_preview(path: Any) -> bool:
    """ThorImage が取得ごとに書き出す表示用ファイル (``ChanA_Preview.tif``) か。

    取得中の画面表示のためのもので、面ではない。XML の
    ``<Streaming previewIndex="1"/>`` に対応し、**どの取得にも必ず出る**。
    """
    return Path(path).stem.lower().split("_")[-1] == "preview"


def _fill_frame(files: Any, max_t: Any, max_z: Any) -> FilledFrame:
    """XML が決めた枠 ``(max_t, max_z)`` をファイルで埋め、埋まらない分を落とす。

    取り込みの組み立てはこの3段階だけで決まる。

    1. **枠** は XML が決める (``SizeT`` x ``SizeZ``)。取得の上限であって、
       実際にそこまで撮れたかは XML には書かれていない。
    2. **埋める** のはファイル。名前の末尾2つの連番が (z, t) の位置を指す。
    3. **埋まらなかった分はカットする**。取得を途中で止めれば後ろの時点は空のまま、
       止めた瞬間の時点は Z が途中まで、という形で必ず端に穴が空く。

    枠は **目標であって上限ではない**。枠からはみ出したファイルは捨てずに使い、
    件数だけ報告する。この装置の XML は当てにならないことが分かっており
    (Z スタックの設定が残ったまま T 連続撮影される)、当てにならない値を上限に
    使うと実在する面を落としてしまう。``SizeZ`` は面数で数えるがファイル名の Z は
    ファイル番号なので、多ページのファイルが混ざると単位も合わない。
    枠が効くのは「宣言された枡が埋まらなかったとき」だけで、それは下の
    カットで自然に処理される。

    連番が読めないファイルはどの枡も指せないので落ちる。1枚混ざっただけで
    チャンネル全体を諦めてはいけない (それが不具合を覆い隠していた)。

    「カット」の切り方だけは自明でない。最後の1時点だけ Z が欠けたとき、
    「全時点に共通する z」を採るとその1時点のために全時点の z が削れ、
    「全 z が揃う時点」を採ると欠けた1時点だけが落ちる。どちらが正しいかは
    件数で決まるので、**残る面の数が最大になる長方形** を選ぶ
    (61面 x 49時点 = 2989 と、12面 x 50時点 = 600 なら前者)。
    """
    slots, unreadable, outside = {}, [], []
    for f in files:
        zt = _thorlabs_zt(f)
        if zt is None:
            unreadable.append(f)
            continue
        z, t = zt
        if t > max_t or z > max_z:
            outside.append(f)           # 使うが、XML とずれている事実は残す
        slots[(t, z)] = f

    if not slots:
        return FilledFrame({}, [], [], unreadable, outside, [])

    z_counts: dict[int, int] = defaultdict(int)
    for _t, z in slots:
        z_counts[z] += 1
    t_values = sorted({t for t, _z in slots})

    best = None
    for threshold in sorted(set(z_counts.values())):
        z_keep = sorted(z for z, n in z_counts.items() if n >= threshold)
        t_keep = [t for t in t_values
                  if all((t, z) in slots for z in z_keep)]
        score = len(z_keep) * len(t_keep)
        if best is None or score > best[0]:
            best = (score, t_keep, z_keep)

    assert best is not None  # slots が空でないので必ず 1 周は回る
    _score, t_keep, z_keep = best
    ragged = [t for t in t_values if t not in set(t_keep)]
    return FilledFrame(slots, t_keep, z_keep, unreadable, outside, ragged)


def _drop_odd_depths(ch: Any, t_values: Any, per_t: Any) -> tuple[list, list]:
    """面数が多数派と違う時点を落とし、``(t_values, per_t)`` を返す。

    枠の枡が揃っていても、多ページのファイルが1枚混ざればその時点だけ面数が変わる。
    そのままだと ``da.stack`` が組めない (組めてしまうと形が壊れる)。
    黙って落とさず、時点・面数・多数派の面数を添えて報告する。

    「ファイルごとの面数」ではなく「時点ごとの面数」で見るのが要点。単一時点の
    Z スタックは多ページと単一ページのファイルが混在してよく (10ページ1枚 +
    1ページ30枚 = Z=40)、そこを止めてはいけない。
    """
    depths = [int(v.shape[0]) for v in per_t]
    if len(set(depths)) <= 1:
        return t_values, per_t

    tally: dict[int, int] = defaultdict(int)
    for d in depths:
        tally[d] += 1
    normal = max(tally, key=lambda d: (tally[d], -d))

    dropped = [(t, d) for t, d in zip(t_values, depths) if d != normal]
    warnings.warn(
        "[thorlab] Channel %s: %d of %d timepoint(s) do not hold %d plane(s) and were "
        "cut — they cannot be stacked with the rest. Found %s. A timepoint with far "
        "more planes than the others is usually a file that does not belong to this "
        "acquisition."
        % (ch, len(dropped), len(depths), normal,
           ", ".join("t=%d has %d" % (t, d) for t, d in dropped[:5])),
        stacklevel=2,
    )
    kept = [(t, v) for t, v, d in zip(t_values, per_t, depths) if d == normal]
    return [t for t, _v in kept], [v for _t, v in kept]


def _examples(files: Any, n: int = 3) -> str:
    return ", ".join(Path(f).name for f in files[:n]) + (" ..." if len(files) > n else "")


def _interior_gaps(kept: Sequence[int]) -> list[int]:
    """採用した連番の **内側** で抜けている番号を返す。

    末尾が短いのは正常である —— 取得を止めれば後ろが空く。**内側の穴は別物**で、
    残りを詰めて積むと目盛りが狂う: z なら面と面の間隔が、t なら時点の間隔が、
    穴の分だけ広がったまま「連続」として扱われる。画素は全部入っているので
    出来上がりは正常に見え、解析まで進んでから初めておかしいと気付く。
    """
    if len(kept) < 2:
        return []
    have = set(kept)
    return [v for v in range(kept[0], kept[-1] + 1) if v not in have]


def _report_cuts(ch: Any, ch_files: Any, frame: Any, max_t: Any, max_z: Any) -> None:
    """枠のどこが埋まり、何が落ちたかを1行で出し、落ちた分は警告する。

    カットは正常系でも起きる (取得を止めれば必ず後ろが空く) ので、落ちたこと自体は
    エラーにしない。ただし **黙って落とさない**: 何枚がなぜ落ちたかが出ていないと、
    出来上がったスタックが正しいのか確かめようがない。
    """
    print(f"DEBUG: Channel {ch}: frame {max_t}T x {max_z}Z from the XML, filled "
          f"{len(frame.slots)}/{len(ch_files)} file(s) → "
          f"T={len(frame.t_keep)} Z={len(frame.z_keep)}")

    if frame.unreadable:
        warnings.warn(
            "[thorlab] Channel %s: %d file(s) have no Z/T sequence numbers in their "
            "name, so they cannot be placed in the acquisition frame and were cut "
            "(%s). Thorlabs raw files end in two numeric fields, "
            "e.g. ChanA_001_001_<Z>_<T>.tif."
            % (ch, len(frame.unreadable), _examples(frame.unreadable)),
            stacklevel=2,
        )
    if frame.outside:
        warnings.warn(
            "[thorlab] Channel %s: %d file(s) sit beyond the frame the XML declares "
            "(SizeT=%d x SizeZ=%d) — the XML is out of date with the data. They were "
            "kept, since the files record what was actually acquired (%s)."
            % (ch, len(frame.outside), max_t, max_z, _examples(frame.outside)),
            stacklevel=2,
        )
    if frame.ragged:
        warnings.warn(
            "[thorlab] Channel %s: %d timepoint(s) do not have all %d plane(s) and "
            "were cut (t=%s). The acquisition probably stopped mid-stack."
            % (ch, len(frame.ragged), len(frame.z_keep),
               frame.ragged[:5] + (["..."] if len(frame.ragged) > 5 else [])),
            stacklevel=2,
        )

    # **目盛りが狂う落ち方だけは別に言う。**
    #
    # 枠の埋め方は「残る面の数を最大にする」規則で選んでいる (:func:`_fill_frame`)。
    # 端の欠けにはこれで正しく働くが、**途中の面が 1 枚欠けた**ときは
    # 「その z を全時点から捨てて、時点を全部残す」方が面数が多くなるため、
    # 深さの途中が抜けたまま詰めて積まれる。
    #
    #   z=1..5 / t=1..3 で (z=3, t=2) の 1 枚だけ欠ける
    #     -> z を捨てる: 4 面 x 3 時点 = 12 面   <- こちらが選ばれる
    #     -> t を捨てる: 5 面 x 2 時点 = 10 面
    #
    # 出来上がりは Z=4 の綺麗なスタックに見えるが、3 番目の面は実際には
    # 元の 4 番目である。**面の間隔が 1 か所だけ 2 倍になっている。**
    # ROI の深さも 3D 位置合わせも、この目盛りの上で狂う。
    # 落ちた枚数ではなく **並びの穴** で見るのが要点 —— 末尾の欠けは正常なので、
    # 枚数で見ると正常系でも鳴ってしまう。
    z_holes = _interior_gaps(frame.z_keep)
    if z_holes:
        discarded = sum(1 for (_t, z) in frame.slots if z not in set(frame.z_keep))
        warnings.warn(
            "[thorlab] Channel %s: plane(s) z=%s are missing from the middle of the "
            "stack and were cut from every timepoint, so the kept planes (z=%s) are "
            "packed together and the spacing between them is no longer uniform. "
            "%d file(s) were read past and discarded. Depth measurements and 3D "
            "registration will be wrong on this stack. Check whether those planes "
            "exist in the acquisition folder."
            % (ch, z_holes, frame.z_keep, discarded),
            stacklevel=2,
        )

    t_holes = _interior_gaps(frame.t_keep)
    if t_holes:
        warnings.warn(
            "[thorlab] Channel %s: timepoint(s) t=%s are missing from the middle of "
            "the series, so the kept timepoints are packed together and the interval "
            "between them is no longer uniform. Frame n in the stack is not "
            "necessarily the n-th timepoint of the acquisition."
            % (ch, t_holes[:10] + (["..."] if len(t_holes) > 10 else [])),
            stacklevel=2,
        )


#: TIFF を開くときは必ずこれを付ける。:data:`_ONE_FILE_ONLY` の説明を読むこと。
#:
#: OME-TIFF は「1つのデータセットが複数ファイルにまたがる」形を持てる。先頭ファイルの
#: OME XML が ``<TiffData><UUID FileName="ChanA_..._0002.tif">`` のように **兄弟ファイルを
#: 名前で指し**、tifffile はそれを追って同じフォルダのファイルを次々に開き、1つの配列に
#: 組み上げて返す。取得フォルダの生ファイル1枚にこの XML が入っていると、そのファイルを
#: 開くだけで取得全体 (3001 枚 x 2ch) が読まれる。
#:
#: 実測 (兄弟6枚を指す OME マスターを1枚読む):
#:
#: ===================  ==========  ==============  ==============
#: 設定                 開いた tif  imread の shape  返る画素
#: ===================  ==========  ==============  ==============
#: 既定 (multifile)     6 個        (6, 8, 8)       兄弟6枚の中身
#: ``_multifile=False`` 1 個        (8, 8)          自分の1面だけ
#: ===================  ==========  ==============  ==============
#:
#: この取り込みでは面の配置をファイル名と Experiment.xml で決めているので、tifffile に
#: 束ねてもらう必要は無い。束ねられると (a) 1枚読むのに数千往復かかり、(b) 宣言された
#: 面数と自分の面数が食い違って ``Page count mismatch`` になる。**1ファイルは1ファイルとして
#: 読む** のが正しい。
_ONE_FILE_ONLY = {"_multifile": False}


def probe_plane_layout(path: Any) -> PlaneLayout:
    """1ファイルのヘッダだけを読んで、面数・縦横・画素の型を返す。画素は読まない。

    数えるのは ``tf.series[0]`` の面数であって ``len(tf.pages)`` ではない。読み取り側
    (:func:`_read_file_planes`) が使う ``tifffile.imread`` が返すのは series[0] なので、
    **ここで数える対象を読み取り側と一致させないと、組み立て時の検査が素通りする**。

    この2つは実際に食い違う。縮小サムネイルを持つファイルは ``len(tf.pages)`` が 2 でも
    読めるのは 1 面である。
    """
    try:
        with tifffile.TiffFile(str(path), **_ONE_FILE_ONLY) as tf:  # type: ignore[arg-type]
            series = tf.series[0]
            shape = tuple(series.shape)
            if len(shape) < 2:
                raise RuntimeError(
                    f"Unexpected TIFF shape {shape} in {path}; expected at least 2D."
                )
            n_planes = int(np.prod(shape[:-2])) if len(shape) > 2 else 1
            return PlaneLayout(n_planes, int(shape[-2]), int(shape[-1]),
                               np.dtype(series.dtype))
    except BaseException as e:
        _note_file(e, "while reading the TIFF header of: %s" % path)
        raise


def _read_file_planes(path: Any, n_pages: Any, height: Any, width: Any,
                      dtype: Any) -> np.ndarray:
    """遅延読みの実体。1ファイルを ``(n_pages, Y, X)`` として返す。

    ヘッダを1枚しか読んでいない以上、残りのファイルが本当に同じ形かはここで初めて
    分かる。食い違いは黙って通さず、**どのファイルか** を添えて落とす
    (dask の shape 不一致エラーはファイル名を持たないため)。
    """
    try:
        arr = tifffile.imread(str(path), **_ONE_FILE_ONLY)  # type: ignore[call-overload]
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


#: ヘッダ読みの同時実行数。
#:
#: 1件あたりの内訳を実測した (ローカル・キャッシュ済みで計ると I/O 待ちが消えるので、
#: 残るのが Python の処理時間になる):
#:
#: - Python の処理: **0.23 ms** (ヘッダ数百バイトの解釈だけ。画素は読まない)
#: - SMB 越しの実時間: **約 320 ms** (`\\yg-storage4` で 50 件/秒 x 16 並列から逆算)
#:
#: つまり 99.9% が往復の待ちで、CPU も RAM もほとんど使っていない。1ファイル開くのに
#: SMB は CREATE / QUERY_INFO / READ x n / CLOSE と往復するので、こうなる。
#: 待ちは並べれば重なるから、同時実行数がそのまま実時間に効く。
#: 3001 枚 x 2ch で 16 並列だと 2 分、64 並列なら 30 秒ほど。
_PROBE_WORKERS = 64


def _page_counts(files: Any, layout: Any, target: Any) -> list[int]:
    """各ファイルの面数を、**全ファイルのヘッダを読んで** 返す。

    以前はファイルサイズで「怪しいファイル」を絞り込み、そこだけヘッダを読んでいた。
    生の ThorLabs TIFF は無圧縮なのでサイズは面数にほぼ比例する、という前提だった。
    **この前提は実データで破れた**: 3001 枚のうち 004 だけが 6000 面なのに、
    サイズは他と変わらなかった (圧縮された別物が置かれていたとみられる)。サイズが
    当てにならない以上、サイズで絞り込む限りこの取りこぼしは無くならない。

    絞り込みをやめて全件読むと往復は N 回になるが、1件あたりはヘッダ数 KB の
    読み取りで、時間のほとんどは往復の待ちである。待ちは並べれば重なるので、
    :data:`_PROBE_WORKERS` 本で読めば実時間は N/16 件ぶんの往復で済む。
    取りこぼして 267 秒走ったあとに落ちるより安い。

    面数が食い違ったときにどう扱うかはここでは決めない。時点ごとに揃っているかを
    :func:`_drop_odd_depths` が見て、多数派と違う時点を報告して落とす。
    """
    if len(files) == 1:
        return [layout.n_pages]

    counts = {}
    with timed_step("thorlab.probe_tiffs", total=len(files), target=target) as step:
        with ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as pool:
            # 進捗は結果を受け取る側 (このスレッド) だけが触る。map は投入順に
            # 返すので、item はいま読み終わったファイルを指す。
            for f, n_pages in zip(files, pool.map(_probe_n_pages, files)):
                step.advance(item=f)
                counts[f] = n_pages
    return [counts[f] for f in files]


def _probe_n_pages(path: Any) -> int:
    return probe_plane_layout(path).n_pages


def stack_thorlab_with_bioio_calibrated(tiff_files: list, xml_path: str,
                                        get_thorlabs_params: Any, min_kb: int = 100,
                                        sizes: Any = None) -> tuple:
    """遅延 (dask) の TCZYX スタックと、実際に使ったファイル一覧を返す。

    Args:
        sizes: ``{パス: バイト数}``。列挙済みなら渡す (:func:`scan_tiff_dir` が返す)。
            渡さなければここで列挙するが、呼び出し側が直前に列挙しているなら
            同じディレクトリを2回列挙することになる。
    """
    params = get_thorlabs_params
    print("PARMS : ", params)
    mode = params["mode"]
    target_total = params.get("SizeZ" if mode == "Z" else "SizeT", 0)

    print(f"DEBUG: XML says Target {mode} is {target_total}")

    # 計測ログの target は取得ディレクトリ (img*) に揃える。sorter 側の load_image と
    # 同じ値になるので、Better Stack 上で工程をまたいで突き合わせられる。
    target = str(Path(xml_path).parent)

    # 表示用のプレビューを最初に外す。ThorImage はどの取得でも ``ChanA_Preview.tif``
    # を書き出すので、これを「連番が読めない不明なファイル」として警告すると
    # **毎回必ず鳴る警告** になり、本当に見てほしい警告まで読まれなくなる。
    # 落とすこと自体は変わらないので、DEBUG に1行残すだけにする。
    #
    # 先に外すのには実利もある。プレビューは名前の並びで最後に来るため、
    # ヘッダの抜き取り検査 (:func:`_page_counts` は末尾のファイルを見る) が
    # プレビューに当たっていた。面数もサイズも他と違うので「面数が食い違う」と
    # 判定され、サイズの分からないファイルが1つでもあると全件のヘッダを読みに行く。
    previews = [f for f in tiff_files if _is_preview(f)]
    if previews:
        tiff_files = [f for f in tiff_files if not _is_preview(f)]
        print(f"DEBUG: Skipped {len(previews)} ThorImage preview file(s) "
              f"({_examples(previews)}) — display copies, not image planes.")
        if not tiff_files:
            raise RuntimeError(
                "Only ThorImage preview file(s) were found in this acquisition "
                "(%s), so there are no image planes to stack."
                % _examples(previews)
            )

    # Size filter (NOT a pixel read). ``tiff_files`` is already sorted by
    # collect_valid_tiffs; honor the ``min_kb`` parameter.
    #
    # 以前は ``os.path.getsize(f)`` を1ファイルずつ呼んでいた。画素は読まないが往復は
    # 1件1回で、生データがネットワークドライブ (V:) 上にあるため数万ファイルでは
    # この stat だけで数分かかり、接続が切れるとここで止まっていた。
    #
    # サイズはディレクトリ列挙の応答に最初から入っているので、列挙1回で全件分が
    # 追加の往復なしで揃う。3001 回の stat が 1 回の列挙になる。
    #
    # 通常は探索側 (scan_tiff_dir) が列挙したときのサイズがそのまま渡ってくるので、
    # ここでは1往復も発生しない。渡されなかったときだけ自分で列挙する
    # (スタッカを直接呼ぶ経路のため)。
    #
    # ただし列挙が返すサイズは、書き込み中のファイルに対して古い値になりうる
    # (Windows のメタデータ更新は遅延する)。そこで **捨てる判断だけ** は
    # ``os.path.getsize`` で裏を取る。正常系では 0 回、取りこぼしかけた件数ぶんしか
    # 追加の往復が発生せず、「取得と並行して走らせたら数枚落ちた」を防げる。
    if sizes is not None:
        scanned_sizes = sizes
    else:
        # 列挙で止まったときに「どのディレクトリを見ているか」を名指しできるよう、
        # ディレクトリに入る直前に item を差し替える。
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
    #
    # ここを try/except Exception で包んではいけない。以前は包んでいて、
    # extract_dimensions が落ちると「mosaic ではない」として素通りしていた
    # (しかも実際に Timelapse のようなトークンを含む名前で落ちた)。安全確認が
    # 確認できなかったときに通す形になっていたので、タイル取得が黙って
    # Z/T 軸へ潰される。extract_dimensions は例外を投げない契約にしたので、
    # ここで何か飛んできたらそれは本物の不具合であり、握り潰さず落とす。
    _, _dims = extract_dimensions(filtered_files)
    if is_mosaic(_dims):
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

    # 縦横と画素の型は取得ごとに1枚で決まる。XML が面の縦横 (pixelX/pixelY) を、
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

    # 取得の枠は XML が決める。上限ではなく目標で、実際にそこまで撮れたかは
    # XML には書かれていない (:func:`_fill_frame` 参照)。
    max_t = max(int(params.get("SizeT") or 1), 1)
    max_z = max(int(params.get("SizeZ") or 1), 1)

    # 面数だけは1枚では決まらないので、チャンネルごとに全件のヘッダを読む。
    read_planes = dask.delayed(_read_file_planes, pure=True)

    channel_stacks, kept_t = [], []
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

        # 枠を埋めて、埋まらなかった分を落とす (:func:`_fill_frame`)。
        # 面の位置はファイル名の連番が決める。XML の mode ("Z スタックとして
        # 組んだか") は取得 **前** の設定でしかなく、Z スタックの設定を残したまま
        # T 連続撮影した取得では実データとずれるため、軸の割り当てには使わない。
        frame = _fill_frame(ch_files, max_t, max_z)
        _report_cuts(ch, ch_files, frame, max_t, max_z)

        if not frame.t_keep:
            raise RuntimeError(
                "Channel %s: none of the %d file name(s) carry the Z/T sequence "
                "numbers needed to place the planes, so no stack can be built. "
                "Thorlabs raw files end in two numeric fields, e.g. "
                "ChanA_001_001_<Z>_<T>.tif. Example of what was found: %s."
                % (ch, len(ch_files), Path(ch_files[0]).name)
            )

        per_t = []
        for t in frame.t_keep:
            zs = [blocks[frame.slots[(t, z)]] for z in frame.z_keep]
            per_t.append(zs[0] if len(zs) == 1 else da.concatenate(zs, axis=0))

        # 時点ごとの面数が揃っていないと (T, Z, Y, X) に積めない。枡の数が同じでも、
        # 多ページのファイルが1枚混ざれば面数は変わる。
        #
        # 実データで、3001 枚のうち 004 だけが 6000 ページだった (取り違えて置かれた
        # 別物とみられる)。以前はこれをグラフに組み込んでしまい、267 秒走ったあとの
        # compute 時に Page count mismatch で落ちていた。落とすこと自体は正しいので、
        # それを **組み立ての時点で** 言えば済む。
        t_keep, per_t = _drop_odd_depths(ch, frame.t_keep, per_t)
        vol = da.stack(per_t, axis=0)                   # (T, Z, Y, X)

        channel_stacks.append(vol)
        kept_t.append(t_keep)
        print(f"DEBUG: Channel {ch}: {len(ch_files)} file(s) → "
              f"T={vol.shape[0]} Z={vol.shape[1]}")

    # チャンネルごとに落ちた時点が違うことがある (混入したファイルは片方の
    # チャンネルにしか無い)。ここで **枚数を揃えてはいけない**: ChanA から t=4 が
    # 落ちた状態で両方を先頭 19 枚に切ると、ChanA の 4 枚目は t=5、ChanB の
    # 4 枚目は t=4 になり、チャンネルが 1 フレームずれたまま以降の位置合わせや
    # 解析に流れる。揃えるのは **時点そのもの** で、全チャンネルに揃っている
    # 時点だけを残す。
    common_t = sorted(set(kept_t[0]).intersection(*kept_t[1:]))
    if any(len(t) != len(common_t) for t in kept_t):
        warnings.warn(
            "[thorlab] The channels do not hold the same timepoints (%s); keeping "
            "the %d present in every channel. Dropping a timepoint from one channel "
            "only would shift the channels against each other in time."
            % (dict(zip(channel_keys, (len(t) for t in kept_t))), len(common_t)),
            stacklevel=2,
        )
        aligned = []
        for s, t_keep in zip(channel_stacks, kept_t):
            at = {t: i for i, t in enumerate(t_keep)}   # 3001 時点なので線形探索にしない
            aligned.append(s[[at[t] for t in common_t]])
        channel_stacks = aligned

    # 面数が揃わないのは「取得が途中で終わった」ときで、欠けるのは必ず後ろ側なので
    # 枚数で切ってよい (時点と違って、面には共通の番号が振れない — 多ページの
    # ファイルが混ざると1面ずつに番号が対応しないため)。
    z_counts = [int(s.shape[1]) for s in channel_stacks]
    if len(set(z_counts)) > 1:
        keep = min(z_counts)
        warnings.warn(
            "[thorlab] The channels do not have the same number of planes (%s); the "
            "acquisition probably ended mid-stack. Continuing with the common %d."
            % (dict(zip(channel_keys, z_counts)), keep),
            stacklevel=2,
        )
        channel_stacks = [s[:, :keep] for s in channel_stacks]

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
    if xml_z and int(xml_z) != got_z:
        warnings.warn(
            "[thorlab] The XML was configured for SizeZ=%s (SizeT=%s, mode=%s) but "
            "the file names give Z=%d over T=%d. Using the layout from the file "
            "names: the XML records how the acquisition was configured, the files "
            "record what it actually produced."
            % (xml_z, xml_t, mode, got_z, got_t),
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

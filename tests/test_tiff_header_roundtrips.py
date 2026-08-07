"""ネットワーク越しの往復回数を減らした経路が、結果を変えていないことのテスト。

SMB 共有上の XYT 取得 (単一プレーン 3001 枚 x チャンネル) で取り込みが止まった。
遅かったのはライブラリではなく **1ファイルあたりの往復回数** で、内訳は

- ``find_tiff_files`` の ``x.resolve()``      : 3 往復/件 (うち 2 回は本物の open)
- ``os.path.getsize``                          : 1 往復/件
- ``BioImage(f, reader=TiffReader)``           : 1 stat + 3 open/件

だった。3001 件で 24,000 往復、うち 15,000 が open である。1 open を 30 ms とすると
ヘッダ読みだけで 4 分半、実測では 1 ファイル目で 60 秒以上進まなかった。

減らし方は 3 つで、いずれも **「形が揃っている」という仮定は置かない**。

1. パス解決はディレクトリに 1 回だけ (ファイル名側は解決しても何も変わらない)
2. サイズはディレクトリ列挙から取る (捨てる判断だけ個別 stat で裏を取る)
3. ヘッダは ``tifffile`` で 1 回開いて読む (ファイルごとに開くことはやめない)

このファイルは「往復が減っていること」と「減らした結果が従来と同一であること」の
両方を固定する。片方だけでは意味がない — 速いが違う配列を作る変更は、
28 GiB を書き出したあとにしか気付けない形で壊れる。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import dask.array as da
import numpy as np
import pytest
import tifffile
from bioio import BioImage
from bioio_tifffile import Reader as TiffReader

import ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder as mod
from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import (
    _open_lazy_tczyx,
    stack_thorlab_with_bioio_calibrated,
)
from ylabcommon.utils.utils import find_tiff_files, sizes_from_dir_scan


# --------------------------------------------------------------------------
# open() の実測。audit hook はプロセス全体に効くので、数えたい区間だけ有効にする。
# --------------------------------------------------------------------------

_open_counter = {"on": False, "n": 0}


def _audit(event, args):
    if event == "open" and _open_counter["on"]:
        _open_counter["n"] += 1


sys.addaudithook(_audit)


class count_opens:
    """``with count_opens() as c:`` の中で起きたファイル open の回数を数える。"""

    def __enter__(self):
        _open_counter["n"] = 0
        _open_counter["on"] = True
        return self

    def __exit__(self, *exc):
        _open_counter["on"] = False
        return False

    @property
    def n(self):
        return _open_counter["n"]


PARAMS_T = {"mode": "T", "SizeT": 8, "PixelSizeX": .5, "PixelSizeY": .5,
            "PixelSizeZ": 1.}


@pytest.fixture
def xyt_dir(tmp_path):
    """XYT 取得の形。プレーンごとに違う値を入れて、並びと欠落を検出できるようにする。"""
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 9):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.full((32, 32), i, dtype=np.uint16))
    return d


# ==========================================================================
# 1. 往復が実際に減っている
# ==========================================================================

def test_header_read_opens_each_file_once_not_three_times(xyt_dir):
    """1ファイルにつき open は 1 回。以前は 3 回だった。

    内訳は ``Reader._is_supported_image`` (「本当に TIFF か」の検証・結果は捨てる)、
    ``_read_delayed`` (本命)、``Reader.scenes`` (``len(tiff.series)`` を数えるだけ)。
    使うのは真ん中の 1 回だけなので、3 回のうち 2 回はそのまま無駄である。
    """
    files = sorted(str(p) for p in xyt_dir.glob("*.tif"))
    _open_lazy_tczyx(files[0])                      # import/キャッシュを温める
    BioImage(files[0], reader=TiffReader).get_image_dask_data("TCZYX")

    with count_opens() as c:
        for f in files:
            BioImage(f, reader=TiffReader).get_image_dask_data("TCZYX")
    before = c.n

    with count_opens() as c:
        for f in files:
            _open_lazy_tczyx(f)
    after = c.n

    assert before == 3 * len(files), before
    assert after == len(files), after


def test_the_whole_stack_build_opens_each_file_once(xyt_dir):
    """入口から出口まで通しても、1ファイルにつき open は 1 回に収まる。

    ここが本番。個々の関数が速くても、``find_tiff_files`` の ``resolve()`` や
    サイズフィルタの stat が残っていれば往復は減らない。
    """
    _open_lazy_tczyx(str(next(xyt_dir.glob("*.tif"))))     # 温め

    with count_opens() as c:
        files = find_tiff_files(str(xyt_dir))
        stacked, kept = stack_thorlab_with_bioio_calibrated(
            files, xyt_dir / "Experiment.xml", PARAMS_T, min_kb=0)

    assert len(kept) == 8
    # 8 ファイル分のヘッダ + ディレクトリ側の定数回。ファイル数に比例するのは
    # ヘッダの 1 回だけなので、余裕を見ても 8 + 少数で収まる。
    assert c.n <= len(kept) + 4, "opens=%d for %d files" % (c.n, len(kept))
    assert isinstance(stacked, da.Array)


def test_listing_resolves_the_directory_once_not_every_file(xyt_dir, monkeypatch):
    """``resolve()`` はディレクトリに 1 回。件数に比例させない。

    Windows/UNC では ``Path.resolve()`` 1 回が ``nt._getfinalpathname`` 2 回
    (= CreateFileW を伴う本物の open が 2 回) と ``p.stat()`` に相当する。
    3001 件に掛けると 9003 往復で、しかも返る文字列は解決前と同一だった。
    """
    calls = {"n": 0}
    real = Path.resolve

    def counting(self, *a, **kw):
        calls["n"] += 1
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", counting)
    files = find_tiff_files(str(xyt_dir))

    assert len(files) == 8
    assert calls["n"] == 1, "resolve() was called %d times" % calls["n"]


def test_listing_returns_the_same_absolute_paths_as_before(xyt_dir):
    """返る文字列が従来の実装と一致する (呼び出し側の比較・並びを壊さない)。"""
    p = Path(str(xyt_dir))
    legacy = []
    for ext in ("*.tif", "*.tiff"):
        legacy.extend(p.glob(ext))
    legacy = [str(x.resolve()) for x in
              sorted(legacy, key=lambda x: mod.Path(x).name)]

    assert sorted(find_tiff_files(str(xyt_dir))) == sorted(legacy)
    assert all(os.path.isabs(f) for f in find_tiff_files(str(xyt_dir)))


def test_size_filter_does_not_stat_every_file(xyt_dir, monkeypatch):
    """正常系ではサイズ用の ``os.path.getsize`` が 1 回も呼ばれない。

    サイズはディレクトリ列挙の応答に最初から入っている。3001 回の stat が
    ディレクトリ 1 回の列挙になる。
    """
    calls = {"n": 0}
    real = os.path.getsize

    def counting(p):
        calls["n"] += 1
        return real(p)

    monkeypatch.setattr(os.path, "getsize", counting)
    files = sorted(str(p) for p in xyt_dir.glob("*.tif"))
    stack_thorlab_with_bioio_calibrated(files, xyt_dir / "Experiment.xml",
                                        PARAMS_T, min_kb=0)

    assert calls["n"] == 0, "getsize was called %d times" % calls["n"]


def test_a_file_the_scan_missed_still_gets_a_real_stat(xyt_dir, monkeypatch):
    """列挙で拾えなかったファイルは個別 stat で補う (黙って落とさない)。

    列挙が返すサイズは、書き込み中のファイルに対して古い値になりうる
    (Windows のメタデータ更新は遅延する)。**捨てる判断だけ** は必ず裏を取るので、
    列挙が嘘をついても取りこぼさない。
    """
    files = sorted(str(p) for p in xyt_dir.glob("*.tif"))
    liar = files[3]

    real_scan = mod.sizes_from_dir_scan
    monkeypatch.setattr(mod, "sizes_from_dir_scan",
                        lambda paths, on_directory=None: {
                            k: (0 if k == liar else v)
                            for k, v in real_scan(paths, on_directory).items()})
    statted = []
    real_getsize = os.path.getsize
    monkeypatch.setattr(os.path, "getsize",
                        lambda p: (statted.append(p), real_getsize(p))[1])

    stacked, kept = stack_thorlab_with_bioio_calibrated(
        files, xyt_dir / "Experiment.xml", PARAMS_T, min_kb=0)

    assert statted == [liar]          # 疑わしい 1 件だけを確認した
    assert liar in kept               # 列挙の古い値で捨てていない
    assert stacked.shape[0] == 8


def test_sizes_from_dir_scan_matches_getsize(tmp_path):
    """列挙由来のサイズが ``os.path.getsize`` と一致する (静止したファイルで)。"""
    paths = []
    for i, n in enumerate((1, 1000, 40000)):
        p = tmp_path / f"f{i}.bin"
        p.write_bytes(b"x" * n)
        paths.append(str(p))
    sizes = sizes_from_dir_scan(paths)
    assert sizes == {p: os.path.getsize(p) for p in paths}


def test_sizes_from_dir_scan_spans_several_directories(tmp_path):
    """複数ディレクトリにまたがる入力でも、ディレクトリごとに 1 回だけ列挙する。"""
    paths = []
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        p = tmp_path / d / "x.tif"
        p.write_bytes(b"y" * 10)
        paths.append(str(p))
    paths.append(str(tmp_path / "a" / "does_not_exist.tif"))

    sizes = sizes_from_dir_scan(paths)
    assert set(sizes) == set(paths[:2])          # 無いものは黙って含めない
    assert all(v == 10 for v in sizes.values())


# ==========================================================================
# 2. 減らした結果が従来と同一
# ==========================================================================

def _write_variants(d: Path):
    rng = np.random.default_rng(0)
    out = {}

    def w(name, arr, **kw):
        p = d / name
        tifffile.imwrite(p, arr, **kw)
        out[name] = str(p)

    # ThorLabs の生データが取りうる形
    w("single_yx.tif", rng.integers(0, 4000, (64, 48), dtype=np.uint16))
    w("multi_zyx.tif", rng.integers(0, 4000, (7, 64, 48), dtype=np.uint16))
    w("multi_zyx_1page.tif", rng.integers(0, 4000, (1, 64, 48), dtype=np.uint16))
    # 形式のゆらぎ
    w("uint8.tif", rng.integers(0, 255, (64, 48), dtype=np.uint8))
    w("float32.tif", rng.random((64, 48)).astype(np.float32))
    w("rgb.tif", rng.integers(0, 255, (64, 48, 3), dtype=np.uint8),
      photometric="rgb")
    w("tiled.tif", rng.integers(0, 4000, (256, 256), dtype=np.uint16),
      tile=(64, 64))
    w("compressed.tif", rng.integers(0, 4000, (64, 48), dtype=np.uint16),
      compression="zlib")
    w("bigtiff.tif", rng.integers(0, 4000, (64, 48), dtype=np.uint16),
      bigtiff=True)
    # bioio がチャンクを分ける形 (退避経路に落ちるはず)
    w("czyx.tif", rng.integers(0, 4000, (3, 5, 64, 48), dtype=np.uint16))
    w("imagej_zcyx.tif", rng.integers(0, 4000, (2, 3, 64, 48), dtype=np.uint16),
      imagej=True)
    w("ome_cyx.tif", rng.integers(0, 4000, (3, 64, 48), dtype=np.uint16), ome=True)
    return out


@pytest.mark.filterwarnings("ignore")
def test_every_tiff_variant_matches_bioio_exactly(tmp_path):
    """shape / chunks / dtype / 画素まで ``BioImage`` の出力と一致する。

    chunks を外せないのが要点。``_write_ometiff_streaming`` は「1 ブロック読んでも
    volume 全体を引かない」ことを前提にしており、チャンクが粗くなるとその前提が
    静かに崩れる (28 GiB のうち何 GiB を一度に掴むかが変わる)。
    """
    variants = _write_variants(tmp_path)
    for name, path in variants.items():
        ref = BioImage(path, reader=TiffReader).get_image_dask_data("TCZYX")
        got = _open_lazy_tczyx(path)
        assert got.shape == ref.shape, name
        assert got.chunks == ref.chunks, name
        assert got.dtype == ref.dtype, name
        assert np.array_equal(np.asarray(got), np.asarray(ref)), name


@pytest.mark.filterwarnings("ignore")
def test_files_bioio_would_chunk_differently_use_the_old_path(tmp_path):
    """T/C を持つファイルは ``BioImage`` の経路に落とす。

    bioio はチャンク対象外の次元を次元ごとに別チャンクへ分ける。同じ分割を手で
    再現すると bioio の内部実装を写経することになるので、そこは従来どおりにする。
    ThorLabs の生データは必ず高速経路に乗るので、実運用の往復には影響しない。
    """
    variants = _write_variants(tmp_path)
    fell_back = []
    real = mod.BioImage
    try:
        mod.BioImage = lambda p, **k: (fell_back.append(os.path.basename(p)),
                                       real(p, **k))[1]
        for path in variants.values():
            _open_lazy_tczyx(path)
    finally:
        mod.BioImage = real

    assert set(fell_back) == {"czyx.tif", "imagej_zcyx.tif", "ome_cyx.tif"}


def test_no_homogeneous_shape_assumption_is_made(tmp_path):
    """解像度の違うプレーンが混ざれば、従来どおりグラフ構築の時点で落ちる。

    「1 枚目のヘッダを全ファイルに使い回す」設計ならここは素通りし、
    数時間かけて書き出したあとに壊れた出力が残る。ファイルごとに開くのを
    やめていないので、その取り違えは起きない。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 5):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.zeros((32, 32), dtype=np.uint16))
    tifffile.imwrite(d / "ChanA_001_001_001_005.tif",
                     np.zeros((16, 16), dtype=np.uint16))
    files = sorted(str(p) for p in d.glob("*.tif"))

    with pytest.raises(ValueError, match="align"):
        stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml",
            {"mode": "T", "SizeT": 5, "PixelSizeX": .5, "PixelSizeY": .5,
             "PixelSizeZ": 1.}, min_kb=0)


def test_the_shapes_come_from_each_file_not_from_the_first(tmp_path):
    """ヘッダ読みが実際に全ファイルへ行っている (1 枚目の使い回しではない)。"""
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 6):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.zeros((32, 32), dtype=np.uint16))
    files = sorted(str(p) for p in d.glob("*.tif"))

    seen = []
    real = mod._read_tiff_header
    try:
        mod._read_tiff_header = lambda p: (seen.append(p), real(p))[1]
        stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml",
            {"mode": "T", "SizeT": 5, "PixelSizeX": .5, "PixelSizeY": .5,
             "PixelSizeZ": 1.}, min_kb=0)
    finally:
        mod._read_tiff_header = real

    assert sorted(seen) == files


# ==========================================================================
# 3. 遅延のまま / 既存の分岐が生きている
# ==========================================================================

def test_the_stack_is_still_lazy_and_chunked_per_source_file(xyt_dir):
    """出口は遅延 dask (TCZYX)、チャンクは元ファイル単位。

    どちらか一方でも崩れると ``_write_ometiff_streaming`` が成立しない
    (実体化すれば構築時に OOM、単一チャンクなら書き出し時に OOM)。
    """
    files = sorted(str(p) for p in xyt_dir.glob("*.tif"))
    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, xyt_dir / "Experiment.xml", PARAMS_T, min_kb=0)

    assert isinstance(stacked, da.Array)
    assert stacked.shape == (8, 1, 1, 32, 32)
    assert stacked.numblocks[0] == 8            # T 方向に 8 ブロック
    assert stacked.chunksize == (1, 1, 1, 32, 32)
    # 並びが保たれている (プレーン i の値は i+1)
    assert np.asarray(stacked)[:, 0, 0, 0, 0].tolist() == list(range(1, 9))


def test_a_single_multipage_file_still_takes_the_multi_branch(tmp_path):
    """多ページファイル 1 つ、の正常系は従来どおり (``shape[axis] > 1`` が立つ)。"""
    d = tmp_path / "img01"
    d.mkdir()
    tifffile.imwrite(d / "ChanA_001_001_001_001.tif",
                     np.arange(10 * 8 * 8, dtype=np.uint16).reshape(10, 8, 8))
    files = [str(p) for p in d.glob("*.tif")]

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml",
        {"mode": "Z", "SizeZ": 10, "PixelSizeX": .5, "PixelSizeY": .5,
         "PixelSizeZ": 1.}, min_kb=0)

    assert isinstance(stacked, da.Array)
    assert stacked.shape == (1, 1, 10, 8, 8)
    assert np.array_equal(np.asarray(stacked)[0, 0],
                          np.arange(10 * 8 * 8, dtype=np.uint16).reshape(10, 8, 8))


def test_multiple_multipage_files_are_still_rejected(tmp_path):
    """複数の多ページファイルは従来どおり明示的に失敗する (黙って捨てない)。"""
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 4):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.full((10, 8, 8), i, dtype=np.uint16))
    files = sorted(str(p) for p in d.glob("*.tif"))

    with pytest.raises(RuntimeError, match="ambiguous"):
        stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml",
            {"mode": "Z", "SizeZ": 30, "PixelSizeX": .5, "PixelSizeY": .5,
             "PixelSizeZ": 1.}, min_kb=0)


def test_mosaic_guard_still_fires_before_any_header_is_read(tmp_path):
    """mosaic 判定はヘッダ読みより前 (1 ファイルも開かずに落ちる)。"""
    d = tmp_path / "img01"
    d.mkdir()
    for xy in (1, 2):
        for i in (1, 2):
            tifffile.imwrite(d / f"ChanA_00{xy}_001_001_{i:03d}.tif",
                             np.zeros((8, 8), dtype=np.uint16))
    files = sorted(str(p) for p in d.glob("*.tif"))

    opened = []
    real = mod._read_tiff_header
    try:
        mod._read_tiff_header = lambda p: (opened.append(p), real(p))[1]
        with pytest.raises(RuntimeError, match="mosaic"):
            stack_thorlab_with_bioio_calibrated(
                files, d / "Experiment.xml",
                {"mode": "T", "SizeT": 2, "PixelSizeX": .5, "PixelSizeY": .5,
                 "PixelSizeZ": 1.}, min_kb=0)
    finally:
        mod._read_tiff_header = real

    assert opened == []


# ==========================================================================
# 4. 止まった/壊れた場所を名指しできる
# ==========================================================================

def test_a_broken_file_is_named_at_write_time_too(tmp_path):
    """画素読みで落ちたとき、例外にファイル名が付く。

    以前は書き出しの最中に壊れたファイルへ当たると
    ``failed to read 2097152 bytes, got 999744`` としか出ず、どのファイルかが
    分からなかった。3001 枚を数時間流したあとの調査ではほぼ役に立たない。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 4):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.zeros((64, 64), dtype=np.uint16))
    files = sorted(str(p) for p in d.glob("*.tif"))

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml",
        {"mode": "T", "SizeT": 3, "PixelSizeX": .5, "PixelSizeY": .5,
         "PixelSizeZ": 1.}, min_kb=0)

    # グラフを組んだ「あとで」壊す = 書き出しの最中に壊れたファイルへ当たる状況
    victim = files[1]
    with open(victim, "r+b") as fh:
        fh.truncate(os.path.getsize(victim) // 2)

    with pytest.raises(Exception) as ei:
        np.asarray(stacked)
    assert victim in "".join(getattr(ei.value, "__notes__", []))


def test_a_stall_in_the_directory_scan_names_the_directory(xyt_dir, monkeypatch):
    """列挙で止まったとき、heartbeat が「いま列挙しているディレクトリ」を名指しする。

    サイズ取得がファイル単位のループでなくなったので、止まる場所も
    「何件目のファイル」ではなく「どのディレクトリ」になる。名指しの対象が
    変わるだけで、「沈黙している相手を特定できる」性質は保つ。
    """
    import threading
    import time

    import ylabcommon.utils.perf as perf

    seen = []
    monkeypatch.setattr(perf, "log_info", lambda _m, **f: seen.append(f))
    monkeypatch.setattr(perf, "log_warning", lambda _m, **f: seen.append(f))

    gate = threading.Event()
    real = mod.sizes_from_dir_scan

    def hanging(paths, on_directory=None):
        if on_directory is not None:
            for d in {os.path.dirname(p) for p in paths}:
                on_directory(d)
        gate.wait(timeout=30)
        return real(paths, on_directory=None)

    monkeypatch.setattr(mod, "sizes_from_dir_scan", hanging)
    files = sorted(str(p) for p in xyt_dir.glob("*.tif"))

    t = threading.Thread(
        target=lambda: stack_thorlab_with_bioio_calibrated(
            files, xyt_dir / "Experiment.xml", PARAMS_T, min_kb=0),
        daemon=True)
    t.start()
    try:
        time.sleep(0.3)
        perf._hb_started_at = time.perf_counter()
        del seen[:]
        perf._emit_heartbeat(stall_after_sec=0.05)
        hb = [f for f in seen if f.get("step") == "thorlab.scan_sizes"
              and f.get("event") == "heartbeat"]
        assert hb, "no heartbeat for the directory scan"
        assert hb[0]["item"] == str(xyt_dir)
        assert hb[0]["stalled"] is True
    finally:
        gate.set()
        t.join(timeout=30)


@pytest.mark.filterwarnings("ignore")
def test_unopenable_files_raise_the_same_exception_type_as_before(tmp_path):
    """開けないファイルの例外型が ``BioImage`` と同じ (呼び出し側の except を守る)。

    素の ``tifffile`` は ``TiffFileError`` / ``FileNotFoundError`` /
    ``IsADirectoryError`` を投げるが、``bioio_tifffile.Reader._is_supported_image``
    は ``except Exception`` でそれらを ``UnsupportedFileFormatError`` に包んでいた。
    型が静かに変わると、上位の ``except UnsupportedFileFormatError`` (このリポジトリ
    には無いが sorter 側にはありうる) をすり抜けて落ち方が変わる。
    """
    from bioio_base.exceptions import UnsupportedFileFormatError

    good = tmp_path / "good.tif"
    tifffile.imwrite(good, np.zeros((32, 32), dtype=np.uint16))
    raw = good.read_bytes()

    broken = {}
    broken["missing"] = str(tmp_path / "nope.tif")
    p = tmp_path / "empty.tif"; p.write_bytes(b""); broken["empty"] = str(p)
    p = tmp_path / "garbage.tif"; p.write_bytes(b"not a tiff" * 10)
    broken["garbage"] = str(p)
    p = tmp_path / "adir.tif"; p.mkdir(); broken["directory"] = str(p)

    for name, path in broken.items():
        with pytest.raises(UnsupportedFileFormatError):
            BioImage(path, reader=TiffReader).get_image_dask_data("TCZYX")
        with pytest.raises(UnsupportedFileFormatError) as ei:
            _open_lazy_tczyx(path)
        # どのファイルで落ちたかは従来どおり traceback に残る
        assert path in "".join(getattr(ei.value, "__notes__", [])), name

    # 半端に切れたファイルはヘッダだけなら従来どおり通る (compute 時に落ちる)。
    p = tmp_path / "trunc.tif"; p.write_bytes(raw[:len(raw) // 2])
    assert _open_lazy_tczyx(str(p)).shape == \
        BioImage(str(p), reader=TiffReader).get_image_dask_data("TCZYX").shape

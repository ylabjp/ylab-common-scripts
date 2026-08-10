"""ネットワーク越しの往復回数を減らした経路が、結果を変えていないことのテスト。

SMB 共有上の XYT 取得 (単一プレーン 3001 枚 x チャンネル) で取り込みが止まった。
遅かったのはライブラリではなく **1ファイルあたりの往復回数** で、内訳は

- ``find_tiff_files`` の ``x.resolve()``      : 3 往復/件 (うち 2 回は本物の open)
- ``os.path.getsize``                          : 1 往復/件
- ``BioImage(f, reader=TiffReader)``           : 1 stat + 3 open/件

だった。3001 件で 24,000 往復、うち 15,000 が open である。1 open を 30 ms とすると
ヘッダ読みだけで 4 分半、実測では 1 ファイル目で 60 秒以上進まなかった。

減らし方は 3 つ。

1. パス解決はディレクトリに 1 回だけ (ファイル名側は解決しても何も変わらない)
2. サイズはディレクトリ列挙から取る (捨てる判断だけ個別 stat で裏を取る)
3. ヘッダは **取得ごとに 1〜2 枚** だけ読む

3 が最も効く。生データの形は Experiment.xml とファイル名で決まっており、
1ファイルから分からないのは画素の型と面数だけなので、全ファイルのヘッダを
読む必要が無い。往復はファイル数に **比例しなくなる**。

その代わり「全ファイルが同じ形」という仮定を置くことになるので、仮定が破れた
ときに黙って通さないことが要件になる。ここは読み取り時 (compute 時) に
ファイル名つきで落として担保する。このファイルは

- 往復が実際に減っていること
- 減らした結果が bioio 経由と同一の配列になること
- 仮定が破れたときに黙らないこと

の 3 つを固定する。片方だけでは意味がない — 速いが違う配列を作る変更は、
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
    probe_plane_layout,
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


def _params(mode, n):
    return {"mode": mode, "Size" + mode: n, "PixelSizeX": .5, "PixelSizeY": .5,
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

def test_the_whole_stack_build_reads_a_constant_number_of_headers(xyt_dir):
    """入口から出口まで通しても、open 回数がファイル数に比例しない。

    ここが本番。個々の関数が速くても、``find_tiff_files`` の ``resolve()`` や
    サイズフィルタの stat が残っていれば往復は減らない。
    ヘッダは先頭と末尾の 2 枚だけ読むので、8 ファイルでも 3001 ファイルでも
    open は数回で頭打ちになる。
    """
    probe_plane_layout(str(next(xyt_dir.glob("*.tif"))))     # import/キャッシュを温め

    with count_opens() as c:
        files = find_tiff_files(str(xyt_dir))
        stacked, kept = stack_thorlab_with_bioio_calibrated(
            files, xyt_dir / "Experiment.xml", PARAMS_T, min_kb=0)

    assert len(kept) == 8
    # 先頭と末尾のヘッダ 2 枚 + ディレクトリ側の定数回。ファイル数には比例しない。
    assert c.n <= 6, "opens=%d for %d files" % (c.n, len(kept))
    assert isinstance(stacked, da.Array)


def test_header_reads_do_not_grow_with_the_file_count(tmp_path):
    """ファイル数を 4 倍にしても、読むヘッダの枚数は変わらない。

    「定数回」を 1 点の実測で主張すると、たまたま小さいだけの可能性が残る。
    件数を変えて 2 回測り、増えないことを直接見る。
    """
    def build(n):
        d = tmp_path / f"img{n:03d}"
        d.mkdir()
        for i in range(1, n + 1):
            tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                             np.full((16, 16), i, dtype=np.uint16))
        files = sorted(str(p) for p in d.glob("*.tif"))
        seen = []
        real = mod.probe_plane_layout
        mod.probe_plane_layout = lambda p: (seen.append(str(p)), real(p))[1]
        try:
            stack_thorlab_with_bioio_calibrated(
                files, d / "Experiment.xml", _params("T", n), min_kb=0)
        finally:
            mod.probe_plane_layout = real
        return seen

    assert len(build(5)) == len(build(20)) == 2


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
    find_tiff_files(str(xyt_dir))

    assert calls["n"] == 1, "resolve() was called %d times" % calls["n"]


def test_listing_returns_the_same_absolute_paths_as_before(xyt_dir):
    """速くしても返る文字列は従来と同一 (後段の突き合わせが壊れない)。"""
    legacy = [str(x.resolve()) for x in sorted(Path(xyt_dir).glob("*.tif"))]
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
# 2. 減らした結果が従来 (bioio 経由) と同一
# ==========================================================================

def _write_variants(d: Path):
    """ThorLabs の生データが取りうる形と、その形式のゆらぎ。

    RGB や T/C 軸を持つファイルは入れない。ThorLabs の raw は必ず
    「1ファイル = 1チャンネルの連続プレーン」なので、この経路の対象外である
    (対象外のものを黙って通さないことは別途 ``test_thorlab_stacking`` が見る)。
    """
    rng = np.random.default_rng(0)
    out = {}

    def w(name, arr, **kw):
        p = d / name
        tifffile.imwrite(p, arr, **kw)
        out[name] = (str(p), arr)

    w("single_yx.tif", rng.integers(0, 4000, (64, 48), dtype=np.uint16))
    w("multi_zyx.tif", rng.integers(0, 4000, (7, 64, 48), dtype=np.uint16))
    w("multi_zyx_1page.tif", rng.integers(0, 4000, (1, 64, 48), dtype=np.uint16))
    w("uint8.tif", rng.integers(0, 255, (64, 48), dtype=np.uint8))
    w("float32.tif", rng.random((64, 48)).astype(np.float32))
    w("tiled.tif", rng.integers(0, 4000, (256, 256), dtype=np.uint16),
      tile=(64, 64))
    w("compressed.tif", rng.integers(0, 4000, (64, 48), dtype=np.uint16),
      compression="zlib")
    w("bigtiff.tif", rng.integers(0, 4000, (64, 48), dtype=np.uint16),
      bigtiff=True)
    return out


@pytest.mark.filterwarnings("ignore")
def test_every_tiff_variant_matches_bioio_exactly(tmp_path):
    """dtype / 面数 / 画素まで ``BioImage`` の出力と一致する。

    bioio を入力側から外したので、「速いが違う配列」を作っていないことは
    ここでしか担保できない。tile / compression / bigtiff / float32 など、
    ヘッダの読み方で取り違えやすい形をひととおり通す。
    """
    for name, (path, expected) in _write_variants(tmp_path).items():
        ref = np.asarray(
            BioImage(path, reader=TiffReader).get_image_dask_data("TCZYX"))
        # bioio の TCZYX から、この経路が扱う「連続プレーン」へ畳む
        ref_planes = ref.reshape((-1,) + ref.shape[-2:])

        layout = probe_plane_layout(path)
        got = mod._read_file_planes(path, layout.n_pages, layout.height,
                                    layout.width, layout.dtype)

        assert got.dtype == ref.dtype, name
        assert got.shape == ref_planes.shape, name
        assert np.array_equal(got, ref_planes), name
        # ヘッダから読んだ形が、実際に読めた画素と食い違っていない
        assert (layout.n_pages, layout.height, layout.width) == got.shape, name


@pytest.mark.filterwarnings("ignore")
def test_the_built_stack_matches_a_bioio_built_one(xyt_dir):
    """取得まるごとでも bioio 経由と同じ配列になる (並び順を含む)。

    ファイル単位の一致だけでは、連結の順序が入れ替わっていても気付けない。
    """
    files = sorted(str(p) for p in xyt_dir.glob("*.tif"))
    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, xyt_dir / "Experiment.xml", PARAMS_T, min_kb=0)

    ref = np.concatenate([
        np.asarray(BioImage(f, reader=TiffReader).get_image_dask_data("TCZYX"))
        .reshape((-1,) + (32, 32))
        for f in files
    ], axis=0)[:, None, None]

    assert np.array_equal(np.asarray(stacked), ref)


# ==========================================================================
# 3. 「全ファイル同じ形」の仮定が破れたら黙らない
# ==========================================================================

def test_a_frame_size_mismatch_is_caught_and_names_the_file(tmp_path):
    """解像度の違うプレーンが混ざったら、その **ファイル名つきで** 落ちる。

    ヘッダを 1 枚しか読まない以上、食い違いはグラフ構築では見つからず
    compute 時 (書き出しの最中) に出る。dask の shape 不一致エラーはファイル名を
    持たないので、そのままでは 28 GiB 流したあとに「どれが悪いか分からない」に
    なる。ここが黙ると、この設計の速さは代償に見合わない。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 5):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.zeros((32, 32), dtype=np.uint16))
    odd = d / "ChanA_001_001_001_003.tif"
    tifffile.imwrite(odd, np.zeros((16, 16), dtype=np.uint16))
    files = sorted(str(p) for p in d.glob("*.tif"))

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml", _params("T", 4), min_kb=0)

    with pytest.raises(ValueError, match="Frame size mismatch"):
        np.asarray(stacked)
    with pytest.raises(ValueError, match=str(odd)):
        np.asarray(stacked)


def test_a_page_count_mismatch_is_caught_and_names_the_file(tmp_path):
    """面数の食い違いも同じく、ファイル名つきで落ちる。

    先頭と末尾は面数を確かめているので、ここで漏れるのは **途中の** ファイルだけ。
    その 1 枚が黙って通ると、Z=10 のはずのスタックに 3 面のプレーンが混ざる。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for i in (1, 2, 4, 5):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.zeros((4, 16, 16), dtype=np.uint16),
                         photometric="minisblack")
    odd = d / "ChanA_001_001_001_003.tif"
    tifffile.imwrite(odd, np.zeros((3, 16, 16), dtype=np.uint16),
                     photometric="minisblack")
    files = sorted(str(p) for p in d.glob("*.tif"))

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml", _params("Z", 20), min_kb=0)

    with pytest.raises(ValueError, match="Page count mismatch"):
        np.asarray(stacked)
    with pytest.raises(ValueError, match=str(odd)):
        np.asarray(stacked)


def test_an_uneven_tail_falls_back_to_probing_every_file(tmp_path):
    """先頭と末尾で面数が違えば、そのときだけ全ファイルのヘッダを読む。

    「2 枚で済ませる」が効くのは形が揃っているときだけ。揃っていないと分かった
    時点で仮定を捨てないと、取得が途中で終わった回で壊れた出力を作る。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for i in (1, 2, 3):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.full((4, 8, 8), i, dtype=np.uint16),
                         photometric="minisblack")
    tifffile.imwrite(d / "ChanA_001_001_001_004.tif",       # 途中で終わった末尾
                     np.full((2, 8, 8), 4, dtype=np.uint16),
                     photometric="minisblack")
    files = sorted(str(p) for p in d.glob("*.tif"))

    seen = []
    real = mod.probe_plane_layout
    mod.probe_plane_layout = lambda p: (seen.append(str(p)), real(p))[1]
    try:
        stacked, _ = stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", _params("Z", 14), min_kb=0)
    finally:
        mod.probe_plane_layout = real

    assert sorted(set(seen)) == files          # 全ファイルを確かめに行った
    assert stacked.shape == (1, 1, 14, 8, 8)   # 4+4+4+2
    assert np.asarray(stacked)[0, 0, :, 0, 0].tolist() == \
        [1] * 4 + [2] * 4 + [3] * 4 + [4] * 2


# ==========================================================================
# 4. 遅延のまま / 既存の分岐が生きている
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


def test_building_the_graph_reads_no_pixels(xyt_dir, monkeypatch):
    """グラフ構築の時点で ``tifffile.imread`` が 1 回も呼ばれない。

    遅延の要。ここが崩れると 28 GiB が構築時にメモリへ乗る。
    """
    calls = []
    monkeypatch.setattr(mod.tifffile, "imread",
                        lambda *a, **kw: calls.append(a) or (_ for _ in ()).throw(
                            AssertionError("imread was called during graph build")))
    files = sorted(str(p) for p in xyt_dir.glob("*.tif"))
    stack_thorlab_with_bioio_calibrated(files, xyt_dir / "Experiment.xml",
                                        PARAMS_T, min_kb=0)
    assert calls == []


def test_a_single_multipage_file_becomes_the_whole_stack(tmp_path):
    """多ページファイル 1 つ、の正常系。全ページが面として並ぶ。"""
    d = tmp_path / "img01"
    d.mkdir()
    tifffile.imwrite(d / "ChanA_001_001_001_001.tif",
                     np.arange(10 * 8 * 8, dtype=np.uint16).reshape(10, 8, 8))
    files = [str(p) for p in d.glob("*.tif")]

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml", _params("Z", 10), min_kb=0)

    assert isinstance(stacked, da.Array)
    assert stacked.shape == (1, 1, 10, 8, 8)
    assert np.array_equal(np.asarray(stacked)[0, 0],
                          np.arange(10 * 8 * 8, dtype=np.uint16).reshape(10, 8, 8))


def test_multiple_multipage_files_are_concatenated_not_discarded(tmp_path):
    """多ページのファイルが複数あれば、全部つなぐ。

    以前は「最も大きい1ファイル」だけを採用して残りを黙って捨てており、
    10 ページ x 3 ファイルなら 30 面のうち 10 面しか残らなかった (警告も無し)。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 4):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.full((10, 8, 8), i, dtype=np.uint16))
    files = sorted(str(p) for p in d.glob("*.tif"))

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml", _params("Z", 30), min_kb=0)

    assert stacked.shape == (1, 1, 30, 8, 8)
    assert np.asarray(stacked)[0, 0, :, 0, 0].tolist() == \
        [1] * 10 + [2] * 10 + [3] * 10


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
    real = mod.probe_plane_layout
    try:
        mod.probe_plane_layout = lambda p: (opened.append(str(p)), real(p))[1]
        with pytest.raises(RuntimeError, match="mosaic"):
            stack_thorlab_with_bioio_calibrated(
                files, d / "Experiment.xml", _params("T", 2), min_kb=0)
    finally:
        mod.probe_plane_layout = real

    assert opened == []


# ==========================================================================
# 5. 止まった/壊れた場所を名指しできる
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
        files, d / "Experiment.xml", _params("T", 3), min_kb=0)

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
def test_unopenable_files_keep_their_native_exception_and_name_the_file(tmp_path):
    """開けないファイルは素の例外型のまま、ただし **どれか** は分かる形で落ちる。

    bioio を外したので、``bioio_tifffile.Reader._is_supported_image`` が
    ``except Exception`` で包んでいた ``UnsupportedFileFormatError`` は出なくなる
    (このリポジトリにも sorter 側にもそれを捕まえる ``except`` は無いので、
    包み直す理由が無い)。代わりに素の型が出る。

    問題は ``tifffile`` の例外がファイル名を持たないことで、
    ``not a TIFF file: header=b''`` だけでは 3001 枚のどれかが分からない。
    型を変えずに PEP 678 の note で足す。
    """
    good = tmp_path / "good.tif"
    tifffile.imwrite(good, np.zeros((32, 32), dtype=np.uint16))
    raw = good.read_bytes()

    broken = {}
    broken["missing"] = (str(tmp_path / "nope.tif"), FileNotFoundError)
    p = tmp_path / "empty.tif"; p.write_bytes(b"")
    broken["empty"] = (str(p), tifffile.TiffFileError)
    p = tmp_path / "garbage.tif"; p.write_bytes(b"not a tiff" * 10)
    broken["garbage"] = (str(p), tifffile.TiffFileError)
    p = tmp_path / "adir.tif"; p.mkdir()
    broken["directory"] = (str(p), IsADirectoryError)

    for name, (path, exc_type) in broken.items():
        with pytest.raises(exc_type) as ei:
            probe_plane_layout(path)
        assert path in "".join(getattr(ei.value, "__notes__", [])), name

    # 半端に切れたファイルはヘッダだけなら通る (compute 時に落ちる)。
    p = tmp_path / "trunc.tif"; p.write_bytes(raw[:len(raw) // 2])
    layout = probe_plane_layout(str(p))
    assert (layout.n_pages, layout.height, layout.width) == (1, 32, 32)

    with pytest.raises(Exception) as ei:
        mod._read_file_planes(str(p), 1, 32, 32, np.uint16)
    assert str(p) in "".join(getattr(ei.value, "__notes__", []))

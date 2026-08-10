"""volume 全体を RAM へ展開する経路が無いことを固定するテスト。

取り込みは 28.8 GiB 超の volume を日常的に扱い、書き出しは
``BioIOWriter._write_ometiff_streaming`` がブロック単位で流すことで成立している。
そこへ至るまでのどこか 1 箇所でも実体化すると、その時点で MemoryError / OOM kill に
なるので、「遅延のままであること」自体をテストで固定する。

bioio には遅延版と EAGER 版がほぼ同名で並んでいる (``dask_data`` / ``data``、
``xarray_dask_data`` / ``xarray_data``)。取り違えても小さなテストデータでは何事も
なく通ってしまい、実データで初めて落ちる。ここでは「戻り値が dask のままか」を
直接確かめる。
"""
from __future__ import annotations

import numpy as np
import pytest
import tifffile
import dask.array as da

from ylabcommon.bioio.core.bioio_reader import BioIOReader
from ylabcommon.bioio.core.bioio_writer import BioIOWriter
from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import (
    stack_thorlab_with_bioio_calibrated,
)


VOLUME = np.arange(1 * 1 * 5 * 64 * 64, dtype=np.uint16).reshape(1, 1, 5, 64, 64)


# ---- BioIOReader: 既定は遅延 -------------------------------------------------

def test_get_data_returns_a_lazy_array():
    """既定のアクセサが volume 全体を実体化しない。

    以前は bioio の EAGER アクセサ (_img.data) を返しており、呼ぶだけで全画素を
    RAM に展開したうえ bioio 側がそれをキャッシュしていた。
    """
    assert isinstance(BioIOReader(VOLUME).get_data(), da.Array)


def test_read_is_lazy_too():
    """read() は get_data() の別名。名前が素直な分こちらが誤って使われやすい。"""
    assert isinstance(BioIOReader(VOLUME).read(), da.Array)


def test_lazy_data_still_exposes_shape_and_values():
    """遅延にしても shape/dtype はそのまま使え、必要な範囲だけ実体化できる。"""
    lazy = BioIOReader(VOLUME).get_data()
    assert lazy.shape == (1, 1, 5, 64, 64)
    assert lazy.dtype == np.uint16
    # 1 プレーンだけ取り出す (これがストリーミング書き出しのやり方)
    assert np.array_equal(np.asarray(lazy[0, 0, 2]), VOLUME[0, 0, 2])


def test_get_xarray_is_backed_by_dask():
    """xarray ビューも遅延裏付け (_img.xarray_data は EAGER なので使わない)。"""
    xr_view = BioIOReader(VOLUME).get_xarray()
    assert isinstance(xr_view.data, da.Array)


def test_eager_access_requires_an_explicit_call():
    """実体化したいときは明示的な名前のメソッドを呼ぶ (既定では起きない)。"""
    reader = BioIOReader(VOLUME)
    eager = reader.get_data_eager()
    assert isinstance(eager, np.ndarray)
    assert np.array_equal(eager, VOLUME)


# ---- ThorLabs スタック: 取り込みが遅延のまま返る -----------------------------

@pytest.fixture
def thorlab_dir(tmp_path):
    d = tmp_path / "img01"
    d.mkdir()
    for ch in ("ChanA", "ChanB"):
        for i in range(1, 4):
            tifffile.imwrite(d / f"{ch}_001_001_001_{i:03d}.tif",
                             np.full((8, 8), i, dtype=np.uint16))
    return d


PARAMS = {"mode": "T", "SizeT": 3, "PixelSizeX": 0.5, "PixelSizeY": 0.5, "PixelSizeZ": 1.0}


def test_stacking_returns_a_lazy_array(thorlab_dir):
    """取り込みの出口が dask のままであること (ここが実体だと全部無意味になる)。"""
    files = sorted(str(p) for p in thorlab_dir.glob("*.tif"))
    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, thorlab_dir / "Experiment.xml", PARAMS, min_kb=0
    )
    assert isinstance(stacked, da.Array)
    assert stacked.shape == (3, 2, 1, 8, 8)


def test_stacking_chunks_are_per_source_file(thorlab_dir):
    """チャンクが元ファイル単位であること。

    _write_ometiff_streaming は「1ブロック読んでも volume 全体を引かない」ことを
    前提にしている (docstring 参照)。単一チャンクの巨大配列だと streaming が
    成立しないので、チャンク分割されていること自体を固定する。
    """
    files = sorted(str(p) for p in thorlab_dir.glob("*.tif"))
    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, thorlab_dir / "Experiment.xml", PARAMS, min_kb=0
    )
    assert stacked.numblocks[0] > 1        # T 方向に分かれている
    assert stacked.chunksize != stacked.shape


# ---- 多ページファイルの取り違え (データ欠落) ---------------------------------

def test_multiple_multipage_files_are_concatenated_not_silently_dropped(tmp_path):
    """複数の多ページファイルを黙って捨てない。

    以前は max() で最大の 1 ファイルだけを採用し、残りを警告もなく捨てていた
    (10ページ x 3ファイル = 30 プレーンのはずが 10 プレーンになる)。

    「連続した一部」なのか「同じものの別コピー」なのかはファイル名からは
    決められない、という理由で以前はここで失敗させていたが、ThorLabs の生データで
    後者が出たことは無く、ファイル名の連番はそのまま面の順序である。
    素直に全部つなぐ (取り違えるくらいなら落とす、の判断は面の数と順序を
    ここで固定することに置き換える)。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 4):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.full((10, 8, 8), i, dtype=np.uint16))
    files = sorted(str(p) for p in d.glob("*.tif"))

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml",
        {"mode": "Z", "SizeZ": 30, "PixelSizeX": .5, "PixelSizeY": .5, "PixelSizeZ": 1.},
        min_kb=0,
    )

    assert stacked.shape == (1, 1, 30, 8, 8)     # 1 ファイルぶんに縮んでいない
    # 面の順序がファイル名順のまま (ファイル i の 10 面が連続して並ぶ)
    assert np.asarray(stacked)[0, 0, :, 0, 0].tolist() == \
        [1] * 10 + [2] * 10 + [3] * 10


def test_a_single_multipage_file_is_still_accepted(tmp_path):
    """1 ファイルに全プレーンが入っている正常系は従来どおり通す。"""
    d = tmp_path / "img01"
    d.mkdir()
    tifffile.imwrite(d / "ChanA_001_001_001_001.tif",
                     np.zeros((10, 8, 8), dtype=np.uint16))
    files = [str(p) for p in d.glob("*.tif")]

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml",
        {"mode": "Z", "SizeZ": 10, "PixelSizeX": .5, "PixelSizeY": .5, "PixelSizeZ": 1.},
        min_kb=0,
    )
    assert stacked.shape[2] == 10
    assert isinstance(stacked, da.Array)


def test_many_single_plane_files_keep_every_plane(tmp_path):
    """単一プレーン多数 (XYT 取得の形) は全ファイルが残る。"""
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 6):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.full((8, 8), i, dtype=np.uint16))
    files = sorted(str(p) for p in d.glob("*.tif"))

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml",
        {"mode": "T", "SizeT": 5, "PixelSizeX": .5, "PixelSizeY": .5, "PixelSizeZ": 1.},
        min_kb=0,
    )
    assert stacked.shape[0] == 5
    # 5 ファイル分の値がすべて残っている (欠落していない)
    assert sorted(np.unique(np.asarray(stacked)).tolist()) == [1, 2, 3, 4, 5]


# ---- OME-Zarr の二重 compute -------------------------------------------------

def test_zarr_output_is_opt_in(tmp_path):
    """save_zarr の既定は False。

    True だと同じ遅延スタックをもう一度 compute することになり、生 TIFF を全部
    読み直したうえ _write_omezarr 側で volume 全体を RAM へ展開する。安全側を既定に
    しておき、必要な呼び出し側だけが明示的に有効化する。
    """
    import inspect

    sig = inspect.signature(BioIOWriter.write)
    assert sig.parameters["save_zarr"].default is False

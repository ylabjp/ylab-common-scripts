"""遅延読み出しが本当に遅延であること、そして後始末ができること。

大きな画像を扱うので、「遅延のつもりで実体化していた」は静かに効く —— 動くが
メモリを食い、気づくのは落ちたときになる。ここでは **画素を読んでいないこと**
を、dask のグラフがある/`compute` を呼ぶまで値が出ないことで確かめる。
"""
import numpy as np
import pytest
import tifffile

from ylabcommon.bioio.core.dim_order import to_tczyx
from ylabcommon.bioio.core.lazy_read import (
    close_lazy_readers,
    open_lazy_reader_count,
    read_lazy_tczyx,
)


def _write(path, data, axes):
    with tifffile.TiffWriter(str(path), ome=True) as tif:
        tif.write(data, metadata={"axes": axes}, photometric="minisblack")
    return path


@pytest.fixture
def zcyx_file(tmp_path):
    """Z=3, C=2。各 (z, c) に見分けのつく値。"""
    data = np.zeros((1, 2, 3, 8, 6), np.uint16)  # TCZYX for the writer
    for c in range(2):
        for z in range(3):
            data[0, c, z] = 10 * z + c
    path = _write(tmp_path / "stack.ome.tif", data, "TCZYX")
    yield path
    close_lazy_readers(path)


def test_it_comes_back_lazy(zcyx_file):
    """dask 配列であること —— numpy が返っていたら全部 RAM に載っている。"""
    array = read_lazy_tczyx(zcyx_file)

    assert type(array).__module__.startswith("dask.")
    assert hasattr(array, "compute")


def test_the_shape_is_canonical(zcyx_file):
    array = read_lazy_tczyx(zcyx_file)

    assert array.shape == (1, 2, 3, 8, 6)  # TCZYX


def test_pixels_are_right_once_computed(zcyx_file):
    array = read_lazy_tczyx(zcyx_file)

    for c in range(2):
        for z in range(3):
            assert np.all(np.asarray(array[0, c, z]) == 10 * z + c)


def _bytes_read(work):
    """``work`` を走らせる間にファイルから読んだバイト数。

    面の枚数ではなく**バイト数**を数える。zarr ストア経由の読み出しは
    ``TiffPage.asarray`` を通らないので、そこに印を付けても 0 になる (実測)。
    """
    total = []
    original = tifffile.FileHandle.read

    def counting(self, size=-1):
        data = original(self, size)
        total.append(len(data))
        return data

    tifffile.FileHandle.read = counting
    try:
        work()
    finally:
        tifffile.FileHandle.read = original
    return sum(total)


def test_a_slice_does_not_read_the_whole_stack(tmp_path):
    """1 面だけ求めたら、1 面ぶんしか読まないこと。

    これが遅延の効き目そのもの。実体で読む経路 (``tifffile.imread``) は形に
    関係なく全部読むので、ここが崩れたら遅延になっていない。
    """
    data = np.random.randint(0, 4096, (1, 2, 3, 256, 256), np.uint16)  # 6 面
    path = _write(tmp_path / "big.ome.tif", data, "TCZYX")
    try:
        array = read_lazy_tczyx(path)

        one_plane = _bytes_read(lambda: np.asarray(array[0, 0, 0]))
        whole = _bytes_read(lambda: np.asarray(array))

        # 6 面のうち 1 面。端数を見込んでも 1/4 を超えていたら読み過ぎ。
        assert one_plane < whole / 4
        assert one_plane >= 256 * 256 * 2  # 少なくともその 1 面は読んでいる
    finally:
        close_lazy_readers(path)


def test_a_squeezed_axis_comes_back(tmp_path):
    """tifffile は大きさ 1 の軸を落とす。正準レイアウトでは戻っていること。"""
    path = _write(tmp_path / "single.ome.tif", np.zeros((8, 6), np.uint16), "YX")
    try:
        array = read_lazy_tczyx(path)
        assert array.shape == (1, 1, 1, 8, 6)
    finally:
        close_lazy_readers(path)


def test_a_real_time_series_reads(tmp_path):
    """T>1 がそのまま読めること —— slice-analysis が扱うのはこれ。"""
    data = np.zeros((3, 1, 1, 8, 6), np.uint16)
    for t in range(3):
        data[t] = t
    path = _write(tmp_path / "movie.ome.tif", data, "TCZYX")
    try:
        array = read_lazy_tczyx(path)

        assert array.shape == (3, 1, 1, 8, 6)
        for t in range(3):
            assert np.all(np.asarray(array[t]) == t)
    finally:
        close_lazy_readers(path)


def test_a_file_that_is_not_a_tiff_gives_none(tmp_path):
    path = tmp_path / "notatiff.tif"
    path.write_bytes(b"this is not a tiff")

    assert read_lazy_tczyx(path) is None


def test_a_refused_file_leaves_no_handle_open(tmp_path):
    """返さないのにハンドルだけ残ると、そのファイルは二度と上書きできない。

    Windows は開かれているファイルを置き換えられない (slice-analysis が実機で
    踏んだ ``PermissionError [WinError 5]``)。RGB は正準レイアウトへ置けないので、
    断られる側の例として使う。
    """
    path = tmp_path / "rgb.tif"
    tifffile.imwrite(str(path), np.zeros((8, 6, 3), np.uint8), photometric="rgb")

    assert read_lazy_tczyx(path) is None
    assert open_lazy_reader_count(path) == 0


def test_handles_are_tracked_and_closeable(zcyx_file):
    read_lazy_tczyx(zcyx_file)
    read_lazy_tczyx(zcyx_file)

    assert open_lazy_reader_count(zcyx_file) == 2
    assert close_lazy_readers(zcyx_file) == 2
    assert open_lazy_reader_count(zcyx_file) == 0


def test_to_tczyx_does_not_materialise_a_lazy_array():
    """``to_tczyx`` 自身が実体化しないこと。

    ``np.asarray`` を 1 つ挟むだけで、遅延で読んだ意味が消える。静かに効くので
    ここで留める。
    """
    import dask.array as da

    got = to_tczyx(da.zeros((3, 2, 8, 6), dtype="uint16"), "ZCYX")

    assert type(got).__module__.startswith("dask.")
    assert got.shape == (1, 2, 3, 8, 6)

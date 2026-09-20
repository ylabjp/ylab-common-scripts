"""正準レイアウト ``TCZYX`` —— 5 軸すべてを大きさ 1 でも必ず置く。

潰した軸は、読む側に「取得内容ごとに別の並び」を押し付ける。4 通りを書き分けて
いたコードは、5 通り目が来ると落ちる。ここで常に 5 次元に揃うことを確かめる。

**T を落とさない**のは slice-analysis が時系列を扱うから (``AXES_ORDER =
"TCZYX"``、``shape[0]`` をフレーム数として位置合わせを回す)。T を落とす正準形は
共通ライブラリの最大の利用者を締め出す。
"""
import numpy as np
import pytest

from ylabcommon.bioio.core.dim_order import (
    CANONICAL_DIM_ORDER,
    channel_plane,
    to_tczyx,
)


@pytest.mark.parametrize(
    "axes, shape, expected",
    [
        ("YX", (6, 5), (1, 1, 1, 6, 5)),             # 1 チャネル・1 面
        ("ZYX", (4, 6, 5), (1, 1, 4, 6, 5)),         # 1 チャネル・Z スタック
        ("CYX", (2, 6, 5), (1, 2, 1, 6, 5)),         # 複数チャネル・1 面
        ("ZCYX", (4, 2, 6, 5), (1, 2, 4, 6, 5)),     # ImageJ hyperstack の並び
        ("CZYX", (2, 4, 6, 5), (1, 2, 4, 6, 5)),     # T だけ足りない
        ("TCZYX", (3, 2, 4, 6, 5), (3, 2, 4, 6, 5)),  # 既に正準 (T>1 もそのまま)
    ],
)
def test_every_layout_becomes_five_dimensional(axes, shape, expected):
    got = to_tczyx(np.zeros(shape, np.uint16), axes)

    assert got.shape == expected
    assert got.ndim == len(CANONICAL_DIM_ORDER)


def test_pixels_land_where_the_axes_say():
    """並べ替えが正しいこと —— 形だけ合っていても中身が入れ替わっては意味がない。"""
    # ZCYX: Z=2, C=3。各 (z, c) に見分けのつく値を入れる。
    data = np.zeros((2, 3, 6, 5), np.uint16)
    for z in range(2):
        for c in range(3):
            data[z, c] = 10 * z + c

    got = to_tczyx(data, "ZCYX")

    for z in range(2):
        for c in range(3):
            assert np.all(got[0, c, z] == 10 * z + c)


def test_a_real_time_series_survives():
    """T>1 がそのまま通ること —— slice-analysis の位置合わせはこれで回っている。

    以前ここは「T>1 は断る」だった (正準形が CZYX で T を落としていた)。その形は
    共通ライブラリの側から slice-analysis を締め出すので、正準形ごと TCZYX に
    直した。フレームが 1 枚も減らないことを留める。
    """
    data = np.zeros((3, 2, 1, 6, 5), np.uint16)
    for t in range(3):
        data[t] = t

    got = to_tczyx(data, "TCZYX")

    assert got.shape == (3, 2, 1, 6, 5)
    for t in range(3):
        assert np.all(got[t] == t)


def test_a_time_series_without_c_or_z_is_filled_in():
    got = to_tczyx(np.zeros((3, 6, 5), np.uint16), "TYX")

    assert got.shape == (3, 1, 1, 6, 5)


def test_axes_and_array_must_agree():
    with pytest.raises(ValueError, match="describes 4 dimensions"):
        to_tczyx(np.zeros((6, 5), np.uint16), "ZCYX")


def test_an_unnamed_axis_is_refused():
    """tifffile は名前を決められなかった軸を ``Q`` にする。畳み方を決められない。"""
    with pytest.raises(ValueError, match="'Q'"):
        to_tczyx(np.zeros((1, 2, 6, 5), np.uint16), "QQYX")


def test_an_image_needs_y_and_x():
    with pytest.raises(ValueError, match="no X"):
        to_tczyx(np.zeros((4, 6), np.uint16), "ZY")


def test_no_copy_is_made():
    """画素は view で返る —— 大きなスタックを黙って倍に抱えないこと。"""
    data = np.zeros((4, 2, 6, 5), np.uint16)

    got = to_tczyx(data, "ZCYX")

    assert got.base is not None
    assert np.shares_memory(got, data)


def test_channel_plane_averages_over_z():
    data = np.zeros((2, 2, 6, 5), np.float64)  # ZCYX
    data[0, 0] = 10.0
    data[1, 0] = 20.0

    assert np.all(channel_plane(data, "ZCYX", 0) == 15.0)


def test_channel_plane_picks_the_frame():
    data = np.zeros((3, 1, 1, 6, 5), np.float64)  # TCZYX
    for t in range(3):
        data[t] = t

    assert np.all(channel_plane(data, "TCZYX", 0, frame=2) == 2.0)


def test_channel_plane_keeps_values_when_there_is_one_z():
    """Z=1 の平均は要素が 1 つなので値を変えない (dtype は float になる)。"""
    data = np.arange(30, dtype=np.uint16).reshape(1, 6, 5)  # ZYX, Z=1

    got = channel_plane(data, "ZYX", 0)

    assert np.array_equal(got, data[0].astype(float))


def test_the_writer_takes_what_to_tczyx_produces(tmp_path):
    """``to_tczyx`` の出力をそのまま書けること —— 正準形が writer の入口と一致する。"""
    import tifffile
    from ylabcommon.bioio.core.bioio_writer import BioIOWriter

    canonical = to_tczyx(np.zeros((3, 2, 6, 5), np.uint16), "ZCYX")  # C=2, Z=3
    BioIOWriter(str(tmp_path / "canonical")).write(
        canonical, dim_order=CANONICAL_DIM_ORDER, channel_names=["CH1", "CH3"])

    written = tifffile.TiffFile(str(tmp_path / "canonical.ome.tif"))
    # C=2, Z=3 が入った順のまま出てくること (入れ替わっていたら軸を取り違えている)。
    assert written.series[0].shape == (2, 3, 6, 5)
    assert written.series[0].axes == "CZYX"


def test_the_writer_says_how_to_get_to_the_canonical_order(tmp_path):
    from ylabcommon.bioio.core.bioio_writer import BioIOWriter

    with pytest.raises(ValueError, match="to_tczyx"):
        BioIOWriter(str(tmp_path / "x")).write(
            np.zeros((3, 6, 5), np.uint16), dim_order="ZYX")

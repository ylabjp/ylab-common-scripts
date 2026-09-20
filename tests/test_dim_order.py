"""正準レイアウト ``CZYX`` —— C と Z は大きさ 1 でも必ず置く。

潰した軸は、読む側に「取得内容ごとに別の並び」を押し付ける。4 通りを書き分けて
いたコードは、5 通り目が来ると落ちる。ここで常に 4 次元に揃うことを確かめる。
"""
import numpy as np
import pytest

from ylabcommon.bioio.core.dim_order import (
    CANONICAL_DIM_ORDER,
    channel_plane,
    to_czyx,
)


@pytest.mark.parametrize(
    "axes, shape, expected",
    [
        ("YX", (6, 5), (1, 1, 6, 5)),          # 1 チャネル・1 面
        ("ZYX", (4, 6, 5), (1, 4, 6, 5)),      # 1 チャネル・Z スタック
        ("CYX", (2, 6, 5), (2, 1, 6, 5)),      # 複数チャネル・1 面
        ("ZCYX", (4, 2, 6, 5), (2, 4, 6, 5)),  # ImageJ hyperstack の並び
        ("CZYX", (2, 4, 6, 5), (2, 4, 6, 5)),  # 既に正準
        ("TCZYX", (1, 2, 4, 6, 5), (2, 4, 6, 5)),  # T=1 は落とす
    ],
)
def test_every_layout_becomes_four_dimensional(axes, shape, expected):
    got = to_czyx(np.zeros(shape, np.uint16), axes)

    assert got.shape == expected
    assert got.ndim == len(CANONICAL_DIM_ORDER)


def test_pixels_land_where_the_axes_say():
    """並べ替えが正しいこと —— 形だけ合っていても中身が入れ替わっては意味がない。"""
    # ZCYX: Z=2, C=3。各 (z, c) に見分けのつく値を入れる。
    data = np.zeros((2, 3, 6, 5), np.uint16)
    for z in range(2):
        for c in range(3):
            data[z, c] = 10 * z + c

    got = to_czyx(data, "ZCYX")

    for z in range(2):
        for c in range(3):
            assert np.all(got[c, z] == 10 * z + c)


def test_a_real_time_series_is_refused_not_truncated():
    """T>1 を黙って切ると画素が消え、消えたことは出力から分からない。"""
    with pytest.raises(ValueError, match="T=3"):
        to_czyx(np.zeros((3, 2, 4, 6, 5), np.uint16), "TCZYX")


def test_axes_and_array_must_agree():
    with pytest.raises(ValueError, match="describes 4 dimensions"):
        to_czyx(np.zeros((6, 5), np.uint16), "ZCYX")


def test_an_unnamed_axis_is_refused():
    """tifffile は名前を決められなかった軸を ``Q`` にする。畳み方を決められない。"""
    with pytest.raises(ValueError, match="'Q'"):
        to_czyx(np.zeros((1, 2, 6, 5), np.uint16), "QQYX")


def test_an_image_needs_y_and_x():
    with pytest.raises(ValueError, match="no X"):
        to_czyx(np.zeros((4, 6), np.uint16), "ZY")


def test_no_copy_is_made():
    """画素は view で返る —— 大きなスタックを黙って倍に抱えないこと。"""
    data = np.zeros((4, 2, 6, 5), np.uint16)

    got = to_czyx(data, "ZCYX")

    assert got.base is not None
    assert np.shares_memory(got, data)


def test_channel_plane_averages_over_z():
    data = np.zeros((2, 2, 6, 5), np.float64)  # ZCYX
    data[0, 0] = 10.0
    data[1, 0] = 20.0

    assert np.all(channel_plane(data, "ZCYX", 0) == 15.0)


def test_channel_plane_keeps_values_when_there_is_one_z():
    """Z=1 の平均は要素が 1 つなので値を変えない (dtype は float になる)。"""
    data = np.arange(30, dtype=np.uint16).reshape(1, 6, 5)  # ZYX, Z=1

    got = channel_plane(data, "ZYX", 0)

    assert np.array_equal(got, data[0].astype(float))


def test_the_writer_accepts_the_canonical_layout(tmp_path):
    """CZYX をそのまま渡せること。

    呼び出し側ごとに ``data[None]`` を書かせると、書き忘れた 1 か所で C 軸が
    T 軸として通ってしまい、チャネルが時点に化ける。
    """
    import tifffile
    from ylabcommon.bioio.core.bioio_writer import BioIOWriter

    out = tmp_path / "canonical"
    BioIOWriter(str(out)).write(
        np.zeros((2, 3, 6, 5), np.uint16),      # CZYX
        dim_order=CANONICAL_DIM_ORDER,
        channel_names=["CH1", "CH3"],
    )

    written = tifffile.TiffFile(str(tmp_path / "canonical.ome.tif"))
    # C=2, Z=3 が入った順のまま出てくること (入れ替わっていたら軸を取り違えている)。
    assert written.series[0].shape == (2, 3, 6, 5)
    assert written.series[0].axes == "CZYX"


def test_the_writer_names_both_accepted_orders_when_refusing(tmp_path):
    from ylabcommon.bioio.core.bioio_writer import BioIOWriter

    with pytest.raises(ValueError, match="CZYX or TCZYX"):
        BioIOWriter(str(tmp_path / "x")).write(
            np.zeros((3, 6, 5), np.uint16), dim_order="ZYX")

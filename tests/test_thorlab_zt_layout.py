"""ファイル名が持つ Z / T の次元を、実際に軸へ反映できていることのテスト。

ThorLabs の生ファイルは ``ChanA_<X>_<Y>_<Z>_<T>.tif`` で、取得の実際の次元を
連番として持っている。一方 Experiment.xml は **取得前の設定** なので、

- Z スタックを組んだまま実際には T 連続撮影になっていた
- 4D (Z スタックのタイムラプス) で撮ったが途中で止めた

といった場合に実データとずれる。ずれたまま「XML の mode ひとつで全部を1軸へ積む」と、
3001 時点が Z 軸へ潰れて「深さ 1500 um のスタック」が黙って出来上がる
(実際に起きた: XML が SizeZ=61 / ZStackEnabled=1、実データは 3001 枚)。

そこで積む軸はファイル名から決める。時点ごとに Z をまとめて (T, C, Z, Y, X) を
組むので、XYT / Z スタック / 4D / 1時点=1多ページファイル が同じ1本の経路で通る。

連番が読めない命名だけは XML の mode へ退避するが、**黙って退避しない**。
読めなかったことに気付かないまま mode を信じるのが、まさに上の不具合が
表に出なかった経路だった (退避したこと自体がログのどこにも出ていなかった)。
"""
from __future__ import annotations

import numpy as np
import pytest
import tifffile

from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import (
    _group_by_timepoint,
    _thorlabs_zt,
    stack_thorlab_with_bioio_calibrated,
)


def _params(mode, size_z=1, size_t=1):
    return {"mode": mode, "SizeZ": size_z, "SizeT": size_t,
            "SizeY": 8, "SizeX": 8,
            "PixelSizeX": .5, "PixelSizeY": .5, "PixelSizeZ": 2.0}


def _write(d, ch, z, t, value, shape=(8, 8)):
    tifffile.imwrite(d / f"{ch}_001_001_{z:03d}_{t:03d}.tif",
                     np.full(shape, value, dtype=np.uint16))


# ---- ファイル名の読み取り ----------------------------------------------------

def test_zt_is_read_from_the_thorlabs_numeric_name():
    assert _thorlabs_zt("/x/ChanA_001_002_003_004.tif") == (3, 4)
    assert _thorlabs_zt("/x/ChanB_00001_00002_00061_02999.tif") == (61, 2999)


@pytest.mark.parametrize("name,expected", [
    # 接頭辞が増えても末尾2つの連番は同じ意味を持つ。ちょうど5トークンを
    # 要求すると、こういう形で黙って退避してしまう (実際に起きた)。
    ("Image_ChanA_001_001_003_004.tif", (3, 4)),
    ("prefix_more_ChanA_001_001_007_042.tif", (7, 42)),
    # 数値でないトークンは飛ばして、末尾の数値2つを見る。
    ("ChanA_001_001_00a_003_004.tif", (3, 4)),
    # 連番が2つしかない形 (X/Y を持たない命名) も読める。
    ("ChanA_001_004.tif", (1, 4)),
])
def test_trailing_numbers_are_read_whatever_the_prefix(name, expected):
    assert _thorlabs_zt("/x/" + name) == expected


@pytest.mark.parametrize("name", [
    "ChanA.tif",                       # 連番が無い
    "ChanA_001.tif",                   # Z と T を分けられない
    "image_XY01_Z02_CH1_T03.tif",      # ラベル付き形式 (数値トークンが無い)
])
def test_unparsable_names_return_none(name):
    assert _thorlabs_zt("/x/" + name) is None


def test_grouping_orders_timepoints_and_z_within_them():
    files = ["ChanA_001_001_002_002.tif", "ChanA_001_001_001_002.tif",
             "ChanA_001_001_002_001.tif", "ChanA_001_001_001_001.tif"]
    assert _group_by_timepoint(files) == [
        (1, ["ChanA_001_001_001_001.tif", "ChanA_001_001_002_001.tif"]),
        (2, ["ChanA_001_001_001_002.tif", "ChanA_001_001_002_002.tif"]),
    ]


def test_grouping_gives_up_on_names_it_cannot_read():
    assert _group_by_timepoint(["ChanA_001_001_001_001.tif", "odd.tif"]) is None


# ---- 4D 取得 -----------------------------------------------------------------

def test_a_z_stack_timelapse_lands_on_both_axes(tmp_path):
    """Z と T が両方動いていれば (T, C, Z, Y, X) になる。

    これが今回の本題。以前はどちらの mode でも全 12 枚が1軸へ潰れていた。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for t in range(1, 5):
        for z in range(1, 4):
            _write(d, "ChanA", z, t, t * 10 + z)
    files = sorted(str(p) for p in d.glob("*.tif"))

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml", _params("Z", size_z=3, size_t=4), min_kb=0)

    assert stacked.shape == (4, 1, 3, 8, 8)
    got = np.asarray(stacked)[:, 0, :, 0, 0]
    # 面の中身が (t, z) の位置と一致する = 順序も軸の割り当ても正しい
    np.testing.assert_array_equal(
        got, [[t * 10 + z for z in range(1, 4)] for t in range(1, 5)])


def test_the_same_layout_is_built_regardless_of_the_xml_mode(tmp_path):
    """4D と分かった時点で mode は使わない (XML の設定ミスに引きずられない)。"""
    d = tmp_path / "img01"
    d.mkdir()
    for t in range(1, 4):
        for z in range(1, 3):
            _write(d, "ChanA", z, t, t * 10 + z)
    files = sorted(str(p) for p in d.glob("*.tif"))

    shapes = set()
    for mode in ("Z", "T"):
        stacked, _ = stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", _params(mode, size_z=2, size_t=3),
            min_kb=0)
        shapes.add(stacked.shape)

    assert shapes == {(3, 1, 2, 8, 8)}


def test_channels_stay_on_C_in_a_4d_acquisition(tmp_path):
    d = tmp_path / "img01"
    d.mkdir()
    for ch, base in (("ChanA", 0), ("ChanB", 100)):
        for t in range(1, 4):
            for z in range(1, 3):
                _write(d, ch, z, t, base + t * 10 + z)
    files = sorted(str(p) for p in d.glob("*.tif"))

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml", _params("Z", size_z=2, size_t=3), min_kb=0)

    assert stacked.shape == (3, 2, 2, 8, 8)
    arr = np.asarray(stacked)
    assert arr[0, 0, 0, 0, 0] == 11          # ChanA t=1 z=1
    assert arr[0, 1, 0, 0, 0] == 111         # ChanB t=1 z=1


def test_an_interrupted_last_stack_is_dropped_with_a_warning(tmp_path):
    """途中で止まって最後の時点の Z が欠けたら、その時点を捨てて続ける。

    黙って通すと、最後の1時点だけ厚みの違うボリュームが混ざる。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for t in range(1, 4):
        for z in range(1, 5):
            _write(d, "ChanA", z, t, t * 10 + z)
    for z in range(1, 3):                    # 4時点目は 4面のうち 2面で終了
        _write(d, "ChanA", z, 4, 40 + z)
    files = sorted(str(p) for p in d.glob("*.tif"))

    with pytest.warns(UserWarning, match="incomplete Z stack"):
        stacked, _ = stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", _params("Z", size_z=4, size_t=4),
            min_kb=0)

    assert stacked.shape == (3, 1, 4, 8, 8)   # 揃っている3時点だけ
    assert np.asarray(stacked)[:, 0, 0, 0, 0].tolist() == [11, 21, 31]


def test_a_multipage_file_per_timepoint_becomes_the_z_axis(tmp_path):
    """1時点 = 1つの多ページファイル、という形も (T, Z) へ展開する。

    この場合ファイル名の Z は動かないので 4D 判定には乗らないが、mode="Z" で
    面が Z 軸に載るという従来の経路がそのまま正しい。
    """
    d = tmp_path / "img01"
    d.mkdir()
    tifffile.imwrite(d / "ChanA_001_001_001_001.tif",
                     np.arange(6 * 8 * 8, dtype=np.uint16).reshape(6, 8, 8),
                     photometric="minisblack")
    files = [str(p) for p in d.glob("*.tif")]

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml", _params("Z", size_z=6, size_t=1), min_kb=0)

    assert stacked.shape == (1, 1, 6, 8, 8)


# ---- 片方しか動いていない取得 (従来の挙動を変えない) --------------------------

def test_a_pure_z_stack_still_lands_on_Z(tmp_path):
    """Z だけが動く取得は従来どおり Z 軸へ (T=1)。"""
    d = tmp_path / "img01"
    d.mkdir()
    for z in range(1, 6):
        _write(d, "ChanA", z, 1, z)
    files = sorted(str(p) for p in d.glob("*.tif"))

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml", _params("Z", size_z=5, size_t=1), min_kb=0)

    assert stacked.shape == (1, 1, 5, 8, 8)
    assert np.asarray(stacked)[0, 0, :, 0, 0].tolist() == [1, 2, 3, 4, 5]


def test_a_pure_timelapse_still_lands_on_T(tmp_path):
    """T だけが動く取得は従来どおり T 軸へ (Z=1)。"""
    d = tmp_path / "img01"
    d.mkdir()
    for t in range(1, 6):
        _write(d, "ChanA", 1, t, t)
    files = sorted(str(p) for p in d.glob("*.tif"))

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml", _params("T", size_z=1, size_t=5), min_kb=0)

    assert stacked.shape == (5, 1, 1, 8, 8)
    assert np.asarray(stacked)[:, 0, 0, 0, 0].tolist() == [1, 2, 3, 4, 5]


def test_a_stale_z_stack_xml_does_not_push_a_timelapse_onto_Z(tmp_path):
    """XML が Z スタック指定でも、ファイル名が T 連続撮影ならそちらに従う。

    これが報告された不具合そのもの。XML は ZStackEnabled=1 / SizeZ=61 のまま
    T 連続撮影で 3001 枚が撮られ、3001 時点が丸ごと Z 軸へ潰れて
    「深さ 1500 um のスタック」が出来ていた。XML は取得前の設定でしかないので、
    ファイル名の連番を事実として採る。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for t in range(1, 8):
        _write(d, "ChanA", 1, t, t)
    files = sorted(str(p) for p in d.glob("*.tif"))

    with pytest.warns(UserWarning, match="record what it actually produced"):
        stacked, _ = stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", _params("Z", size_z=61, size_t=3000),
            min_kb=0)

    assert stacked.shape == (7, 1, 1, 8, 8)     # T 軸に載る (Z=1)
    assert np.asarray(stacked)[:, 0, 0, 0, 0].tolist() == [1, 2, 3, 4, 5, 6, 7]


def test_names_without_sequence_numbers_fall_back_loudly(tmp_path, capsys):
    """連番が読めないときに黙って mode を信じない。

    回帰: 実データで ``_group_by_timepoint`` が None を返し、何の説明もないまま
    XML の mode="Z" が採用された。結果は以前と同じ「3001 時点が Z 軸」で、
    しかもログには退避したことがどこにも出ていなかったため、修正が効いていない
    のか経路が違うのかを log からは切り分けられなかった。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 5):
        # チャンネルは読めるが連番が読めない命名 ("p1" は数値トークンではない)
        tifffile.imwrite(d / f"ChanA_p{i}.tif",
                         np.full((8, 8), i, dtype=np.uint16))
    files = sorted(str(p) for p in d.glob("*.tif"))

    with pytest.warns(UserWarning, match="Could not read the Z/T sequence numbers"):
        stacked, _ = stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", _params("Z", size_z=4, size_t=1), min_kb=0)

    assert stacked.shape == (1, 1, 4, 8, 8)          # 従来の退避結果は変えない
    # どのファイル名で諦めたのかが分かる (次の調査の起点になる)
    out = capsys.readouterr().out
    assert "file names give no Z/T" in out
    assert "ChanA_p1.tif" in out


def test_a_matching_z_stack_is_not_flagged(tmp_path):
    """XML と実データが一致していれば警告は出さない (雑音にしない)。"""
    d = tmp_path / "img01"
    d.mkdir()
    for z in range(1, 5):
        _write(d, "ChanA", z, 1, z)
    files = sorted(str(p) for p in d.glob("*.tif"))

    with warnings_as_errors():
        stacked, _ = stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", _params("Z", size_z=4, size_t=1),
            min_kb=0)
    assert stacked.shape == (1, 1, 4, 8, 8)


def warnings_as_errors():
    import warnings as _w
    ctx = _w.catch_warnings()

    class _Ctx:
        def __enter__(self):
            ctx.__enter__()
            _w.simplefilter("error")

        def __exit__(self, *exc):
            return ctx.__exit__(*exc)

    return _Ctx()

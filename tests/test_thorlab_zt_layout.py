"""ファイル名が持つ Z / T の次元を、実際に軸へ反映できていることのテスト。

ThorLabs の生ファイルは ``ChanA_<X>_<Y>_<Z>_<T>.tif`` で、取得の実際の次元を
連番として持っている。一方 Experiment.xml は **取得前の設定** なので、

- Z スタックを組んだまま実際には T 連続撮影になっていた
- 4D (Z スタックのタイムラプス) で撮ったが途中で止めた

といった場合に実データとずれる。ずれたまま「XML の mode ひとつで全部を1軸へ積む」と、
3001 時点が Z 軸へ潰れて「深さ 1500 um のスタック」が黙って出来上がる
(実際に起きた: XML が SizeZ=61 / ZStackEnabled=1、実データは 3001 枚)。

組み立ては3段階しかない。

1. **枠** を XML が決める (SizeT x SizeZ)。取得の上限であって、実際にそこまで
   撮れたかは XML には書かれていない。
2. ファイル名の末尾2つの連番が指す枡を **埋める**。
3. 埋まらなかった分を **カットする**。

退避経路は無い。以前は「連番が読めないファイルが1枚でもあれば XML の mode に
従って全部を1軸へ積む」という退避があり、それが 3000 枚から得られた正しい配置を
捨てて不具合を覆い隠していた。読めないファイルは枡を埋めないので落ちるだけで、
残りの配置には影響しない。
"""
from __future__ import annotations

import numpy as np
import pytest
import tifffile

from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import (
    _fill_frame,
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


# ---- 枠を埋める / 埋まらない分をカットする ------------------------------------

def _names(z_range, t_range):
    return ["ChanA_001_001_%03d_%03d.tif" % (z, t) for t in t_range for z in z_range]


def test_the_frame_is_filled_in_order():
    frame = _fill_frame(_names(range(1, 3), range(1, 4)), max_t=3, max_z=2)
    assert frame.t_keep == [1, 2, 3]
    assert frame.z_keep == [1, 2]
    assert frame.slots[(2, 1)] == "ChanA_001_001_001_002.tif"
    assert (frame.unreadable, frame.outside, frame.ragged) == ([], [], [])


def test_an_unfilled_tail_is_cut_not_the_whole_frame():
    """枠が 3000 時点でも、埋まったのが 3 時点ならそこまで。"""
    frame = _fill_frame(_names(range(1, 3), range(1, 4)), max_t=3000, max_z=2)
    assert frame.t_keep == [1, 2, 3]


def test_unfilled_z_is_cut_so_a_timelapse_does_not_become_a_stack():
    """XML が Z=61 でも、ファイルが z=1 しか埋めなければ Z=1 になる。

    報告された不具合そのもの: 枠を信じて 3001 時点を Z 軸へ積むと
    「深さ 1500 um のスタック」が出来ていた。
    """
    frame = _fill_frame(_names([1], range(1, 3002)), max_t=3000, max_z=61)
    assert frame.z_keep == [1]                # 埋まらなかった z=2..61 は落ちる
    # 枠は目標なので、はみ出した t=3001 は落とさず報告だけする
    assert len(frame.t_keep) == 3001
    assert len(frame.outside) == 1


def test_names_without_sequence_numbers_are_cut_not_fatal():
    """読めない名前が1枚混ざっても、残り全部の情報は捨てない。

    回帰: 以前は1枚でも読めないとチャンネル全体を諦めて XML の mode へ退避し、
    3000 枚から得られた正しい配置を丸ごと捨てていた。
    """
    files = _names([1], range(1, 5)) + ["ChanA_Preview.tif"]
    frame = _fill_frame(files, max_t=4, max_z=1)

    assert frame.t_keep == [1, 2, 3, 4]       # 残りは通常どおり埋まる
    assert frame.unreadable == ["ChanA_Preview.tif"]


def test_the_largest_complete_block_wins():
    """最後の1時点だけ Z が欠けたら、その時点を捨てる (z を削らない)。

    「全時点に共通する z」を採ると 12面 x 50時点 = 600 面、
    「全 z が揃う時点」を採ると 61面 x 49時点 = 2989 面。後者を選ぶ。
    """
    files = _names(range(1, 62), range(1, 50)) + _names(range(1, 13), [50])
    frame = _fill_frame(files, max_t=3000, max_z=61)

    assert len(frame.z_keep) == 61
    assert len(frame.t_keep) == 49
    assert frame.ragged == [50]


def test_a_few_stray_planes_do_not_shrink_a_long_timelapse():
    """逆向きの場合。少数の時点だけ余分な z を持つなら、その z の方を捨てる。

    100 時点の XYT に、2 時点だけ z=2..5 のファイルが紛れている状況。
    「z を残す」を選ぶと 5面 x 2時点 = 10 面しか残らず、98 時点が消える。
    「時点を残す」なら 1面 x 100時点 = 100 面。後者を選ぶ。

    上の「最後の1時点だけ欠ける」ケースとは逆向きなので、この2つが揃って
    初めて「残る面の数を最大にする」規則が効いていると言える
    (片方だけなら「常に z を優先」でも通ってしまう)。
    """
    files = _names([1], range(1, 101)) + _names(range(2, 6), [1, 2])
    frame = _fill_frame(files, max_t=3000, max_z=61)

    assert frame.z_keep == [1]
    assert len(frame.t_keep) == 100


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

    with pytest.warns(UserWarning, match="were cut"):
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


def test_names_without_sequence_numbers_are_cut_and_reported(tmp_path, capsys):
    """連番が読めないファイルは落ちるだけ。残りの配置は壊さない。

    回帰: 以前はここに「XML の mode に従って全部を1軸へ積む」退避があり、
    1枚読めないだけでチャンネル全体の配置を捨てていた。実データではそれが起きて
    3001 時点が Z 軸へ潰れ、しかも退避したこと自体がログに出ていなかった。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for t in range(1, 5):
        _write(d, "ChanA", 1, t, t)
    tifffile.imwrite(d / "ChanA_Preview.tif",       # 連番の無い紛れ込み
                     np.zeros((8, 8), dtype=np.uint16))
    files = sorted(str(p) for p in d.glob("*.tif"))

    with pytest.warns(UserWarning, match="no Z/T sequence numbers"):
        stacked, _ = stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", _params("Z", size_z=61, size_t=4), min_kb=0)

    # 紛れ込みを落としたうえで、残り4枚は時点として正しく並ぶ
    assert stacked.shape == (4, 1, 1, 8, 8)
    assert np.asarray(stacked)[:, 0, 0, 0, 0].tolist() == [1, 2, 3, 4]
    # 何がどれだけ落ちたかが分かる
    out = capsys.readouterr().out
    assert "filled 4/5 file(s)" in out


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

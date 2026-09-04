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

import os
from pathlib import Path

import numpy as np
import pytest
import tifffile

from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import (
    _fill_frame,
    _interior_gaps,
    _report_cuts,
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
    files = _names([1], range(1, 5)) + ["ChanA_reference.tif"]
    frame = _fill_frame(files, max_t=4, max_z=1)

    assert frame.t_keep == [1, 2, 3, 4]       # 残りは通常どおり埋まる
    assert frame.unreadable == ["ChanA_reference.tif"]


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


def _warnings_for(files, max_t, max_z):
    """``_report_cuts`` が出す警告の本文を集める。"""
    import warnings

    frame = _fill_frame(files, max_t=max_t, max_z=max_z)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _report_cuts("ChanA", files, frame, max_t, max_z)
    return frame, [str(w.message) for w in caught]


def _missing(missing, z_range=range(1, 6), t_range=range(1, 4)):
    """``missing`` の (z, t) だけ抜いたファイル名一覧。"""
    return ["ChanA_001_001_%03d_%03d.tif" % (z, t)
            for t in t_range for z in z_range if (z, t) not in missing]


# ---- 目盛りが狂う落ち方は黙って通さない ----------------------------------------

def test_a_hole_in_the_middle_of_the_stack_is_reported():
    """途中の面が 1 枚欠けると、その z が **全時点から** 落ちることを言う。

    報告された不具合 (slice-analysis#69) の機構。(z=3, t=2) の 1 枚が欠けると
    「残る面の数を最大にする」規則は

        z を捨てる: 4 面 x 3 時点 = 12 面   <- こちらを選ぶ
        t を捨てる: 5 面 x 2 時点 = 10 面

    となり、z=3 が全時点から消える。出来上がりは Z=4 の綺麗なスタックに見えるが、
    3 番目の面は元の 4 番目で、**面の間隔が 1 か所だけ 2 倍**になっている。
    ここが黙っていると、深さも 3D 位置合わせも狂ったまま解析まで進む。
    """
    frame, messages = _warnings_for(_missing({(3, 2)}), max_t=3, max_z=5)

    assert frame.z_keep == [1, 2, 4, 5]        # 実際に z=3 が消える
    assert frame.ragged == []                  # 時点は 1 つも落ちていない
    assert any("z=[3]" in m and "no longer uniform" in m for m in messages), messages


def test_several_holes_are_all_named():
    """穴が複数あっても、どの面が消えたかを全部言う。"""
    frame, messages = _warnings_for(_missing({(2, 1), (4, 3)}), max_t=3, max_z=5)

    assert frame.z_keep == [1, 3, 5]           # 5 面が 3 面になる
    assert any("z=[2, 4]" in m for m in messages), messages


def test_a_timepoint_missing_from_the_middle_is_reported():
    """時点がまるごと無いときも言う。

    ``ragged`` には出ない —— あれは「枡はあるが Z が欠けた時点」で、丸ごと
    無い時点はそもそも枡が立たない。残った時点は詰めて積まれるので、
    **n 番目のフレームが n 番目の時点とは限らない** 状態になる
    (slice-analysis#67 の「T:27 と T:28 が大きく飛ぶ」がこの形)。
    """
    frame, messages = _warnings_for(_missing({(z, 2) for z in range(1, 6)}),
                                    max_t=3, max_z=5)

    assert frame.t_keep == [1, 3]
    assert frame.ragged == []                  # 既存の警告では拾えない
    assert any("t=[2]" in m and "not necessarily the n-th" in m for m in messages), messages


# ---- 正常系では鳴らない (鳴ると本物の警告が読まれなくなる) -----------------------

def test_a_run_that_stopped_mid_stack_does_not_get_the_new_warning():
    """末尾が切れただけなら、従来の ragged の警告だけが出ること。

    取得を止めれば必ず後ろが空くので、ここで鳴らすと毎回鳴る警告になる。
    """
    frame, messages = _warnings_for(_missing({(z, 3) for z in range(3, 6)}),
                                    max_t=3, max_z=5)

    assert frame.z_keep == [1, 2, 3, 4, 5]
    assert any("stopped mid-stack" in m for m in messages)
    assert not any("no longer uniform" in m for m in messages), messages


def test_a_complete_acquisition_is_silent():
    _frame, messages = _warnings_for(_missing(set()), max_t=3, max_z=5)
    assert messages == []


def test_a_plain_timelapse_is_silent():
    """z が 1 面だけの連続取得で鳴らないこと (穴の判定は 2 点以上で意味を持つ)。"""
    files = ["ChanA_001_001_001_%03d.tif" % t for t in range(1, 101)]
    _frame, messages = _warnings_for(files, max_t=3000, max_z=61)
    assert messages == []


def test_only_the_inside_of_the_range_counts_as_a_hole():
    """穴の判定は端を数えない。"""
    assert _interior_gaps([1, 2, 4, 5]) == [3]
    assert _interior_gaps([1, 3, 5]) == [2, 4]
    assert _interior_gaps([1, 2, 3]) == []
    assert _interior_gaps([5, 6, 7]) == []      # 1..4 が無くても端は端
    assert _interior_gaps([1]) == []
    assert _interior_gaps([]) == []


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
    tifffile.imwrite(d / "ChanA_reference.tif",     # 連番の無い紛れ込み
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


# ---- ThorImage のプレビュー ---------------------------------------------------

@pytest.mark.parametrize("name", [
    "ChanA_Preview.tif", "ChanB_Preview.tif", "Preview.tif",
    "Image_ChanA_Preview.tif",
])
def test_previews_are_dropped_without_a_warning(tmp_path, capsys, name):
    """プレビューは **どの取得にも必ずある** ので、警告ではなく DEBUG で落とす。

    ThorImage は表示用に ``ChanA_Preview.tif`` を書き出す。面ではないので落とすのは
    正しいが、これを「連番の読めない不明なファイル」として警告すると毎回必ず鳴り、
    本当に見てほしい警告まで読み飛ばされるようになる。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for t in range(1, 5):
        _write(d, "ChanA", 1, t, t)
    tifffile.imwrite(d / name, np.zeros((8, 8), dtype=np.uint16))
    files = sorted(str(p) for p in d.glob("*.tif"))

    with warnings_as_errors():                  # 警告が1つでも出たら失敗する
        stacked, used = stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", _params("Z", size_z=1, size_t=4), min_kb=0)

    assert stacked.shape == (4, 1, 1, 8, 8)
    assert np.asarray(stacked)[:, 0, 0, 0, 0].tolist() == [1, 2, 3, 4]
    assert not any(Path(f).name == name for f in used)   # 使ったファイルにも残らない

    out = capsys.readouterr().out
    assert f"Skipped 1 ThorImage preview file(s) ({name})" in out
    assert "filled 4/4 file(s)" in out          # 枠の勘定にも入らない


def test_a_preview_does_not_get_probed_as_an_odd_sized_file(tmp_path, monkeypatch):
    """プレビューはヘッダの抜き取り検査に当たらない。

    プレビューは名前の並びで最後に来るので、以前は :func:`_page_counts` が
    「末尾のファイル」として必ずヘッダを読んでいた。面数もサイズも他と違うため
    「面数が食い違う」と判定され、サイズの分からないファイルが1つでもあれば
    全件のヘッダを読みに行っていた (数千往復)。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for t in range(1, 6):
        _write(d, "ChanA", 1, t, t)
    tifffile.imwrite(d / "ChanA_Preview.tif",   # 面数もサイズも他と違う
                     np.zeros((6, 8, 8), dtype=np.uint16), photometric="minisblack")
    files = sorted(str(p) for p in d.glob("*.tif"))

    opened = []
    real_tifffile = tifffile.TiffFile

    def _spy(path, *a, **kw):
        opened.append(os.path.basename(str(path)))
        return real_tifffile(path, *a, **kw)

    monkeypatch.setattr(tifffile, "TiffFile", _spy)

    stacked, _ = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml", _params("Z", size_z=1, size_t=5),
        min_kb=0, sizes={})              # サイズ不明: 全件読みの経路に入りうる

    assert stacked.shape == (5, 1, 1, 8, 8)
    assert "ChanA_Preview.tif" not in opened


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


# ---- 面数の違うファイルが1枚混ざる場合 ----------------------------------------

def test_a_stray_multipage_file_is_cut_at_build_time_not_at_compute_time(tmp_path):
    """時点ごとの面数が揃わないものは、組み立ての時点で落とす。

    回帰: 3001 枚の XYT 取得に 004 だけ 6000 ページのファイルが混ざっていた
    (取り違えて置かれた別物とみられる)。先頭と末尾しか見ていなかったので気付かず、
    グラフは 1 ページ前提で組まれ、267 秒走ったあとの compute 時にようやく
    ``Page count mismatch`` で落ちていた。

    面数はファイルのヘッダにしか無いので、全件のヘッダを読んで確かめる。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for t in range(1, 9):
        _write(d, "ChanA", 1, t, t)
    # 1枚だけ大量ページ (他は 8x8 の1ページ)
    tifffile.imwrite(d / "ChanA_001_001_001_004.tif",
                     np.zeros((60, 8, 8), dtype=np.uint16),
                     photometric="minisblack")
    files = sorted(str(p) for p in d.glob("*.tif"))
    sizes = {f: os.path.getsize(f) for f in files}

    with pytest.warns(UserWarning, match="do not hold 1 plane"):
        stacked, _ = stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", _params("T", size_z=1, size_t=8),
            min_kb=0, sizes=sizes)

    # 混入した時点だけ落ち、残りはそのまま積める
    assert stacked.shape == (7, 1, 1, 8, 8)
    # 画素まで通しても落ちない (組み立ての時点で除いてある)
    assert np.asarray(stacked)[:, 0, 0, 0, 0].tolist() == [1, 2, 3, 5, 6, 7, 8]


def test_a_stray_file_of_the_same_size_is_still_found(tmp_path):
    """**サイズが他と変わらなくても** 面数の違うファイルを見つけること。

    回帰: 以前はファイルサイズで「怪しいファイル」を絞り込み、そこだけヘッダを
    読んでいた。「生の ThorLabs TIFF は無圧縮だからサイズは面数に比例する」という
    前提だったが、実データで破れた。3001 枚のうち 004 だけが 6000 面なのに
    サイズは他と変わらず (圧縮された別物が置かれていたとみられる)、素通りして
    compute 時に ``Page count mismatch`` で落ちていた。

    サイズが当てにならない以上、サイズで絞り込む限りこの取りこぼしは無くならない。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for t in range(1, 21):
        _write(d, "ChanA", 1, t, t)
    odd = d / "ChanA_001_001_001_007.tif"
    tifffile.imwrite(odd, np.zeros((60, 8, 8), dtype=np.uint16),
                     photometric="minisblack")
    files = sorted(str(p) for p in d.glob("*.tif"))
    # サイズは全件同じ、と呼び出し側が伝えてくる状況。実データではファイルが
    # 圧縮されていてこうなった。サイズからは何の手がかりも得られない。
    sizes = {f: 1000 for f in files}

    with pytest.warns(UserWarning, match="do not hold 1 plane"):
        stacked, _ = stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", _params("T", size_z=1, size_t=20),
            min_kb=0, sizes=sizes)

    assert stacked.shape == (19, 1, 1, 8, 8)        # 混入した時点だけ落ちる
    assert 7 not in np.asarray(stacked)[:, 0, 0, 0, 0].tolist()


def test_every_header_is_read_exactly_once(tmp_path):
    """ヘッダは全件読むが、1ファイルにつき1回だけ。

    面数はヘッダにしか無いので全件読むのは避けられない。避けられるのは
    **同じファイルを何度も読むこと** で、SMB 越しではそれがそのまま往復になる。
    """
    import ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder as m

    d = tmp_path / "img01"
    d.mkdir()
    for t in range(1, 21):
        _write(d, "ChanA", 1, t, t)
    files = sorted(str(p) for p in d.glob("*.tif"))
    sizes = {f: os.path.getsize(f) for f in files}

    probed = []
    real = m.probe_plane_layout
    m.probe_plane_layout = lambda p: (probed.append(str(p)), real(p))[1]
    try:
        stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", _params("T", size_z=1, size_t=20),
            min_kb=0, sizes=sizes)
    finally:
        m.probe_plane_layout = real

    # 縦横/画素型を決める1枚 (先頭) だけが2回。それ以外は1回ずつ。
    assert sorted(set(probed)) == sorted(files)
    assert len(probed) == len(files) + 1, probed


def test_dropping_a_timepoint_from_one_channel_does_not_shift_the_other(tmp_path):
    """片方のチャンネルから時点が落ちても、チャンネル同士を時間でずらさない。

    回帰: 落ちたあとは「時点数の少ない方に合わせて先頭から切る」実装だった。
    ChanA から t=4 が落ちた状態で両方を先頭 7 枚に切ると、ChanA の 4 枚目は t=5、
    ChanB の 4 枚目は t=4 になり、**チャンネルが 1 フレームずれたまま** 以降の
    位置合わせと解析に流れる。2 チャンネル同時取得ではこれが黙って起きると
    (蛍光指示薬と構造マーカーの対応が壊れるので) 結果が読めなくなる。

    揃えるのは枚数ではなく時点そのもの。全チャンネルに揃っている時点だけを残す。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for ch in ("ChanA", "ChanB"):
        for t in range(1, 9):
            tifffile.imwrite(d / f"{ch}_001_001_001_{t:03d}.tif",
                             np.full((4, 4), t, np.uint16))
    # ChanA の t=4 だけ面数が違う (混入)。ChanB の t=4 は正常。
    tifffile.imwrite(d / "ChanA_001_001_001_004.tif",
                     np.zeros((60, 4, 4), np.uint16), photometric="minisblack")
    files = sorted(str(p) for p in d.glob("*.tif"))
    params = dict(_params("T", size_z=1, size_t=8), SizeY=4, SizeX=4)

    with pytest.warns(UserWarning, match="do not hold the same timepoints"):
        stacked, _ = stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", params, min_kb=0)

    arr = np.asarray(stacked)
    # 画素値が時点番号なので、両チャンネルが同じ時点を指しているか直接見える
    assert arr[:, 0, 0, 0, 0].tolist() == [1, 2, 3, 5, 6, 7, 8]
    assert arr[:, 1, 0, 0, 0].tolist() == [1, 2, 3, 5, 6, 7, 8]

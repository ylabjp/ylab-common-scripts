"""取り込み後の検証が「実際に失敗しうること」だけを見ていることのテスト。

以前は 7 項目を表で出していたが、検証として働いていたのは 2 つだけだった。

- ``PixelSizeX`` / ``PixelSizeZ`` は ``image_meta.pixel_size`` が XML の params から
  そのまま作られているため、XML を XML と比べていた。``Δ=0.000000`` 以外に
  なりようがなく、構造的に必ず PASS する。
- ``SizeX`` / ``SizeY`` / ``SizeZ`` は本物の比較だが、食い違いは取り込み側が
  「実データを採用した」と添えて既に警告している。ここで再掲しても情報は増えず、
  ``Final Status: NOT VALIDATED`` という強い語だけが残る。

残したのは他のどこでも見ていない 2 つ (チャンネル数 / XML より多い T)。
このファイルは「その 2 つが本当に鳴ること」と「正常系では黙ること」を固定する。
必ず PASS する項目を足し戻すと、テストは通るが検証は死ぬ —— それを防ぐために
「必ず通る項目が無いこと」も直接確かめる。
"""
from __future__ import annotations

import warnings
from types import SimpleNamespace

import pytest

from ylabcommon.bioio.thorlab.builder import ThorlabBioioBuilder


def _builder():
    """検証だけを呼びたいので __init__ を通さずに作る。"""
    return ThorlabBioioBuilder.__new__(ThorlabBioioBuilder)


def _image(size_c=2, size_t=10, size_z=5, size_x=512, size_y=512):
    return SimpleNamespace(size_c=size_c, size_t=size_t, size_z=size_z,
                           size_x=size_x, size_y=size_y,
                           pixel_size=(0.5, 0.17, 0.17))


def _xml(channels=("ChanA", "ChanB"), size_t=10, size_z=5,
         size_x=512, size_y=512):
    return {"Channels": list(channels), "SizeT": size_t, "SizeZ": size_z,
            "SizeX": size_x, "SizeY": size_y,
            "PixelSizeX": 0.17, "PixelSizeY": 0.17, "PixelSizeZ": 0.5}


# ---- 正常系は黙る ------------------------------------------------------------

def test_a_matching_stack_reports_nothing(recwarn):
    assert _builder()._validate_thorlab_stack(_xml(), _image()) == []
    assert list(recwarn) == []


def test_no_xml_means_nothing_to_check(recwarn):
    assert _builder()._validate_thorlab_stack(None, _image()) == []
    assert list(recwarn) == []


# ---- チャンネル数 ------------------------------------------------------------

def test_a_channel_count_mismatch_is_reported():
    """XML の Wavelength 数と実際のチャンネル数が違えば鳴る。

    ここが黙ると、2 波長で撮ったのに 1 チャンネルしか拾えていない取り込みが
    そのまま解析へ流れる。
    """
    with pytest.warns(UserWarning, match="Channels"):
        problems = _builder()._validate_thorlab_stack(
            _xml(channels=("ChanA", "ChanB")), _image(size_c=1))

    assert len(problems) == 1
    assert "2 wavelength" in problems[0] and "1 channel" in problems[0]


def test_an_extra_channel_is_reported_too():
    """多い側も鳴る (ファイル名の解析が余計なチャンネルを作っていないか)。"""
    with pytest.warns(UserWarning, match="Channels"):
        problems = _builder()._validate_thorlab_stack(
            _xml(channels=("ChanA",)), _image(size_c=3))
    assert len(problems) == 1


# ---- 時点数 ------------------------------------------------------------------

def test_more_timepoints_than_the_xml_is_reported():
    """XML より **多い** T は取得の打ち切りでは説明がつかない。"""
    with pytest.warns(UserWarning, match="SizeT"):
        problems = _builder()._validate_thorlab_stack(
            _xml(size_t=10), _image(size_t=12))
    assert len(problems) == 1
    assert problems[0].startswith("SizeT")


def test_fewer_timepoints_warns_but_is_not_a_problem():
    """短い T は打ち切りとして許容する (警告は出すが problems には入れない)。"""
    with pytest.warns(UserWarning, match="途中で終了"):
        problems = _builder()._validate_thorlab_stack(
            _xml(size_t=100), _image(size_t=60))
    assert problems == []


# ---- 死んだ検証を足し戻させない ----------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("size_z", 3001),      # Z スタック設定のまま T 連続撮影した取得
    ("size_x", 256),
    ("size_y", 256),
])
def test_sizes_are_left_to_the_stack_builder(field, value):
    """XML と実データの大きさの食い違いは、ここでは報告しない。

    取り込み側が「実データを採用した」と添えて既に警告しており、ここで再掲すると
    正常な取得でも毎回 NOT VALIDATED になる。二重報告をやめたことを固定する。
    """
    image = _image(**{field: value})
    with warnings.catch_warnings():
        warnings.simplefilter("error")          # 余計な警告が出たら失敗
        assert _builder()._validate_thorlab_stack(_xml(), image) == []


def test_pixel_size_is_not_compared_against_itself():
    """画素サイズの比較を足し戻していないこと。

    ``image_meta.pixel_size`` は XML の params から作られるので、XML と比べても
    必ず一致する。「必ず PASS する検証」は、通っているという誤った安心だけを生む。
    """
    image = _image()
    image.pixel_size = (999.0, 999.0, 999.0)    # 現実にはあり得ない値
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        # 画素サイズを見ていないので、何も言わない (見ていたらここで鳴ってしまう)
        assert _builder()._validate_thorlab_stack(_xml(), image) == []

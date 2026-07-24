"""ThorLabs 取り込みで、XML 指定より短い T(タイムラプス取得の途中終了)を
Warning を出しつつ OK 扱いにする _check_size_t_tolerant のテスト。

- image_t < xml_t : 取得打ち切りとみなして許容(Warning + OK)
- image_t == xml_t: そのまま OK(Warning なし)
- image_t > xml_t : XML と構造が一致しない異常として NG
"""
import warnings

import pytest

from ylabcommon.bioio.thorlab.builder import _check_size_t_tolerant


def test_short_t_is_tolerated_with_warning():
    ok, warn, detail = _check_size_t_tolerant(xml_size_t=100, image_size_t=60)
    assert ok is True                      # 検証は通す
    assert warn is not None                # Warning 文言が返る
    assert "60" in warn and "100" in warn  # 実T と XML SizeT を明示
    assert "tolerated" in detail.lower()


def test_exact_t_passes_without_warning():
    ok, warn, detail = _check_size_t_tolerant(100, 100)
    assert ok is True
    assert warn is None


def test_more_timepoints_than_xml_is_rejected():
    ok, warn, detail = _check_size_t_tolerant(100, 120)
    assert ok is False                     # 多い T は構造不一致として NG
    assert warn is None
    assert "more timepoints" in detail.lower()


@pytest.mark.parametrize("xml_t, img_t", [(None, 10), (100, None), (None, None)])
def test_missing_sizes_skip_the_check(xml_t, img_t):
    ok, warn, detail = _check_size_t_tolerant(xml_t, img_t)
    assert ok is True
    assert warn is None


def test_warning_text_is_emittable_as_userwarning():
    # _validate_thorlab_stack はこの文言を warnings.warn で出す
    _ok, warn, _detail = _check_size_t_tolerant(50, 10)
    with pytest.warns(UserWarning, match="SizeT"):
        warnings.warn(warn)

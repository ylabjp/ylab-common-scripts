"""未定義名(NameError)で壊れていた経路の回帰テスト。

いずれも pyflakes の "undefined name" で検出されたもの。どちらも
「エラーを知らせるはずの経路」や「呼べば必ず落ちる関数」で、
静かに壊れているのではなく **使った瞬間に別の例外になる** 種類の欠陥。
"""
from pathlib import Path

import pytest

from ylabcommon.analysis import crawler
from ylabcommon.utils.report_builder import ReportBuilder


def _preprocess_cell():
    """raw cell の preprocess_dir 関数を LevelSpec 経由で取り出す。

    定義元 __make_raw_cell_spec は module private。モジュール直下の `__` 名は
    マングリングされないので getattr で引ける(クラス body 内で書くと
    マングリングされるため、この helper は必ずモジュール直下に置く)。
    """
    return getattr(crawler, "__make_raw_cell_spec")().preprocess_dir


class TestCrawlerInvalidCellDirName:
    """不正な cell ディレクトリ名で、NameError ではなく理由の分かる ValueError を出す。

    以前は `"Invalid directory name: %s" % sess_name` と書かれていて sess_name が
    未定義だったため、**名前が不正なときにだけ** NameError になっていた。
    メッセージが一番必要な場面で、メッセージごと壊れていた。
    """

    def test_raises_value_error_not_name_error(self, tmp_path):
        bad = tmp_path / "XYT_Cell01"          # "_" 区切りが3つ未満
        bad.mkdir()
        with pytest.raises(ValueError) as ei:
            _preprocess_cell()(bad)
        assert not isinstance(ei.value, NameError)

    def test_message_names_the_offending_directory(self, tmp_path):
        bad = tmp_path / "XYT_Cell01"
        bad.mkdir()
        with pytest.raises(ValueError) as ei:
            _preprocess_cell()(bad)
        # どのディレクトリが悪いか分からないと直しようがない
        assert "XYT_Cell01" in str(ei.value)

    def test_valid_name_passes_through(self, tmp_path):
        ok = tmp_path / "XYT_Cell01_01_ACSF"
        ok.mkdir()
        assert _preprocess_cell()(ok) == ok

    def test_bad_image_type_still_reports_the_type(self, tmp_path):
        bad = tmp_path / "ABC_Cell01_01"
        bad.mkdir()
        with pytest.raises(ValueError, match="Image type error: ABC"):
            _preprocess_cell()(bad)


class TestReportBuilderCompactSmallLists:
    """compact_small_lists は `re` を使うのに import されておらず、呼べば NameError だった。"""

    def test_compacts_two_element_int_lists(self):
        out = ReportBuilder().compact_small_lists('{"roi": [\n    10,\n    20\n  ]}')
        assert out == '{"roi": [10, 20]}'

    def test_leaves_other_lists_alone(self):
        text = '{"roi": [\n    10,\n    20,\n    30\n  ]}'
        assert ReportBuilder().compact_small_lists(text) == text

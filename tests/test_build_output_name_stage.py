"""``build_output_name`` が名前を組むところ。

この関数はいま **どこからも呼ばれていない** (参照は
``thorlab_bioio_stack_builder`` の docstring だけ)。それでも読める状態にして
おくのは、名前の付け方の決まりがここに書かれているためである。

直したのは 2 つ:

1. 裸の ``except:`` —— ``KeyboardInterrupt`` や ``SystemExit`` まで飲むので、
   取り込みの途中で Ctrl-C が効かなくなる。
2. ``stageX`` / ``stageY`` に **ループを抜けたあとの残り物** (最後に見た
   ファイルの値) を使っていた。DataFrame から取り直す行はコメントアウトされた
   ブロックの中にあり、動いていない。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ylabcommon.utils.outfile_name import build_output_name

THORLABS = ["ChanA_012_034_001_001.tif",
            "ChanA_012_034_002_001.tif",
            "ChanA_012_034_003_001.tif"]


def test_the_stage_position_comes_from_the_file_names(tmp_path):
    got = build_output_name(tmp_path, THORLABS, Z_stack_val=3, T_stack_val=1)

    assert got.name == "Output_ChanA_X012_Y034_Z001_to_3_stack_T001", got.name


def test_a_trailing_unparsable_name_does_not_break_the_output(tmp_path):
    """回帰: 最後の 1 本が読めないと None が f-string へ渡り TypeError だった。

    ``stageX`` はループ変数の残り物だったので、**並びの最後** が読めるかどうかで
    結果が変わっていた。
    """
    files = THORLABS + ["Preview.tif"]          # 連番を持たない名前

    got = build_output_name(tmp_path, files, Z_stack_val=3, T_stack_val=1)

    assert "X012_Y034" in got.name, got.name


def test_a_leading_unparsable_name_gives_the_same_answer(tmp_path):
    """負のコントロール: 読めない名前の位置で結果が変わらないこと。"""
    first = build_output_name(tmp_path, ["Preview.tif"] + THORLABS, 3, 1)
    last = build_output_name(tmp_path, THORLABS + ["Preview.tif"], 3, 1)

    assert first.name == last.name


def test_no_readable_position_is_reported_not_a_type_error(tmp_path):
    """どれも読めないときは、何を期待しているのかを言って止まること。"""
    with pytest.raises(RuntimeError, match="stage position"):
        build_output_name(tmp_path, ["ChanA_preview.tif", "ChanA_other.tif"], 1, 1)


def test_the_name_parse_names_the_exceptions_it_expects():
    """回帰: 裸の ``except:`` が ``KeyboardInterrupt`` まで飲んでいた。

    取り込みの途中で Ctrl-C が効かなくなる。ここで起きうるのは、要素が
    足りない (IndexError) か数として読めない (ValueError) かの 2 つだけ。

    実際に ``KeyboardInterrupt`` を起こさせるには組み込みの ``int`` を
    差し替えるしかなく、それは pytest 自身まで壊す (試して壊した)。
    例外の型を書いているかどうかを、構文として見る。
    """
    import ast
    import inspect
    import textwrap

    from ylabcommon.utils import outfile_name

    src = textwrap.dedent(inspect.getsource(outfile_name.build_output_name))
    handlers = [h for h in ast.walk(ast.parse(src))
                if isinstance(h, ast.ExceptHandler)]
    assert handlers, "except が無い (前提が変わった)"
    for h in handlers:
        assert h.type is not None, "裸の except: が残っている"
        named = ast.unparse(h.type)
        assert "IndexError" in named and "ValueError" in named, named
        assert "BaseException" not in named and "Exception" not in named, named


def test_an_empty_list_says_so(tmp_path):
    with pytest.raises(RuntimeError, match="empty"):
        build_output_name(tmp_path, [], 1, 1)


def test_names_without_a_z_do_not_crash_on_nan(tmp_path):
    """回帰: z がどこからも読めないと ``int(NaN)`` で落ちていた。

    条件が「列があるか」だったので、全部 NaN の列でも真になっていた。
    すぐ下の ``t_start`` は最初から ``dropna()`` を見ており、そちらが正しい形。
    """
    # 位置は読めるが z が数でない名前
    files = ["ChanA_012_034_xxx_001.tif", "ChanA_012_034_yyy_001.tif"]

    got = build_output_name(tmp_path, files, Z_stack_val=1, T_stack_val=1)

    assert "X012_Y034" in got.name, got.name
    assert "Z001" in got.name, got.name       # 読めなければ 1 に倒す


def test_a_readable_z_is_still_used(tmp_path):
    """負のコントロール: 読めるときはその値を使うこと。"""
    got = build_output_name(tmp_path, ["ChanA_012_034_007_001.tif"],
                            Z_stack_val=1, T_stack_val=1)

    assert "Z007" in got.name, got.name

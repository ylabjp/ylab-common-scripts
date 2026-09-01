"""出荷するコードに型注釈が付いていること。

なぜ要るか
----------
注釈は **誰も確かめないとただのコメント** になり、やがて実物と食い違う。
ylabcommon は slice-analysis / slice-controller / behavior-* から共有で使われる
ので、ここの関数が「何を受けて何を返すか」がずれると、下流の全部に効く。
確認は 2 段構えにしてある:

1. ``uv run mypy`` —— 注釈どうしの整合と、注釈の付いた関数の中身を見る。
   設定は pyproject.toml の ``[tool.mypy]``。
2. このファイル —— **付いているかどうか** を数える。mypy は「注釈が無い」
   ことは責めない (既定では無注釈の関数の中身すら見ない) ので、放っておくと
   新しいコードだけ素通りして、割合が静かに下がる。

いま ``src/ylabcommon`` は 100%。下げないための下限をここに書いておく。
"""
from __future__ import annotations

import ast
import os
import pathlib
import tomllib

import pytest

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG = ROOT / "src" / "ylabcommon"


def _slots(path: pathlib.Path):
    """``(引数 + 返り値, 注釈済み)`` を数える。``self`` / ``cls`` は数えない。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    total = done = 0
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = node.args
        params = [x for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
                  if x.arg not in ("self", "cls")]
        if a.vararg:
            params.append(a.vararg)
        if a.kwarg:
            params.append(a.kwarg)
        total += len(params) + 1                       # + 返り値
        done += sum(1 for x in params if x.annotation is not None)
        done += 1 if node.returns is not None else 0
        gaps = [x.arg for x in params if x.annotation is None]
        if node.returns is None:
            gaps.append("<return>")
        if gaps:
            missing.append("%s:%d %s -> %s" % (path.name, node.lineno, node.name,
                                               ", ".join(gaps)))
    return total, done, missing


def _all_files():
    return sorted(PKG.rglob("*.py"))


def test_there_are_files_to_check():
    """対象が 0 件なら、このファイルは何も守っていない。"""
    assert len(_all_files()) >= 50


def test_every_function_is_annotated():
    """引数と返り値に注釈があること。

    落ちたら **その関数に付ける** のが直し方。ここの下限を下げるのではない。
    """
    total = done = 0
    missing: list[str] = []
    for path in _all_files():
        t, d, m = _slots(path)
        total += t
        done += d
        missing.extend(m)

    assert total > 900, total
    assert not missing, (
        "%d 個の引数/返り値に型注釈が無い (全 %d 個中 %d 個が注釈済み):\n  %s"
        % (total - done, total, done, "\n  ".join(missing[:40])))


def test_the_type_checker_is_configured():
    """``uv run mypy`` が「出荷するパッケージ全体を、pydantic 込みで」見ること。

    設定が消えると、上の数だけ増えて **中身は誰も見ていない** 状態になる。
    """
    with open(ROOT / "pyproject.toml", "rb") as f:
        cfg = tomllib.load(f)

    mypy = cfg["tool"]["mypy"]
    assert mypy["files"] == ["src/ylabcommon"]
    assert "pydantic.mypy" in mypy["plugins"]
    # src はパッケージではなく置き場。これが無いと module 名が src.* になる。
    assert mypy["mypy_path"] == "src"
    assert mypy["explicit_package_bases"] is True

    dev = cfg["dependency-groups"]["dev"]
    assert any(d.split(">")[0].split("=")[0].strip() == "mypy" for d in dev), dev


def test_nothing_is_excused_from_the_checker():
    """検査を緩める ``overrides`` が増えていないこと。

    ここが増えると、緩めた範囲で本物の指摘まで消える。必要になったときは
    **どのファイルの、どの指摘を、なぜ** 消すのかを添えて足すこと。
    """
    with open(ROOT / "pyproject.toml", "rb") as f:
        cfg = tomllib.load(f)
    assert cfg["tool"]["mypy"].get("overrides", []) == []


@pytest.mark.parametrize("path", _all_files(), ids=lambda p: p.name)
def test_each_file_still_compiles(path):
    """注釈を足したときにファイルを壊していないこと。

    ``ast.parse`` では足りない —— ``from __future__ import annotations`` が
    先頭でないといった規則は **compile まで行かないと出ない** (実際、
    import を 1 行入れる位置を間違えてこれを踏んだ)。字下げが落ちて
    メソッドがモジュール直下へ出る事故は下の
    ``test_methods_stay_in_their_class`` が見る。
    """
    compile(path.read_text(encoding="utf-8"), str(path), "exec")


@pytest.mark.parametrize("path", _all_files(), ids=lambda p: p.name)
def test_methods_stay_in_their_class(path):
    """``self`` を第 1 引数に取る関数が、クラスの外に出ていないこと。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    in_class = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    in_class.add(child.lineno)

    stray = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args.posonlyargs + node.args.args
        if args and args[0].arg == "self" and node.lineno not in in_class:
            stray.append("%s:%d %s" % (path.name, node.lineno, node.name))
    assert not stray, stray

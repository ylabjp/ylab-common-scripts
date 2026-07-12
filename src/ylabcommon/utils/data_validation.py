"""処理の前提とするデータ構造を「事前に」検証するための共通ヘルパー。

解析コードは h5 のグループ/データセット構成や DataFrame の列, config dict の
キーなど、多くの構造的前提の上に成り立っている。前提が崩れた入力では、これまで
処理の深い階層で ``KeyError: 'param_task'`` のような文脈の乏しい例外が出ていた。
どのファイルの・どの構造が・どう欠けているのかが分からず、Better Stack のログを
見ても原因追跡が難しかった。

このモジュールは前提チェックを処理の入口に集約し、欠落を具体的なメッセージ付きの
``DataStructureError`` として送出する。call site は既存の解析パイプラインの
try/except (``on_file`` など) に包まれているため、送出された ``DataStructureError``
はそのまま Better Stack へ転送される。

- 特定リポジトリ (behavior-analysis 等) に依存しない汎用実装。h5py / pandas を
  直接 import せず、``__contains__`` / ``columns`` を持つオブジェクトを鴨型で受ける。
- ``DataStructureError`` は ``ValueError`` のサブクラス。既存の ``except ValueError`` /
  ``except Exception`` をそのまま活かせ、「不正なデータ」という意味も保たれる。
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence


class DataStructureError(ValueError):
    """処理が前提とするデータ構造が満たされていないことを表す例外。

    ``ValueError`` を継承しているため、既存の広い except 節でも捕捉され、
    解析パイプライン経由で Better Stack に送信される。
    """


def _fmt_context(context: str | None) -> str:
    return f" ({context})" if context else ""


def require_h5_paths(
    h5obj: Any,
    paths: Sequence[str],
    *,
    context: str | None = None,
) -> None:
    """h5 (file-like) が指定パスをすべて含むことを検証する。

    Parameters
    ----------
    h5obj:
        ``"a/b/c" in h5obj`` で存在確認できるオブジェクト (h5py.File/Group 等)。
    paths:
        必須の group/dataset パス。``"param_task/general/daq_block_length_in_ms"``
        のようなネストパスも可。
    context:
        エラーメッセージに添えるファイル名など。原因追跡を容易にする。

    Raises
    ------
    DataStructureError
        1 つでも欠けているパスがあれば、欠落パスを列挙して送出する。
    """
    missing = [p for p in paths if p not in h5obj]
    if missing:
        raise DataStructureError(
            "Required h5 path(s) not found%s: %s"
            % (_fmt_context(context), ", ".join(repr(p) for p in missing))
        )


def require_h5_path(h5obj: Any, path: str, *, context: str | None = None) -> None:
    """h5 (file-like) が単一パスを含むことを検証する。"""
    require_h5_paths(h5obj, [path], context=context)


def require_columns(
    df: Any,
    columns: Iterable[str],
    *,
    context: str | None = None,
) -> None:
    """DataFrame が指定列をすべて持つことを検証する。

    ``df.columns`` を参照できれば MultiIndex でない通常の DataFrame を想定。
    """
    existing = set(df.columns)
    missing = [c for c in columns if c not in existing]
    if missing:
        raise DataStructureError(
            "Required column(s) not found%s: %s. Available columns: %s"
            % (
                _fmt_context(context),
                ", ".join(repr(c) for c in missing),
                ", ".join(repr(c) for c in df.columns),
            )
        )


def require_keys(
    mapping: Any,
    keys: Iterable[str],
    *,
    context: str | None = None,
) -> None:
    """dict / config オブジェクトが指定キーをすべて持つことを検証する。"""
    missing = [k for k in keys if k not in mapping]
    if missing:
        raise DataStructureError(
            "Required key(s) not found%s: %s"
            % (_fmt_context(context), ", ".join(repr(k) for k in missing))
        )

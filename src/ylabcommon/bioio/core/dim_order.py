"""画像の正準レイアウト: **CZYX**。

方針
----
ラボの画像は原則 ``CZYX`` で持つ。標準の軸順 (``TCZYX``) から **T だけを外した
もの**で、``C`` と ``Z`` は**大きさが 1 でも必ず置く**。

なぜ「1 でも置く」のか。潰すと、軸の並びが取得内容によって変わる:

    1 チャネル・1 面      -> YX
    1 チャネル・Z スタック -> ZYX
    複数チャネル・1 面     -> CYX
    複数チャネル・Z スタック -> ZCYX

読む側はこの 4 通りを **全部書き分けることになり、書き漏らした並びが来ると落ちる**。
実際 histology の plot_image_n_histogram はこの 4 つで分岐していて、それ以外は
``Unsupported axes format`` で止まる。軸を常に置けば分岐は要らなくなり、
``img[c]`` は必ず ``ZYX`` になる。

T について
----------
T は**落とす**。ただし大きさが 1 のときだけ —— 実体のある時系列 (T>1) を黙って
削ると画素が消え、消えたことは出力から分からない。T>1 は方針の対象外なので、
黙って切らずに **断る**。
"""
from __future__ import annotations

from typing import Any

import numpy as np

#: 正準の軸順。
CANONICAL_DIM_ORDER = "CZYX"

#: 受け付ける軸の文字。これ以外 (tifffile が未知の軸に使う ``Q``、RGB の ``S`` など)
#: は、どう畳むべきか決められないので断る。
_KNOWN_AXES = frozenset("TCZYX")


def to_czyx(data: Any, axes: str) -> Any:
    """``data`` を ``CZYX`` にする。足りない ``C`` / ``Z`` は大きさ 1 で入れる。

    Args:
        data: 配列。``axes`` と次元数が一致していること。
        axes: ``data`` の軸を表す文字列 (例 ``"ZCYX"``、``"YX"``)。
            tifffile なら ``TiffFile.series[0].axes`` がこれ。

    Returns:
        ``CZYX`` の 4 次元配列。**画素は写しではなく view** になりうる。

    Raises:
        ValueError: 軸と次元数が食い違う、``Y`` か ``X`` が無い、知らない軸が
            ある、または ``T`` の大きさが 1 でないとき。
    """
    axes = axes.upper()
    array = np.asarray(data) if not hasattr(data, "ndim") else data

    if array.ndim != len(axes):
        raise ValueError(
            "axes %r describes %d dimensions but the array has %d (shape %s)."
            % (axes, len(axes), array.ndim, tuple(array.shape))
        )
    unknown = sorted(set(axes) - _KNOWN_AXES)
    if unknown:
        raise ValueError(
            "Cannot place %s into %s: only %s are known. tifffile uses 'Q' for an "
            "axis it could not name and 'S' for samples (RGB); such a file has to "
            "be resolved before it can be given a canonical layout."
            % (", ".join(repr(a) for a in unknown), CANONICAL_DIM_ORDER,
               ", ".join(sorted(_KNOWN_AXES)))
        )
    if len(set(axes)) != len(axes):
        raise ValueError("axes %r repeats an axis." % axes)
    for required in ("Y", "X"):
        if required not in axes:
            raise ValueError("axes %r has no %s; an image needs both Y and X."
                             % (axes, required))

    # --- T: 1 なら落とす。実体があるなら断る (黙って切ると画素が消える) ---
    if "T" in axes:
        size_t = array.shape[axes.index("T")]
        if size_t != 1:
            raise ValueError(
                "This image has T=%d. The canonical layout is %s, which has no "
                "time axis, and dropping a real time series would silently "
                "discard %d of its %d time points. Handle T explicitly instead."
                % (size_t, CANONICAL_DIM_ORDER, size_t - 1, size_t)
            )
        array = array[(slice(None),) * axes.index("T") + (0,)]
        axes = axes.replace("T", "")

    # --- 足りない軸を大きさ 1 で入れる ---
    for axis in ("C", "Z"):
        if axis not in axes:
            array = array[np.newaxis]
            axes = axis + axes

    # --- 正準の順へ ---
    order = [axes.index(axis) for axis in CANONICAL_DIM_ORDER]
    return array.transpose(order)


def channel_plane(data: Any, axes: str, channel: int) -> Any:
    """チャネル ``channel`` の 2 次元面。Z があれば平均で潰す。

    ``to_czyx`` の上の薄い便宜。Z=1 のときの平均は値を変えない (要素が 1 つ)
    ので、Z の有無で場合分けせずに同じ式で書ける。ただし ``mean`` は float を
    返すので、**整数のまま欲しい呼び出し側はここを使わないこと**。
    """
    return to_czyx(data, axes)[channel].mean(axis=0)

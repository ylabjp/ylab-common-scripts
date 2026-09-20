"""画像の正準レイアウト: **TCZYX**。

方針
----
ラボの画像は原則 ``TCZYX`` で持つ。**5 軸すべてを、大きさが 1 でも必ず置く**。

なぜ「1 でも置く」のか。潰すと、軸の並びが取得内容によって変わる:

    1 チャネル・1 面      -> YX
    1 チャネル・Z スタック -> ZYX
    複数チャネル・1 面     -> CYX
    複数チャネル・Z スタック -> ZCYX

読む側はこの 4 通りを **全部書き分けることになり、書き漏らした並びが来ると落ちる**。
実際 histology の plot_image_n_histogram はこの 4 つで分岐していて、それ以外は
``Unsupported axes format`` で止まる。軸を常に置けば分岐は要らなくなり、
``img[t, c]`` は必ず ``ZYX`` になる。

なぜ T を落とさないのか
-----------------------
**slice-analysis が時系列を扱うから。** あちらの ``models/stack_image`` は
``AXES_ORDER = "TCZYX"`` を正とし、``shape[0]`` をフレーム数として位置合わせを
回している (``Model_Stack_Image needs a 5-D (T, C, Z, Y, X) array``)。T を落とす
正準形にすると、共通ライブラリの側から**最大の利用者を締め出す**ことになる。

histology のように T を使わない取得では T=1 が入るだけで、``img[0, c]`` と書く
ぶんの差しかない。逆に T を落とす形にすると、T>1 のデータは表現できない。

``utils/normalize_bioImage.normalize_to_tczyx`` と目的は同じだが、あちらは
``BioImage`` を受けて xarray を返す。ここは **配列と軸の文字列**を受けるので、
tifffile で読んだ素の配列に効く。
"""
from __future__ import annotations

from typing import Any

import numpy as np

#: 正準の軸順。slice-analysis の ``AXES_ORDER`` と同じ。
CANONICAL_DIM_ORDER = "TCZYX"

#: 受け付ける軸の文字。これ以外 (tifffile が未知の軸に使う ``Q``、RGB の ``S`` など)
#: は、どう置くべきか決められないので断る。
_KNOWN_AXES = frozenset(CANONICAL_DIM_ORDER)


def to_tczyx(data: Any, axes: str) -> Any:
    """``data`` を ``TCZYX`` にする。足りない軸は大きさ 1 で入れる。

    Args:
        data: 配列。``axes`` と次元数が一致していること。**dask 配列はそのまま**
            (実体化しない)。
        axes: ``data`` の軸を表す文字列 (例 ``"ZCYX"``、``"YX"``)。
            tifffile なら ``TiffFile.series[0].axes`` がこれ。

    Returns:
        ``TCZYX`` の 5 次元配列。**画素は写しではなく view** になりうる。

    Raises:
        ValueError: 軸と次元数が食い違う、``Y`` か ``X`` が無い、軸が重複している、
            または知らない軸があるとき。
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
            "Cannot place %s into %s: only %s are known. tifffile uses \'Q\' for an "
            "axis it could not name and \'S\' for samples (RGB); such a file has to "
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

    # --- 足りない軸を大きさ 1 で入れる (T, C, Z の順に前へ積む) ---
    for axis in ("Z", "C", "T"):
        if axis not in axes:
            array = array[np.newaxis]
            axes = axis + axes

    # --- 正準の順へ ---
    order = [axes.index(axis) for axis in CANONICAL_DIM_ORDER]
    return array.transpose(order)


def channel_plane(data: Any, axes: str, channel: int, frame: int = 0) -> Any:
    """1 フレーム・1 チャネルの 2 次元面。Z があれば平均で潰す。

    :func:`to_tczyx` の上の薄い便宜。Z=1 のときの平均は値を変えない (要素が 1 つ)
    ので、Z の有無で場合分けせずに同じ式で書ける。ただし ``mean`` は float を
    返すので、**整数のまま欲しい呼び出し側はここを使わないこと**。
    """
    return to_tczyx(data, axes)[frame, channel].mean(axis=0)

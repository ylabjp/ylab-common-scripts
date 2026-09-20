"""トレースに掛ける低域通過フィルタ。

slice-controller の ``widgets/lowpass_filter.py`` にあった計算を、画面を外して
ここへ移したもの。**式・次数・掛け方は 1 ビットも変えていない** (取得側の表示と
解析側の値が食い違わないことが、共有する目的そのものなので)。
"""
from __future__ import annotations

import numpy as np
from scipy import signal

from ylabcommon.utils.betterstack_log import log_warning

#: 表示・検出で選べる遮断周波数 [Hz]。``None`` は素通し。
#: slice-controller の ``LowpassFilterSelector.OPTIONS`` と同じ並び。
CUTOFF_CHOICES_HZ: tuple[int | None, ...] = (1000, 2000, 5000, None)


def apply_lowpass_filter(y: np.ndarray, sampling_rate_in_kilo_hz: float,
                         cutoff_hz: float | None) -> np.ndarray:
    """4 次のゼロ位相 Butterworth 低域通過を最後の軸に掛ける。

    2 次元配列なら行ごとに独立に掛かる。``cutoff_hz=None`` は素通し。

    フィルタが組めなかったとき (遮断がナイキストを超える等) は、**止めずに
    元の波形をそのまま返し、警告を残す**。表示の途中で例外を投げると画面が
    固まるため、移す前からこの振る舞いである。
    """
    if cutoff_hz is None:
        return y
    try:
        b, a = signal.butter(
            N=4, Wn=cutoff_hz / (sampling_rate_in_kilo_hz * 1000 / 2), btype="low")
        return signal.filtfilt(b, a, y)
    except Exception as e:
        log_warning("apply_lowpass_filter: filter failed, returning unfiltered data",
                    error=e)
        return y

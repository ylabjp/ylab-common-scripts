"""EPSC 様イベント (内向き = 下向きの振れ) の検出。

slice-controller の ``widgets/epsc_detection.py`` にあった計算を、画面を外して
ここへ移したもの。取得側の viewer と解析側が同じ値を出すために共有する。

**振る舞いは移す前と同じにしてある。** テンプレートの作り方、相関の取り方、山の
拾い方、振幅の測り方のどれも変えていない。共有のための移動と、解析の中身の変更を
同時にやると、値が変わった原因がどちらか分からなくなるため。

手順:

1. 二重指数関数のテンプレートを作る (前に ``baseline_ms`` の 0 を付ける)
2. 検出用に ``detection_cutoff_hz`` で低域通過を掛けた波形を反転し、テンプレートと
   相互相関を取る。**この平滑化は山を探すためだけ** で、振幅は元の ``y`` から読む
3. ``scipy.signal.find_peaks`` で山を拾い、山ごとにテンプレート長の窓を切る
4. 窓の先頭 ``baseline_ms`` の平均を引き、下位 1 パーセンタイルの符号を反転した
   ものを振幅とする。``amplitude_threshold_pa`` 以上を ``included`` とする

## 未確認 (移す前からの持ち越し。直すなら実験者の判断が要る)

* ``corr_threshold`` は ``find_peaks`` の ``threshold`` に渡している。scipy の
  ``threshold`` は **隣の標本との縦の距離** であって高さではない (高さなら
  ``height=``)。相関も正規化していないので、値の大きさはトレースの振幅と
  テンプレート長に依存する。画面のラベルは "Corr. threshold" だが相関係数ではない
* ``onset_time_s`` は **窓の先頭** の時刻である。テンプレートの先頭 ``baseline_ms``
  はベースラインの 0 なので、実際の立ち上がりはその ``baseline_ms`` 後になる。
  イベント間隔 (IEI) は差なので影響しないが、時刻そのものは一律にずれる
* 振幅は窓の下位 1 パーセンタイルで、ピーク値そのものではない
* 山の最小間隔 (``find_peaks`` の ``distance``) を指定していないので、重なった
  イベントを分ける規則が無い
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from scipy.signal import find_peaks

from ylabcommon.ephys.filters import apply_lowpass_filter


@dataclass(frozen=True)
class DetectionParams:
    """検出のパラメータ。既定値は slice-controller の画面の既定と同じ。

    値を変えると検出結果が変わるので、**結果と一緒に必ず記録する**
    (:class:`~ylabcommon.ephys.event_record.MepscResult`)。記録に残っていない
    パラメータで出した数字は、後から誰にも確かめられない。
    """

    tau_rise_ms: float = 1.0
    tau_decay_ms: float = 5.0
    corr_threshold: float = 0.07
    detection_cutoff_hz: float = 500.0
    amplitude_threshold_pa: float = 10.0
    baseline_ms: float = 10.0
    response_ms: float = 30.0

    def as_kwargs(self) -> dict[str, float]:
        return asdict(self)


def epsc_template(dt_s: float, tau_rise_s: float, tau_decay_s: float,
                  baseline_ms: float, response_ms: float) -> tuple[np.ndarray, int]:
    """二重指数関数のテンプレートと、先頭のベースラインの点数。

    先頭に ``baseline_ms`` ぶんの 0 を付ける。テンプレートは最大値 1 に正規化する。
    """
    baseline_points = int(baseline_ms / 1000 / dt_s)
    baseline = np.zeros(baseline_points)

    template_t = np.arange(0, response_ms / 1000, dt_s)
    template = np.exp(-template_t / tau_decay_s) - np.exp(-template_t / tau_rise_s)
    template = template / np.max(np.abs(template))

    return np.concatenate([baseline, template]), baseline_points


def detect_epsc_events(y: np.ndarray, dt_s: float, tau_rise_ms: float = 1.0,
                       tau_decay_ms: float = 5.0, corr_threshold: float = 0.07,
                       detection_cutoff_hz: float = 500.0,
                       amplitude_threshold_pa: float = 10.0,
                       baseline_ms: float = 10.0,
                       response_ms: float = 30.0) -> list[dict]:
    """1 本のトレースから EPSC 様 (内向き・下向き) のイベントを拾う。

    手順と、持ち越している未確認の点はモジュールの説明を参照。

    Returns:
        ``event_num`` / ``onset_idx`` / ``onset_time_s`` / ``amplitude_pa`` /
        ``included`` / ``trace`` (ベースラインを引いた窓) を持つ dict の一覧。
    """
    template, baseline_points = epsc_template(
        dt_s, tau_rise_ms / 1000, tau_decay_ms / 1000, baseline_ms, response_ms)

    sampling_rate_khz = 1.0 / dt_s / 1000
    detect_input = apply_lowpass_filter(y, sampling_rate_khz, detection_cutoff_hz)
    inverted = np.max(detect_input) - detect_input
    corr = np.correlate(inverted, template)
    peaks, _ = find_peaks(corr, threshold=corr_threshold)

    window_len = template.shape[0]
    events = []
    for i, p in enumerate(peaks):
        start, end = int(p), int(p) + window_len
        if end > y.shape[0]:
            continue
        trace = y[start:end]
        baseline_val = trace[:baseline_points].mean()
        normalized = trace - baseline_val
        amplitude = float(-np.quantile(normalized, 0.01))
        events.append({
            "event_num": i,
            "onset_idx": start,
            "onset_time_s": start * dt_s,
            "amplitude_pa": amplitude,
            "included": amplitude >= amplitude_threshold_pa,
            "trace": normalized,
        })
    return events


def detect(y: np.ndarray, dt_s: float, params: DetectionParams) -> list[dict]:
    """:class:`DetectionParams` を渡す形の :func:`detect_epsc_events`。"""
    return detect_epsc_events(y, dt_s, **params.as_kwargs())


def included_events(events: list[dict]) -> list[dict]:
    """``included`` が真のイベントだけ。"""
    return [e for e in events if e["included"]]


def inter_event_intervals_ms(events: list[dict]) -> np.ndarray:
    """採用したイベントの間隔 [ms]。時刻で並べ直してから差を取る。

    イベントが 1 つ以下なら空配列 (間隔が定義できない)。``onset_time_s`` が
    一律にずれていても差なので影響しない (モジュールの説明を参照)。
    """
    times = np.sort(np.asarray([e["onset_time_s"] for e in events], dtype=np.float64))
    if times.size < 2:
        return np.zeros(0, dtype=np.float64)
    return np.diff(times) * 1000.0


def summarize(events: list[dict], analysed_seconds: float) -> dict[str, Any]:
    """採用したイベントの要約。``analysed_seconds`` は **掛けた波形の長さ** [s]。

    ``frequency_hz`` は「採用したイベント数 ÷ 掛けた波形の長さ」である。頻度は
    記録長が無いと出せないので、長さは呼ぶ側が必ず渡す (トレースの点数 ÷
    サンプリングレートで出る。:meth:`SortedRecording.duration_s` も同じ)。

    イベントが無いときは平均を ``None`` にする。**0 で埋めない** —— 0 pA の
    イベントが並んでいるのと見分けが付かなくなる。
    """
    if not (analysed_seconds > 0):
        raise ValueError(
            "analysed_seconds must be positive to give a frequency; got %r. Pass the "
            "length of the trace that was actually analysed." % (analysed_seconds,))
    kept = included_events(events)
    amps = np.asarray([e["amplitude_pa"] for e in kept], dtype=np.float64)
    ieis = inter_event_intervals_ms(kept)
    return {
        "n_detected": len(events),
        "n_included": len(kept),
        "analysed_seconds": float(analysed_seconds),
        "frequency_hz": len(kept) / float(analysed_seconds),
        "mean_amplitude_pa": float(amps.mean()) if amps.size else None,
        "median_amplitude_pa": float(np.median(amps)) if amps.size else None,
        "mean_iei_ms": float(ieis.mean()) if ieis.size else None,
    }

"""記録 1 つに検出を掛けて記録を組むまでの **手順**。画面も CLI も解析もここを通る。

下の層 (読み口・検出・記録の形) を並べただけだが、並べ方をここに 1 つだけ置くのが
目的である。画面の中に段取りを書くと、agent やコマンドから同じことを回せず、
「画面で見た値」と「集計に載った値」が別の道から出ることになる。

呼ぶ側:

- slice-controller の viewer (``Detect Events`` と ``Save result``)
- slice-controller の ``scli mepsc`` (agent が回す)
- slice-analysis の ``workflow/mepsc`` と ``roicli mepsc``
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ylabcommon.ephys.event_detection import DetectionParams, detect
from ylabcommon.ephys.event_record import MepscResult, build_result
from ylabcommon.ephys.filters import apply_lowpass_filter
from ylabcommon.ephys.recording import (
    Recording,
    TraceSelection,
    open_recording,
    refuse_average_for_detection,
    trace_of,
)


@dataclass(frozen=True)
class DetectionRun:
    """1 回の検出の、入力・途中・出力をひとまとめにしたもの。

    画面は ``values`` を描き ``events`` を重ね、``result`` を保存する。CLI は
    ``result`` だけを見る。**同じ 1 回の計算から全部が出る** ので、描いた波形と
    記録された値が食い違わない。
    """

    recording: Recording
    selection: TraceSelection
    #: 実際に検出へ渡した波形 (表示用の低域通過を通した後)。
    values: np.ndarray
    #: イベントの生の並び (``onset_idx`` と窓の波形つき)。画面の重ね描き用。
    events: list[dict]
    result: MepscResult


def run(recording: Recording, selection: TraceSelection | None = None,
        params: DetectionParams | None = None,
        display_cutoff_hz: float | None = None) -> DetectionRun:
    """開いてある記録に検出を掛ける (**まだ書かない**)。

    Args:
        recording: :func:`~ylabcommon.ephys.recording.open_recording` の返り値。
        selection: どの波形に掛けるか。既定は全 train の連結。
        params: 検出のパラメータ。既定は取得側の画面の既定と同じ。
        display_cutoff_hz: 検出に入れる **前** に通す表示用の低域通過 [Hz]。
            ``None`` なら通さない。**ここで実際に掛ける。** 記録に残すだけで
            掛けないと、記録が嘘になる (画面は掛けた波形を見ているため)。

    Raises:
        ValueError: 平均波形を渡した (:data:`~ylabcommon.ephys.recording.
            AVERAGE_REFUSAL`)、その train が無い、サンプリングレートが読めない。
    """
    picked = selection or TraceSelection()
    refuse_average_for_detection(picked)
    used = DetectionParams() if params is None else params

    values = trace_of(recording, picked)
    if display_cutoff_hz is not None:
        values = apply_lowpass_filter(values, recording.sampling_rate_khz,
                                      display_cutoff_hz)
    events = detect(values, recording.dt_s, used)
    result = build_result(
        events, source_file=recording.group,
        sampling_rate_khz=recording.sampling_rate_khz,
        analysed_seconds=values.shape[0] * recording.dt_s, params=used,
        train_idx=picked.train_idx, config_name=recording.config_name,
        display_cutoff_hz=display_cutoff_hz)
    return DetectionRun(recording=recording, selection=picked, values=values,
                        events=events, result=result)


def analyse(recording: Recording, selection: TraceSelection | None = None,
            params: DetectionParams | None = None,
            display_cutoff_hz: float | None = None) -> MepscResult:
    """:func:`run` の結果だけが要るとき。"""
    return run(recording, selection, params, display_cutoff_hz).result


def run_detection(path: str | Path, group: str | None = None,
                  selection: TraceSelection | None = None,
                  params: DetectionParams | None = None,
                  display_cutoff_hz: float | None = None) -> MepscResult:
    """ファイルを開いて :func:`analyse` に掛ける (開く手間を省いた形)。"""
    return analyse(open_recording(path, group), selection, params, display_cutoff_hz)

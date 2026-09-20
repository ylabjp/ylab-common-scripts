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

from pathlib import Path

from ylabcommon.ephys.event_detection import DetectionParams, detect
from ylabcommon.ephys.event_record import MepscResult, build_result
from ylabcommon.ephys.recording import (
    Recording,
    TraceSelection,
    duration_s,
    open_recording,
    refuse_average_for_detection,
    trace_of,
)


def analyse(recording: Recording, selection: TraceSelection | None = None,
            params: DetectionParams | None = None,
            display_cutoff_hz: float | None = None) -> MepscResult:
    """開いてある記録に検出を掛けて、結果を組む (**まだ書かない**)。

    Args:
        recording: :func:`~ylabcommon.ephys.recording.open_recording` の返り値。
        selection: どの波形に掛けるか。既定は全 train の連結。
        params: 検出のパラメータ。既定は取得側の画面の既定と同じ。
        display_cutoff_hz: 検出に入れる **前** に通した表示用の低域通過 [Hz]。
            通していなければ ``None``。**振幅に効くので記録に残す。**

    Raises:
        ValueError: 平均波形を渡した (:data:`~ylabcommon.ephys.recording.
            AVERAGE_REFUSAL`)、その train が無い、サンプリングレートが読めない。
    """
    picked = selection or TraceSelection()
    refuse_average_for_detection(picked)
    used = DetectionParams() if params is None else params
    values = trace_of(recording, picked)
    events = detect(values, recording.dt_s, used)
    return build_result(
        events, source_file=recording.group,
        sampling_rate_khz=recording.sampling_rate_khz,
        analysed_seconds=duration_s(recording, picked), params=used,
        train_idx=picked.train_idx, config_name=recording.config_name,
        display_cutoff_hz=display_cutoff_hz)


def run_detection(path: str | Path, group: str | None = None,
                  selection: TraceSelection | None = None,
                  params: DetectionParams | None = None,
                  display_cutoff_hz: float | None = None) -> MepscResult:
    """ファイルを開いて :func:`analyse` に掛ける (開く手間を省いた形)。"""
    return analyse(open_recording(path, group), selection, params, display_cutoff_hz)

"""取得直後の h5 と仕分け後の ``*_df.h5`` を、同じ形で開いて同じ形で切り出す層。

**画面と CLI と解析が、同じ手順を同じ入口から呼ぶための中間層。**
下は形ごとの読み口 (:mod:`~ylabcommon.ephys.raw_trace` /
:mod:`~ylabcommon.ephys.sorted_trace`)、上は画面 (slice-controller の viewer) と
コマンド (agent が回す) と解析 (slice-analysis)。ここに段取りを 1 つ置くことで、
画面で見た波形とコマンドで掛けた波形が同じものになる。

やることは 3 つだけ:

1. ファイルの形を見て、正しい読み口で開く (:func:`open_recording`)
2. 「どの波形に掛けるか」を 1 つの値で表す (:class:`TraceSelection`)
3. 中身を **機械が読める形** で言う (:func:`describe`)。agent はこれを見てから
   どの train に掛けるかを決める
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

import numpy as np

from ylabcommon.ephys.raw_trace import RawRecording
from ylabcommon.ephys.sorted_trace import DF_SUFFIX, SortedRecording

#: 開いた記録。どちらも同じメソッドを持つ (raw_trace の説明)。
Recording = Union[RawRecording, SortedRecording]


def is_sorted_output(path: str | Path) -> bool:
    """sorter が書いた ``*_df.h5`` か。"""
    return str(path).endswith(DF_SUFFIX)


def list_groups(path: str | Path) -> list[str]:
    """そのファイルに入っている記録の名前。

    仕分け後は 1 ファイル = 1 記録なのでファイル名が 1 つ。取得直後の h5 は
    train グループが複数入りうるので、その並び。
    """
    if is_sorted_output(path):
        return [Path(path).name]
    return RawRecording.groups(path)


def open_recording(path: str | Path, group: str | None = None) -> Recording:
    """形を見て正しい読み口で開く。``group`` は取得直後の h5 の train グループ名。"""
    if is_sorted_output(path):
        return SortedRecording.open(path)
    return RawRecording.open(path, group)


@dataclass(frozen=True)
class TraceSelection:
    """どの波形に掛けるかを 1 つの値で表す。

    - ``train_idx=None`` (既定): **全 train を連結**。mEPSC はランダムに出るので
      train 1 本では頻度が出ない
    - ``train_idx=n``: その train だけ
    - ``average_trains=True``: train の平均。**表示のためだけ** で、イベント検出に
      渡してはいけない (平均するとランダムなイベントは消える)。
      :func:`refuse_average_for_detection` が入口で断る
    """

    train_idx: int | None = None
    average_trains: bool = False

    def describe(self) -> str:
        if self.average_trains:
            return "average of all trains"
        if self.train_idx is None:
            return "all trains concatenated"
        return "train %d" % self.train_idx


#: 平均波形を検出に渡そうとしたときの断り文句 (画面も CLI も同じ言葉で断る)。
AVERAGE_REFUSAL = (
    "Detection does not run on a train average: averaging removes randomly occurring "
    "events such as mEPSCs. Use all trains (the default) or pick a single train.")


def refuse_average_for_detection(selection: TraceSelection) -> None:
    """平均波形なら :class:`ValueError`。検出に入る手前で必ず通す。"""
    if selection.average_trains:
        raise ValueError(AVERAGE_REFUSAL)


def trace_of(recording: Recording, selection: TraceSelection | None = None) -> np.ndarray:
    """選んだ波形を切り出す。

    平均は **取得できた train だけ** で取る。設定上の本数で割ると、記録されて
    いない 0 埋めのぶんまで平均に混ざる。
    """
    picked = selection or TraceSelection()
    if not picked.average_trains:
        return recording.values(picked.train_idx)
    count = recording.n_trains
    if count <= 0:
        raise ValueError("%s holds no complete train to average." % recording.path)
    per = recording.samples_per_train
    whole = recording.values(None)[: count * per]
    return np.asarray(whole, dtype=np.float64).reshape(count, per).mean(axis=0)


def duration_s(recording: Recording, selection: TraceSelection | None = None) -> float:
    """選んだ波形の長さ [s]。頻度の分母になる。"""
    return trace_of(recording, selection).shape[0] * recording.dt_s


def describe(recording: Recording) -> dict[str, Any]:
    """記録の中身を機械が読める形で言う (``--json`` で出すもの)。

    agent はこれを見てから「どの train に掛けるか」を決める。長さと train の数が
    分からないと、頻度が意味を持つ掛け方かどうかも判断できない。
    """
    return {
        "path": str(recording.path),
        "group": recording.group,
        "kind": "sorted" if isinstance(recording, SortedRecording) else "raw",
        "config_name": recording.config_name,
        "sampling_rate_khz": recording.sampling_rate_khz,
        "n_trains": recording.n_trains,
        "samples_per_train": recording.samples_per_train,
        "n_samples": int(recording.values(None).shape[0]),
        "duration_s": recording.duration_s(None),
        "train_indices": recording.train_indices,
    }

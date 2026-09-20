"""検出結果の記録。

**検出した値だけでなく、その値が出た条件も一緒に残す。** どの波形の、どの train を、
どのフィルタと、どのパラメータで掛けたのかが無いと、後から誰も確かめられない。
頻度は掛けた波形の長さが無いと出せないので、長さも必ず入れる。

置き場と名前は :data:`RESULT_FILENAME` / :data:`EVENTS_CSV_FILENAME` で固定する。
**保存先を人が選ぶ形にしない** —— crawl (集計) は決まった名前のファイルを探すので、
その場で選んだ名前ではセッションと結び付かない。
"""
from __future__ import annotations

import csv
import datetime
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from ylabcommon.ephys.event_detection import DetectionParams, summarize

#: 結果の名前の後ろ。掛けた記録 1 つにつき 1 ファイル。
#:
#: **セッションのフォルダに固定名を 1 つ、にはしない。** 1 つのセル (フォルダ) に
#: V-test と STDP のように記録が 2 つ入ることがあり (sorter は記録ごとに
#: ``<prefix><名前>_df.h5`` を書く)、固定名だと後から掛けたほうが前のを消す。
#: sorter の ``<base>_df.h5`` / ``<base>.json`` と同じく、元の名前で対にする。
RESULT_SUFFIX = "_mepsc_result.json"
#: 人が見るための、イベント 1 つ 1 行の表。
EVENTS_CSV_SUFFIX = "_mepsc_events.csv"
#: 集計 (crawl) が探すパターン。
RESULT_GLOB = "*" + RESULT_SUFFIX


def result_basename(source_file: str) -> str:
    """掛けた記録の名前から、結果の名前の頭を作る。

    ``V-test_260904-001_df.h5`` -> ``V-test_260904-001``。取得直後の h5 を直接
    掛けたときも同じように拡張子だけ落とす。
    """
    name = Path(source_file).name
    for suffix in ("_df.h5", ".h5"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def result_path_for(session_dir: str | Path, source_file: str) -> Path:
    """その記録の結果ファイルのパス。"""
    return Path(session_dir) / (result_basename(source_file) + RESULT_SUFFIX)

#: ``export_events_csv`` の列。移す前の slice-controller の出力と同じ。
EVENT_CSV_COLUMNS = ("event_num", "onset_time_s", "amplitude_pa", "included")


class EventRow(BaseModel):
    """イベント 1 つ。波形 (``trace``) は重いので記録には入れない。"""

    event_num: int
    onset_time_s: float
    amplitude_pa: float
    included: bool


class MepscResult(BaseModel):
    """1 つの波形に対する検出の結果と、その条件。

    ``mean_*`` は採用したイベントが無ければ ``None`` である。**0 で埋めない**
    —— 0 pA のイベントが並んでいるのと見分けが付かなくなる。
    """

    model_config = ConfigDict(extra="allow")

    #: 掛けた波形の出どころ。
    source_file: str
    #: 掛けた train の番号。``None`` は全 train を連結したもの。
    train_idx: int | None = None
    #: 記録に使った ini の名前 (sorter の json の ``config_name``)。無ければ空。
    config_name: str = ""
    #: サンプリングレート [kHz] と、掛けた波形の長さ [s] (頻度の分母)。
    sampling_rate_khz: float
    analysed_seconds: float
    #: 検出に掛ける **前** に通した表示用の低域通過 [Hz]。``None`` は素通し。
    #: 検出そのものの ``detection_cutoff_hz`` とは別で、**振幅に効く**。
    display_cutoff_hz: float | None = None
    #: 検出のパラメータ (:class:`DetectionParams` の各値)。
    params: dict[str, float]
    #: 要約。``frequency_hz`` は採用したイベント数 ÷ ``analysed_seconds``。
    n_detected: int
    n_included: int
    frequency_hz: float
    mean_amplitude_pa: float | None = None
    median_amplitude_pa: float | None = None
    mean_iei_ms: float | None = None
    #: イベント 1 つ 1 行。
    events: list[EventRow] = []
    #: 記録した日時 (ISO 8601)。
    created_at: str = ""


def build_result(events: list[dict], *, source_file: str, sampling_rate_khz: float,
                 analysed_seconds: float, params: DetectionParams,
                 train_idx: int | None = None, config_name: str = "",
                 display_cutoff_hz: float | None = None) -> MepscResult:
    """検出結果から記録を組む (まだ書かない)。"""
    summary = summarize(events, analysed_seconds)
    return MepscResult(
        source_file=source_file, train_idx=train_idx, config_name=config_name,
        sampling_rate_khz=float(sampling_rate_khz),
        analysed_seconds=summary["analysed_seconds"],
        display_cutoff_hz=display_cutoff_hz, params=params.as_kwargs(),
        n_detected=summary["n_detected"], n_included=summary["n_included"],
        frequency_hz=summary["frequency_hz"],
        mean_amplitude_pa=summary["mean_amplitude_pa"],
        median_amplitude_pa=summary["median_amplitude_pa"],
        mean_iei_ms=summary["mean_iei_ms"],
        events=[EventRow(event_num=int(e["event_num"]),
                         onset_time_s=float(e["onset_time_s"]),
                         amplitude_pa=float(e["amplitude_pa"]),
                         included=bool(e["included"])) for e in events],
        created_at=datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def export_events_csv(path: str | Path, events: list[dict]) -> None:
    """イベント 1 つ 1 行の CSV。列は移す前と同じ。"""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(list(EVENT_CSV_COLUMNS))
        for e in events:
            writer.writerow([e["event_num"], e["onset_time_s"], e["amplitude_pa"],
                             e["included"]])


def save_result(session_dir: str | Path, result: MepscResult,
                write_csv: bool = True) -> Path:
    """セッションのフォルダへ ``<記録の名前>_mepsc_result.json`` を書く。

    書いたパスを返す。**別名に書いてから差し替える。** 途中で止まったときに
    壊れた json が最終名で残ると、次の集計がそこで止まる (sorter の ``_df.h5``
    と同じ理由)。
    """
    out_dir = Path(session_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = result_basename(result.source_file)
    out = out_dir / (base + RESULT_SUFFIX)
    tmp = out.with_name("_" + out.name + ".tmp")
    tmp.write_text(result.model_dump_json(indent=4), encoding="utf-8")
    tmp.replace(out)
    if write_csv:
        export_events_csv(out_dir / (base + EVENTS_CSV_SUFFIX),
                          [e.model_dump() for e in result.events])
    return out


def load_result(path: str | Path) -> MepscResult:
    """:data:`RESULT_FILENAME` を読む。"""
    return MepscResult(**json.loads(Path(path).read_text(encoding="utf-8")))


def result_row(result: MepscResult) -> dict[str, Any]:
    """集計の表の 1 行 (イベントの並びは落として、要約と条件だけ)。"""
    row: dict[str, Any] = {
        "source_file": result.source_file, "train_idx": result.train_idx,
        "config_name": result.config_name,
        "sampling_rate_khz": result.sampling_rate_khz,
        "analysed_seconds": result.analysed_seconds,
        "display_cutoff_hz": result.display_cutoff_hz,
        "n_detected": result.n_detected, "n_included": result.n_included,
        "frequency_hz": result.frequency_hz,
        "mean_amplitude_pa": result.mean_amplitude_pa,
        "median_amplitude_pa": result.median_amplitude_pa,
        "mean_iei_ms": result.mean_iei_ms,
        "created_at": result.created_at,
    }
    row.update({"param_" + k: v for k, v in result.params.items()})
    return row

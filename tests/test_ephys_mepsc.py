# -*- coding: utf-8 -*-
"""``ylabcommon.ephys`` の単体テスト。

検出そのものは slice-controller の ``widgets/epsc_detection.py`` から
**振る舞いを変えずに** 移したもの。移す前のテストをここへ持ってきてある
(向こうは Qt を引き込む同じファイルに計算が同居していたので、画面の無い環境では
まるごと skip されていた。ここでは Qt 無しで走る)。
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ylabcommon.ephys.event_detection import (
    DetectionParams,
    detect,
    detect_epsc_events,
    epsc_template,
    included_events,
    inter_event_intervals_ms,
    summarize,
)
from ylabcommon.ephys.event_record import (
    EVENT_CSV_COLUMNS,
    RESULT_FILENAME,
    build_result,
    export_events_csv,
    load_result,
    result_row,
    save_result,
)
from ylabcommon.ephys.filters import apply_lowpass_filter
from ylabcommon.ephys.sorted_trace import HDF_KEY, SortedRecording, meta_path_for

DT = 1.0 / 20000  # 20 kHz


def _epsc_event(n, onset, amp, tau_rise=1.0, tau_decay=5.0):
    """下向き (内向き) の EPSC 様の振れ 1 つ。ピークはおよそ ``amp`` pA。"""
    t = np.arange(n) * DT
    shape = np.zeros(n)
    tt = t[onset:] - t[onset]
    kernel = np.exp(-tt / (tau_decay / 1000)) - np.exp(-tt / (tau_rise / 1000))
    kernel = kernel / kernel.max()
    shape[onset:] = -amp * kernel
    return shape


# ---- 検出 (移す前からのテスト) ----------------------------------------------------

def test_flat_trace_yields_no_events():
    y = np.zeros(8000)
    assert detect_epsc_events(y, DT) == []


def test_detects_downward_events_above_threshold():
    n = 12000
    y = _epsc_event(n, 2000, 60) + _epsc_event(n, 6000, 60) + _epsc_event(n, 9000, 60)
    events = detect_epsc_events(y, DT, amplitude_threshold_pa=10.0)
    included = included_events(events)
    assert len(included) >= 1
    assert all(e["amplitude_pa"] >= 10.0 for e in included)
    assert max(e["amplitude_pa"] for e in included) > 30


def test_amplitude_threshold_reduces_included_count():
    n = 12000
    y = _epsc_event(n, 2000, 60) + _epsc_event(n, 6000, 60)
    low = detect_epsc_events(y, DT, amplitude_threshold_pa=10.0)
    high = detect_epsc_events(y, DT, amplitude_threshold_pa=10000.0)
    assert sum(e["included"] for e in high) == 0
    assert sum(e["included"] for e in low) >= sum(e["included"] for e in high)


def test_returned_events_stay_within_trace_bounds():
    n = 6000
    y = _epsc_event(n, 5900, 60)          # 末尾ぎりぎり
    for e in detect_epsc_events(y, DT):
        assert e["onset_idx"] + e["trace"].shape[0] <= n


def test_export_csv_round_trip(tmp_path):
    events = [
        {"event_num": 0, "onset_time_s": 0.0005, "amplitude_pa": 42.5,
         "included": True, "trace": np.zeros(3)},
        {"event_num": 1, "onset_time_s": 0.00495, "amplitude_pa": 5.0,
         "included": False, "trace": np.zeros(3)},
    ]
    out = tmp_path / "events.csv"
    export_events_csv(out, events)
    rows = list(csv.reader(out.open(newline="")))
    assert rows[0] == list(EVENT_CSV_COLUMNS)
    assert rows[1][0] == "0" and rows[1][3] == "True"
    assert rows[2][0] == "1" and rows[2][3] == "False"


def test_the_template_starts_with_the_baseline_and_peaks_at_one():
    template, baseline_points = epsc_template(DT, 0.001, 0.005, baseline_ms=10.0,
                                              response_ms=30.0)
    assert baseline_points == int(0.010 / DT)
    assert np.all(template[:baseline_points] == 0.0)
    assert template.max() == pytest.approx(1.0)
    assert template.shape[0] == baseline_points + int(0.030 / DT)


def test_detect_takes_the_same_parameters_as_a_record():
    """``DetectionParams`` 経由でも素の呼び出しと同じ結果になること。"""
    y = _epsc_event(12000, 2000, 60) + _epsc_event(12000, 6000, 60)
    params = DetectionParams(amplitude_threshold_pa=25.0)

    got = detect(y, DT, params)
    expected = detect_epsc_events(y, DT, amplitude_threshold_pa=25.0)

    assert [e["onset_idx"] for e in got] == [e["onset_idx"] for e in expected]
    assert [e["amplitude_pa"] for e in got] == [e["amplitude_pa"] for e in expected]
    assert params.as_kwargs()["amplitude_threshold_pa"] == 25.0


# ---- 要約 (頻度は記録長が要る) -------------------------------------------------------

def _row(num, t, amp, included=True):
    return {"event_num": num, "onset_time_s": t, "amplitude_pa": amp,
            "included": included}


def test_the_frequency_is_the_included_count_over_the_analysed_length():
    events = [_row(0, 0.10, 20.0), _row(1, 0.30, 30.0), _row(2, 0.70, 5.0, False)]

    got = summarize(events, analysed_seconds=2.0)

    assert (got["n_detected"], got["n_included"]) == (3, 2)
    assert got["frequency_hz"] == pytest.approx(1.0)          # 2 個 / 2 s
    assert got["mean_amplitude_pa"] == pytest.approx(25.0)    # 除外したものは入れない
    assert got["median_amplitude_pa"] == pytest.approx(25.0)
    assert got["mean_iei_ms"] == pytest.approx(200.0)         # 0.30 - 0.10 s
    assert got["analysed_seconds"] == 2.0


def test_the_intervals_are_taken_in_time_order():
    unsorted_events = [_row(0, 0.50, 20.0), _row(1, 0.10, 20.0), _row(2, 0.20, 20.0)]
    np.testing.assert_allclose(inter_event_intervals_ms(unsorted_events), [100.0, 300.0])
    assert inter_event_intervals_ms([_row(0, 0.1, 20.0)]).size == 0   # 1 つでは間隔が無い


def test_no_events_leaves_the_averages_empty_rather_than_zero():
    """0 で埋めない。0 pA のイベントが並んでいるのと見分けが付かなくなる。"""
    got = summarize([_row(0, 0.1, 2.0, included=False)], analysed_seconds=5.0)

    assert got["n_included"] == 0 and got["frequency_hz"] == 0.0
    assert got["mean_amplitude_pa"] is None
    assert got["median_amplitude_pa"] is None
    assert got["mean_iei_ms"] is None


def test_a_summary_without_a_length_is_refused():
    """記録長が無いと頻度が出せない。適当な値に倒さず断る。"""
    with pytest.raises(ValueError, match="analysed_seconds"):
        summarize([_row(0, 0.1, 20.0)], analysed_seconds=0.0)


# ---- フィルタ ------------------------------------------------------------------------

def test_the_lowpass_passes_the_trace_through_when_no_cutoff_is_chosen():
    y = np.random.default_rng(0).normal(size=512)
    assert apply_lowpass_filter(y, 20.0, None) is y
    smoothed = apply_lowpass_filter(y, 20.0, 1000.0)
    assert smoothed.shape == y.shape and smoothed.std() < y.std()


# ---- sorter の出力を読む -------------------------------------------------------------

SR_KHZ = 20.0
SAMPLES_PER_TRAIN = 400
N_TRAINS = 3


def _write_sorted(dir_path: Path, base: str = "V-test_2609xx-001") -> Path:
    """sorter が書くのと同じ組 (``<base>_df.h5`` と ``<base>.json``) を作る。"""
    n = SAMPLES_PER_TRAIN * N_TRAINS
    val = np.arange(n, dtype=np.float64)            # train ごとに値が違うと分かる形
    frame = pd.DataFrame({
        "val": val,
        "time_ms": np.arange(n) / SR_KHZ,
        "train_idx": np.repeat(np.arange(N_TRAINS, dtype=np.uint16), SAMPLES_PER_TRAIN),
        "rel_time_ms": np.tile(np.arange(SAMPLES_PER_TRAIN) / SR_KHZ, N_TRAINS),
    })
    df_path = dir_path / (base + "_df.h5")
    frame.to_hdf(df_path, key=HDF_KEY, mode="w", complevel=9)
    (dir_path / (base + ".json")).write_text(json.dumps({"param": [{
        "sampling_rate_in_kHz": int(SR_KHZ), "config_name": "mEPSC.ini",
        "train_train_number": N_TRAINS, "train_sample_number": SAMPLES_PER_TRAIN,
    }]}), encoding="utf-8")
    return df_path


def test_the_sorter_output_is_read_with_its_sampling_rate_and_trains(tmp_path):
    df_path = _write_sorted(tmp_path)

    rec = SortedRecording.open(df_path)

    assert rec.sampling_rate_khz == SR_KHZ
    assert rec.dt_s == pytest.approx(1 / (SR_KHZ * 1000))
    assert rec.config_name == "mEPSC.ini"
    assert rec.train_indices == [0, 1, 2]
    # 既定は全 train の連結 (mEPSC は train をまたいで数えないと頻度が出ない)
    assert rec.values().shape[0] == SAMPLES_PER_TRAIN * N_TRAINS
    assert rec.duration_s() == pytest.approx(SAMPLES_PER_TRAIN * N_TRAINS / (SR_KHZ * 1000))
    # train を選べば、その train だけ
    np.testing.assert_allclose(rec.values(1),
                               np.arange(SAMPLES_PER_TRAIN, 2 * SAMPLES_PER_TRAIN))
    assert rec.duration_s(1) == pytest.approx(SAMPLES_PER_TRAIN / (SR_KHZ * 1000))


def test_a_sorter_output_without_its_metadata_is_refused(tmp_path):
    """相方の json が無いとサンプリングレートが分からない。既定値に倒さず断る。"""
    df_path = _write_sorted(tmp_path)
    meta_path_for(df_path).unlink()

    with pytest.raises(FileNotFoundError, match="sampling rate"):
        SortedRecording.open(df_path)

    with pytest.raises(ValueError, match="_df.h5"):
        meta_path_for(tmp_path / "raw.h5")               # 取得直後の h5 は別物
    with pytest.raises(FileNotFoundError):
        SortedRecording.open(tmp_path / "nothing_df.h5")


def test_an_unknown_train_is_named_rather_than_returning_nothing(tmp_path):
    rec = SortedRecording.open(_write_sorted(tmp_path))
    with pytest.raises(ValueError, match=r"No train 9 .*\[0, 1, 2\]"):
        rec.values(9)


# ---- 記録 ----------------------------------------------------------------------------

def test_the_record_keeps_the_conditions_that_produced_the_numbers(tmp_path):
    """値だけでなく、どの波形をどの条件で掛けたのかが残ること。"""
    events = [_row(0, 0.10, 20.0), _row(1, 0.30, 30.0), _row(2, 0.70, 5.0, False)]
    params = DetectionParams(amplitude_threshold_pa=10.0, tau_decay_ms=7.0)

    result = build_result(events, source_file="V-test_2609xx-001_df.h5",
                          sampling_rate_khz=SR_KHZ, analysed_seconds=2.0,
                          params=params, train_idx=None, config_name="mEPSC.ini",
                          display_cutoff_hz=1000.0)
    written = save_result(tmp_path, result)

    assert written.name == RESULT_FILENAME
    assert not list(tmp_path.glob("*.tmp"))              # 書き掛けを残さない
    back = load_result(written)
    assert back.source_file == "V-test_2609xx-001_df.h5" and back.train_idx is None
    assert back.config_name == "mEPSC.ini"
    assert back.sampling_rate_khz == SR_KHZ and back.analysed_seconds == 2.0
    assert back.display_cutoff_hz == 1000.0              # 振幅に効くので残す
    assert back.params["tau_decay_ms"] == 7.0
    assert (back.n_detected, back.n_included) == (3, 2)
    assert back.frequency_hz == pytest.approx(1.0)
    assert back.mean_amplitude_pa == pytest.approx(25.0)
    assert [e.event_num for e in back.events] == [0, 1, 2]
    assert back.created_at

    rows = list(csv.reader((tmp_path / "mepsc_events.csv").open(newline="")))
    assert rows[0] == list(EVENT_CSV_COLUMNS) and len(rows) == 4

    row = result_row(back)
    assert row["frequency_hz"] == pytest.approx(1.0)
    assert row["param_tau_decay_ms"] == 7.0
    assert "events" not in row                            # 表には並びを入れない


def test_a_record_without_events_says_none_not_zero(tmp_path):
    result = build_result([], source_file="x_df.h5", sampling_rate_khz=SR_KHZ,
                          analysed_seconds=4.0, params=DetectionParams())

    assert result.n_included == 0 and result.frequency_hz == 0.0
    assert result.mean_amplitude_pa is None and result.mean_iei_ms is None
    back = load_result(save_result(tmp_path, result))
    assert back.mean_amplitude_pa is None


def test_detection_through_to_a_record_on_a_sorted_recording(tmp_path):
    """sorter の出力を読んで検出し、記録まで通ること (解析側が踏む道)。

    1 train は検出の窓 (ベースライン 10 ms + 応答 30 ms = 800 標本 @20 kHz) より
    十分に長く取る。短いと相関を取れる範囲が窓のぶん縮み、置いたイベントが窓から
    はみ出して 1 つも拾えない。
    """
    per_train, n_trains = 4000, 3
    n = per_train * n_trains
    y = _epsc_event(n, 2000, 60) + _epsc_event(n, 6000, 60)
    frame = pd.DataFrame({
        "val": y, "time_ms": np.arange(n) / SR_KHZ,
        "train_idx": np.repeat(np.arange(n_trains, dtype=np.uint16), per_train),
        "rel_time_ms": np.tile(np.arange(per_train) / SR_KHZ, n_trains),
    })
    frame.to_hdf(tmp_path / "a_df.h5", key=HDF_KEY, mode="w", complevel=9)
    (tmp_path / "a.json").write_text(
        json.dumps({"param": [{"sampling_rate_in_kHz": int(SR_KHZ)}]}), encoding="utf-8")

    rec = SortedRecording.open(tmp_path / "a_df.h5")
    params = DetectionParams()
    events = detect(rec.values(), rec.dt_s, params)
    result = build_result(events, source_file=rec.path.name,
                          sampling_rate_khz=rec.sampling_rate_khz,
                          analysed_seconds=rec.duration_s(), params=params,
                          config_name=rec.config_name)

    assert result.n_included >= 1
    assert result.analysed_seconds == pytest.approx(n / (SR_KHZ * 1000))
    assert result.frequency_hz == pytest.approx(
        result.n_included / result.analysed_seconds)
    assert not math.isnan(result.frequency_hz)

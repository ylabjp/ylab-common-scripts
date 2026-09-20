# -*- coding: utf-8 -*-
"""取得直後の h5 と仕分け後の ``*_df.h5`` を同じ形で扱う中間層のテスト。

ここが守るのは「画面と CLI と解析が同じ手順を通る」こと。形が 2 つあっても、
開き方・切り出し方・断り方が 1 つであること。
"""
from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np
import pandas as pd
import pytest

from ylabcommon.ephys.event_detection import DetectionParams
from ylabcommon.ephys.procedure import analyse, run_detection
from ylabcommon.ephys.raw_trace import RawRecording
from ylabcommon.ephys.recording import (
    AVERAGE_REFUSAL,
    TraceSelection,
    describe,
    duration_s,
    is_sorted_output,
    list_groups,
    open_recording,
    refuse_average_for_detection,
    trace_of,
)
from ylabcommon.ephys.sorted_trace import HDF_KEY, SortedRecording

SR_KHZ = 20
DT = 1.0 / (SR_KHZ * 1000)
PER_TRAIN = 4000
N_TRAINS = 3
N = PER_TRAIN * N_TRAINS


def _spiky(n=N, onsets=(2000, 6000, 10000), amp=60.0):
    t = np.arange(n) * DT
    y = np.zeros(n)
    for onset in onsets:
        tt = t[onset:] - t[onset]
        kernel = np.exp(-tt / 0.005) - np.exp(-tt / 0.001)
        y[onset:] += -amp * kernel / kernel.max()
    return y


def _write_raw(path: Path, groups=("train_param_000",), values=None,
               acquired=None, n_samples=None) -> Path:
    """取得直後の h5。``acquired`` / ``n_samples`` で途中停止を作れる。"""
    with h5py.File(path, "w") as f:
        for i, name in enumerate(groups):
            y = _spiky() if values is None else values
            if n_samples is not None:
                y = y[:n_samples]
            g = f.create_group(name)
            g.create_dataset("data", data=y[None, :].astype(np.float32))
            p = g.create_group("param")
            param = {"sampling_rate_in_kHz": SR_KHZ, "train_train_interval": 200,
                     "train_train_number": N_TRAINS,
                     "train_sample_number": PER_TRAIN, "config_name": "mEPSC.ini"}
            if acquired is not None:
                param["acquired_train_number"] = acquired
            for k, v in param.items():
                p.create_dataset(k, data=v)
    return path


def _write_sorted(path: Path) -> Path:
    y = _spiky()
    pd.DataFrame({
        "val": y, "time_ms": np.arange(N) / SR_KHZ,
        "train_idx": np.repeat(np.arange(N_TRAINS, dtype=np.uint16), PER_TRAIN),
        "rel_time_ms": np.tile(np.arange(PER_TRAIN) / SR_KHZ, N_TRAINS),
    }).to_hdf(path, key=HDF_KEY, mode="w", complevel=9)
    path.with_name(path.name[: -len("_df.h5")] + ".json").write_text(
        json.dumps({"param": [{"sampling_rate_in_kHz": SR_KHZ,
                               "config_name": "mEPSC.ini"}]}), encoding="utf-8")
    return path


# ---- 取得直後の h5 -------------------------------------------------------------------

def test_the_raw_h5_is_read_one_train_group_at_a_time(tmp_path):
    path = _write_raw(tmp_path / "raw.h5",
                      groups=("train_param_000", "train_param_001"))

    assert RawRecording.groups(path) == ["train_param_000", "train_param_001"]
    rec = RawRecording.open(path)                       # 既定は先頭
    assert rec.group == "train_param_000"
    assert RawRecording.open(path, "train_param_001").group == "train_param_001"

    assert rec.sampling_rate_khz == SR_KHZ
    assert rec.dt_s == pytest.approx(DT)
    assert rec.config_name == "mEPSC.ini"
    assert (rec.n_trains, rec.samples_per_train) == (N_TRAINS, PER_TRAIN)
    assert rec.train_indices == [0, 1, 2]
    assert rec.values().shape[0] == N
    assert rec.values(1).shape[0] == PER_TRAIN
    assert rec.duration_s() == pytest.approx(N * DT)


def test_a_recording_stopped_early_keeps_only_the_acquired_trains(tmp_path):
    """バッファは全長ぶん 0 埋めで確保される。読むと平坦な実測と区別できない。"""
    path = _write_raw(tmp_path / "raw.h5", acquired=2)

    rec = RawRecording.open(path)

    assert rec.n_trains == 2                            # 設定は 3 だが 2 しか取れていない
    assert rec.values().shape[0] == 2 * PER_TRAIN
    with pytest.raises(ValueError, match=r"No train 2 .*\[0, 1\]"):
        rec.values(2)


def test_a_recording_shorter_than_its_setting_is_measured_from_the_data(tmp_path):
    """acquired_train_number の無い古いファイルでも、入っている長さで数える。"""
    path = _write_raw(tmp_path / "raw.h5", n_samples=PER_TRAIN * 2)

    assert RawRecording.open(path).n_trains == 2


def test_a_raw_h5_without_a_sampling_rate_is_refused(tmp_path):
    path = tmp_path / "raw.h5"
    with h5py.File(path, "w") as f:
        g = f.create_group("train_param_000")
        g.create_dataset("data", data=np.zeros((1, 100), dtype=np.float32))
        g.create_group("param")

    with pytest.raises(ValueError, match="sampling_rate_in_kHz"):
        RawRecording.open(path).sampling_rate_khz


def test_an_unknown_group_and_a_missing_file_are_named(tmp_path):
    path = _write_raw(tmp_path / "raw.h5")
    with pytest.raises(ValueError, match="No train group 'nope'"):
        RawRecording.open(path, "nope")
    with pytest.raises(FileNotFoundError):
        RawRecording.open(tmp_path / "nothing.h5")


# ---- 両方を同じ形で開く -------------------------------------------------------------

def test_either_format_opens_through_one_entry_point(tmp_path):
    raw = _write_raw(tmp_path / "raw.h5")
    sorted_path = _write_sorted(tmp_path / "V-test_001_df.h5")

    assert is_sorted_output(sorted_path) and not is_sorted_output(raw)
    assert list_groups(raw) == ["train_param_000"]
    assert list_groups(sorted_path) == ["V-test_001_df.h5"]

    a, b = open_recording(raw), open_recording(sorted_path)
    assert isinstance(a, RawRecording) and isinstance(b, SortedRecording)
    # 同じ問いに同じ形で答える
    for rec in (a, b):
        assert rec.sampling_rate_khz == SR_KHZ
        assert (rec.n_trains, rec.samples_per_train) == (N_TRAINS, PER_TRAIN)
        assert rec.values().shape[0] == N
        assert rec.config_name == "mEPSC.ini"
    # 中身も同じ (同じ波形を 2 つの形で書いたので)
    np.testing.assert_allclose(a.values(), b.values(), rtol=1e-5)


def test_the_selection_says_which_trace_in_one_value(tmp_path):
    rec = open_recording(_write_raw(tmp_path / "raw.h5"))

    assert trace_of(rec).shape[0] == N                               # 既定は全 train
    assert trace_of(rec, TraceSelection()).shape[0] == N
    assert trace_of(rec, TraceSelection(train_idx=1)).shape[0] == PER_TRAIN
    averaged = trace_of(rec, TraceSelection(average_trains=True))
    assert averaged.shape[0] == PER_TRAIN
    np.testing.assert_allclose(
        averaged, rec.values().reshape(N_TRAINS, PER_TRAIN).mean(axis=0), rtol=1e-6)

    assert duration_s(rec) == pytest.approx(N * DT)
    assert duration_s(rec, TraceSelection(train_idx=0)) == pytest.approx(PER_TRAIN * DT)
    assert TraceSelection().describe() == "all trains concatenated"
    assert TraceSelection(train_idx=2).describe() == "train 2"
    assert TraceSelection(average_trains=True).describe() == "average of all trains"


def test_the_average_is_refused_for_detection_in_one_place(tmp_path):
    """画面も CLI も同じ言葉で断ること。平均するとランダムなイベントは消える。"""
    refuse_average_for_detection(TraceSelection())                   # 通る
    refuse_average_for_detection(TraceSelection(train_idx=0))
    with pytest.raises(ValueError, match="averaging removes"):
        refuse_average_for_detection(TraceSelection(average_trains=True))
    assert "mEPSC" in AVERAGE_REFUSAL

    rec = open_recording(_write_raw(tmp_path / "raw.h5"))
    with pytest.raises(ValueError, match="averaging removes"):
        analyse(rec, TraceSelection(average_trains=True))


def test_describe_tells_an_agent_what_it_needs_to_choose_a_trace(tmp_path):
    got = describe(open_recording(_write_raw(tmp_path / "raw.h5")))

    assert got["kind"] == "raw" and got["group"] == "train_param_000"
    assert got["sampling_rate_khz"] == SR_KHZ
    assert (got["n_trains"], got["samples_per_train"]) == (N_TRAINS, PER_TRAIN)
    assert got["n_samples"] == N
    assert got["duration_s"] == pytest.approx(N * DT)
    assert got["train_indices"] == [0, 1, 2]
    assert got["config_name"] == "mEPSC.ini"
    assert json.loads(json.dumps(got))                               # そのまま --json に出せる

    assert describe(open_recording(_write_sorted(tmp_path / "a_df.h5")))["kind"] == "sorted"


# ---- 手順 ----------------------------------------------------------------------------

def test_the_procedure_runs_the_same_way_on_both_formats(tmp_path):
    raw = run_detection(_write_raw(tmp_path / "raw.h5"))
    sorted_result = run_detection(_write_sorted(tmp_path / "V-test_001_df.h5"))

    for result in (raw, sorted_result):
        assert result.n_included >= 1
        assert result.train_idx is None
        assert result.analysed_seconds == pytest.approx(N * DT)
        assert result.frequency_hz == pytest.approx(
            result.n_included / result.analysed_seconds)
        assert result.config_name == "mEPSC.ini"
    assert raw.source_file == "train_param_000"
    assert sorted_result.source_file == "V-test_001_df.h5"
    assert raw.n_included == sorted_result.n_included      # 同じ波形なので同じ結果


def test_the_procedure_records_the_selection_and_the_display_filter(tmp_path):
    path = _write_raw(tmp_path / "raw.h5")

    result = run_detection(path, selection=TraceSelection(train_idx=0),
                           params=DetectionParams(amplitude_threshold_pa=25.0),
                           display_cutoff_hz=1000.0)

    assert result.train_idx == 0
    assert result.analysed_seconds == pytest.approx(PER_TRAIN * DT)
    assert result.display_cutoff_hz == 1000.0            # 振幅に効くので残す
    assert result.params["amplitude_threshold_pa"] == 25.0

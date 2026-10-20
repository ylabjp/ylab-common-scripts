"""patch-clamp mode の記録の形: 書いたものがそのまま読め、既存の読み口でも開けること。"""
from __future__ import annotations

import math

import h5py
import numpy as np
import pytest

from ylabcommon.ephys.patch_recording import (
    PATCH_GROUP,
    PULSE_COLUMNS,
    TRAIN_GROUP,
    WRITING_SUFFIX,
    PatchRecording,
    PatchSessionWriter,
)
from ylabcommon.ephys.raw_trace import RawRecording
from ylabcommon.ephys.recording import describe, list_groups, open_recording
from ylabcommon.ephys.test_pulse import (
    TestPulseParams,
    TestPulseTracker,
    simulate_response,
)

FS = 20000.0


def _write_session(path, n_chunks=4, with_video=True):
    params = TestPulseParams(delta_v_mv=5.0, frequency_hz=20.0)
    y = simulate_response(FS, 0.25 * n_chunks, params, rs_mohm=20.0, rm_mohm=200.0,
                          cm_pf=100.0, holding_pa=-50.0, noise_pa=2.0, phase=0.3)
    attrs = {"camera_id": "EMU0", "fps": 20, "width": 64, "height": 48, "bit_depth": 12,
             "test_pulse_mv": 5.0, "test_pulse_hz": 20.0, "stem": "cell01",
             "video_file": "x.avi" if with_video else "",
             "test_pulse_params": params.as_record()}
    w = PatchSessionWriter(path, FS, 1000.0, attrs=attrs)
    w.set_sync_origin(ai_start_perf_s=100.0, ai_start_unix_s=1.7e9)
    tracker = TestPulseTracker(FS, params)
    frame_index = 0
    for c in range(n_chunks):
        chunk = y[c * 5000:(c + 1) * 5000]
        w.append_current(chunk, chunk_end_perf_s=100.0 + 0.25 * (c + 1))
        w.append_pulses(tracker.feed(chunk))
        for k in range(5):                      # 20 fps -> 5 frames per 250 ms
            t = 100.0 + 0.25 * c + 0.05 * k
            idx = w.append_frame(perf_s=t, unix_s=1.7e9 + (t - 100.0),
                                 frame_count=frame_index + 1, hw_ns=None if k == 0 else 1000 * k,
                                 video_index=frame_index)
            assert idx == frame_index
            w.append_tip(idx, 10.0 + k, 20.0, None, 0.5)
            frame_index += 1
    w.append_tip(frame_index - 1, None, None, None, None)
    w.add_event(100.4, 8000, "dimple", "light positive pressure")
    w.set_target_cell(30.0, 25.0, 100.1)
    return w, y


def test_round_trip(tmp_path):
    path = tmp_path / "20260101-000000-patch-cell01.h5"
    w, y = _write_session(path)
    assert (tmp_path / (path.name + WRITING_SUFFIX)).exists()
    final = w.close()
    assert final == path and path.exists()
    assert not (tmp_path / (path.name + WRITING_SUFFIX)).exists()

    rec = PatchRecording.open(path)
    assert rec.n_samples == 20000
    np.testing.assert_allclose(rec.current_pa, y, atol=1e-3)   # float32 で書く
    assert rec.sampling_rate_hz == FS
    assert rec.n_frames == 20
    assert rec.attrs["camera_id"] == "EMU0"
    assert rec.attrs["test_pulse_params"]        # json 文字列として残る
    assert rec.video_path == tmp_path / "x.avi"
    assert rec.sync["ai_start_perf_s"] == 100.0
    assert list(rec.sync["chunk_end_sample"]) == [5000, 10000, 15000, 20000]
    # フレーム -> 標本: perf_s の差 x fs
    idx = rec.frame_sample_index()
    assert idx[0] == 0 and idx[1] == 1000 and idx[-1] == 19000
    assert rec.frame_at_sample(1020) == 1
    # hw_ns が無いフレームは -1
    assert rec.camera["frame_hw_ns"][0] == -1 and rec.camera["frame_hw_ns"][1] == 1000
    # seal test の表
    assert set(PULSE_COLUMNS) <= set(rec.test_pulse)
    n_pulses = rec.test_pulse["onset_idx"].shape[0]
    assert n_pulses == 20                          # 1 s @ 20 Hz
    assert rec.test_pulse["transient_detected"].dtype == np.uint8
    assert np.all(rec.test_pulse["transient_detected"] == 1)
    assert np.median(rec.test_pulse["cm_pf"]) == pytest.approx(100.0, rel=0.06)
    # 先端: None は NaN
    assert rec.tip["x_px"].shape[0] == 21
    assert math.isnan(rec.tip["x_px"][-1]) and rec.tip["x_px"][0] == 10.0
    assert rec.events == [{"perf_s": 100.4, "sample": 8000, "label": "dimple",
                           "note": "light positive pressure"}]
    assert rec.target_cell == {"x_px": 30.0, "y_px": 25.0, "perf_s": 100.1}
    d = rec.describe()
    assert d["n_pulses"] == 20 and d["n_frames"] == 20 and d["n_tip_estimates"] == 20
    assert d["frame_interval_ms_median"] == pytest.approx(50.0)
    assert d["video_present"] is False


def test_existing_readers_open_the_current_as_one_train(tmp_path):
    """viewer / scli info / RawRecording が /patch を train と間違えないこと。"""
    path = tmp_path / "s.h5"
    w, y = _write_session(path, n_chunks=2)
    w.close()
    assert list_groups(path) == [TRAIN_GROUP]
    rec = open_recording(path)
    assert rec.n_trains == 1
    assert rec.samples_per_train == 10000
    assert rec.config_name == "patch-clamp-mode"
    assert describe(rec)["duration_s"] == pytest.approx(0.5)
    np.testing.assert_allclose(rec.values(0), y, atol=1e-3)
    with h5py.File(path, "r") as f:
        assert PATCH_GROUP in f


def test_unfinished_recording_stays_readable_under_writing_name(tmp_path):
    path = tmp_path / "s.h5"
    w, y = _write_session(path, n_chunks=2)
    w.flush()
    w.abort()
    writing = tmp_path / ("s.h5" + WRITING_SUFFIX)
    assert writing.exists() and not path.exists()
    rec = PatchRecording.open(writing)
    assert rec.n_samples == 10000
    # train_sample_number が無いので全長を 1 train とみなす
    raw = RawRecording.open(writing, TRAIN_GROUP)
    assert raw.n_trains == 1 and raw.samples_per_train == 10000


def test_is_patch_file_and_refusals(tmp_path):
    plain = tmp_path / "plain.h5"
    with h5py.File(plain, "w") as f:
        g = f.create_group(TRAIN_GROUP)
        g.create_dataset("data", data=np.zeros((1, 10), dtype=np.float32))
        g.create_group("param").create_dataset("sampling_rate_in_kHz", data=20)
    assert not PatchRecording.is_patch_file(plain)
    with pytest.raises(ValueError):
        PatchRecording.open(plain)
    with pytest.raises(FileNotFoundError):
        PatchRecording.open(tmp_path / "missing.h5")
    path = tmp_path / "s.h5"
    _write_session(path, n_chunks=1)[0].close()
    assert PatchRecording.is_patch_file(path)

"""seal test (テストパルス) の位置決めと Rs / Rm / Cm の測定。

期待値は :func:`simulate_response` の回路の **真値** で、測り方の精度 (フィルタで
削れたピーク、定常に届かない 50 Hz) が許す幅を見ている。
"""
from __future__ import annotations

import numpy as np
import pytest

from ylabcommon.ephys.test_pulse import (
    PHASE_APPROACH,
    PHASE_BATH,
    PHASE_NONE,
    PHASE_SEALED,
    PHASE_WHOLE_CELL,
    PhaseThresholds,
    TestPulseParams,
    TestPulseTracker,
    analyse_pulse,
    analyse_trace,
    classify_phase,
    fold_profile,
    locate_pulses,
    simulate_response,
    summarize,
)

FS = 20000.0
P20 = TestPulseParams(delta_v_mv=5.0, frequency_hz=20.0)


def _whole_cell(duration_s=1.0, phase=0.37, noise=3.0, params=P20, **kw):
    kw.setdefault("rs_mohm", 20.0)
    kw.setdefault("rm_mohm", 200.0)
    kw.setdefault("cm_pf", 100.0)
    kw.setdefault("holding_pa", -50.0)
    return simulate_response(FS, duration_s, params, noise_pa=noise, filter_tau_ms=0.05,
                             phase=phase, **kw)


# --- 位置決め ------------------------------------------------------------------

def test_locate_finds_every_pulse_at_the_right_phase():
    y = _whole_cell()
    w = locate_pulses(y, FS, P20)
    assert len(w) == 20                     # 1 s @ 20 Hz
    # phase 0.37 x 1000 標本 = 370 から 1000 ごと
    np.testing.assert_array_equal(w.onsets, 370 + 1000 * np.arange(20))
    assert w.measured_period_ms == pytest.approx(50.0, abs=0.1)
    assert w.edge_resolved.all()


def test_locate_returns_nothing_on_noise():
    y = np.random.default_rng(1).normal(0.0, 3.0, 20000)
    w = locate_pulses(y, FS, P20)
    assert len(w) == 0
    assert w.step_snr < P20.min_step_snr


def test_locate_works_on_a_giga_seal_step_of_a_few_pa():
    """seal 中のステップ (5 mV / 1.5 GΩ = 3.3 pA) は 1 標本では雑音に埋もれるが、
    折り畳めば位相が取れる。立ち上がりは微分では見えないので周期は測れない。"""
    y = simulate_response(FS, 1.0, P20, rs_mohm=0.0, rm_mohm=1500.0, cm_pf=0.0,
                          holding_pa=-60.0, noise_pa=3.0, phase=0.2)
    w = locate_pulses(y, FS, P20)
    assert len(w) == 20
    assert np.abs(w.onsets - (200 + 1000 * np.arange(20))).max() <= 3
    assert w.measured_period_ms is None
    assert not w.edge_resolved.any()


def test_locate_refuses_a_zero_step_and_a_negative_frequency():
    y = _whole_cell()
    with pytest.raises(ValueError):
        locate_pulses(y, FS, TestPulseParams(delta_v_mv=0.0, frequency_hz=20.0))
    with pytest.raises(ValueError):
        locate_pulses(y, FS, TestPulseParams(delta_v_mv=5.0, frequency_hz=0.0))


def test_fold_profile_keeps_the_square_shape():
    y = simulate_response(FS, 0.5, P20, rs_mohm=10.0, rm_mohm=0.0, cm_pf=0.0, phase=0.0)
    profile, counts = fold_profile(y, FS, 20.0)
    assert profile.shape == (1000,)
    assert counts.min() == 10
    assert profile[:500].mean() == pytest.approx(500.0, abs=1e-6)   # 5 mV / 10 MΩ
    assert profile[500:].mean() == pytest.approx(0.0, abs=1e-6)


# --- 測定 ----------------------------------------------------------------------

def test_whole_cell_values_match_the_circuit():
    y = _whole_cell()
    metrics, _ = analyse_trace(y, FS, P20)
    s = summarize(metrics)
    assert s["n_pulses"] == 20
    assert s["holding_pa"] == pytest.approx(-50.0, abs=1.0)
    assert s["r_total_mohm"] == pytest.approx(220.0, rel=0.03)
    # 生のピークはフィルタで削れるので Rs は少し大きく出る (手順書の定義のまま)。
    assert 19.0 < s["rs_mohm"] < 24.0
    # 外挿したピークは真値に戻る。
    assert s["rs_extrapolated_mohm"] == pytest.approx(20.0, rel=0.06)
    assert s["rm_mohm"] == pytest.approx(200.0, rel=0.05)
    assert s["cm_pf"] == pytest.approx(100.0, rel=0.05)
    assert s["cm_from_tau_pf"] == pytest.approx(100.0, rel=0.08)
    # τ = Cm Rs Rm / Rt = 100 pF x 18.18 MΩ = 1.82 ms
    assert s["tau_ms"] == pytest.approx(1.82, rel=0.1)
    assert classify_phase(s) == PHASE_WHOLE_CELL


def test_negative_step_and_50_hz_are_handled():
    p = TestPulseParams(delta_v_mv=-5.0, frequency_hz=50.0)
    y = simulate_response(FS, 1.0, p, noise_pa=3.0, filter_tau_ms=0.05, phase=0.1,
                          rs_mohm=30.0, rm_mohm=400.0, cm_pf=60.0, holding_pa=-50.0)
    metrics, w = analyse_trace(y, FS, p)
    # 50 発のうち最初の 1 発は、ベースライン (直前の半周期) が配列の外なので測らない。
    assert len(w) == 50 and len(metrics) == 49
    assert w.measured_period_ms == pytest.approx(20.0, abs=0.1)
    s = summarize(metrics)
    assert s["holding_pa"] == pytest.approx(-50.0, abs=2.0)
    assert s["cm_pf"] == pytest.approx(60.0, rel=0.08)
    assert s["rs_extrapolated_mohm"] == pytest.approx(30.0, rel=0.08)
    # 10 ms のパルスでは定常に届ききらないので R_total は小さめに出る
    # (手順書が「Rs が大きいときは 20 Hz」と言う理由)。真値 430 MΩ。
    assert 330.0 < s["r_total_mohm"] <= 430.0


def test_bath_and_seal_report_no_rs_or_cm():
    """過渡の無い応答に Rs と Cm は無い。雑音の最大値から作った数字を出さない。"""
    bath = simulate_response(FS, 1.0, P20, rs_mohm=6.5, rm_mohm=0.0, cm_pf=0.0,
                             noise_pa=3.0, phase=0.5)
    metrics, _ = analyse_trace(bath, FS, P20)
    s = summarize(metrics)
    assert s["r_total_mohm"] == pytest.approx(6.5, rel=0.02)
    assert s["rs_mohm"] is None and s["cm_pf"] is None and s["tau_ms"] is None
    assert not any(m.transient_detected for m in metrics)
    assert classify_phase(s) == PHASE_BATH

    seal = simulate_response(FS, 1.0, P20, rs_mohm=0.0, rm_mohm=1500.0, cm_pf=0.0,
                             holding_pa=-60.0, noise_pa=3.0, phase=0.2)
    metrics, _ = analyse_trace(seal, FS, P20)
    s = summarize(metrics)
    assert s["r_total_mohm"] == pytest.approx(1500.0, rel=0.15)
    assert s["holding_pa"] == pytest.approx(-60.0, abs=1.0)
    assert s["rs_mohm"] is None
    assert classify_phase(s) == PHASE_SEALED


def test_wrong_sign_setting_gives_no_pulse_metrics():
    """符号が逆の設定では ΔI が負になる。符号を黙って直さず、そのパルスは測らない。"""
    y = simulate_response(FS, 0.5, P20, rs_mohm=10.0, rm_mohm=0.0, cm_pf=0.0, phase=0.0)
    wrong = TestPulseParams(delta_v_mv=-5.0, frequency_hz=20.0)
    # 対称な方形波では、位置決めは符号に合わせて半周期を選び直してしまう (どちらの
    # 半周期も「ステップ」である) ので、ここでは **本来のパルスの窓** y[1000:1500]
    # (電流が高い側) を逆符号で測り、None になることを直接見る。
    assert analyse_pulse(y, FS, 1000, 1500, P20) is not None
    assert analyse_pulse(y, FS, 1000, 1500, wrong) is None


def test_summary_of_nothing_is_none_not_zero():
    s = summarize([])
    assert s["n_pulses"] == 0
    assert s["r_total_mohm"] is None
    assert classify_phase(s) == PHASE_NONE


def test_phase_thresholds_come_from_the_manual_where_they_exist():
    th = PhaseThresholds()
    assert th.seal_min_mohm == 1000.0      # patch-clamp manual "Giga seal" 17.
    assert th.rs_max_mohm == 35.0          # patch-clamp manual "Whole cell"
    assert classify_phase({"n_pulses": 5, "r_total_mohm": 120.0, "cm_pf": None}) == PHASE_APPROACH


# --- チャンクで渡す (リアルタイム) ----------------------------------------------

def test_tracker_counts_each_pulse_once_across_chunk_boundaries():
    y = _whole_cell(duration_s=2.0)
    tracker = TestPulseTracker(FS, P20)
    got = []
    for c in range(8):                      # 250 ms ずつ (取得側の REFRESH と同じ)
        got += tracker.feed(y[c * 5000:(c + 1) * 5000])
    assert tracker.samples_seen == 40000
    onsets = [m.onset_idx for m in got]
    assert onsets == sorted(set(onsets))
    assert onsets == [370 + 1000 * k for k in range(40)]
    assert summarize(got)["cm_pf"] == pytest.approx(100.0, rel=0.05)


def test_tracker_is_silent_before_the_test_pulse_starts():
    tracker = TestPulseTracker(FS, P20)
    quiet = np.random.default_rng(2).normal(-50.0, 3.0, 5000)
    assert tracker.feed(quiet) == []
    assert tracker.feed(quiet) == []
    y = _whole_cell(duration_s=0.5)
    got = tracker.feed(y[:5000]) + tracker.feed(y[5000:])
    assert len(got) == 10
    assert all(m.onset_idx >= 10000 for m in got)

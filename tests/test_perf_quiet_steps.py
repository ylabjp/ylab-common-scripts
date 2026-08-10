"""一瞬で終わった工程を端末に出さないことのテスト。

取り込み1件で ``timed_step`` は十数回走るが、そのほとんどは 0.0 s で終わる。
開始・完了の2行が対で並ぶだけの工程が端末を埋めると、本当に時間を使っている
工程が押し流されて「どこが遅いか」が読めなくなる。

ただし端末を静かにするのと、記録を捨てるのは別の話である。
「いつから遅くなったか」の比較や工程別の集計には短い工程の記録も要るので、
Better Stack への送信は落とさない。ここではその2つが分かれていることを固定する。
"""
from __future__ import annotations

import time

import pytest

import ylabcommon.utils.perf as perf


@pytest.fixture
def sink(monkeypatch, capsys):
    """送信側を捕まえる。端末側は capsys が拾う。"""
    sent = []

    def _info(message, *, console=True, **fields):
        sent.append(("info", message, fields))
        if console:
            print("[INFO] %s" % message)

    def _warn(message, *, console=True, **fields):
        sent.append(("warning", message, fields))
        if console:
            print("[WARNING] %s" % message)

    monkeypatch.setattr(perf, "log_info", _info)
    monkeypatch.setattr(perf, "log_warning", _warn)
    return sent


def _events(sent, event):
    return [f for _lvl, _m, f in sent if f.get("event") == event]


# ---- 短い工程 ----------------------------------------------------------------

def test_a_fast_step_prints_nothing_but_is_still_recorded(sink, capsys):
    with perf.timed_step("quick", target="x"):
        pass

    assert capsys.readouterr().out == ""
    # 記録は残る (開始・完了とも)
    assert len(_events(sink, "start")) == 1
    done = _events(sink, "done")
    assert len(done) == 1
    assert done[0]["duration_sec"] < perf.QUIET_UNDER_SEC
    assert done[0]["target"] == "x"


def test_many_fast_steps_stay_silent(sink, capsys):
    """取り込み1件ぶんの工程数を流しても端末は空のまま。"""
    for i in range(12):
        with perf.timed_step("step%d" % i):
            pass
    assert capsys.readouterr().out == ""
    assert len(_events(sink, "done")) == 12


# ---- 長い工程 ----------------------------------------------------------------

def test_a_slow_step_prints_its_completion(sink, capsys, monkeypatch):
    """しきい値を超えたら完了行が出る。"""
    monkeypatch.setattr(perf, "QUIET_UNDER_SEC", 0.05)

    with perf.timed_step("slow"):
        time.sleep(0.08)

    assert "step done: slow" in capsys.readouterr().out


def test_a_finished_step_does_not_print_its_start_line_afterwards(
        sink, capsys, monkeypatch):
    """完了時に開始行を遡って出さない。

    回帰: 工程が終わってから "step start" が現れると、その工程自身が出力した行より
    **後ろ** に並ぶ。実際のログでは DEBUG の出力群のあとに
    "step start: thorlab.stack" が出て、読み順が壊れていた。
    完了行が工程名と所要時間を持っているので、開始行が要るのは
    「まだ終わっていない」あいだだけ。
    """
    monkeypatch.setattr(perf, "QUIET_UNDER_SEC", 0.05)

    with perf.timed_step("slow"):
        print("...work output...")
        time.sleep(0.08)

    out = capsys.readouterr().out
    assert "step start: slow" not in out
    assert out.index("...work output...") < out.index("step done: slow")


def test_a_failure_always_names_the_step_however_fast(sink, capsys):
    """失敗は所要時間によらず出す (どこで落ちたかは常に要る)。"""
    with pytest.raises(ValueError):
        with perf.timed_step("boom"):
            raise ValueError("nope")

    out = capsys.readouterr().out
    assert "step start: boom" in out
    assert "step failed: boom" in out
    assert _events(sink, "failed")[0]["error_type"] == "ValueError"


def test_a_progress_log_pulls_the_start_line_in_first(sink, capsys):
    """進捗が出るほど長い工程は、進捗の前に開始行が出る。

    開始行だけ遅れて出ると「進捗の後に開始した」ように読めてしまう。
    """
    with perf.timed_step("loop", total=3, progress_interval_sec=0) as step:
        step.advance(item="a")

    out = capsys.readouterr().out
    assert "step start: loop" in out
    assert out.index("step start: loop") < out.index("step progress: loop")


def test_a_heartbeat_pulls_the_start_line_in_too(sink, capsys, monkeypatch):
    """止まった工程は heartbeat が出る前に開始行を出す。

    heartbeat しか出ないと、端末には「入った」記録が無いまま経過だけが並ぶ。
    """
    monkeypatch.setattr(perf, "_hb_started_at", time.perf_counter())

    with perf.timed_step("hung"):
        perf._emit_heartbeat(stall_after_sec=0)

    out = capsys.readouterr().out
    assert "step start: hung" in out
    assert "heartbeat: hung" in out
    assert out.index("step start: hung") < out.index("heartbeat: hung")


def test_the_start_line_is_printed_only_once(sink, capsys, monkeypatch):
    """開始行を引き出す契機が複数あっても重複しない。"""
    monkeypatch.setattr(perf, "QUIET_UNDER_SEC", 0.0)
    monkeypatch.setattr(perf, "_hb_started_at", time.perf_counter())

    with perf.timed_step("once", total=2, progress_interval_sec=0) as step:
        step.advance(item="a")
        perf._emit_heartbeat(stall_after_sec=0)

    assert capsys.readouterr().out.count("step start: once") == 1


# ---- 送信側は静かにしない ----------------------------------------------------

def test_console_silence_does_not_reach_better_stack(sink, capsys):
    """端末に出さない工程も、送信側には start と done が両方届く。"""
    with perf.timed_step("quiet", target="v:/img01", total=5) as step:
        step.advance(n=5, item="v:/img01/f.tif")

    assert capsys.readouterr().out == ""
    start = _events(sink, "start")[0]
    done = _events(sink, "done")[0]
    assert start["total"] == 5 and start["target"] == "v:/img01"
    assert done["done"] == 5
    assert "duration_sec" in done

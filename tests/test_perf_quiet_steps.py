"""一瞬で終わった工程を報告しないことのテスト。

取り込み1件で ``timed_step`` は十数回走るが、そのほとんどは 0.0 s で終わる。
1工程につき開始と完了の2件を送っていたので、1.4 秒の読み込みだけで 26 件が
Better Stack に並び、時間を使った工程がその中に埋もれていた。

残すのは「時間を使った工程」と「失敗した工程」だけにする。

- 開始のログは送らない。実行中の工程は heartbeat がレジストリを読んで名指しし、
  完了ログは所要時間を持っているので開始時刻はそこから引ける
- 完了は ``QUIET_UNDER_SEC`` 以上かかったときだけ
- 失敗は所要時間によらず必ず (どこで落ちたかが唯一の手がかり)
- 進捗と heartbeat はそれぞれの間隔で間引かれるのでそのまま

端末と Better Stack で別の規則にしない。読み手が違うだけで「何が意味のある
記録か」は同じであり、規則が2つあると片方だけ直して食い違う。
"""
from __future__ import annotations

import time

import pytest

import ylabcommon.utils.perf as perf


@pytest.fixture(autouse=True)
def clean_registry():
    perf._active_steps.clear()
    yield
    perf._active_steps.clear()


@pytest.fixture
def sink(monkeypatch, capsys):
    """送信側を捕まえる。端末側は capsys が拾う。"""
    sent = []

    def _info(message, **fields):
        sent.append(("info", message, fields))
        print("[INFO] %s" % message)

    def _warn(message, **fields):
        sent.append(("warning", message, fields))
        print("[WARNING] %s" % message)

    monkeypatch.setattr(perf, "log_info", _info)
    monkeypatch.setattr(perf, "log_warning", _warn)
    return sent


def _events(sent, event):
    return [f for _lvl, _m, f in sent if f.get("event") == event]


# ---- 短い工程は残さない --------------------------------------------------------

def test_a_fast_step_is_not_reported_at_all(sink, capsys):
    with perf.timed_step("quick", target="x"):
        pass

    assert sink == []
    assert capsys.readouterr().out == ""


def test_many_fast_steps_stay_silent(sink, capsys):
    """取り込み1件ぶんの工程数を流しても1件も残らない。"""
    for i in range(12):
        with perf.timed_step("step%d" % i):
            pass

    assert sink == []
    assert capsys.readouterr().out == ""


def test_no_start_event_is_sent_even_for_a_slow_step(sink, monkeypatch):
    """長い工程でも開始は送らない (1工程1件)。"""
    monkeypatch.setattr(perf, "QUIET_UNDER_SEC", 0.05)

    with perf.timed_step("slow"):
        time.sleep(0.08)

    assert _events(sink, "start") == []
    assert len(_events(sink, "done")) == 1


# ---- 長い工程・失敗は残す ------------------------------------------------------

def test_a_slow_step_is_reported_with_its_duration(sink, capsys, monkeypatch):
    monkeypatch.setattr(perf, "QUIET_UNDER_SEC", 0.05)

    with perf.timed_step("slow", target="v:/img01"):
        time.sleep(0.08)

    done = _events(sink, "done")[0]
    assert done["step"] == "slow"
    assert done["target"] == "v:/img01"
    assert done["duration_sec"] >= 0.05
    assert "step done: slow" in capsys.readouterr().out


def test_a_failure_is_reported_however_fast(sink, capsys):
    """失敗は所要時間によらず出す (どこで落ちたかは常に要る)。"""
    with pytest.raises(ValueError):
        with perf.timed_step("boom"):
            raise ValueError("nope")

    failed = _events(sink, "failed")
    assert len(failed) == 1
    assert failed[0]["error_type"] == "ValueError"
    assert "step failed: boom" in capsys.readouterr().out


def test_a_failure_keeps_the_progress_it_had_made(sink):
    """途中まで進んでいたなら、その件数も一緒に残す。"""
    with pytest.raises(RuntimeError):
        with perf.timed_step("thorlab.open_tiffs", total=100) as step:
            step.advance(n=7, item="v:/img01/f7.tif")
            raise RuntimeError("drive went away")

    failed = _events(sink, "failed")[0]
    assert failed["done"] == 7
    assert failed["item"].endswith("f7.tif")


# ---- 進捗と heartbeat はそのまま ----------------------------------------------

def test_progress_is_still_reported_for_a_long_loop(sink):
    """短い工程を落としても、長いループの進捗は残る。"""
    with perf.timed_step("loop", total=3, progress_interval_sec=0) as step:
        step.advance(item="a")

    progress = _events(sink, "progress")
    assert len(progress) == 1
    assert progress[0]["item"] == "a"


def test_a_running_step_is_still_named_by_the_heartbeat(sink, monkeypatch):
    """開始ログを送らなくても、実行中の工程は heartbeat が名指しできる。

    これが「開始を送らなくてよい」根拠なので、直接確かめる。
    """
    monkeypatch.setattr(perf, "_hb_started_at", time.perf_counter())

    with perf.timed_step("hung", target="v:/img01"):
        perf._emit_heartbeat(stall_after_sec=1e-9)   # 即座に「停止」とみなす

    beats = _events(sink, "heartbeat")
    assert len(beats) == 1
    assert beats[0]["step"] == "hung"
    assert beats[0]["target"] == "v:/img01"
    assert beats[0]["stalled"] is True


def test_the_heartbeat_names_the_whole_chain(sink, monkeypatch):
    """入れ子の工程は「どこの何か」が1行で分かる形で出る。"""
    monkeypatch.setattr(perf, "_hb_started_at", time.perf_counter())

    with perf.timed_step("session", target="v:/day1"):
        with perf.timed_step("load_image"):
            with perf.timed_step("thorlab.stack"):
                perf._emit_heartbeat(stall_after_sec=3600)

    beats = _events(sink, "heartbeat")
    assert beats[0]["step_stack"] == "session > load_image > thorlab.stack"


# ---- 端末と送信で規則を分けない ------------------------------------------------

def test_the_console_and_better_stack_keep_the_same_events(sink, capsys,
                                                           monkeypatch):
    """出す/出さないの判断は端末と送信で同じ。

    片方だけ静かにすると、端末で見えないものが Better Stack には溜まる
    (あるいはその逆) という食い違いが生まれ、どちらが本当か分からなくなる。
    """
    monkeypatch.setattr(perf, "QUIET_UNDER_SEC", 0.05)

    with perf.timed_step("fast"):
        pass
    with perf.timed_step("slow"):
        time.sleep(0.08)

    out = capsys.readouterr().out
    steps_sent = {f["step"] for _l, _m, f in sink}
    assert steps_sent == {"slow"}
    assert "slow" in out and "fast" not in out

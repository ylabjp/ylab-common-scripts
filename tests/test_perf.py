"""perf (工程計測 + 生存確認) のテスト。

sorter が「途中で止まる」ときに Better Stack だけを見て原因の場所を特定できること、
がこの機能の目的。したがってここでは次の3点を固定する。

1. 工程の開始・完了・失敗と所要時間が構造化フィールドで出ること。
2. 長いループの進捗 (何件目・どの item・速度) が出ること。
3. **沈黙してしまった実行でも** 生存確認が実行中の工程を送り続け、進捗が止まれば
   warning へ格上げされること (完了ログを待つ仕組みだけでは停止を検出できない)。
"""
from __future__ import annotations

import threading
import time

import pytest

import ylabcommon.utils.perf as perf


@pytest.fixture(autouse=True)
def clean_registry():
    """実行中工程レジストリはプロセス共有なので、テスト間で持ち越さない。"""
    perf._active_steps.clear()
    yield
    perf._active_steps.clear()
    perf.stop_heartbeat()


@pytest.fixture
def sent(monkeypatch):
    """log_info / log_warning を差し替えて、送信内容を (level, message, fields) で集める。"""
    records = []
    monkeypatch.setattr(
        perf, "log_info", lambda msg, **f: records.append(("info", msg, f))
    )
    monkeypatch.setattr(
        perf, "log_warning", lambda msg, **f: records.append(("warning", msg, f))
    )
    return records


def events(records):
    return [f.get("event") for _l, _m, f in records]


# ---- timed_step: 正常系 ------------------------------------------------------

def test_timed_step_does_not_log_until_the_body_finishes(sent, monkeypatch):
    """開始ログは送らない (1工程1件に絞る)。

    以前は開始と完了の2件を送っており、取り込み1件で 26 件が並んで、時間を使った
    工程がその中に埋もれていた。実行中であることは heartbeat がレジストリを読んで
    名指しするので、開始ログが無くても「どこで止まったか」は分かる。
    """
    during = []

    monkeypatch.setattr(perf, "QUIET_UNDER_SEC", 0.0)
    with perf.timed_step("load_image"):
        during.append(len(sent))

    assert during == [0], "本体の実行中に送っている"
    assert events(sent) == ["done"]


def test_timed_step_reports_duration_on_success(sent, monkeypatch):
    monkeypatch.setattr(perf, "QUIET_UNDER_SEC", 0.0)
    with perf.timed_step("z_projection"):
        pass

    level, message, fields = sent[-1]
    assert level == "info"
    assert fields["step"] == "z_projection"
    assert fields["event"] == "done"
    assert isinstance(fields["duration_sec"], float)
    assert fields["duration_sec"] >= 0
    assert "z_projection" in message


def test_timed_step_passes_extra_fields_to_the_log(sent):
    """集計時に時間をサイズで正規化できるよう、付加フィールドを載せる。"""
    with perf.timed_step("drift3d", target="/mnt/v/raw/x", n_bytes=1234):
        pass

    for _level, _msg, fields in sent:
        assert fields["target"] == "/mnt/v/raw/x"
        assert fields["n_bytes"] == 1234


def test_timed_step_measures_elapsed_time(monkeypatch, sent):
    """duration_sec は実際の経過時間 (perf_counter の差) から出す。"""
    ticks = iter([100.0, 112.5])
    monkeypatch.setattr(perf.time, "perf_counter", lambda: next(ticks))

    with perf.timed_step("session"):
        pass

    assert sent[-1][2]["duration_sec"] == pytest.approx(12.5)


# ---- timed_step: 失敗系 ------------------------------------------------------

def test_timed_step_reports_duration_on_failure_and_reraises(sent):
    """失敗しても所要時間を残し、例外はそのまま送出する(呼び出し側のexceptが処理する)。"""
    with pytest.raises(MemoryError):
        with perf.timed_step("save_tiff", target="t"):
            raise MemoryError("too big")

    level, message, fields = sent[-1]
    assert level == "warning"
    assert fields["event"] == "failed"
    assert fields["error_type"] == "MemoryError"
    assert isinstance(fields["duration_sec"], float)
    assert "save_tiff" in message


def test_timed_step_records_keyboard_interrupt(sent):
    """Ctrl-C など BaseException でも「どの工程で止めたか」を残す。"""
    with pytest.raises(KeyboardInterrupt):
        with perf.timed_step("session"):
            raise KeyboardInterrupt

    assert sent[-1][2]["event"] == "failed"
    assert sent[-1][2]["error_type"] == "KeyboardInterrupt"


def test_timed_step_does_not_emit_done_when_the_body_fails(sent):
    """失敗を成功として数えない(集計で所要時間の分布が歪むため)。"""
    with pytest.raises(ValueError):
        with perf.timed_step("median_blur"):
            raise ValueError

    assert events(sent) == ["failed"]


# ---- 実行中工程レジストリ ----------------------------------------------------

def test_active_steps_tracks_the_running_stack(sent):
    """生存確認が「今どこにいるか」を言えるよう、入れ子の工程がスタックとして見える。"""
    seen = []

    with perf.timed_step("session"):
        with perf.timed_step("load_image"):
            with perf.timed_step("thorlab.stack"):
                (_ident, stack), = perf.active_steps()
                seen = [s.name for s in stack]

    assert seen == ["session", "load_image", "thorlab.stack"]
    assert perf.active_steps() == []


def test_active_steps_is_cleaned_up_when_a_step_raises(sent):
    """例外で内側を飛び越えても取り残さない(古い工程が居座ると停止を誤検出する)。"""
    with pytest.raises(ValueError):
        with perf.timed_step("session"):
            with perf.timed_step("load_image"):
                raise ValueError

    assert perf.active_steps() == []


def test_active_steps_are_tracked_per_thread(sent):
    """別スレッドの工程は互いのスタックを汚さない。"""
    ready, done = threading.Event(), threading.Event()

    def worker():
        with perf.timed_step("worker_step"):
            ready.set()
            done.wait(timeout=5)

    t = threading.Thread(target=worker)
    t.start()
    ready.wait(timeout=5)
    try:
        with perf.timed_step("main_step"):
            by_thread = {
                ident: [s.name for s in stack] for ident, stack in perf.active_steps()
            }
    finally:
        done.set()
        t.join(timeout=5)

    assert sorted(by_thread.values()) == [["main_step"], ["worker_step"]]


# ---- 進捗 (advance) ----------------------------------------------------------

def test_advance_reports_position_rate_and_eta(sent):
    """何件目・どの item・どれくらいの速度かが構造化フィールドで出る。"""
    with perf.timed_step("thorlab.open_tiffs", total=4,
                         progress_interval_sec=0) as step:
        step.advance(item="/v/raw/ChanA_0001.tif")
        step.advance(item="/v/raw/ChanA_0002.tif")

    progress = [f for _l, _m, f in sent if f.get("event") == "progress"]
    assert len(progress) == 2
    last = progress[-1]
    assert last["done"] == 2
    assert last["total"] == 4
    assert last["percent"] == pytest.approx(50.0)
    assert last["item"] == "/v/raw/ChanA_0002.tif"
    assert last["items_per_sec"] >= 0
    assert "eta_sec" in last


def test_advance_throttles_by_time_not_by_count(sent):
    """5万ファイルのループでも送信キュー(maxsize=1000)を溢れさせない。"""
    with perf.timed_step("thorlab.open_tiffs", total=10_000,
                         progress_interval_sec=3600) as step:
        for i in range(10_000):
            step.advance(item=f"f{i}")

    assert [f for _l, _m, f in sent if f.get("event") == "progress"] == []


def test_advance_without_total_still_reports_count(sent):
    """総件数が事前に分からないループでも件数と速度は出せる。"""
    with perf.timed_step("scan", progress_interval_sec=0) as step:
        step.advance()

    last = [f for _l, _m, f in sent if f.get("event") == "progress"][-1]
    assert last["done"] == 1
    assert "total" not in last
    assert "eta_sec" not in last


def test_total_is_reported_on_the_completion_log(sent, monkeypatch):
    """総件数は完了ログに載る (実行中の「何件中どこで」は heartbeat が出す)。"""
    monkeypatch.setattr(perf, "QUIET_UNDER_SEC", 0.0)
    with perf.timed_step("thorlab.filter_by_size", total=42) as step:
        step.advance(n=42)

    assert sent[-1][2]["total"] == 42


# ---- 生存確認 (heartbeat) ----------------------------------------------------

def test_heartbeat_reports_the_running_step_and_its_chain(sent):
    """止まった実行でも「どのセッションの取り込みのどこか」が1行で分かる。"""
    with perf.timed_step("session", target="/v/raw/day1/sess1"):
        with perf.timed_step("load_image"):
            with perf.timed_step("thorlab.open_tiffs", total=100) as step:
                step.advance(item="/v/raw/ChanA_0007.tif")
                sent.clear()
                perf._emit_heartbeat(stall_after_sec=600)
                # with を抜けると done ログが増えるので、この場で確定させる
                emitted = list(sent)

    (level, message, fields), = emitted
    assert level == "info"
    assert fields["event"] == "heartbeat"
    assert fields["step"] == "thorlab.open_tiffs"
    assert fields["step_stack"] == "session > load_image > thorlab.open_tiffs"
    assert fields["item"] == "/v/raw/ChanA_0007.tif"
    assert fields["stalled"] is False
    assert fields["elapsed_sec"] >= 0
    assert "thorlab.open_tiffs" in message


def test_heartbeat_escalates_to_warning_when_progress_stops(sent):
    """一定時間 進捗が無ければ warning。Better Stack のアラート条件にできる。"""
    with perf.timed_step("thorlab.open_tiffs", total=100) as step:
        step.advance(item="/v/raw/ChanA_0007.tif")
        # 最後の進捗を 700 秒前に見せかける
        step.progress_at = time.perf_counter() - 700
        sent.clear()
        perf._emit_heartbeat(stall_after_sec=600)
        emitted = list(sent)

    (level, message, fields), = emitted
    assert level == "warning"
    assert fields["stalled"] is True
    assert fields["since_progress_sec"] >= 700
    assert "STALLED" in message
    # 止まっているファイルを名指しできることが、この機能の要点
    assert "/v/raw/ChanA_0007.tif" in message


def test_heartbeat_does_not_flag_a_long_step_that_keeps_progressing(sent):
    """長いだけで着実に進んでいる工程を停止と誤判定しない。"""
    with perf.timed_step("ometiff.stream_write", total=100) as step:
        step.started = time.perf_counter() - 3600   # 1時間動いているが
        step.advance(item="T=5 C=0 Z=0:8")          # 進捗は今あった
        # 進捗ログの直後は heartbeat を省くので、その窓を抜けた状態にする
        # (省く挙動そのものは下のテストで確かめる)。
        step.last_log_at = time.perf_counter() - 3 * perf.DEFAULT_HEARTBEAT_INTERVAL_SEC
        sent.clear()
        perf._emit_heartbeat(stall_after_sec=600)
        emitted = list(sent)

    (level, _message, fields), = emitted
    assert level == "info"
    assert fields["stalled"] is False
    assert fields["elapsed_sec"] >= 3600


def test_the_heartbeat_stays_quiet_right_after_a_progress_log(sent):
    """進捗ログを出した直後の工程には heartbeat を重ねない。

    heartbeat が言えることは「生きている・どこまで進んだ」で、進捗ログと同じ内容に
    なる。実データでは 1 秒違いで同じ数字が 2 行並び、端末が同じ行の繰り返しで
    埋まっていた。生存確認は進捗ログが出ていること自体で足りる。
    """
    with perf.timed_step("ometiff.stream_write", total=100) as step:
        step.started = time.perf_counter() - 3600
        step.advance(item="T=5 C=0 Z=0")            # ここで進捗ログが出る
        assert step.last_log_at is not None
        sent.clear()
        perf._emit_heartbeat(stall_after_sec=600)
        emitted = list(sent)                        # 工程の完了ログが入る前に見る

    assert emitted == [], emitted


def test_a_stalled_step_is_named_even_right_after_a_progress_log(sent):
    """止まっている工程は、直前に進捗ログが出ていても黙らせない。

    重複を避けるための省略が、**名指ししたい唯一の場面** まで消してはいけない。
    """
    with perf.timed_step("ometiff.stream_write", total=100,
                         progress_interval_sec=0) as step:
        step.advance(item="T=5 C=0 Z=0")                # 進捗ログが実際に出る
        assert step.last_log_at is not None
        # そのあと 700 秒 何も起きなかった状態にする。進捗ログを出せるのは advance()
        # の中だけなので、実際にはこの2つは必ず一緒に古くなる。
        stale = time.perf_counter() - 700
        step.progress_at = step.last_log_at = stale
        sent.clear()
        perf._emit_heartbeat(stall_after_sec=600)
        emitted = list(sent)

    (level, message, fields), = emitted
    assert level == "warning"
    assert fields["stalled"] is True
    assert "T=5 C=0 Z=0" in message


def test_a_step_that_never_reports_progress_still_gets_a_heartbeat(sent):
    """進捗を報告しない工程 (total を渡さない) は必ず heartbeat に出る。

    回帰: 省略の判定を「開始時刻」で初期化していたため、1 行も進捗を出さない工程が
    永久に飛ばされていた。長く沈黙する工程こそ生存確認が要る。
    """
    with perf.timed_step("load_image") as step:
        step.started = time.perf_counter() - 120
        sent.clear()
        perf._emit_heartbeat(stall_after_sec=600)
        emitted = list(sent)

    (level, _message, fields), = emitted
    assert level == "info"
    assert fields["step"] == "load_image"


def test_the_first_progress_line_waits_for_the_interval(sent):
    """1 行目も間隔を空けてから出す。

    着手直後は rate が 0 件/秒で「ETA 334891 s」のような無意味な数字しか出せない。
    """
    with perf.timed_step("ometiff.stream_write", total=12000) as step:
        sent.clear()
        step.advance(item="T=0 C=0 Z=0")
        assert sent == [], sent                     # 1 件目では出さない
        step.started = time.perf_counter() - 2 * perf.DEFAULT_PROGRESS_INTERVAL_SEC
        step.advance(item="T=1 C=0 Z=0")
        assert len(sent) == 1


def test_heartbeat_flags_a_step_without_progress_reporting_by_elapsed_time(sent):
    """進捗を報告しない工程(drift3d 等)は経過時間で判定する。"""
    with perf.timed_step("drift3d") as step:
        step.started = time.perf_counter() - 700
        sent.clear()
        perf._emit_heartbeat(stall_after_sec=600)

    level, _message, fields = sent[0]
    assert level == "warning"
    assert fields["stalled"] is True


def test_heartbeat_reports_idle_when_no_step_is_running(sent):
    """工程の切れ目でも生存だけは伝える(無音だと停止と区別が付かない)。"""
    perf._emit_heartbeat(stall_after_sec=600)

    (level, message, fields), = sent
    assert level == "info"
    assert fields["event"] == "heartbeat"
    assert fields["step"] == "(idle)"
    assert "idle" in message


def test_heartbeat_fields_win_over_caller_fields(sent):
    """呼び出し側が同名フィールドを渡しても監視側のキーが壊れない。"""
    with perf.timed_step("load_image", event="not-an-event", elapsed_sec="wrong"):
        sent.clear()
        perf._emit_heartbeat(stall_after_sec=600)

    _level, _message, fields = sent[0]
    assert fields["event"] == "heartbeat"
    assert fields["step"] == "load_image"
    assert isinstance(fields["elapsed_sec"], float)


def test_start_heartbeat_emits_periodically_and_stops(sent):
    """実スレッドで動き、stop_heartbeat で止まること。"""
    perf.start_heartbeat(interval_sec=0.02, stall_after_sec=0)
    try:
        with perf.timed_step("load_image"):
            deadline = time.monotonic() + 5
            while not [f for _l, _m, f in sent if f.get("event") == "heartbeat"]:
                if time.monotonic() > deadline:
                    pytest.fail("heartbeat was never emitted")
                time.sleep(0.01)
    finally:
        perf.stop_heartbeat()

    sent.clear()
    time.sleep(0.1)
    assert [f for _l, _m, f in sent if f.get("event") == "heartbeat"] == []


def test_start_heartbeat_is_idempotent(sent):
    perf.start_heartbeat(interval_sec=30)
    first = perf._hb_thread
    perf.start_heartbeat(interval_sec=30)
    assert perf._hb_thread is first
    perf.stop_heartbeat()


def test_start_heartbeat_is_disabled_by_a_non_positive_interval(sent):
    perf.start_heartbeat(interval_sec=0)
    assert perf._hb_thread is None


def test_heartbeat_interval_can_be_set_by_environment(monkeypatch):
    monkeypatch.setenv(perf.HEARTBEAT_INTERVAL_ENV, "5")
    assert perf._env_float(perf.HEARTBEAT_INTERVAL_ENV, 60.0) == 5.0
    # 不正な値で落とさない(監視の設定ミスでアプリを止めない)
    monkeypatch.setenv(perf.HEARTBEAT_INTERVAL_ENV, "not-a-number")
    assert perf._env_float(perf.HEARTBEAT_INTERVAL_ENV, 60.0) == 60.0


def test_heartbeat_survives_a_failure_while_emitting(monkeypatch):
    """監視の失敗でアプリ本体を止めない。"""
    def boom(*_a, **_k):
        raise RuntimeError("betterstack down")

    monkeypatch.setattr(perf, "log_info", boom)
    stop = threading.Event()
    t = threading.Thread(
        target=perf._heartbeat_worker, args=(stop, 0.01, 600), daemon=True
    )
    t.start()
    time.sleep(0.1)
    assert t.is_alive()
    stop.set()
    t.join(timeout=5)


# ---- describe_array ----------------------------------------------------------

def test_describe_array_reports_shape_and_bytes():
    import numpy as np

    fields = perf.describe_array(np.zeros((2, 3, 4), dtype=np.uint16))
    assert fields["shape"] == "(2, 3, 4)"
    assert fields["n_bytes"] == 2 * 3 * 4 * 2


def test_describe_array_works_without_a_materialised_array():
    """dask/xarray や shape/dtype だけのダミーでも見積れる(実体化させない)。"""
    import numpy as np

    class _Lazy:
        shape = (2000, 1, 31, 1024, 1024)
        dtype = np.dtype(np.uint16)

    fields = perf.describe_array(_Lazy())
    assert fields["n_bytes"] == 2000 * 1 * 31 * 1024 * 1024 * 2


def test_describe_array_never_raises_for_odd_objects():
    """計測の付随情報のために処理を止めない。"""
    class _NoShape:
        pass

    assert perf.describe_array(_NoShape()) == {}

    class _ShapeOnly:
        shape = (2, 3)

    assert perf.describe_array(_ShapeOnly()) == {"shape": "(2, 3)"}

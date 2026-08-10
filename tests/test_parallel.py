"""ordered_bounded_map が「順序」と「止まった場所」を壊さないことを固定するテスト。

I/O ループの並行化は、速くなる代わりに *監視が壊れる* のが典型的な失敗の仕方で、
しかも壊れ方が「本番のネットワークドライブで、止まったときだけ」現れる。
ここでは壊れうる4点 — 結果の順序 / 同時実行数の上限 / 先読みの上限 / 例外の同一性 —
と、進捗 (done, item) の意味を、スレッド起動順に依存しない形で固定する。
"""
from __future__ import annotations

import random
import threading
import time

import pytest

from ylabcommon.utils.parallel import ordered_bounded_map


class FakeStep:
    """perf.StepHandle の advance() だけを持つ最小の代役。"""

    def __init__(self):
        self.done = 0
        self.item = None
        self.trace = []

    def advance(self, n=1, item=None):
        self.done += n
        if item is not None:
            self.item = item
        self.trace.append((self.done, item))


# ---- 結果の順序 --------------------------------------------------------------

@pytest.mark.parametrize("workers", [1, 2, 4, 8, 16])
def test_results_stay_in_input_order(workers):
    """完了順ではなく入力順で返る。

    プレーンの並び順そのものなので、ここが崩れると T/Z 方向が入れ替わった
    スタックが「エラーも出さずに」出来上がる。
    """
    def jittery(x):
        time.sleep(random.random() * 0.002)
        return x * 2

    got = ordered_bounded_map(jittery, list(range(120)), max_workers=workers)
    assert got == [x * 2 for x in range(120)]


def test_empty_and_single_inputs():
    assert ordered_bounded_map(lambda x: x, [], max_workers=8) == []
    assert ordered_bounded_map(lambda x: x + 1, [41], max_workers=8) == [42]


def test_serial_mode_uses_no_threads():
    """max_workers=1 は本当に逐次 (呼び出しスレッドで実行)。

    並行化を疑ったときに環境変数ひとつで「従来どおり」へ確実に戻せることが、
    本番で切り分けるための最後の逃げ道になる。
    """
    seen = set()
    ordered_bounded_map(lambda x: seen.add(threading.current_thread().name),
                        list(range(20)), max_workers=1)
    assert seen == {threading.current_thread().name}


# ---- 同時実行数と先読みの上限 ------------------------------------------------

def test_concurrency_never_exceeds_max_workers():
    """共有側へ同時に投げる要求数が max_workers を超えない。

    弱っている共有に同時要求を積むと1件あたりが遅くなるので、上限は守る必要がある。
    """
    lock = threading.Lock()
    live = {"now": 0, "peak": 0}

    def work(x):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.003)
        with lock:
            live["now"] -= 1
        return x

    ordered_bounded_map(work, list(range(80)), max_workers=6)
    assert live["peak"] <= 6
    assert live["peak"] > 1          # 実際に並行している (テストが空回りしていない)


def test_a_hung_item_bounds_read_ahead_to_the_window():
    """先頭が固まっても、先読みは max_workers 件までしか進まない。

    全件を一度に submit する実装だと、1件のハングで残り全部がキューに積まれ、
    共有側に無駄な要求を投げ続ける。窓で抑えることで「掴んだままのファイル」の数を
    上限つきにする。
    """
    gate = threading.Event()
    started = []
    lock = threading.Lock()

    def work(x):
        with lock:
            started.append(x)
        if x == 0:
            gate.wait(timeout=10)
        return x

    t = threading.Thread(
        target=lambda: ordered_bounded_map(work, list(range(200)), max_workers=5),
        daemon=True)
    t.start()
    time.sleep(0.3)
    with lock:
        n_started = len(started)
    gate.set()
    t.join(timeout=10)

    assert n_started == 5, "read-ahead was %d, expected the window size 5" % n_started


# ---- 進捗の意味 --------------------------------------------------------------

def test_progress_names_the_item_it_is_waiting_for():
    """done/item が「i 件目まで着手済み、いま item を待っている」を保つ。

    submit のループで advance を呼ぶ実装だと done が投入件数になり、1件も終わって
    いないのに 100% と報告される。as_completed で刻む実装だと item が「たまたま
    最後に終わったもの」になり、固まっているものは決して item に現れない。
    """
    gate = threading.Event()
    step = FakeStep()
    files = ["f%d" % i for i in range(50)]

    def work(f):
        if f == "f3":
            gate.wait(timeout=10)
        return f

    t = threading.Thread(
        target=lambda: ordered_bounded_map(work, files, max_workers=8, step=step),
        daemon=True)
    t.start()
    time.sleep(0.3)

    # 固まっているのは f3。進捗は「4件目に着手して f3 を待っている」で止まる。
    assert step.item == "f3"
    assert step.done == 4
    gate.set()
    t.join(timeout=10)
    assert step.done == 50
    # advance は入力順にちょうど1回ずつ
    assert [item for _done, item in step.trace] == files


def test_progress_is_advanced_before_the_item_is_awaited():
    """例外で落ちたときも、落ちたファイルが item に入っている。

    result() の後で advance すると、落ちた1件だけが記録から漏れる。
    """
    step = FakeStep()

    def work(x):
        if x == "c":
            raise OSError(5, "Input/output error")
        return x

    with pytest.raises(OSError):
        ordered_bounded_map(work, ["a", "b", "c", "d"], max_workers=4, step=step)

    assert step.item == "c"
    assert step.done == 3          # a, b に着手・完了し、c に着手して落ちた


# ---- 例外 --------------------------------------------------------------------

def test_the_first_failure_in_input_order_is_the_one_raised():
    """複数が同時に落ちても、送出されるのは常に入力順で最初のもの。

    どの例外が出るかがスレッドの気分で変わると、同じ壊れたディレクトリを
    再実行するたびに別のファイルが原因として報告され、調査にならない。
    """
    def work(x):
        time.sleep(random.random() * 0.003)
        if x in (5, 9, 40):
            raise ValueError("boom %d" % x)
        return x

    for _ in range(60):
        with pytest.raises(ValueError, match="boom 5"):
            ordered_bounded_map(work, list(range(60)), max_workers=8)


def test_exception_type_is_preserved_exactly():
    """呼び出し側の except OSError などがそのまま効くよう、型を包み替えない。"""
    class Weird(OSError):
        pass

    def work(x):
        raise Weird("nope")

    with pytest.raises(Weird):
        ordered_bounded_map(work, [1, 2, 3], max_workers=4)


def test_failure_does_not_wait_for_still_running_work():
    """1件が固まっていても、別の件の失敗はすぐ呼び出し側へ返る。

    with ThreadPoolExecutor(...) の暗黙の shutdown(wait=True) だと、固まった
    ワーカーの完了を待ってしまい、失敗ログすら出ないまま沈黙する。
    """
    gate = threading.Event()

    def work(x):
        if x == 0:
            raise RuntimeError("fail fast")
        gate.wait(timeout=30)      # 残りは固まったまま
        return x

    t0 = time.perf_counter()
    with pytest.raises(RuntimeError, match="fail fast") as ei:
        ordered_bounded_map(work, list(range(8)), max_workers=4)
    elapsed = time.perf_counter() - t0
    gate.set()
    assert elapsed < 5, "failure was blocked for %.1f s by hung workers" % elapsed

    # 置き去りにしたワーカーがあることを例外に添えてある。
    # これが無いと「例外は出たのにプロセスが終わらない」という、原因の見えない
    # 症状になる (ThreadPoolExecutor のスレッドは非デーモンで、インタプリタ終了時に
    # join される)。
    notes = "".join(getattr(ei.value, "__notes__", []))
    assert "abandoned" in notes
    assert "cannot exit" in notes


def test_no_abandonment_note_when_nothing_is_still_running():
    """全部すぐ落ちるときは余計な注記を足さない (ノイズにしない)。"""
    def work(x):
        raise ValueError("bad %d" % x)

    with pytest.raises(ValueError) as ei:
        ordered_bounded_map(work, list(range(6)), max_workers=3)
    assert "abandoned" not in "".join(getattr(ei.value, "__notes__", []))

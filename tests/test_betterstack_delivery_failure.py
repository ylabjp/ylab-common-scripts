"""Better Stack へ送れないときに、解析側が巻き添えを食わないことのテスト。

送信先は落ちる (SSL ハンドシェイクが切れる、ネットワークが詰まる)。そのとき
守りたいのは 3 つ。

1. 解析は止まらない — 送信は別スレッドで、本体は待たない
2. 端末が埋まらない — 数十行の traceback を毎回出すと本物のエラーが埋もれる
3. 終了できる — 送れないログを送り切ろうとしてプロセスが終わらない、を防ぐ
"""
from __future__ import annotations

import time

import pytest

from ylabcommon.utils import betterstack_log as bs


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv(bs.TOKEN_ENV, "dummy-token")
    monkeypatch.setattr(bs, "_send_failures", 0)
    monkeypatch.setattr(bs, "_flushed", False)
    yield


def test_repeated_failures_do_not_flood_the_terminal(monkeypatch, capsys):
    """1件目と 100 件ごとだけ 1 行。traceback は出さない。"""
    def boom(*a, **kw):
        raise OSError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF in violation of protocol")

    monkeypatch.setattr(bs.urllib.request, "urlopen", boom)
    for _ in range(250):
        bs._report_send_failure(OSError("SSL boom"))

    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if "log delivery failed" in ln]
    assert len(lines) == 3, out          # 1 件目 + 100 + 200
    assert "Traceback" not in out
    assert "the run is unaffected" in lines[0]


def test_a_failed_send_never_reaches_the_caller(monkeypatch):
    """送信が落ちても send() は例外を投げない (解析を止めない)。"""
    def boom(*a, **kw):
        raise OSError("SSL boom")

    monkeypatch.setattr(bs.urllib.request, "urlopen", boom)
    bs.send("info", "hello", step="probe")      # 例外が出なければ合格
    bs._queue.join()


def test_flush_gives_up_instead_of_hanging(monkeypatch, capsys):
    """送れないログを送り切ろうとして、プロセスの終了を待たせない。

    回帰: flush() は ``_queue.join()`` を無制限に待っていた。送信が 1 件 10 秒の
    タイムアウトで失敗する状況でキューに数百件溜まっていると、解析が終わっている
    のにプロセスが数時間終わらない。
    """
    monkeypatch.setattr(bs, "_FLUSH_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(bs, "_worker_started", True)

    def never_finishes(*a, **kw):
        time.sleep(30)

    monkeypatch.setattr(bs._queue, "join", never_finishes)

    t0 = time.perf_counter()
    bs.flush()
    elapsed = time.perf_counter() - t0

    assert elapsed < 5, "flush() が待ち続けている (%.1f s)" % elapsed
    assert "gave up flushing logs" in capsys.readouterr().out

"""`analysis/claim.py` の約束を固定する。

* 掴めるのは 1 プロセスだけ(同時に掴みにいっても)
* 抜けるときに消す(正常終了でも例外でも)。掴めなかった側は何も消さない
* 古い claim (ANALYZING_STALE_HOURS 以上前) は引き継ぐ。それより新しいものは奪わない
* 自分の claim が引き継がれていたら、それは消さない
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from ylabcommon.models.parameters.behavior import ANALYZING_STALE_HOURS

from ylabcommon.analysis.claim import Claim, claim_day, live_claim

NAME = ".test_claim.json"


def _write_claim(path: Path, hours_ago: float, host: str = "other-pc", pid: int = 99999) -> None:
    started = datetime.now().astimezone() - timedelta(hours=hours_ago)
    path.write_text(json.dumps({"host": host, "pid": pid, "gpu": 0,
                                "started_at": started.isoformat(timespec="seconds")}),
                    encoding="utf-8")


def test_claim_is_held_while_inside_and_removed_after(tmp_path: Path) -> None:
    with claim_day(tmp_path, NAME, gpu=1) as c:
        assert c.acquired
        data = json.loads((tmp_path / NAME).read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
        assert data["gpu"] == 1
    assert not (tmp_path / NAME).exists()


def test_second_claim_is_refused_and_names_the_holder(tmp_path: Path) -> None:
    with claim_day(tmp_path, NAME, gpu=0):
        with claim_day(tmp_path, NAME, gpu=1) as second:
            assert not second.acquired
            assert second.holder is not None and second.holder.gpu == 0
            assert "gpu 0" in second.holder.label()
        # 掴めなかった側は抜けても消さない
        assert (tmp_path / NAME).exists()


def test_claim_is_removed_on_exception(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        with claim_day(tmp_path, NAME, gpu=None):
            raise RuntimeError("boom")
    assert not (tmp_path / NAME).exists()


def test_only_one_of_many_simultaneous_claims_wins(tmp_path: Path) -> None:
    n = 16
    barrier = threading.Barrier(n)
    release = threading.Event()
    won: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        with claim_day(tmp_path, NAME, gpu=None) as c:
            with lock:
                won.append(c.acquired)
            if c.acquired:
                release.wait(5)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    while True:
        with lock:
            if len(won) == n:
                break
    release.set()
    for t in threads:
        t.join()
    assert won.count(True) == 1


def test_fresh_claim_of_another_process_is_not_taken(tmp_path: Path) -> None:
    _write_claim(tmp_path / NAME, hours_ago=ANALYZING_STALE_HOURS - 1)
    with claim_day(tmp_path, NAME, gpu=0) as c:
        assert not c.acquired
        assert c.holder is not None and c.holder.host == "other-pc"
    assert json.loads((tmp_path / NAME).read_text(encoding="utf-8"))["host"] == "other-pc"


def test_stale_claim_is_taken_over(tmp_path: Path) -> None:
    _write_claim(tmp_path / NAME, hours_ago=ANALYZING_STALE_HOURS + 1)
    with claim_day(tmp_path, NAME, gpu=0) as c:
        assert c.acquired
        assert c.taken_over is not None and c.taken_over.host == "other-pc"
        assert json.loads((tmp_path / NAME).read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert not (tmp_path / NAME).exists()
    assert list(tmp_path.iterdir()) == []      # 退かした古い claim も残さない


def test_unreadable_claim_is_judged_by_mtime(tmp_path: Path) -> None:
    path = tmp_path / NAME
    path.write_text("", encoding="utf-8")       # 作成直後で中身がまだ無い
    with claim_day(tmp_path, NAME, gpu=0) as c:
        assert not c.acquired
    old = (datetime.now() - timedelta(hours=ANALYZING_STALE_HOURS + 1)).timestamp()
    os.utime(path, (old, old))
    with claim_day(tmp_path, NAME, gpu=0) as c:
        assert c.acquired


def test_claim_taken_over_by_someone_else_is_not_removed(tmp_path: Path) -> None:
    """12 時間を超えて走っている間に引き継がれたら、抜けるときに相手の claim を消さない。"""
    with claim_day(tmp_path, NAME, gpu=0) as c:
        assert c.acquired
        _write_claim(tmp_path / NAME, hours_ago=0, host="new-owner")
    assert json.loads((tmp_path / NAME).read_text(encoding="utf-8"))["host"] == "new-owner"


def test_label_shows_host_pid_gpu_and_age() -> None:
    started = datetime(2026, 11, 15, 10, 0, tzinfo=datetime.now().astimezone().tzinfo)
    c = Claim(host="ws-hpc", pid=1234, gpu=1, started_at=started.isoformat(timespec="seconds"))
    label = c.label(now=started + timedelta(hours=1.5))
    assert label.startswith("ws-hpc pid 1234 gpu 1 since ")
    assert label.endswith("(1.5h ago)")


def test_live_claim_reports_a_held_day_without_writing(tmp_path: Path) -> None:
    assert live_claim(tmp_path, NAME).acquired              # 無ければ掴める
    assert list(tmp_path.iterdir()) == []                   # 見るだけで何も作らない
    with claim_day(tmp_path, NAME, gpu=2):
        got = live_claim(tmp_path, NAME)
        assert not got.acquired and got.holder is not None and got.holder.gpu == 2
    _write_claim(tmp_path / NAME, hours_ago=ANALYZING_STALE_HOURS + 1)
    assert live_claim(tmp_path, NAME).acquired              # 古いものは掴める
    (tmp_path / NAME).write_text("", encoding="utf-8")      # 作成直後で中身がまだ無い
    got = live_claim(tmp_path, NAME)
    assert not got.acquired and got.holder is None

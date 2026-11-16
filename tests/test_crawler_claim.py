"""`GenericKernel.CLAIM_FILENAME` を置いた Kernel の、crawler 側の約束を固定する。

* claim を作れた node だけ on_file を呼び、処理中は claim がある。終われば(例外でも)消える
* 他のプロセスが掴んでいる node は飛ばす(on_file を呼ばない。success にも数えない)
* 掴んだあと check_overwrite を見直す(掴むまでの間に他のプロセスが書き終えていたら飛ばす)
* claim には ctx.arg.gpu を書く
* CLAIM_FILENAME が None の Kernel は今までどおり(claim を作らない)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ylabcommon.analysis.crawler import CrawlContext, GenericCrawler, GenericKernel, HierNode
from ylabcommon.models.parameters.behavior import ANALYZING_STALE_HOURS
from ylabcommon.models.parameters.general import ArgModel

CLAIM = ".test_claim.json"
OUTPUT = "out.txt"


class _Kernel(GenericKernel):
    CLAIM_FILENAME: str | None = CLAIM

    def __init__(self, fail: bool = False) -> None:
        super().__init__(target_filename=OUTPUT)
        self.fail = fail
        self.seen: list[dict | None] = []

    def retrieve_file_with_pattern(self, node) -> str:
        return "input.txt" if node.level == "day" else ""

    def on_file(self, ctx, node, file_path: Path) -> None:
        claim = node.path / CLAIM
        self.seen.append(json.loads(claim.read_text(encoding="utf-8")) if claim.exists() else None)
        if self.fail:
            raise RuntimeError("boom")
        (node.path / OUTPUT).write_text("x", encoding="utf-8")


class _NoClaim(_Kernel):
    CLAIM_FILENAME = None


def _day(tmp_path: Path) -> HierNode:
    day = tmp_path / "day01"
    day.mkdir()
    (day / "input.txt").write_text("", encoding="utf-8")
    return HierNode(name=day.name, path=day, level="day")


def _crawl(kernel: GenericKernel, root: HierNode, tmp_path: Path, overwrite: bool = False) -> GenericCrawler:
    crawler = GenericCrawler(
        kernels=[kernel],
        ctx=CrawlContext(analysis_param=None, project_dir=tmp_path, arg=ArgModel(overwrite=overwrite, gpu=1)),
    )
    crawler.crawl_from_nodes([root])
    return crawler


def _write_claim(path: Path, hours_ago: float) -> None:
    started = datetime.now().astimezone() - timedelta(hours=hours_ago)
    path.write_text(json.dumps({"host": "other-pc", "pid": 1, "gpu": 0,
                                "started_at": started.isoformat(timespec="seconds")}), encoding="utf-8")


def test_on_file_runs_under_a_claim_that_carries_the_gpu_and_is_removed(tmp_path: Path) -> None:
    root = _day(tmp_path)
    k = _Kernel()
    crawler = _crawl(k, root, tmp_path)
    assert len(k.seen) == 1 and k.seen[0] is not None and k.seen[0]["gpu"] == 1
    assert not (root.path / CLAIM).exists()
    assert list(crawler.get_log()["result"]) == ["success"]


def test_node_claimed_by_another_process_is_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _day(tmp_path)
    _write_claim(root.path / CLAIM, hours_ago=1)
    k = _Kernel()
    crawler = _crawl(k, root, tmp_path)
    assert k.seen == []
    assert "Other process is analyzing [other-pc pid 1 gpu 0" in capsys.readouterr().out
    assert json.loads((root.path / CLAIM).read_text(encoding="utf-8"))["host"] == "other-pc"
    assert crawler.get_log().empty          # 飛ばした node は success にも error にも数えない
    assert not crawler.has_failures


def test_stale_claim_is_taken_over(tmp_path: Path) -> None:
    root = _day(tmp_path)
    _write_claim(root.path / CLAIM, hours_ago=ANALYZING_STALE_HOURS + 1)
    k = _Kernel()
    _crawl(k, root, tmp_path)
    assert len(k.seen) == 1
    assert not (root.path / CLAIM).exists()


def test_output_is_rechecked_after_claiming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1 回目の check_overwrite と claim の間に、他のプロセスが出力を書き終えて claim を消した。"""
    root = _day(tmp_path)
    k = _Kernel()
    calls = {"n": 0}
    original = k.check_overwrite

    def check(node, ctx) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            ok = original(node, ctx)
            (node.path / OUTPUT).write_text("by another process", encoding="utf-8")
            return ok
        return original(node, ctx)

    monkeypatch.setattr(k, "check_overwrite", check)
    crawler = _crawl(k, root, tmp_path)
    assert calls["n"] == 2
    assert k.seen == []
    assert (root.path / OUTPUT).read_text(encoding="utf-8") == "by another process"
    assert not (root.path / CLAIM).exists()
    assert crawler.get_log().empty


def test_claim_is_removed_when_on_file_raises(tmp_path: Path) -> None:
    root = _day(tmp_path)
    crawler = _crawl(_Kernel(fail=True), root, tmp_path)
    assert crawler.has_failures
    assert not (root.path / CLAIM).exists()


def test_kernel_without_claim_filename_creates_no_claim(tmp_path: Path) -> None:
    root = _day(tmp_path)
    k = _NoClaim()
    _crawl(k, root, tmp_path)
    assert k.seen == [None]

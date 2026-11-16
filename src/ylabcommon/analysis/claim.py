"""day フォルダを 1 つのプロセスだけが処理するための「掴み」(claim)。

同じ config の解析を複数の GPU / PC で同時に走らせると、どのプロセスも同じ順で木を巡る。
出力が day の処理を全部終えてから書かれる段(behavior-analysis の `pvdd`)では、処理中の day は
「未解析」に見えるので、同じ day を 2 つのプロセスが同時に解析し、同じ出力へ書いていた。

day フォルダに claim ファイルを **排他的に作成** (`O_CREAT | O_EXCL`) できたプロセスだけが
処理する。Kernel は `GenericKernel.CLAIM_FILENAME` を置くだけでよく、掴む・出力を見直す・
離すは crawler がやる(`GenericCrawler._run_on_file`)。claim のファイル名は段ごとに分ける
(`.pvd_claim.json` / `.pvdd_claim.json`)——出力が別の段どうしは互いを待たない。

behavior-analysis-dlc の `pvd` が `video_info.json` に書く `analysis_status = "analyzing"` と
同じ役目だが、2 点を変えている。

* **確認と書き込みを 1 つの操作にする。** `analyzing` は「読む → analyzing でなければ書く」で、
  同時に起動した 2 プロセスがどちらも pending を読んで両方とも掴みうる。作成の排他は
  ファイルシステムが保証する
* **持ち主は host・pid・gpu で記録する。** 1 台の 2 枚の GPU で走らせると host は同じになる

**正常終了でも例外でも claim は消す**(`claim_day` の finally)。kill・停電で残ったものは、
作ってから `ANALYZING_STALE_HOURS` (`analyzing` と同じ 12 時間) を過ぎたら古いと見なして
引き継ぐ。それより前に引き継ぐ経路は作らない——走っている解析を奪わないため。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 実行PC名は VideoInfo.mark_analyzing と同じ出典(Better Stack の host とも同じ)
from ylabcommon.models.parameters.behavior import ANALYZING_STALE_HOURS, _hostname


@dataclass(frozen=True)
class Claim:
    """claim ファイルの中身。"""

    host: str
    pid: int
    gpu: int | None
    started_at: str

    def label(self, now: datetime | None = None) -> str:
        """`ws-hpc pid 1234 gpu 1 since 2026-11-15T10:00:00+09:00 (1.2h ago)`。"""
        gpu = "-" if self.gpu is None else str(self.gpu)
        head = f"{self.host} pid {self.pid} gpu {gpu} since {self.started_at}"
        try:
            started = datetime.fromisoformat(self.started_at)
        except ValueError:
            return head
        now = now or datetime.now().astimezone()
        return f"{head} ({(now - started).total_seconds() / 3600:.1f}h ago)"


@dataclass(frozen=True)
class ClaimResult:
    """`claim_day` が返す結果。acquired=False なら `holder` が掴んでいる。"""

    acquired: bool
    holder: Claim | None = None
    #: 古い claim を引き継いだとき、その持ち主
    taken_over: Claim | None = None


def _read(path: Path) -> Claim | None:
    """読めない(作成直後で中身がまだ無い、壊れている)なら None。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Claim(host=str(data["host"]), pid=int(data["pid"]),
                     gpu=None if data.get("gpu") is None else int(data["gpu"]),
                     started_at=str(data["started_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _is_stale(path: Path, claim: Claim | None, now: datetime, hours: float) -> bool:
    """作ってから hours 以上経ったか。中身が読めなければファイルの mtime で測る。"""
    started: datetime | None = None
    if claim is not None:
        try:
            started = datetime.fromisoformat(claim.started_at)
        except ValueError:
            started = None
    if started is None:
        try:
            started = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        except OSError:
            return False
    return (now - started).total_seconds() >= hours * 3600


def _create(path: Path, claim: Claim) -> bool:
    """排他的に作る。既にあれば False。"""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(claim.__dict__, f, indent=4)
    return True


def _take_over_stale(path: Path, now: datetime, hours: float) -> Claim | None | bool:
    """古い claim を退かす。退かせたらその中身(読めなければ None)、退かせなければ False。

    消してから作り直すと、同時に古いと判断した 2 プロセスの片方が、もう片方の作った新しい
    claim を消しうる。そこで一意な名前へ rename する(rename は 1 つしか成功しない)。
    rename したものが古くなかった(読んでから rename するまでに別のプロセスが引き継いで
    作り直していた)なら元へ戻して諦める。
    """
    tomb = path.with_name(f"{path.name}.takeover-{_hostname()}-{os.getpid()}")
    try:
        os.rename(path, tomb)
    except FileNotFoundError:
        return False
    old = _read(tomb)
    if not _is_stale(tomb, old, now, hours):
        try:
            os.rename(tomb, path)
        except OSError:
            pass
        return False
    tomb.unlink(missing_ok=True)
    return old


def live_claim(day: Path, filename: str, *, hours: float = ANALYZING_STALE_HOURS) -> ClaimResult:
    """day が他のプロセスに掴まれているかを、掴まずに見る(何も書かない)。

    掴まれていれば acquired=False と持ち主(中身が読めなければ None)、
    claim が無いか古ければ acquired=True を返す——「いま掴みにいけば掴める」の意味。
    掴んだ day の状態を書き換える道具(qcdlc など)が、走っている解析の邪魔をしないために使う。
    """
    path = day / filename
    if not path.exists():
        return ClaimResult(acquired=True)
    holder = _read(path)
    if _is_stale(path, holder, datetime.now().astimezone(), hours):
        return ClaimResult(acquired=True)
    return ClaimResult(acquired=False, holder=holder)


@contextmanager
def claim_day(
    day: Path,
    filename: str,
    *,
    gpu: int | None,
    hours: float = ANALYZING_STALE_HOURS,
) -> Iterator[ClaimResult]:
    """day を掴む。掴めたら抜けるときに消す(正常終了でも例外でも)。

    掴めなかったとき(他のプロセスが処理中)は acquired=False を返し、何も消さない。
    """
    path = day / filename
    now = datetime.now().astimezone()
    me = Claim(host=_hostname(), pid=os.getpid(), gpu=gpu,
               started_at=now.isoformat(timespec="seconds"))
    taken_over: Claim | None = None
    if not _create(path, me):
        holder = _read(path)
        if not _is_stale(path, holder, now, hours):
            yield ClaimResult(acquired=False, holder=holder)
            return
        old = _take_over_stale(path, now, hours)
        if old is False or not _create(path, me):
            yield ClaimResult(acquired=False, holder=_read(path))
            return
        taken_over = old if isinstance(old, Claim) else None
    try:
        yield ClaimResult(acquired=True, taken_over=taken_over)
    finally:
        # 12 時間を超えて走り、その間に別のプロセスに引き継がれていたら、それは消さない
        if _read(path) == me:
            path.unlink(missing_ok=True)

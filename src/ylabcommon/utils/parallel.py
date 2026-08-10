"""順序と進捗を保ったまま、I/O 待ちのループを並行化するための小さなヘルパ。

ネットワークドライブ (SMB) 上の生データを1ファイルずつ開くループは、1回あたりの
往復遅延が支配的で CPU はほとんど遊んでいる。ワーカースレッドを増やせば往復を
重ねられるが、素朴に ``ThreadPoolExecutor.map`` や ``as_completed`` へ置き換えると
**止まった場所が分からなくなる** という、いちばん困る形で監視が壊れる。

- ``executor.submit`` のループで ``step.advance(item=f)`` を呼ぶと、``done`` は
  「投入した件数」になる。3001 件を投入し終えた直後に heartbeat が走れば
  ``done=3001/3001`` と出るのに、実際には1件も終わっていない。
- ``as_completed`` で進捗を刻むと ``done`` は正しくなるが、``item`` は
  「たまたま最後に終わったファイル」を指す。止まっているファイルは終わらないので、
  絶対に ``item`` に現れない。名指ししたい相手だけが構造的に漏れる。

そこで :func:`ordered_bounded_map` は **投入は先行させるが、回収は入力順に行う**。
``i`` 番目の結果を待って止まっているあいだ ``done=i+1`` / ``item=items[i]`` が
固定されるので、heartbeat の意味は逐次ループとまったく同じになる
(「``done`` 件目まで着手済み、いま ``item`` を掴んだまま止まっている」)。
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Iterable, List, Optional, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def ordered_bounded_map(
    fn: Callable[[T], R],
    items: Iterable[T],
    *,
    max_workers: int,
    step=None,
    thread_name_prefix: str = "ylab-io",
) -> List[R]:
    """``[fn(x) for x in items]`` と同じ結果を、I/O を最大 ``max_workers`` 並行で返す。

    逐次版との違いは所要時間だけで、返る要素の順序・値・例外の同一性は変わらない。

    Args:
        fn: 各要素に適用する関数。ワーカースレッドから呼ばれるのでスレッドセーフで
            あること。
        items: 入力。長さを確定させるため内部でリスト化する。
        max_workers: 同時に走らせる最大数。**同時に投入する数もこれで抑える**ので、
            1件が固まっても先読みは最大 ``max_workers`` 件までしか進まない
            (共有側に対して開きっぱなしのハンドルを増やさない)。``1`` 以下ならスレッドを
            まったく使わず、逐次ループそのものになる。
        step: :func:`ylabcommon.utils.perf.timed_step` のハンドル。渡すと
            ``i`` 番目の回収に入る直前に ``step.advance(item=items[i])`` を呼ぶ。
        thread_name_prefix: ワーカースレッド名の接頭辞 (スタックダンプの読み取り用)。

    Returns:
        ``items`` と同じ長さ・同じ順序の結果リスト。

    Raises:
        いずれかの ``fn(x)`` が投げた例外を、**入力順で最初のもの**をそのまま送出する。
        送出前に残りの投入済みタスクを取り消し、待たずにプールを畳む
        (固まったワーカーの完了を待って呼び出し側の失敗ログを止めないため)。
    """
    items = list(items)
    n = len(items)
    if n == 0:
        return []

    # 逐次経路。並行化を切ったときに「スレッドを使わない従来どおりの動き」へ確実に
    # 戻せるよう、プールを 1 ワーカーで回すのではなく本当にループする。
    if max_workers <= 1 or n == 1:
        out: List[R] = []
        for it in items:
            if step is not None:
                step.advance(item=it)
            out.append(fn(it))
        return out

    window = min(max_workers, n)
    results: List[Optional[R]] = [None] * n
    executor = ThreadPoolExecutor(max_workers=window,
                                  thread_name_prefix=thread_name_prefix)
    pending: "deque[Future]" = deque()
    i = -1
    try:
        submitted = 0
        while submitted < window:
            pending.append(executor.submit(fn, items[submitted]))
            submitted += 1

        for i in range(n):
            future = pending.popleft()
            # result() で待つ「前」に進捗を刻む。ここで止まったとき、heartbeat が
            # 読む item が「待っている当のファイル」になる。逆順にすると、止まった
            # ファイルは advance されないまま沈黙する。
            if step is not None:
                step.advance(item=items[i])
            results[i] = future.result()
            if submitted < n:
                pending.append(executor.submit(fn, items[submitted]))
                submitted += 1
    except BaseException as e:
        # まだ走っているワーカーは待たずに置き去りにする (下の shutdown(wait=False))。
        # ただし黙って置き去りにすると、呼び出し側が例外を処理し終えたあとに
        # **プロセスが終了できない** という分かりにくい症状になる:
        # ThreadPoolExecutor のスレッドは非デーモンで、インタプリタ終了時に join
        # されるため、固まった open() が返るまで exit しない。
        # 何が起きているのかを例外に添えておく。
        abandoned = [items[j] for j, f in enumerate(pending, start=i + 1) if f.running()]
        if abandoned:
            add_note = getattr(e, "add_note", None)
            if add_note is not None:
                add_note(
                    "%d worker(s) were still blocked inside fn() and have been "
                    "abandoned; the process cannot exit until they return. "
                    "Still blocked on: %s"
                    % (len(abandoned), ", ".join(str(x) for x in abandoned[:5]))
                )
        raise
    finally:
        for future in pending:
            future.cancel()
        # wait=False: 固まったワーカーの完了を待たない。呼び出し側の except と
        # timed_step の失敗ログを先に走らせるため。
        executor.shutdown(wait=False, cancel_futures=True)

    return results  # type: ignore[return-value]

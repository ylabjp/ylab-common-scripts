"""処理時間の計測と「生きているか」の確認を Better Stack のログへ載せる共通ヘルパ。

sorter のような長時間バッチが「途中で止まる」とき、従来のログでは何も分からなかった。
所要時間を記録していても、止まった実行はその工程の完了ログを出さないまま沈黙するだけで、
Better Stack 上には「開始」しか残らないためである。しかも ``betterstack_log.flush()`` は
atexit 依存なので、OOM killer や強制終了ではキューの末尾ごと失われる。

そこで本モジュールは、計測を「後から集計するための記録」ではなく
**動いている最中の監視 (パフォーマンスモニタ)** として使えるようにする。

- :func:`timed_step` — 工程の開始・完了 (または失敗) と所要時間を送る。同時に
  「今どの工程を実行中か」をプロセス内のレジストリへ登録する。
- :func:`timed_step` が返すハンドルの ``advance()`` — ファイル1つずつのような長いループで
  進捗を送る。送信は時間で間引くので、5万ファイルを回しても数十件にしかならない。
- :func:`start_heartbeat` — デーモンスレッドから定期的に「生存確認」を送る。実行中の工程名・
  経過時間・進捗・**いま処理中の item** を載せるので、沈黙してしまう実行でも
  「どこで止まったか」が Better Stack 側に残り続ける。
- 停止の検出 — 一定時間 (既定 10 分) 進捗が無い工程は heartbeat を ``warning`` へ格上げする。
  Better Stack のアラートはこの level を条件にできる。

外部依存は持たない (``betterstack_log`` と同じく標準ライブラリのみ)。トークン未設定の
環境では送信自体がスキップされるため、ローカル実行や CI を壊さない。

Note:
    本モジュールは ylab 共通基盤なので、解析パイプライン (slice-analysis) と取り込み
    ライブラリ (``ylabcommon.bioio``) の双方から同じレジストリを共有する。これにより
    heartbeat の ``step_stack`` が ``project > session > load_image > thorlab.stack``
    のようにリポジトリをまたいで繋がり、「sorter のどのセッションの、取り込みのどの段階か」
    を1行で特定できる。
"""
from __future__ import annotations

import atexit
import contextlib
import os
import threading
import time
from typing import Optional

from ylabcommon.utils.betterstack_log import log_info, log_warning

# 生存確認の間隔と、進捗が無いことを「停止」とみなすまでの時間。実行環境ごとに
# 変えられるよう環境変数で上書きできる (既定値のままで運用できることを想定)。
HEARTBEAT_INTERVAL_ENV = "YLAB_HEARTBEAT_SEC"
STALL_AFTER_ENV = "YLAB_STALL_AFTER_SEC"

# 60 秒: 20 分の実行で 20 件程度。送信キュー (maxsize=1000) を圧迫しない粒度。
DEFAULT_HEARTBEAT_INTERVAL_SEC = 60.0
# 600 秒: ネットワークドライブが不安定なときの一時的な待ちと、本当の停止を区別できる長さ。
DEFAULT_STALL_AFTER_SEC = 600.0
# 進捗ログの最短間隔。ファイル単位で送るとキューを溢れさせるので時間で間引く。
DEFAULT_PROGRESS_INTERVAL_SEC = 5.0
# これ未満で終わった工程は報告しない (端末にも Better Stack にも)。
#
# 取り込み1件で timed_step は十数回走るが、そのほとんどは 0.0 s で終わる。
# 1工程につき開始と完了の2件を送っていたので、1.4 秒の読み込みだけで 26 件が
# Better Stack に並び、時間を使った工程がその中に埋もれていた。
#
# 記録すべきなのは「時間を使った工程」と「失敗した工程」だけである。
# 開始のログは送らない。実行中の工程は heartbeat が (レジストリを読んで) 名指しし、
# 完了ログは所要時間を持っているので開始時刻はそこから引ける。沈黙して止まった
# 実行でも heartbeat が残り続けるので、開始ログが無くて困る場面は無い。
QUIET_UNDER_SEC = 1.0


def _env_float(name: str, default: float) -> float:
    """環境変数を float として読む。未設定・不正な値なら既定値を使う (監視でアプリを止めない)。"""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def describe_array(volume) -> dict:
    """所要時間を正規化して比較するための、配列のサイズ情報を返す。

    実体を持たない配列 (dask/xarray や shape/dtype だけのダミー) でも壊れないよう、
    属性が取れないものは黙って落とす。計測の付随情報のために処理を止めない。
    """
    fields: dict = {}
    try:
        fields["shape"] = str(tuple(volume.shape))
    except Exception:
        return fields
    try:
        n = 1
        for s in volume.shape:
            n *= int(s)
        fields["n_bytes"] = n * volume.dtype.itemsize
    except Exception:
        pass
    return fields


class StepHandle:
    """実行中の工程1つ分の状態。:func:`timed_step` が ``with ... as`` で返す。

    heartbeat スレッドがこのオブジェクトを読んで「今どこで何をしているか」を送るため、
    属性の更新はすべて単純な代入にとどめる (ロックを取らない)。多少古い値が混ざっても
    監視としては問題にならない一方、計測のためにロック待ちを発生させたくないため。
    """

    __slots__ = (
        "name", "fields", "started", "total", "done", "item",
        "progress_at", "tracked", "_interval", "_last_log",
    )

    def __init__(self, name: str, fields: dict, total: Optional[int],
                 progress_interval_sec: float) -> None:
        self.name = name
        self.fields = fields
        self.total = total
        self.started = time.perf_counter()
        self.done = 0
        self.item: Optional[str] = None
        # 最後に進捗があった時刻。「経過時間」ではなく「無進捗の時間」で停止を判定する
        # (正しく進み続けている長い工程を停止と誤判定しないため)。
        self.progress_at = self.started
        self.tracked = total is not None
        self._interval = progress_interval_sec
        self._last_log = self.started

    def advance(self, n: int = 1, item=None) -> None:
        """ループ1件分の進捗を記録し、必要なら進捗ログを送る。

        **これから処理する item に着手した時点で呼ぶ。** ``item`` に処理対象を渡しておくと、
        そこで止まった場合に heartbeat が「いま掴んだまま止まっているファイル」を名指しできる
        (停止箇所の特定にはこれが最も効く)。したがって ``done`` は「着手済み件数」を表す。

        送信は ``progress_interval_sec`` で時間間引きするので、呼び出し自体は
        属性更新だけで済み、5万件のループに入れてもコストにならない。
        """
        now = time.perf_counter()
        self.done += n
        if item is not None:
            self.item = str(item)
        self.progress_at = now
        self.tracked = True
        if now - self._last_log < self._interval:
            return
        self._last_log = now
        payload = dict(self.fields)
        payload.update(self.progress_fields(now))
        payload["step"] = self.name
        payload["event"] = "progress"
        log_info(
            "step progress: %s %s" % (self.name, self._progress_summary(now)),
            **payload,
        )

    def progress_fields(self, now: Optional[float] = None) -> dict:
        """進捗を Better Stack 側で集計できる構造化フィールドにして返す。"""
        if not self.tracked:
            return {}
        if now is None:
            now = time.perf_counter()
        elapsed = max(now - self.started, 1e-9)
        rate = self.done / elapsed
        fields = {
            "done": self.done,
            "items_per_sec": round(rate, 3),
            # 無進捗の時間。止まっているかどうかはこの値で判断する。
            "since_progress_sec": round(now - self.progress_at, 1),
        }
        if self.total:
            fields["total"] = self.total
            fields["percent"] = round(100.0 * self.done / self.total, 1)
            if rate > 0:
                fields["eta_sec"] = round(max(self.total - self.done, 0) / rate, 1)
        if self.item is not None:
            fields["item"] = self.item
        return fields

    def _progress_summary(self, now: float) -> str:
        """人が読む1行 (標準出力とログ本文用)。"""
        elapsed = max(now - self.started, 1e-9)
        rate = self.done / elapsed
        if self.total:
            text = "%d/%d (%.1f%%) %.1f items/s" % (
                self.done, self.total, 100.0 * self.done / self.total, rate,
            )
            if rate > 0:
                text += ", ETA %.0f s" % (max(self.total - self.done, 0) / rate)
        else:
            text = "%d items, %.1f items/s" % (self.done, rate)
        if self.item is not None:
            text += " (at %s)" % self.item
        return text


# ---------------------------------------------------------------------------
# 実行中の工程レジストリ
#
# heartbeat スレッドが「今どの工程を実行中か」を読むための、プロセス内の共有状態。
# スレッドごとにスタックを持つので、複数スレッドで並行に処理していてもそれぞれの
# 現在地が分かる。
# ---------------------------------------------------------------------------

_registry_lock = threading.Lock()
_active_steps: dict = {}


def _push(step: StepHandle) -> None:
    with _registry_lock:
        _active_steps.setdefault(threading.get_ident(), []).append(step)


def _pop(step: StepHandle) -> None:
    """レジストリから step を外す (多重呼び出しは無害)。

    例外で内側の工程を飛び越えて抜けた場合に取り残しが出ないよう、自分より上
    (内側) に積まれたものもまとめて外す。
    """
    with _registry_lock:
        stack = _active_steps.get(threading.get_ident())
        if not stack:
            return
        for i in range(len(stack) - 1, -1, -1):
            if stack[i] is step:
                del stack[i:]
                break
        if not stack:
            _active_steps.pop(threading.get_ident(), None)


def active_steps() -> list:
    """実行中の工程スタックのスナップショットを ``[(thread_ident, [StepHandle, ...]), ...]`` で返す。"""
    with _registry_lock:
        return [(ident, list(stack)) for ident, stack in _active_steps.items() if stack]


@contextlib.contextmanager
def timed_step(step: str, *, total: Optional[int] = None,
               progress_interval_sec: Optional[float] = None, **fields):
    """with ブロックの所要時間を計測し、開始・完了 (または失敗) を Better Stack へ送る。

    ブロック実行中は「この工程を実行中」としてレジストリに登録されるので、
    :func:`start_heartbeat` を有効にしてあれば、途中で止まっても heartbeat が
    工程名と経過時間を送り続ける。

    Args:
        step: 工程名。Better Stack 側で工程別に集計するキーになるので、実行ごとに
            変わらない安定した名前を使う (対象パス等は fields に入れる)。
        total: ループの総件数。渡すと進捗率と ETA が出せる。件数が事前に分からない
            場合は省略してよい (``advance()`` を呼べば件数と速度だけが出る)。
        progress_interval_sec: 進捗ログの最短間隔 (既定 5 秒)。
        **fields: ログへ付ける追加フィールド (target / n_bytes / shape など)。
            stage / target_file は analysis_context が外側から自動で付けるため、
            ここで渡す必要はない。

    Yields:
        StepHandle: ``advance()`` で進捗を報告できるハンドル。``as`` を省略してもよい。

    失敗時は所要時間つきの warning を出してから例外をそのまま送出する。失敗そのものの
    報告 (原因と対処の案内) は呼び出し側の except が log_error で行うので、ここでは
    「どの工程で何秒使って落ちたか」の記録に徹して二重報告にしない。
    """
    if progress_interval_sec is None:
        progress_interval_sec = DEFAULT_PROGRESS_INTERVAL_SEC
    handle = StepHandle(step, fields, total, progress_interval_sec)

    # 開始のログは出さない (QUIET_UNDER_SEC の説明を参照)。実行中であることは
    # レジストリに載るので heartbeat が名指しでき、完了ログが所要時間を持つ。
    _push(handle)
    try:
        yield handle
    except BaseException as e:
        elapsed = time.perf_counter() - handle.started
        payload = dict(fields)
        payload.update(handle.progress_fields())
        payload["step"] = step
        payload["event"] = "failed"
        payload["duration_sec"] = round(elapsed, 3)
        payload["error_type"] = type(e).__name__
        # 失敗は所要時間によらず必ず報告する (どこで落ちたかが唯一の手がかり)。
        log_warning(
            "step failed: %s after %.1f s (%s)" % (step, elapsed, type(e).__name__),
            **payload,
        )
        raise
    finally:
        _pop(handle)

    elapsed = time.perf_counter() - handle.started
    payload = dict(fields)
    payload.update(handle.progress_fields())
    payload["step"] = step
    payload["event"] = "done"
    payload["duration_sec"] = round(elapsed, 3)
    if elapsed < QUIET_UNDER_SEC:
        # 一瞬で終わった工程は報告しない。取り込み1件で十数回走り、そのほとんどが
        # これに当たるので、残すと時間を使った工程が埋もれる。
        return
    log_info("step done: %s in %.1f s" % (step, elapsed), **payload)


# ---------------------------------------------------------------------------
# 生存確認 (heartbeat)
#
# 「止まった」実行は完了ログを出さないまま沈黙するため、記録を待つ側の仕組みだけでは
# 検出できない。定期的にこちらから状態を送ることで、沈黙そのものを情報に変える。
# ---------------------------------------------------------------------------

_hb_lock = threading.Lock()
_hb_thread: Optional[threading.Thread] = None
_hb_stop: Optional[threading.Event] = None
_hb_started_at: Optional[float] = None
_hb_atexit_registered = False


def _emit_heartbeat(stall_after_sec: float) -> None:
    """実行中の工程それぞれについて生存確認を1件送る (工程が無ければ idle を1件)。"""
    now = time.perf_counter()
    alive_sec = round(now - _hb_started_at, 1) if _hb_started_at is not None else None
    snapshot = active_steps()

    if not snapshot:
        # 工程の切れ目。何も送らないと「止まった」のと区別が付かないので、
        # 生きていることだけは伝える。
        log_info(
            "heartbeat: idle (no step running)",
            step="(idle)", event="heartbeat", alive_sec=alive_sec,
        )
        return

    for ident, stack in snapshot:
        inner = stack[-1]
        elapsed = now - inner.started
        # 進捗を報告している工程は「無進捗の時間」で、そうでない工程は「経過時間」で
        # 停止を判定する。長いが着実に進んでいる工程を停止と誤判定しないため。
        idle_for = (now - inner.progress_at) if inner.tracked else elapsed
        stalled = stall_after_sec > 0 and idle_for >= stall_after_sec

        payload = dict(inner.fields)
        payload.update(inner.progress_fields(now))
        # 監視側のキーは常に優先する (呼び出し側の fields と衝突しても壊れないように)。
        payload.update({
            "step": inner.name,
            "event": "heartbeat",
            "elapsed_sec": round(elapsed, 1),
            # 「project > session > load_image > thorlab.stack」のような呼び出し階層。
            # どのセッションの取り込みで止まったのかが1フィールドで分かる。
            "step_stack": " > ".join(s.name for s in stack),
            "alive_sec": alive_sec,
            "thread": ident,
            "stalled": stalled,
        })

        if stalled:
            log_warning(
                "heartbeat: %s appears STALLED - no progress for %.0f s (running %.0f s)%s"
                % (inner.name, idle_for, elapsed,
                   "; last item: %s" % inner.item if inner.item else ""),
                **payload,
            )
        else:
            summary = " " + inner._progress_summary(now) if inner.tracked else ""
            log_info(
                "heartbeat: %s running for %.0f s%s" % (inner.name, elapsed, summary),
                **payload,
            )


def _heartbeat_worker(stop_event: threading.Event, interval: float,
                      stall_after_sec: float) -> None:
    # wait() は間隔待ちと停止要求の待ち受けを兼ねる (True が返れば停止要求)。
    while not stop_event.wait(interval):
        try:
            _emit_heartbeat(stall_after_sec)
        except Exception:
            # 監視の失敗でアプリ本体を止めない。
            pass


def start_heartbeat(interval_sec: Optional[float] = None,
                    stall_after_sec: Optional[float] = None) -> None:
    """生存確認の定期送信を開始する (冪等)。

    長時間バッチの入口 (sorter の app() など) で1回呼ぶ。デーモンスレッドなので
    プロセスの終了を妨げず、``BETTER_STACK_TOKEN`` 未設定なら送信自体がスキップされる。

    Args:
        interval_sec: 送信間隔 (既定 60 秒、``YLAB_HEARTBEAT_SEC`` で上書き可)。
        stall_after_sec: この秒数だけ進捗が無ければ heartbeat を warning へ格上げする
            (既定 600 秒、``YLAB_STALL_AFTER_SEC`` で上書き可)。0 以下で無効。
    """
    global _hb_thread, _hb_stop, _hb_started_at, _hb_atexit_registered

    if interval_sec is None:
        interval_sec = _env_float(HEARTBEAT_INTERVAL_ENV, DEFAULT_HEARTBEAT_INTERVAL_SEC)
    if stall_after_sec is None:
        stall_after_sec = _env_float(STALL_AFTER_ENV, DEFAULT_STALL_AFTER_SEC)
    if interval_sec <= 0:
        return

    with _hb_lock:
        if _hb_thread is not None and _hb_thread.is_alive():
            return
        _hb_started_at = time.perf_counter()
        _hb_stop = threading.Event()
        _hb_thread = threading.Thread(
            target=_heartbeat_worker,
            args=(_hb_stop, interval_sec, stall_after_sec),
            daemon=True,
            name="ylab-perf-heartbeat",
        )
        _hb_thread.start()
        if not _hb_atexit_registered:
            atexit.register(stop_heartbeat)
            _hb_atexit_registered = True


def stop_heartbeat() -> None:
    """生存確認の定期送信を止める (開始していなければ何もしない)。"""
    global _hb_thread, _hb_stop
    with _hb_lock:
        thread, stop = _hb_thread, _hb_stop
        _hb_thread, _hb_stop = None, None
    if stop is not None:
        stop.set()
    if thread is not None:
        # 送信中の1件を待つだけ。ここでプロセス終了を待たせない。
        thread.join(timeout=5)

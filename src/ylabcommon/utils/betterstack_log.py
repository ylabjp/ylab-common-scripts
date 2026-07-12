"""解析パイプライン全体のエラー・警告を Better Stack (Logs) へ非同期送信する。

behavior-analysis リポジトリで実績のある実装を ylabcommon へ移設したもの。
crawler など ylabcommon の共通基盤から使えるよう、特定リポジトリに依存しない形にしている。

- BETTER_STACK_TOKEN が環境変数(.env 等)に無い場合は送信をスキップする
  (ローカル実行やCIをBetter Stack未設定でも壊さないため)。
- 送信はバックグラウンドスレッドのキュー経由で行われ、呼び出し元(解析処理)をブロックしない。
- analysis_context() で stage/config/target_file をwithブロック内に暗黙的に伝搬できる。
  CrawlContextを持たない深い階層からの警告もこれで出処を残せる。
"""
import atexit
import contextlib
import contextvars
import os
import queue
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

TOKEN_ENV = "BETTER_STACK_TOKEN"
HOST_ENV = "BETTER_STACK_HOST"
DEFAULT_HOST = "https://in.logs.betterstack.com"

_ctx_var: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "ylabcommon_betterstack_ctx", default={}
)

_queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=1000)
_worker_lock = threading.Lock()
_worker_started = False
_warned_missing_token = False


@contextlib.contextmanager
def analysis_context(*, stage: str | None = None, config: str | None = None, target_file=None):
    """このwithブロック内で送信されるログに stage/config/target_file を自動付与する。

    ネストした場合は外側の値を引き継ぎ、指定したキーだけ上書きする。
    """
    parent = _ctx_var.get()
    merged = dict(parent)
    if stage is not None:
        merged["stage"] = stage
    if config is not None:
        merged["config"] = config
    if target_file is not None:
        merged["target_file"] = str(target_file)
    token = _ctx_var.set(merged)
    try:
        yield
    finally:
        _ctx_var.reset(token)


def _enabled() -> bool:
    return bool(os.environ.get(TOKEN_ENV))


def _worker() -> None:
    while True:
        payload = _queue.get()
        try:
            if payload is None:
                return
            token = os.environ.get(TOKEN_ENV)
            host = os.environ.get(HOST_ENV, DEFAULT_HOST)
            url = host if host.startswith("http") else f"https://{host}"
            try:
                requests.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
            except Exception:
                # Better Stackへの送信失敗で解析本体を止めない
                traceback.print_exc()
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, daemon=True, name="betterstack-sender").start()
        atexit.register(_flush_on_exit)
        _worker_started = True


def _flush_on_exit() -> None:
    try:
        _queue.put_nowait(None)
        _queue.join()
    except Exception:
        pass


def send(
    level: str,
    message: str,
    *,
    stage: str | None = None,
    config: str | None = None,
    target_file: Path | str | None = None,
    error: Exception | None = None,
) -> None:
    """Better Stackへ1件のログを非同期送信する(キュー投入のみで即時return)。"""
    global _warned_missing_token
    if not _enabled():
        if not _warned_missing_token:
            print(
                f"[betterstack] {TOKEN_ENV} is not set; skipping Better Stack log delivery "
                "(set it in your .env / environment)"
            )
            _warned_missing_token = True
        return

    ctx = _ctx_var.get()
    payload = {
        "dt": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
        "stage": stage if stage is not None else ctx.get("stage"),
        "config": config if config is not None else ctx.get("config"),
        "target_file": str(target_file) if target_file is not None else ctx.get("target_file"),
    }
    if error is not None:
        payload["error_type"] = type(error).__name__
        payload["traceback"] = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )

    _ensure_worker()
    try:
        _queue.put_nowait(payload)
    except queue.Full:
        print("[betterstack] send queue is full; dropping log event")


def log_warning(
    message: str,
    *,
    stage: str | None = None,
    config: str | None = None,
    target_file: Path | str | None = None,
) -> None:
    print(f"[WARNING] {message}")
    send("warning", message, stage=stage, config=config, target_file=target_file)


def log_error(
    message: str,
    *,
    stage: str | None = None,
    config: str | None = None,
    target_file: Path | str | None = None,
    error: Exception | None = None,
) -> None:
    print(f"[ERROR] {message}")
    send("error", message, stage=stage, config=config, target_file=target_file, error=error)

"""Better Stack (Logs) への非同期ログ送信 (自己完結・標準ライブラリのみ)。

ylab 共通の Better Stack ロギング基盤。canonical は ylab-common-scripts
(``ylabcommon/utils/betterstack_log.py``) に置き、ylabcommon を依存に取り込めない
軽量アプリ (behavior-controller / slice-controller 等) へは本ファイルを同一内容で
vendor する。両者が食い違わないよう、変更は canonical 側で行い各リポジトリへ反映する。

- ``BETTER_STACK_TOKEN`` が環境変数/.env に無ければ送信をスキップする
  (ローカル実行や CI を Better Stack 未設定でも壊さないため)。
- 送信はデーモンスレッド + キュー経由で非同期に行い、呼び出し元をブロックしない。
- ``requests`` 等の外部依存を持たず標準ライブラリ (urllib) のみで送信する。
- ``init_logging()`` で .env を読み込みトークンを解決する。
- ``install_excepthook()`` で未捕捉例外を自動送信する (GUI/常駐アプリのクラッシュ検知)。
- ``log_context()`` で with ブロック内のログへ任意のフィールドを自動付与できる。
  解析パイプライン向けには ``analysis_context()`` (stage/config/target_file) を用意する。
"""
import atexit
import contextlib
import contextvars
import json
import os
import queue
import sys
import threading
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TOKEN_ENV = "BETTER_STACK_TOKEN"
KEY_ENV = "BETTER_STACK_KEY"
HOST_ENV = "BETTER_STACK_HOST"
DEFAULT_HOST = "https://in.logs.betterstack.com"

_ctx_var: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "ylab_betterstack_ctx", default={}
)

_queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=1000)
_worker_lock = threading.Lock()
_worker_started = False
_warned_missing_token = False
_flushed = False
_app_name: Optional[str] = None
_initialized = False


@contextlib.contextmanager
def log_context(**fields):
    """この with ブロック内で送信されるログへ任意のフィールドを付与する。

    ネストした場合は外側の値を引き継ぎ、指定したキー (None 以外) だけ上書きする。
    """
    parent = _ctx_var.get()
    merged = dict(parent)
    for key, value in fields.items():
        if value is not None:
            merged[key] = str(value) if isinstance(value, Path) else value
    token = _ctx_var.set(merged)
    try:
        yield
    finally:
        _ctx_var.reset(token)


@contextlib.contextmanager
def analysis_context(*, stage=None, config=None, target_file=None):
    """解析パイプライン向け: stage/config/target_file を with ブロック内へ伝搬する。

    ``log_context`` の薄いラッパ (後方互換のために維持)。CrawlContext を持たない
    深い階層からの警告も、これで出処 (stage/target_file) を残せる。
    """
    with log_context(stage=stage, config=config, target_file=target_file):
        yield


def _parse_env_file(path: Path) -> dict:
    """.env を簡易パースする (KEY=VALUE 形式、# コメント/空行は無視)。"""
    env: dict = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                env[key] = value
    except OSError:
        pass
    return env


def _find_env_file(start_dir: Path) -> Optional[Path]:
    """start_dir から親方向へ .env を探索する。"""
    cur = start_dir
    while True:
        candidate = cur / ".env"
        if candidate.exists():
            return candidate
        if cur.parent == cur:
            return None
        cur = cur.parent


def init_logging(base_path=None, app: Optional[str] = None) -> None:
    """.env を読み込み Better Stack のトークンを環境変数へ設定する (冪等)。

    base_path: .env を親方向へ探索する起点。省略時は本ファイルの位置。
    app: ログに付与するアプリ名 (例 "cc_controller")。
    """
    global _initialized, _app_name
    if app is not None:
        _app_name = app
    if _initialized:
        return
    _initialized = True

    if base_path is None:
        base_path = Path(__file__).resolve().parent
    env_path = _find_env_file(Path(base_path))
    if env_path is not None:
        for key, value in _parse_env_file(env_path).items():
            # 既存の環境変数を上書きしない (実行環境側の設定を優先)
            os.environ.setdefault(key, value)

    # トークンは BETTER_STACK_TOKEN を使う。KEY のみ設定されている場合は補完する。
    if not os.environ.get(TOKEN_ENV) and os.environ.get(KEY_ENV):
        os.environ[TOKEN_ENV] = os.environ[KEY_ENV]


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
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10).close()
            except Exception:
                # Better Stack への送信失敗でアプリ本体を止めない
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
        threading.Thread(
            target=_worker, daemon=True, name="betterstack-sender"
        ).start()
        atexit.register(flush)
        _worker_started = True


def flush() -> None:
    """キューに残ったログを送信し切ってから戻る (プロセス終了前に呼ぶ)。"""
    global _flushed
    if _flushed or not _worker_started:
        return
    _flushed = True
    try:
        _queue.put_nowait(None)
        _queue.join()
    except Exception:
        pass


def send(
    level: str,
    message: str,
    *,
    error: Optional[BaseException] = None,
    **fields,
) -> None:
    """Better Stack へ1件のログを非同期送信する (キュー投入のみで即時 return)。

    stage / config / target_file 等は fields として渡せる (解析パイプライン互換)。
    """
    global _warned_missing_token
    if not _enabled():
        if not _warned_missing_token:
            print(
                f"[betterstack] {TOKEN_ENV} is not set; skipping Better Stack log delivery "
                "(set it in your .env / environment)"
            )
            _warned_missing_token = True
        return

    payload = {
        "dt": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
        "app": _app_name,
    }
    payload.update(_ctx_var.get())
    for key, value in fields.items():
        if value is not None:
            payload[key] = str(value) if isinstance(value, Path) else value
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


def log_info(message: str, **fields) -> None:
    print(f"[INFO] {message}")
    send("info", message, **fields)


def log_warning(message: str, **fields) -> None:
    print(f"[WARNING] {message}")
    send("warning", message, **fields)


def log_error(message: str, *, error: Optional[BaseException] = None, **fields) -> None:
    print(f"[ERROR] {message}")
    send("error", message, error=error, **fields)


def install_excepthook(app: Optional[str] = None) -> None:
    """未捕捉例外を Better Stack へ送信する excepthook を設定する。

    メインスレッド (sys.excepthook) とサブスレッド (threading.excepthook) の
    両方に対応する。GUI (PyQt) のスロット内で発生した未捕捉例外も sys.excepthook を
    経由するため捕捉できる。
    """
    global _app_name
    if app is not None:
        _app_name = app

    prev_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        try:
            if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
                log_error(f"Uncaught exception: {exc_value}", error=exc_value)
        finally:
            prev_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

    prev_thread_hook = threading.excepthook

    def _thread_hook(args):
        try:
            if args.exc_type is not None and not issubclass(
                args.exc_type, (KeyboardInterrupt, SystemExit)
            ):
                log_error(
                    f"Uncaught thread exception in {args.thread.name}: {args.exc_value}",
                    error=args.exc_value,
                )
        finally:
            prev_thread_hook(args)

    threading.excepthook = _thread_hook

"""図を作ったコードの出所(リポジトリ / commit / 未コミット変更の有無)。

`docs/reporting-spec.md` の `source` フィールドの実装。図から「どのコードで作ったか」を
辿れるようにするのがこの機能の要点なので、**取得に失敗しても解析は止めない**が、
分からなかったことは `None` として明示的に残す(キーごと消すと「調べていない」のか
「調べて分からなかった」のか区別できない)。
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: git の呼び出しが刺さっても解析を止めないための上限(秒)。
#: `git status` は大きな作業ツリーだと数秒かかることがある。
GIT_TIMEOUT_S = 15.0


def _git(args: list[str], cwd: Path) -> str | None:
    """git を1回呼んで stdout を返す。失敗・git 無し・タイムアウトなら None。"""
    try:
        r = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


@dataclass(frozen=True)
class SourceInfo:
    """`FigureStore` が全レコードに付ける出所情報。

    commit は **短縮せず 40 桁**で入れる。spec の例は `abc1234` と短縮形だが、
    短縮 sha は将来衝突しうるうえ復元もできないため、記録は完全な sha にして
    表示側(report.md)で短くする。
    """

    repo: str | None = None
    commit: str | None = None
    dirty: bool | None = None
    script: str | None = None
    params_hash: str | None = None

    @classmethod
    def capture(
        cls,
        path: Path | str | None = None,
        script: Path | str | None = None,
        params_hash: str | None = None,
    ) -> "SourceInfo":
        """呼び出し元のリポジトリ状態を読み取る。

        path: どのリポジトリを見るか。省略時は**呼び出し元のファイルの場所**。
        script: 記録するスクリプト名。省略時は呼び出し元ファイルのリポジトリ相対パス。
        params_hash: 解析パラメータのハッシュ。**何を入れてどう正規化するかは
            spec の open question のままなので、ここでは決めずに呼び出し側から受ける。**
        """
        caller = _caller_file()
        base = Path(path) if path is not None else (
            caller.parent if caller is not None else Path.cwd()
        )
        if base.is_file():
            base = base.parent

        top = _git(["rev-parse", "--show-toplevel"], base)
        root = Path(top) if top else None
        commit = _git(["rev-parse", "HEAD"], base) if root else None
        status = _git(["status", "--porcelain"], base) if root else None

        script_path = Path(script) if script is not None else caller
        return cls(
            repo=root.name if root else None,
            commit=commit or None,
            dirty=(status != "") if status is not None else None,
            script=_relative_to(script_path, root),
            params_hash=params_hash,
        )

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "commit": self.commit,
            "dirty": self.dirty,
            "script": self.script,
            "params_hash": self.params_hash,
        }


def _caller_file() -> Path | None:
    """このモジュールの外側にある最初の呼び出し元ファイルを返す。

    `SourceInfo.capture()` を素で呼んだときに「呼んだスクリプト」を拾うため。
    ylabcommon.reporting 内部からの中継(FigureStore の既定値など)は読み飛ばす。
    """
    pkg_dir = Path(__file__).resolve().parent
    depth = 1
    while True:
        try:
            frame = sys._getframe(depth)
        except ValueError:
            return None
        name = frame.f_globals.get("__file__")
        if name:
            p = Path(name).resolve()
            if p.parent != pkg_dir:
                return p
        depth += 1


def _relative_to(path: Path | None, root: Path | None) -> str | None:
    """リポジトリ相対の posix パス。root 外/不明ならファイル名だけ返す。"""
    if path is None:
        return None
    if root is None:
        return path.name
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name

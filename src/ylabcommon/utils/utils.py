# src/thorlab_loader/utils.py
import fnmatch
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List
import logging
from datetime import datetime

def natural_sort_key(s: str):
    parts = re.split(r"(\d+)", s)
    return [int(p) if p.isdigit() else p.lower() for p in parts]

def scan_tiff_dir(folder: str):
    """``folder`` 直下の TIFF を **1 回の列挙で** 列挙し、``(paths, sizes)`` を返す。

    サイズは列挙の応答に最初から入っている (Windows の
    ``FindFirstFileW``/``FindNextFileW`` は ``nFileSizeHigh/Low`` を返し、SMB2 は
    1 応答に多数のエントリをまとめる)。``os.DirEntry`` はそれを保持しているので、
    ここで受け取っておけば後段が同じディレクトリをもう一度列挙せずに済む。

    以前は :func:`find_tiff_files` が列挙してサイズを捨て、サイズフィルタが
    :func:`sizes_from_dir_scan` でもう一度同じディレクトリを列挙していた。
    取り込み1回あたり列挙が2回、つまり SMB 越しの往復が2倍かかっていた。

    Returns:
        tuple[list[str], dict[str, int]]: 自然順に並べた絶対パスと ``{パス: バイト数}``。
    """
    p = Path(folder)
    if not p.exists():
        raise FileNotFoundError(f"Input folder not found: {folder}")
    base = p.resolve()

    entries = []
    with os.scandir(base) as it:
        for entry in it:
            if any(fnmatch.fnmatch(entry.name, ext) for ext in ("*.tif", "*.tiff")):
                entries.append(entry)
    entries.sort(key=lambda e: natural_sort_key(e.name))

    paths, sizes = [], {}
    for entry in entries:
        path = str(base / entry.name)
        paths.append(path)
        try:
            # 列挙時に取得済みの値。追加の往復は発生しない。
            sizes[path] = entry.stat().st_size
        except OSError:
            # 拾えなければ呼び出し側が個別 stat で補える (含めないだけ)。
            pass
    return paths, sizes


def find_tiff_files(folder: str) -> List[str]:
    """Return the absolute paths of the TIFF files directly under ``folder``.

    サイズも要るなら :func:`scan_tiff_dir` を使うこと。こちらを呼んでから
    サイズを別途取りに行くと、同じディレクトリを2回列挙することになる。

    ネットワークドライブ (SMB/UNC) 上での往復回数がこの関数の設計上の制約になる。

    以前は ``[str(x.resolve()) for x in files]`` で **1ファイルにつき** パスを解決して
    いた。``Path.resolve()`` は Windows では ``nt._getfinalpathname`` を 2 回
    (UNC 入力は ``\\\\?\\UNC\\...`` に化けるため、接頭辞なしの形をもう一度検証する)
    呼び、加えて非 strict モードの ``p.stat()`` が走る。1件あたり 3 往復、うち 2 回は
    ``CreateFileW`` を伴う本物のファイル open である。3001 件で 9003 往復。

    しかもこの解決は **何も生んでいなかった** (実測:
    ``[str(x.resolve()) for x in files] == [str(x) for x in files]`` が True)。
    ファイル名自体には解決すべき ``..`` もシンボリックリンクも無く、絶対パスに
    しているのはディレクトリ部分だけだからである。

    そこでディレクトリを **1回だけ** 解決し、以降は列挙した名前を連結する。
    返る文字列は従来と同一で、往復は 3001 x 3 から 1 x 3 になる。

    列挙も ``Path.glob`` から ``os.scandir`` に変えている。glob と同じ
    ``fnmatch``(= プラットフォームの大小文字規則) で絞るので選ばれるファイルは
    変わらないが、``os.DirEntry`` が列挙時に取得済みのサイズを保持するため、
    :func:`sizes_from_dir_scan` が追加の stat 無しでサイズを再利用できる。

    Note:
        ファイル名自体がシンボリックリンクだった場合、以前はリンク先の実体パスが
        返っていたが、今はリンクのパスが返る。Thorlabs の生データにも SMB 共有にも
        該当するものは無く、後段はどちらでも同じファイルを開く。
    """
    return scan_tiff_dir(folder)[0]


def sizes_from_dir_scan(paths, on_directory=None) -> Dict[str, int]:
    """``{path: size}`` を、ファイル単位の stat ではなくディレクトリ列挙から作る。

    ``os.path.getsize`` は1ファイルにつき1往復する。SMB 越しの 3001 ファイルでは
    それだけで数分になる一方、サイズはディレクトリ列挙の応答に最初から入っている
    (Windows は ``FindFirstFileW``/``FindNextFileW`` が ``nFileSizeHigh/Low`` を返し、
    SMB2 は 1 応答に多数のエントリをまとめる)。``os.scandir`` はその値を
    ``DirEntry.stat()`` にキャッシュするので、ディレクトリごとに 1 回列挙すれば
    全ファイル分のサイズが追加の往復なしで揃う。

    Args:
        paths: サイズを知りたいファイルパスの列。複数ディレクトリにまたがってよい
            (親ディレクトリごとに1回ずつ列挙する)。
        on_directory: 列挙を **始める直前** に、そのディレクトリのパスで呼ばれる
            省略可能なコールバック。共有が応答しなくなるとこの列挙で止まるので、
            進捗の ``item`` を「いま列挙しているディレクトリ」に更新するために使う
            (:func:`ylabcommon.utils.perf.timed_step` の ``advance``)。

    Returns:
        ``paths`` に含まれるもののうち、列挙で見つかったものだけの ``{パス: バイト数}``。
        見つからなかったものは **含めない** ので、呼び出し側は個別に stat して補える。
        列挙自体に失敗したディレクトリは黙って諦める (呼び出し側の従来経路に任せる)。

    Warning:
        ディレクトリ列挙が返すサイズは、Windows では「書き込み中のファイル」に対して
        古い値になりうる (メタデータの更新が遅延するため)。取得と同時に走らせる
        運用では、サイズを根拠に **捨てる** 判断だけは個別 stat で裏を取ること。
    """
    wanted = defaultdict(dict)
    for path in paths:
        head, tail = os.path.split(os.fspath(path))
        wanted[head][tail] = path

    sizes: Dict[str, int] = {}
    for directory, by_name in wanted.items():
        if on_directory is not None:
            on_directory(directory)
        try:
            with os.scandir(directory or ".") as it:
                for entry in it:
                    path = by_name.get(entry.name)
                    if path is None:
                        continue
                    try:
                        sizes[path] = entry.stat().st_size
                    except OSError:
                        pass
        except OSError:
            continue
    return sizes

def count_files_in_directory(directory_path):
    path = Path(directory_path)
    count = len([p for p in path.iterdir() if p.is_file()])
    return count

def get_theme():
    """Returns a dictionary of ANSI escape codes for styling."""
    return {
        "header": "\033[92m\033[1m",  # Bold Green
        "info": "\033[96m",           # Cyan
        "success": "\033[92m",        # Green
        "error": "\033[91m\033[1m",   # Bold Red
        "reset": "\033[0m",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "header1": "\033[38;5;48m\033[1m",  
        "success1": "\033[38;5;48m",   # Emerald Green
        "warning": "\033[93m",        # Yellow
    }

def style_print(text, style_key="header"):
    """Helper to print styled text with a reset."""
    theme = get_theme()
    style = theme.get(style_key, theme["reset"])
    print(f"{style}{text}{theme['reset']}")

def progress_bar(i, total, width=30):
    progress = i / total
    filled = int(width * progress)

    bar = "#" * filled + "-" * (width - filled)

    print(f"\r[{bar}] {i}/{total}", end="", flush=True)


# Simple colored logs
def setup_logging(args):
    level = logging.WARNING
    if args.debug:
        level = logging.DEBUG
    elif args.verbose:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(message)s"
    )

def log_info(msg):
    logging.info(f"\033[94m[INFO]\033[0m {msg}")


def log_done(msg):
    logging.done(f"\033[92m[DONE]\033[0m {msg}")


def log_warn(msg):
    logging.warning(f"\033[93m[WARN]\033[0m {msg}")


def log_error(msg):
    logging.error(f"\033[91m[ERROR]\033[0m {msg}")

def ensure_parent(path: str):
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


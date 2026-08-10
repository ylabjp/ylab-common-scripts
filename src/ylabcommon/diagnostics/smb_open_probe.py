#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smb_open_probe.py -- ネットワーク共有(SMB)上で「メタデータは速いのに open だけ遅い」を切り分けるプローブ

目的:
    CreateFile(= os.open)の待ち時間と ReadFile(= os.read)の待ち時間を「同じファイル上で分離して」計測する。
    これが AV スキャン / HSM リコール / oplock 待ち と 単なる低速リンク を分ける唯一の決定的な測定。

    2 MB のファイルが 91 秒かかるなら実効 ~23 kB/s。これは帯域の問題として説明できる領域ではない
    (10 Mbit の劣悪リンクでも 2 MB は約 2 秒)。したがって「open が遅いのか read が遅いのか」を
    分離できれば原因群はほぼ二分できる。

特徴:
    - 標準ライブラリのみ。パイプラインと同じインタプリタ・同じプロセス環境で実行できる。
    - 読み取り専用。ファイルを一切書かない・作らない・属性を変えない(O_RDONLY のみ)。
    - 1 ファイルごとにワーカスレッド + タイムアウト。共有がハングしていても全体が固まらない。
    - Windows では os.stat().st_file_attributes を見て OFFLINE / RECALL_ON_OPEN /
      RECALL_ON_DATA_ACCESS / REPARSE_POINT を直接判定する(= 階層ストレージ仮説の直接検証)。
    - 列挙(os.scandir)のコストを別建てで計測し、「列挙がリダイレクタのキャッシュを温めてしまう」
      という測定バイアスを明示する。--template で列挙を回避したコールド測定も可能。

使い方:
    # 通常(ディレクトリを列挙して先頭 20 件)
    python smb_open_probe.py "V:\\2PM_raw\\...\\img01"
    python smb_open_probe.py "\\\\yg-storage4\\Storage-4\\2PM_raw\\...\\img01" -n 10

    # 列挙を一切せず、ファイル名を算術生成する(= 真にコールドなメタデータ測定)
    python smb_open_probe.py "\\\\yg-storage4\\Storage-4\\..\\img01" \
        --template "ChanA_001_001_001_{:03d}.tif" --start 1 -n 10

    # 全読みを省く(巨大ファイル / 明らかに遅い共有で安全に回したいとき)
    python smb_open_probe.py <dir> --no-full-read

    # 機械可読出力
    python smb_open_probe.py <dir> --json

注意(測定設計上の正直な但し書き):
    * 同一ファイルに対して open を 3 回行うため、2 回目以降はサーバ側/クライアント側キャッシュ、
      および付与された oplock/lease の恩恵を受ける。これは欠陥ではなく、むしろ
      「初回だけ高い = ファイル単位の一度きりコスト(AV スキャン結果のキャッシュ、HSM リコール)」と
      「毎回高い = 恒常的コスト(oplock ブレーク、低速リンク、サーバ過負荷)」を分ける材料になる。
      本スクリプトはこれを cold open / warm open として明示的に分けて出力する。
    * 4 KiB 読みの後にリダイレクタが先読み(read-ahead)している可能性があるため、
      full read のスループットは楽観側に振れうる。過小評価ではなく過大評価方向のバイアス。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import threading
import time

PC = time.perf_counter

# ---------------------------------------------------------------------------
# 判定しきい値(すべて「ミリ秒」。LAN 上の SMB の正常値を基準に置く)
# ---------------------------------------------------------------------------
TH_LAN_OK_MS = 20.0        # LAN 正常域: 1 op あたり 0.5-20 ms(SMB Client Shares の Avg.sec/Read 相当)
TH_SLOW_MS = 100.0         # 明確に遅い
TH_STALL_MS = 1000.0       # 「待たされている」= 帯域では説明できない領域
TH_ASYM_RATIO = 3.0        # 1 個目 / 残りの中央値 がこれ以上なら「初回のみコスト」
TH_WARM_RATIO = 5.0        # cold open / warm open がこれ以上なら「ファイル単位の一度きりコスト」
GOOD_MIBPS = 30.0          # ギガビット LAN で普通に出る下限の目安
POOR_MIBPS = 5.0           # これ未満はデータ経路が明確に細い

FULL_READ_CHUNK = 1 << 20  # 1 MiB
HEADER_BYTES = 4096        # 実パイプラインが実際に必要としている TIFF ヘッダ相当

# Windows のファイル属性(os.stat().st_file_attributes に載る)
FILE_ATTRIBUTE_FLAGS = {
    0x00000200: "SPARSE_FILE",
    0x00000400: "REPARSE_POINT",
    0x00001000: "OFFLINE",                  # 典型的な HSM / テープ / アーカイブ済み
    0x00020000: "NOT_CONTENT_INDEXED",
    0x00040000: "RECALL_ON_OPEN",           # open でリコールが走る = まさに今回の症状
    0x00400000: "RECALL_ON_DATA_ACCESS",    # Azure File Sync / NAS コールド層の典型
}
HSM_FLAGS = {"OFFLINE", "RECALL_ON_OPEN", "RECALL_ON_DATA_ACCESS"}


# ---------------------------------------------------------------------------
# 小道具
# ---------------------------------------------------------------------------
NOISE_FLOOR_MS = 1.0  # これ未満は計測ノイズ。比を取っても意味がない。


def fmt_ms(v):
    """ミリ秒を読みやすく。None は '-'。ローカル SSD の sub-ms も潰さない桁数にする。"""
    if v is None:
        return "       -"
    if v >= 100000:
        return "%8.0f" % v
    if v >= 10:
        return "%8.1f" % v
    return "%8.3f" % v


def summarize(values):
    """min / median / max を返す。空なら None 三つ組。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return (None, None, None)
    return (min(vals), statistics.median(vals), max(vals))


def median_of(values):
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def decode_attrs(attrs):
    """st_file_attributes のビットを名前のリストへ。"""
    if attrs is None:
        return []
    return [name for bit, name in FILE_ATTRIBUTE_FLAGS.items() if attrs & bit]


# ---------------------------------------------------------------------------
# 環境情報(Windows / WSL2 / Linux をできる範囲で自己申告させる)
# ---------------------------------------------------------------------------
def describe_environment(target_dir):
    """実行環境と対象パスの素性を best-effort で調べる。失敗しても致命傷にしない。"""
    info = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "is_windows": os.name == "nt",
        "target": target_dir,
        "is_unc": target_dir.startswith("\\\\") or target_dir.startswith("//"),
        "drive_type": None,
        "unc_of_drive": None,
        "mount_hint": None,
    }

    if os.name == "nt":
        # マップドドライブなら「実体がどこか」を UNC に解決する。
        # ctypes は標準ライブラリなので外部依存にはならない。
        try:
            import ctypes
            from ctypes import wintypes

            drive = os.path.splitdrive(os.path.abspath(target_dir))[0]
            if drive and drive.endswith(":"):
                root = drive + "\\"
                types = {0: "UNKNOWN", 1: "NO_ROOT_DIR", 2: "REMOVABLE",
                         3: "FIXED(local)", 4: "REMOTE(network)", 5: "CDROM", 6: "RAMDISK"}
                dt = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
                info["drive_type"] = types.get(dt, str(dt))

                # WNetGetConnectionW: ドライブレター -> \\server\share
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                rc = ctypes.windll.mpr.WNetGetConnectionW(
                    ctypes.c_wchar_p(drive), buf, ctypes.byref(size))
                if rc == 0:
                    info["unc_of_drive"] = buf.value
        except Exception as exc:  # 環境依存なので握りつぶす
            info["drive_type"] = "lookup-failed: %s" % exc.__class__.__name__
    else:
        # Linux / WSL2: /proc/mounts から cifs / drvfs / 9p を拾う。
        # (WSL2 で \\server\share を触っている場合、実体は drvfs 経由の可能性が高い)
        try:
            best = None
            real = os.path.realpath(target_dir)
            with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mnt, fstype = parts[1], parts[2]
                    if real == mnt or real.startswith(mnt.rstrip("/") + "/"):
                        if best is None or len(mnt) > len(best[0]):
                            best = (mnt, fstype, parts[0])
            if best:
                info["mount_hint"] = "%s on %s type %s" % (best[2], best[0], best[1])
        except Exception:
            pass

    return info


# ---------------------------------------------------------------------------
# 対象ファイルの決定
# ---------------------------------------------------------------------------
def list_via_enumeration(target_dir, count):
    """os.scandir で列挙する。列挙自体の時間と総エントリ数も返す。

    重要: この列挙が SMB リダイレクタのディレクトリキャッシュを温めてしまうため、
    この後の os.stat() は「ネットワーク往復ゼロ」で返りうる。これはまさに元の症状
    (メタデータ 6000 回が速い)を説明しうるので、測定バイアスとして明示する。
    """
    t0 = PC()
    names = []
    total = 0
    with os.scandir(target_dir) as it:
        for entry in it:
            total += 1
            if entry.name.lower().endswith(".tif") or entry.name.lower().endswith(".tiff"):
                names.append(entry.name)
    enum_ms = (PC() - t0) * 1e3
    names.sort()
    return names[:count], enum_ms, total


def list_via_template(target_dir, template, start, count):
    """ファイル名を算術生成する。列挙しないのでキャッシュを温めない = 真のコールド測定。"""
    names = []
    for i in range(start, start + count):
        try:
            names.append(template.format(i))
        except (IndexError, KeyError, ValueError) as exc:
            raise SystemExit("--template の書式が不正です (%s): %r" % (exc, template))
    return names, None, None


# ---------------------------------------------------------------------------
# 本体: 1 ファイルの計測
# ---------------------------------------------------------------------------
def _measure_core(path, do_full, max_full_bytes, progress, out):
    """1 ファイルに対する全フェーズ。読み取り専用。progress は監視スレッド用の共有 dict。"""
    open_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    # NOTE: O_SEQUENTIAL / O_RANDOM はあえて付けない。CPython の組み込み open() と
    #       同じフラグにして、実パイプラインのアクセスパターンを再現するため。
    #       共有モードも CPython の os.open と同じ(= 他プロセスの読み書きを妨げない)。

    # --- フェーズ 1: stat -------------------------------------------------
    progress["phase"] = "stat"
    t = PC()
    st = os.stat(path)
    out["stat_ms"] = (PC() - t) * 1e3
    out["size"] = st.st_size
    attrs = getattr(st, "st_file_attributes", None)
    out["attr_raw"] = attrs
    out["attr_names"] = decode_attrs(attrs)
    out["reparse_tag"] = getattr(st, "st_reparse_tag", None)

    # --- フェーズ 2: open + close(0 バイト読まない) = コールド open ------
    # ここが決定的。CreateFile 単体の待ち時間。
    progress["phase"] = "open_cold"
    t = PC()
    fd = os.open(path, open_flags)
    out["open_cold_ms"] = (PC() - t) * 1e3
    progress["phase"] = "close_cold"
    t = PC()
    os.close(fd)
    out["close_cold_ms"] = (PC() - t) * 1e3

    # --- フェーズ 3: open + 先頭 4 KiB(実パイプラインが本当に必要な分) ---
    progress["phase"] = "open_hdr"
    t = PC()
    fd = os.open(path, open_flags)
    out["open_hdr_ms"] = (PC() - t) * 1e3
    progress["phase"] = "read_hdr"
    t = PC()
    buf = os.read(fd, HEADER_BYTES)
    out["read_hdr_ms"] = (PC() - t) * 1e3
    out["hdr_bytes"] = len(buf)
    if out["read_hdr_ms"] > 0:
        out["hdr_mibps"] = (len(buf) / (1024.0 * 1024.0)) / (out["read_hdr_ms"] / 1e3)
    # TIFF マジック(II* / MM*)の確認。中身は読むだけで書かない。
    out["tiff_magic"] = buf[:4].hex() if len(buf) >= 4 else ""
    out["looks_tiff"] = buf[:2] in (b"II", b"MM")
    os.close(fd)

    # --- フェーズ 4: open + 全読み --------------------------------------
    if do_full and out["size"] <= max_full_bytes:
        progress["phase"] = "open_full"
        t = PC()
        fd = os.open(path, open_flags)
        out["open_full_ms"] = (PC() - t) * 1e3
        progress["phase"] = "read_full"
        t = PC()
        got = 0
        while True:
            chunk = os.read(fd, FULL_READ_CHUNK)
            if not chunk:
                break
            got += len(chunk)
        out["read_full_ms"] = (PC() - t) * 1e3
        out["full_bytes"] = got
        if out["read_full_ms"] > 0:
            out["full_mibps"] = (got / (1024.0 * 1024.0)) / (out["read_full_ms"] / 1e3)
        os.close(fd)
    else:
        out["skipped_full"] = True

    progress["phase"] = "done"


def measure_one(path, do_full, max_full_bytes, timeout_s):
    """_measure_core をワーカスレッドで実行し、タイムアウトしたら「どのフェーズで詰まったか」を記録。

    ブロックしたスレッドは daemon なので放置してよい(I/O 待ちは GIL を解放しているため
    インタプリタ終了を妨げない)。共有がハングしていても本体は先に進める。
    """
    progress = {"phase": "start"}
    out = {"path": path, "name": os.path.basename(path)}
    err_box = []

    def work():
        try:
            _measure_core(path, do_full, max_full_bytes, progress, out)
        except BaseException as exc:  # noqa: BLE001 - 何が来ても計測は続ける
            err_box.append(exc)

    th = threading.Thread(target=work, name="probe", daemon=True)
    t0 = PC()
    th.start()
    th.join(timeout_s)

    if th.is_alive():
        snapshot = dict(out)  # スレッドがまだ触る可能性があるのでコピーを取る
        snapshot["timeout"] = True
        snapshot["timeout_phase"] = progress.get("phase")
        snapshot["timeout_after_s"] = PC() - t0
        return snapshot

    if err_box:
        exc = err_box[0]
        out["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        out["errno"] = getattr(exc, "errno", None)
        out["winerror"] = getattr(exc, "winerror", None)
        out["error_phase"] = progress.get("phase")
    return out


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------
def print_header(text):
    print("")
    print("=" * 78)
    print(text)
    print("=" * 78)


def print_per_file(results):
    print_header("1) ファイルごとの生の計測値 (ms)")
    print("%-34s %8s %8s %8s %8s %8s %9s" % (
        "file", "stat", "openCOLD", "openWARM", "hdr4KiB", "openFULL", "readFULL"))
    print("-" * 78)
    for r in results:
        if r.get("timeout"):
            print("%-34s  >>> TIMEOUT after %.1fs in phase '%s' <<<" % (
                r["name"][:34], r.get("timeout_after_s", 0.0), r.get("timeout_phase")))
            continue
        if r.get("error"):
            print("%-34s  !!! %s (phase=%s) !!!" % (
                r["name"][:34], r["error"], r.get("error_phase")))
            continue
        print("%-34s %s %s %s %s %s %s" % (
            r["name"][:34],
            fmt_ms(r.get("stat_ms")),
            fmt_ms(r.get("open_cold_ms")),
            fmt_ms(r.get("open_hdr_ms")),
            fmt_ms(r.get("read_hdr_ms")),
            fmt_ms(r.get("open_full_ms")),
            fmt_ms(r.get("read_full_ms")),
        ))
    print("")
    print("  openCOLD = そのファイルへの最初の CreateFile(0 バイト読まない)")
    print("  openWARM = 同じファイルへの 2 回目の CreateFile(キャッシュ/oplock 済み)")
    print("  hdr4KiB  = 先頭 4 KiB の ReadFile のみ = 実パイプラインが本当に必要としている I/O")


def print_phase_summary(ok):
    print_header("2) フェーズ別サマリ (ms) と スループット")
    rows = [
        ("stat()", "stat_ms"),
        ("open COLD (1st CreateFile)", "open_cold_ms"),
        ("close (cold)", "close_cold_ms"),
        ("open WARM (2nd CreateFile)", "open_hdr_ms"),
        ("read 4 KiB header", "read_hdr_ms"),
        ("open WARM (3rd CreateFile)", "open_full_ms"),
        ("read whole file", "read_full_ms"),
    ]
    print("%-30s %5s %10s %10s %10s" % ("phase", "n", "min", "median", "max"))
    print("-" * 78)
    for label, key in rows:
        vals = [r.get(key) for r in ok if r.get(key) is not None]
        lo, mid, hi = summarize(vals)
        print("%-30s %5d %s %s %s" % (label, len(vals), fmt_ms(lo), fmt_ms(mid), fmt_ms(hi)))

    hmibps = [r["hdr_mibps"] for r in ok if r.get("hdr_mibps") is not None]
    if hmibps:
        lo, mid, hi = summarize(hmibps)
        print("")
        print("  4 KiB ヘッダ読みの実効レート: min %.1f / median %.1f / max %.1f MiB/s" % (lo, mid, hi))
        print("  (4 KiB は 1 往復で終わるのでレイテンシ支配。帯域ではなく往復遅延の指標として読む)")

    mibps = [r["full_mibps"] for r in ok if r.get("full_mibps") is not None]
    if mibps:
        lo, mid, hi = summarize(mibps)
        print("")
        print("  全読みスループット: min %.1f / median %.1f / max %.1f MiB/s" % (lo, mid, hi))
        if mid > 500.0:
            print("  ※ ネットワークの実効帯域を超えています。OS のページキャッシュ、または")
            print("     4 KiB 読みの後にリダイレクタが先読みした分から返っている可能性が高い。")
    sizes = [r["size"] for r in ok if r.get("size")]
    if sizes:
        print("  ファイルサイズ: median %.2f MiB" % (statistics.median(sizes) / 1048576.0))


def print_asymmetry(ok):
    """3) 1 個目 vs 残り。「一度きりのコスト」か「恒常的なコスト」かを分ける。"""
    print_header("3) 初回ファイル vs 残り(一度きりコストか、恒常的コストか)")
    if len(ok) < 3:
        print("  計測できたファイルが少なすぎて判定できません (n=%d)。" % len(ok))
        return {}

    first, rest = ok[0], ok[1:]
    flags = {}
    for label, key in (("stat()", "stat_ms"),
                       ("open COLD", "open_cold_ms"),
                       ("read 4 KiB", "read_hdr_ms"),
                       ("read whole", "read_full_ms")):
        f = first.get(key)
        m = median_of([r.get(key) for r in rest])
        if f is None or m is None:
            continue
        ratio = (f / m) if m > 0 else float("inf")
        flags[key] = ratio
        if f < NOISE_FLOOR_MS and m < NOISE_FLOOR_MS:
            # 両方 1 ms 未満。比を取っても計測ノイズしか見ていない。
            mark = "  (両方ノイズ域 - 比に意味なし)"
        elif ratio >= TH_ASYM_RATIO and f > TH_LAN_OK_MS:
            mark = "  <== 初回のみ突出"
        else:
            mark = ""
        print("  %-12s 1個目 %s ms / 残りの中央値 %s ms  = x%.1f%s" % (
            label, fmt_ms(f).strip(), fmt_ms(m).strip(), ratio, mark))

    # cold open vs warm open(= ファイル単位の一度きりコストの検出)
    cold = median_of([r.get("open_cold_ms") for r in ok])
    warm = median_of([r.get("open_hdr_ms") for r in ok])
    print("")
    if cold is not None and warm is not None and warm > 0:
        cw = cold / warm
        print("  cold open 中央値 %s ms / warm open 中央値 %s ms = x%.1f" % (
            fmt_ms(cold).strip(), fmt_ms(warm).strip(), cw))
        if cold < NOISE_FLOOR_MS:
            print("  → cold open が 1 ms 未満。ここは何も待っていない。比は計測ノイズなので無視すること。")
        elif cw >= TH_WARM_RATIO and cold > TH_SLOW_MS:
            print("  → ファイル単位で「初回 open だけ」高コスト。AV のスキャン結果キャッシュ、")
            print("     または HSM のリコール(1 度実体化すれば以後オンライン)の署名。")
        elif cold > TH_SLOW_MS:
            print("  → 2 回目の open も遅い。ファイル単位の一度きりコストでは説明できない。")
            print("     oplock ブレーク待ち / サーバ側の恒常的な遅延 / 経路そのものを疑う。")
    return flags


def print_environment(env, enum_ms, enum_total, used_template):
    print_header("0) 環境と対象")
    print("  Python        : %s" % env["python"])
    print("  Platform      : %s" % env["platform"])
    print("  Target        : %s" % env["target"])
    print("  UNC path?     : %s" % env["is_unc"])
    if env.get("drive_type"):
        print("  Drive type    : %s" % env["drive_type"])
    if env.get("unc_of_drive"):
        print("  Mapped to     : %s" % env["unc_of_drive"])
    if env.get("mount_hint"):
        print("  Mount         : %s" % env["mount_hint"])
    if used_template:
        print("  Enumeration   : スキップ(--template で算術生成)= キャッシュを温めていない")
    elif enum_ms is not None:
        print("  Enumeration   : os.scandir %d エントリ を %.1f ms" % (enum_total or 0, enum_ms))
        print("                  ※ この列挙が SMB リダイレクタのディレクトリキャッシュを温めるため、")
        print("                     以降の stat() はネットワーク往復ゼロで返りうる。")
        print("                     「stat が速い」ことだけを根拠に AV と結論してはいけない理由がこれ。")


def print_storage_flags(ok):
    """Windows のファイル属性から HSM/階層ストレージを直接判定する。"""
    print_header("4) ファイル属性(階層ストレージ / オフライン判定)")
    any_attr = any(r.get("attr_raw") is not None for r in ok)
    if not any_attr:
        print("  st_file_attributes を取得できません(非 Windows で実行中、または取得失敗)。")
        print("  → 階層ストレージ仮説は、Windows 側で以下を実行して確認してください:")
        print("     Get-ChildItem <dir> -File | Select-Object -First 5 Name, Attributes")
        print("     属性に Offline / RecallOnDataAccess / ReparsePoint が出たら仮説 3 が本命。")
        return set()

    seen = set()
    for r in ok:
        for name in r.get("attr_names", []):
            seen.add(name)
    hits = sorted(seen)
    print("  観測された属性: %s" % (", ".join(hits) if hits else "(特筆すべきものなし)"))
    hsm = seen & HSM_FLAGS
    if hsm:
        print("  *** %s を検出。これは階層/アーカイブストレージの直接証拠です。***" % ", ".join(sorted(hsm)))
        print("      stat が安く、最初の open/read でリコールが走る挙動と完全に一致します。")
    else:
        print("  OFFLINE / RECALL_ON_OPEN / RECALL_ON_DATA_ACCESS は立っていない。")
        print("  → 少なくとも Windows に見えている範囲では、階層ストレージのリコールではない。")
        print("     (ただし NAS 内部で完結する自前の階層化は Windows から見えないことがある)")
    return seen


def print_verdict(ok, results, env, storage_flags):
    print_header("5) 判定")

    timeouts = [r for r in results if r.get("timeout")]
    errors = [r for r in results if r.get("error")]

    # --- 共有違反 / アクセス拒否 は先に潰す(仮説 2) ---------------------
    sharing = [r for r in errors
               if r.get("winerror") in (32, 33) or "Permission" in (r.get("error") or "")]
    if sharing:
        print("  [仮説 2 = 共有違反] WinError 32/33 または権限エラーを検出。")
        print("  → 顕微鏡の取得ソフトがファイルを排他で掴んでいる可能性が高い。")
        for r in sharing[:5]:
            print("     %s : %s" % (r["name"], r["error"]))
        print("")

    if timeouts:
        phases = [r.get("timeout_phase") for r in timeouts]
        print("  %d 件がタイムアウト。停止フェーズ: %s" % (len(timeouts), ", ".join(sorted(set(phases)))))
        open_phases = {"open_cold", "open_hdr", "open_full"}
        if set(phases) & open_phases:
            print("  *** CreateFile そのものでブロックしている。ワイヤ上を 1 バイトも流していない。***")
            print("      → 帯域仮説(4)は反証される。AV のオープン時スキャン / HSM リコール /")
            print("        oplock ブレーク待ち のいずれか。")
        if {"read_hdr", "read_full"} & set(phases):
            print("  read フェーズで停止 → データ経路側(リンク、NAS の負荷、リビルド、リコール)。")
        print("")

    if not ok:
        print("  完了した計測が 0 件。上記のタイムアウト/エラーがそのまま結論です。")
        print_next_steps(None)
        return

    o_cold = median_of([r.get("open_cold_ms") for r in ok])
    o_warm = median_of([r.get("open_hdr_ms") for r in ok])
    r_hdr = median_of([r.get("read_hdr_ms") for r in ok])
    r_full = median_of([r.get("read_full_ms") for r in ok])
    st = median_of([r.get("stat_ms") for r in ok])
    mibps = median_of([r.get("full_mibps") for r in ok])

    print("  中央値: stat %s ms | openCOLD %s ms | 4KiB read %s ms | full read %s ms%s" % (
        fmt_ms(st).strip(), fmt_ms(o_cold).strip(), fmt_ms(r_hdr).strip(),
        fmt_ms(r_full).strip(),
        (" (%.1f MiB/s)" % mibps) if mibps else ""))
    print("")

    open_stalled = o_cold is not None and o_cold >= TH_STALL_MS
    open_slow = o_cold is not None and o_cold >= TH_SLOW_MS
    read_slow = r_hdr is not None and r_hdr >= TH_SLOW_MS
    all_fast = (o_cold or 0) < TH_LAN_OK_MS * 3 and (r_hdr or 0) < TH_LAN_OK_MS * 3

    # 実効バイト毎秒: 「帯域では説明できない」ことを数値で示す
    # (open は 0 バイトしか読んでいないので「レート」は本来無意味だが、
    #  『もしこの待ち時間が帯域由来だとしたら何 B/s か』を示すと帯域仮説を数値で棄却できる)
    size = median_of([r.get("size") for r in ok]) or 0
    if o_cold and size and o_cold >= TH_SLOW_MS:
        implied = size / (o_cold / 1e3)
        print("  仮に open の待ち時間がすべて転送だったとすると %.0f B/s 相当"
              " (%.2f MiB を %.2f s)。" % (implied, size / 1048576.0, o_cold / 1e3))
        if implied < 200000 and o_cold >= TH_STALL_MS:
            print("  → これは帯域の領域ではない(10 Mbit の劣悪リンクでも 2 MiB は約 2 秒)。")
            print("     低速リンク仮説(4)は単独では成立しない。")
        print("")

    # --- 主判定 ----------------------------------------------------------
    if open_stalled and not read_slow:
        print("  ■ open >> read。CreateFile の中で待たされている(ワイヤとサーバのデータ経路は健全)。")
        print("    残る候補: 1) オープン時 AV スキャン  3) HSM/階層ストレージのリコール  2) oplock ブレーク")
        if storage_flags & HSM_FLAGS:
            print("    → ファイル属性に %s。仮説 3(階層ストレージ)が最有力。" % ", ".join(sorted(storage_flags & HSM_FLAGS)))
        elif o_warm is not None and o_cold / max(o_warm, 1e-9) >= TH_WARM_RATIO:
            print("    → 初回 open のみ高コスト = 仮説 1(AV、スキャン結果はファイル単位でキャッシュ)")
            print("      または仮説 3。両者はここまでの数値では区別できない。属性が clean なら AV 寄り。")
        else:
            print("    → 2 回目の open も遅い = AV のキャッシュでも HSM の実体化でも説明しにくい。")
            print("      仮説 2(取得ソフトが掴んでいて oplock ブレークが毎回走る)を優先して疑う。")
    elif open_slow and read_slow and mibps is not None and mibps < POOR_MIBPS:
        print("  ■ open も read も遅い = サーバ/経路が全面的に遅い。")
        print("    仮説 4(リンク飽和・NAS 過負荷/リビルド)または サーバ側 AV の全 I/O スキャン。")
    elif not open_slow and mibps is not None and mibps < POOR_MIBPS:
        print("  ■ open は速く read だけ遅い(%.1f MiB/s)。データ経路の問題。" % mibps)
        print("    仮説 4(帯域/NIC/NAS 負荷)、または データアクセス時リコール(RECALL_ON_DATA_ACCESS)。")
    elif all_fast:
        print("  ■ 今この瞬間は、すべて正常域(open %s ms / read %s ms)。" % (
            fmt_ms(o_cold).strip(), fmt_ms(r_hdr).strip()))
        print("    → 症状を再現できていない。次のいずれか:")
        print("      ・取得ソフトが動いている最中だけ発生する(仮説 2: oplock 競合)")
        print("      ・当該ディレクトリ/ファイル群だけの問題(仮説 6)→ 実際に詰まったパスで再実行する")
        print("      ・初回接続時のみのコスト(セッション確立/DFS 参照)→ 下の初回 vs 残りを見る")
        print("      ・既にリコール済み/スキャン済みで温まっている → 別の未アクセスの img ディレクトリで再実行")
    else:
        print("  ■ 中間的な値。単独では断定できない。下の追試を実施してください。")

    print("")
    print("  3001 ファイルへの外挿(ヘッダのみ読む実パイプライン相当 = openCOLD + 4KiB read):")
    if o_cold is not None and r_hdr is not None:
        per_ms = o_cold + r_hdr
        print("    %.2f ms/file x 3001 = %.1f 分" % (per_ms, per_ms * 3001 / 1000.0 / 60.0))
    print_next_steps(env)


def print_next_steps(env):
    print("")
    print("  --- 追試(このスクリプトだけでは決着しない部分) ---")
    print("  a) AV(仮説 1)")
    print("     注意: 『Defender は既定でネットワーク上のファイルをスキャンしないから AV は無罪』")
    print("     という推論は誤り。DisableScanningNetworkFiles(既定 True)が抑えるのは *スキャン")
    print("     エンジン* 側の経路であって、オンアクセス/リアルタイム保護は別設定で、そちらは")
    print("     すべて既定 ON (AllowRealtimeMonitoring / AllowOnAccessProtection /")
    print("     AllowIOAVProtection = 1)。Microsoft も『リアルタイム保護またはオンアクセス保護が")
    print("     有効なら、スキャン対象にネットワーク共有も含まれる』と明記している。")
    print("     したがって既定構成でも UNC の CreateFile でスキャンが走りうる。実機で見ること:")
    print("       Get-MpPreference | Select-Object DisableScanningNetworkFiles, DisableRealtimeMonitoring, DisableIOAVProtection, RealTimeScanDirection")
    print("     ただし先に『Defender がそもそも現役か』を確認する(第三者製 AV があると Passive):")
    print("       Get-MpComputerStatus | Select-Object AMRunningMode")
    print("     Passive なら Defender の計測(Performance Analyzer)は空になるが、それは AV 全体の")
    print("     無罪証明ではない。第三者製 AV を列挙する:")
    print("       Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct")
    print("       fltmc filters      (I/O 経路に何が挟まっているか。AV も階層化ドライバも見える)")
    print("     決定打は A/B: 当該 UNC パスを除外に入れて本スクリプトを再実行し、openCOLD が落ちるか。")
    print("     除外は必ず UNC 形式で。マップドライブ(V:)の除外は等価ではないと明記されている。")
    print("  b) 掴んでいるプロセス(仮説 2)")
    print("     サーバ側: Get-SmbOpenFile | Where-Object Path -like '*img01*'")
    print("     クライアント側: handle64.exe / Resource Monitor の CPU>関連付けられたハンドル")
    print("  c) NAS か Windows Server か(前提の検証。ここは必ず事実確認する)")
    print("     Test-NetConnection yg-storage4 -Port 445 ; nbtstat -A <ip>")
    print("     Get-SmbConnection | Format-List *   (Dialect / Encrypted / Signed を見る)")
    print("     ServerOS が空 / Dialect が 2.x 止まり なら NAS アプライアンス(Samba/独自実装)の疑い。")
    print("  d) 停止中に本当にバイトが流れているか(仮説 4 の直接反証)")
    print("     別ウィンドウで: Get-Counter '\\SMB Client Shares(*)\\Data Bytes/sec' -SampleInterval 1 -MaxSamples 120")
    print("     ストール中に ~0 B/s なら『低速リンク』は否定される。")
    print("  e) このディレクトリ固有か(仮説 6)")
    print("     別の img ディレクトリ、および同一サーバの別共有で本スクリプトを再実行して比較。")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="SMB 共有上の open 遅延を CreateFile / ReadFile に分離して計測する読み取り専用プローブ",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("directory", help="対象ディレクトリ(UNC でもマップドドライブでも可)")
    p.add_argument("-n", "--count", type=int, default=20, help="計測するファイル数 (既定 20)")
    p.add_argument("--template", default=None,
                   help="ファイル名を算術生成する書式。例: 'ChanA_001_001_001_{:03d}.tif'。"
                        "指定すると os.scandir を一切呼ばないのでキャッシュを温めない。")
    p.add_argument("--start", type=int, default=1, help="--template の開始インデックス (既定 1)")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="1 ファイルあたりのタイムアウト秒 (既定 30)")
    p.add_argument("--max-timeouts", type=int, default=3,
                   help="この件数タイムアウトしたら打ち切る (既定 3)")
    p.add_argument("--no-full-read", action="store_true", help="全読みフェーズを行わない")
    p.add_argument("--max-full-mib", type=float, default=64.0,
                   help="このサイズを超えるファイルは全読みしない (既定 64 MiB)")
    p.add_argument("--json", action="store_true", help="生データを JSON で標準出力に追記する")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    target = args.directory
    if args.count < 1:
        raise SystemExit("--count は 1 以上にしてください")

    if not os.path.isdir(target):
        raise SystemExit("ディレクトリが存在しない、または到達できません: %s" % target)

    env = describe_environment(target)

    if args.template:
        names, enum_ms, enum_total = list_via_template(target, args.template, args.start, args.count)
    else:
        try:
            names, enum_ms, enum_total = list_via_enumeration(target, args.count)
        except OSError as exc:
            raise SystemExit("列挙に失敗: %s" % exc)
        if not names:
            raise SystemExit("*.tif / *.tiff が見つかりません: %s" % target)

    print_environment(env, enum_ms, enum_total, bool(args.template))
    print("")
    print("計測開始: %d ファイル / 1 ファイルあたりタイムアウト %.0f 秒 (読み取り専用)" % (
        len(names), args.timeout))
    sys.stdout.flush()

    results = []
    n_timeout = 0
    max_full = int(args.max_full_mib * 1048576)
    for i, name in enumerate(names, 1):
        path = os.path.join(target, name)
        print("  [%d/%d] %s ..." % (i, len(names), name), end="")
        sys.stdout.flush()
        r = measure_one(path, not args.no_full_read, max_full, args.timeout)
        results.append(r)
        if r.get("timeout"):
            print(" TIMEOUT (%s)" % r.get("timeout_phase"))
            n_timeout += 1
            if n_timeout >= args.max_timeouts:
                print("  タイムアウトが %d 件に達したので打ち切ります。" % n_timeout)
                break
        elif r.get("error"):
            print(" ERROR %s" % r["error"])
        else:
            print(" openCOLD=%sms hdr4K=%sms" % (
                fmt_ms(r.get("open_cold_ms")).strip(), fmt_ms(r.get("read_hdr_ms")).strip()))
        sys.stdout.flush()

    ok = [r for r in results if not r.get("timeout") and not r.get("error")]

    print_per_file(results)
    if ok:
        print_phase_summary(ok)
        print_asymmetry(ok)
    storage_flags = print_storage_flags(ok) if ok else set()
    print_verdict(ok, results, env, storage_flags or set())

    if args.json:
        print_header("RAW JSON")
        print(json.dumps({"env": env, "enum_ms": enum_ms, "enum_entries": enum_total,
                          "results": results}, ensure_ascii=False, indent=2, default=str))

    # 終了コード: 2 = ストール検出、1 = エラーあり、0 = 正常域
    if any(r.get("timeout") for r in results):
        return 2
    if ok and (median_of([r.get("open_cold_ms") for r in ok]) or 0) >= TH_STALL_MS:
        return 2
    if any(r.get("error") for r in results):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断されました。", file=sys.stderr)
        sys.exit(130)

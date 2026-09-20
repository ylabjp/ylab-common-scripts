"""TIFF を **遅延 (dask) 配列** として正準レイアウト ``TCZYX`` で読む。

slice-analysis の ``models/stack_image.read_lazy_tczyx`` が元。**名前も返す形も
そちらに合わせてある** —— slice-analysis がそのまま import に差し替えられるように。
踏んだ問題と実測はこちらに写した。

いつ遅延が得か (**測ってから使うこと**)
--------------------------------------
遅延は「速い読み方」ではない。**大きいものの一部だけ読むとき**に効く仕掛けで、
小さいものを丸ごと読むときは余計な段取りのぶん損をする。この環境での実測:

    A) Keyence の生タイル 360x480 を **丸ごと** 読む
        BioImage dask         11.8 ms
        tifffile zarr         25.1 ms   <- 遅延のほうが 2 倍遅い
        tifffile 直読み        2.0 ms   <- 一番速い

    B) マージ後くらいの塊 (C=2, Z=40, 1024x1024, 160 MB) の **1 面だけ** 読む
        tifffile zarr          6.4 ms
        tifffile 直読み       50.6 ms   <- 8 倍遅い
       同じ 1 面のピーク RAM
        tifffile zarr          2.1 MB
        tifffile 直読み      160.0 MB   <- 76 倍

つまり **この関数はマージ後の切片 (B) 用**である。生タイルを全部読む経路 (A) に
持ち込むと遅くなる —— タイルは必ず丸ごと使うので、遅延にする意味が無い。

slice-analysis の ``read_lazy_tczyx`` は同じ選択を、大きい OME-TIFF (1024x1024)
での実測 —— BioImage dask 144.6 / tifffile zarr 28.0 / page 直読み 17.5 ms/面 ——
から取っている。**そちらと A の順位が逆なのは矛盾ではない**: 測っている物が
「大きい塊の 1 面」と「小さいファイル 1 つ丸ごと」で違う。

開いたままのファイル
--------------------
遅延配列は「読むときに開く」のではなく、**開いたハンドルを掴んだまま**必要な面
だけ読む。読み終わりが分からないので閉じ時を決められない。そこで開いたものを
控えておき、**上書きする直前に呼び出し側が閉じられるようにする**
(:func:`close_lazy_readers`)。

要る理由 (slice-analysis が実機で踏んだ): Windows は**開かれているファイルを
置き換えられない**。上書き保存が ``os.replace`` で ``PermissionError
[WinError 5]`` になり、本体は前回のまま残る。POSIX は開いたファイルへの rename
を許すので、Linux では再現しない。
"""
from __future__ import annotations

import os
from typing import Any

from ylabcommon.bioio.core.dim_order import to_tczyx

#: 開いたまま持っている ``TiffFile``。**パス -> ハンドルの一覧**。
_OPEN_LAZY_READERS: dict[str, list[Any]] = {}


def _reader_key(path: str | os.PathLike) -> str:
    """パスの表記ゆれを吸収した鍵。Windows は大文字小文字を区別しない。"""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def close_lazy_readers(path: str | os.PathLike) -> int:
    """``path`` に対して開いてある遅延読み出しを閉じる。閉じた数を返す。

    **これを呼んだあと、その配列からは読めなくなる。** 呼ぶのは「そのファイルを
    これから上書きする」ときだけにすること —— 上書きするなら、古い画素を指した
    配列はどのみち用済みである。
    """
    handles = _OPEN_LAZY_READERS.pop(_reader_key(path), [])
    closed = 0
    for tif in handles:
        try:
            tif.close()
            closed += 1
        except Exception:
            # 閉じられなくても先へ進む。ここで止めると、閉じるための処理が
            # 保存そのものを妨げることになる。
            pass
    return closed


def open_lazy_reader_count(path: str | os.PathLike) -> int:
    """``path`` に対して開いたままの数。後片付けを確かめるためのもの。"""
    return len(_OPEN_LAZY_READERS.get(_reader_key(path), []))


def read_lazy_tczyx(path: str | os.PathLike) -> Any:
    """TIFF を遅延配列として ``TCZYX`` で返す。遅延で読めなければ ``None``。

    ``None`` になるのは、TIFF でない・開けない・軸が ``TCZYX`` 以外を含む場合。
    呼び出し側は実体で読む従来の経路へ落とす。**理由を知りたいときは実体で
    読んでから** :func:`to_tczyx` **を呼ぶこと** —— そちらは黙って ``None`` に
    せず、どの軸が駄目かを言って落ちる。

    tifffile は**大きさ 1 の軸を落とす** (Z=1 で保存した OME-TIFF は ``TCYX``
    になる、slice-analysis 実測)。落ちた軸は :func:`to_tczyx` が戻す。
    """
    import dask.array as da
    import tifffile

    # **TiffFile を控える。** ``tifffile.TiffFile(path).series[0]`` と書くと
    # 開いたハンドルはどこからも掴めず、閉じる手が無くなる。
    try:
        tif = tifffile.TiffFile(os.fspath(path))
    except Exception:
        return None

    try:
        series = tif.series[0]
        array = to_tczyx(da.from_zarr(series.aszarr()), series.axes)
    except Exception:
        # 配列を返さないのにハンドルだけ残ると、そのファイルは以降どこからも
        # 上書きできなくなる。**ここで閉じる。**
        tif.close()
        return None

    _OPEN_LAZY_READERS.setdefault(_reader_key(path), []).append(tif)
    return array

"""ストリーミング書き出しが、生ファイルを **まとめて同時に** 読むことのテスト。

生データはネットワークドライブ (SMB) 上にあり、1 ファイル開くのに往復が数回
かかる (実測 ~320 ms)。一方 CPU は 1 件 0.23 ms しか使わない。つまりこの工程は
ほぼ全部が往復の待ちで、**同時に読むかどうかがそのまま実時間になる**。

回帰: 以前はブロックを Z 方向だけで区切っていた。Z=1 の連続撮影ではブロックが
1 面になり、6000 面を 1 面ずつ順番に読んでいた。実データで 2.5 面/秒・ETA 2350 秒
(約 39 分)。1 面あたり 300 ms の遅延を入れた再現では 3.2 面/秒 で、これを時点
まとめ + 32 並列にすると 85.6 面/秒 になった (27 倍)。
"""
from __future__ import annotations

import threading

import dask
import dask.array as da
import numpy as np
import tifffile

from ylabcommon.bioio.core import bioio_writer as W


class _Tracker:
    """同時に走った読み取りの最大数を数える。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0
        self.reads = 0

    def plane(self, t, c, z):
        with self.lock:
            self.now += 1
            self.reads += 1
            self.peak = max(self.peak, self.now)
        try:
            # 同時実行を観測できるだけの間、掴んでおく
            threading.Event().wait(0.02)
            return np.full((1, 4, 4), t * 100 + c * 10 + z, np.uint16)
        finally:
            with self.lock:
                self.now -= 1


def _volume(tracker, T, C, Z):
    rows = []
    for t in range(T):
        chans = []
        for c in range(C):
            zs = [da.from_delayed(
                dask.delayed(tracker.plane, pure=False)(t, c, z),
                shape=(1, 4, 4), dtype=np.uint16) for z in range(Z)]
            chans.append(zs[0] if Z == 1 else da.concatenate(zs, axis=0))
        rows.append(da.stack(chans, axis=0))         # (C, Z, Y, X)
    return da.stack(rows, axis=0)                    # (T, C, Z, Y, X)


def _write(tmp_path, vol, name="v.ome.tif"):
    out = tmp_path / name
    writer = W.BioIOWriter(out, compression="zlib", compression_level=1)
    writer._write_ometiff_streaming(
        vol, out, channel_names=None, physical_pixel_sizes=(1.0, 0.5, 0.5))
    return out


def test_planes_are_read_concurrently(tmp_path):
    """1 面ずつ順番に読まない。Z=1 でも同時に読む。

    ここが 1 に戻ると、実データでは 6000 面 x 0.32 秒 = 32 分になる。
    """
    tracker = _Tracker()
    _write(tmp_path, _volume(tracker, T=20, C=2, Z=1))

    assert tracker.reads == 40
    assert tracker.peak > 1, "1 面ずつ直列に読んでいる (peak=%d)" % tracker.peak


def test_the_written_plane_order_is_unchanged(tmp_path):
    """まとめて読んでも、書き出す順序は TCZYX のまま。

    まとめ読みで順序が崩れると、時点とチャンネルが入れ替わった volume が
    黙って出来上がる — 速くなった代わりに結果が壊れる、が最悪なので固定する。
    """
    tracker = _Tracker()
    T, C, Z = 7, 2, 3
    out = _write(tmp_path, _volume(tracker, T, C, Z), "order.ome.tif")

    got = tifffile.imread(out).reshape(T, C, Z, 4, 4)
    want = np.array([[[t * 100 + c * 10 + z for z in range(Z)]
                      for c in range(C)] for t in range(T)], np.uint16)
    np.testing.assert_array_equal(got[..., 0, 0], want)


def test_memory_stays_bounded_by_the_block_size(tmp_path, monkeypatch):
    """1 度に実体化するのはブロック 1 つぶんだけ (全量を RAM に載せない)。"""
    tracker = _Tracker()
    T, C, Z = 12, 2, 1
    # 1 ブロック = 2 時点ぶん (2 x C x Z x 4 x 4 x 2 bytes) に絞る
    monkeypatch.setattr(W, "_STREAM_BLOCK_BYTES", 2 * C * Z * 4 * 4 * 2)

    sizes = []
    real = W._read_block
    monkeypatch.setattr(W, "_read_block",
                        lambda b: sizes.append(int(np.prod(b.shape))) or real(b))
    _write(tmp_path, _volume(tracker, T, C, Z), "bounded.ome.tif")

    assert len(sizes) == T // 2, "ブロックに分けずに読んでいる"
    assert max(sizes) == 2 * C * Z * 4 * 4


# ---- 入力を畳む配列 (z 投影など) --------------------------------------------

def test_a_z_reducing_array_is_blocked_by_what_it_reads(tmp_path, monkeypatch):
    """出力ではなく **元をどれだけ読むか** でブロックを決めること。

    回帰 (実データでメモリ不足): z 投影は出力 Z=1 だが、1 時点を作るのに元の面が
    Z 枚要る。出力の大きさでブロックを決めると読む量を Z 倍見誤り、
    C=2・1024x1024・Z=41 で t_block=64 -> 元を 5248 面 = 10.5 GiB 一度に読んで
    落ちた (書きかけの 1 KB が残った)。
    """
    tracker = _Tracker()
    T, C, Z = 12, 2, 6
    volume = _volume(tracker, T, C, Z)
    projection = volume.sum(axis=2, keepdims=True, dtype=np.uint32).astype(np.uint16)

    # 1 ブロック = 元の 2 時点ぶん
    source_per_frame = C * Z * 4 * 4 * 2
    monkeypatch.setattr(W, "_STREAM_BLOCK_BYTES", 2 * source_per_frame)

    sizes = []
    real = W._read_block
    monkeypatch.setattr(W, "_read_block",
                        lambda b: sizes.append(int(b.shape[0])) or real(b))

    out = tmp_path / "proj.ome.tif"
    W.BioIOWriter(out, compression="zlib", compression_level=1)._write_ometiff_streaming(
        projection, out, channel_names=None, physical_pixel_sizes=(1.0, 0.5, 0.5),
        source_bytes_per_frame=source_per_frame)

    assert max(sizes) == 2, "元の読み出し量を無視してブロックを決めている (%s)" % sizes
    assert len(sizes) == T // 2


def test_the_hint_is_optional_and_changes_nothing_when_absent(tmp_path, monkeypatch):
    """渡さなければ従来どおり (出力の大きさで決める)。"""
    tracker = _Tracker()
    T, C, Z = 12, 2, 1
    monkeypatch.setattr(W, "_STREAM_BLOCK_BYTES", 2 * C * Z * 4 * 4 * 2)

    sizes = []
    real = W._read_block
    monkeypatch.setattr(W, "_read_block",
                        lambda b: sizes.append(int(b.shape[0])) or real(b))
    _write(tmp_path, _volume(tracker, T, C, Z), "default.ome.tif")

    assert max(sizes) == 2


def test_the_projection_values_survive_the_blocking(tmp_path):
    """ブロックに分けても、書き出した値が合計と一致すること。"""
    tracker = _Tracker()
    T, C, Z = 6, 2, 3
    volume = _volume(tracker, T, C, Z)
    projection = volume.sum(axis=2, keepdims=True, dtype=np.uint32).astype(np.uint16)

    out = tmp_path / "sum.ome.tif"
    W.BioIOWriter(out, compression="zlib", compression_level=1)._write_ometiff_streaming(
        projection, out, channel_names=None, physical_pixel_sizes=(1.0, 0.5, 0.5),
        source_bytes_per_frame=C * Z * 4 * 4 * 2)

    got = tifffile.imread(out).reshape(T, C, 1, 4, 4)
    np.testing.assert_array_equal(got, np.asarray(projection))

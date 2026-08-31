"""公開 API から流し書きの設定へ手が届くこと。

``source_bytes_per_frame`` と「流し書きを使うかどうか」は、``write()`` から渡す
手が無かった。そのため出力名を自分で決めている呼び出し側 (slice-analysis) は、
非公開の ``_write_ometiff_streaming`` を直接呼ぶしかなかった。**呼び出し側が
非公開のメソッドへ手を伸ばすのは、公開 API に必要な口が無いということ。**
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("dask")

import dask.array as da
import tifffile

import ylabcommon.bioio.core.bioio_writer as W

T, C, Z, Y, X = 8, 2, 3, 4, 4


def _lazy(source):
    return da.from_array(source, chunks=(1, C, Z, Y, X))


def _source(seed=0):
    return np.random.default_rng(seed).integers(
        0, 500, size=(T, C, Z, Y, X), dtype=np.uint16)


def test_the_read_size_hint_reaches_the_streaming_writer(tmp_path, monkeypatch):
    """回帰: 入力を畳む配列でブロックを Z 倍見誤り、メモリ不足で落ちた。

    出力の大きさで決めると z 投影で読む量を Z 倍取り違える。公開 API から
    その値を渡せなければ、呼び出し側は内部メソッドを呼ぶしかない。
    """
    seen = {}
    real = W.BioIOWriter._write_ometiff_streaming

    def _spy(self, data, out_file, **kw):
        seen.update(kw)
        return real(self, data, out_file, **kw)

    monkeypatch.setattr(W.BioIOWriter, "_write_ometiff_streaming", _spy)

    out = tmp_path / "proj.ome.tif"
    W.BioIOWriter(out, compression="zlib", compression_level=1).write(
        _lazy(_source()), source_bytes_per_frame=1234, stream=True)

    assert seen["source_bytes_per_frame"] == 1234


def test_the_hint_actually_sizes_the_blocks(tmp_path, monkeypatch):
    """渡した値でブロックが決まること (受け取るだけで捨てていない)。"""
    per_frame = C * Z * Y * X * 2
    monkeypatch.setattr(W, "_STREAM_BLOCK_BYTES", 2 * per_frame)

    sizes = []
    real = W._read_block
    monkeypatch.setattr(W, "_read_block",
                        lambda b: sizes.append(int(b.shape[0])) or real(b))

    out = tmp_path / "sized.ome.tif"
    W.BioIOWriter(out, compression="zlib", compression_level=1).write(
        _lazy(_source()), source_bytes_per_frame=per_frame, stream=True)

    assert max(sizes) == 2, sizes


def test_streaming_can_be_asked_for_below_the_size_threshold(tmp_path, monkeypatch):
    """既定では 2 GiB 未満の遅延配列は素通し。頼めば流し書きになること。"""
    calls = []
    real = W.BioIOWriter._write_ometiff_streaming
    monkeypatch.setattr(
        W.BioIOWriter, "_write_ometiff_streaming",
        lambda self, data, out_file, **kw: calls.append(out_file) or real(
            self, data, out_file, **kw))

    source = _source()
    out = tmp_path / "small.ome.tif"
    W.BioIOWriter(out, compression="zlib", compression_level=1).write(
        _lazy(source), stream=True)

    assert calls == [out]
    got = tifffile.imread(out).reshape(T, C, Z, Y, X)
    np.testing.assert_array_equal(got, source)


def test_a_small_lazy_array_is_not_streamed_by_default(tmp_path, monkeypatch):
    """負のコントロール: 頼まなければ既定のまま (2 GiB 未満は素通し)。"""
    monkeypatch.setattr(
        W.BioIOWriter, "_write_ometiff_streaming",
        lambda *a, **k: pytest.fail("streamed without being asked"))

    source = _source()
    out = tmp_path / "eager.ome.tif"
    W.BioIOWriter(out, compression="zlib", compression_level=1).write(_lazy(source))

    got = tifffile.imread(out).reshape(T, C, Z, Y, X)
    np.testing.assert_array_equal(got, source)


def test_streaming_can_be_turned_off_for_a_large_lazy_array(tmp_path, monkeypatch):
    """大きくても、呼び出し側が要らないと言えば素通し。"""
    monkeypatch.setattr(
        W.BioIOWriter, "_write_ometiff_streaming",
        lambda *a, **k: pytest.fail("streamed although stream=False"))

    source = _source()
    out = tmp_path / "forced_eager.ome.tif"
    W.BioIOWriter(out, compression="zlib", compression_level=1).write(
        _lazy(source), stream=False)

    assert out.exists()


def test_asking_to_stream_an_in_memory_array_is_refused(tmp_path):
    """実体配列を流し書きしても減るものが無い。黙って従わずに言う。"""
    out = tmp_path / "nope.ome.tif"

    with pytest.raises(ValueError, match="lazy"):
        W.BioIOWriter(out, compression="zlib", compression_level=1).write(
            _source(), stream=True)


def test_the_streamed_pixels_match_the_direct_ones(tmp_path):
    """経路を選べるようになっても、書かれる画素は同じであること。"""
    source = _source(1)

    streamed = tmp_path / "a.ome.tif"
    W.BioIOWriter(streamed, compression="zlib", compression_level=1).write(
        _lazy(source), stream=True)

    direct = tmp_path / "b.ome.tif"
    W.BioIOWriter(direct, compression="zlib", compression_level=1).write(source)

    np.testing.assert_array_equal(
        tifffile.imread(streamed).reshape(T, C, Z, Y, X),
        tifffile.imread(direct).reshape(T, C, Z, Y, X))

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence
from math import prod
import numpy as np
from bioio import PhysicalPixelSizes

from ylabcommon.utils.perf import timed_step

_da: Any
try:
    import dask.array as _da
except Exception:  # pragma: no cover - dask is a hard dep of bioio in practice
    _da = None

try:
    from bioio_ome_tiff.writers import OmeTiffWriter
except ImportError as e:
    raise RuntimeError(
        "bioio-ome-tiff is required. Install with: pip install bioio-ome-tiff"
    ) from e

# Zarr is optional
try:
    from bioio_ome_zarr.writers import Channel, OMEZarrWriter
    _HAS_ZARR = True
except ImportError:
    _HAS_ZARR = False
    # Helpful debug to distinguish between 'no zarr' and 'no bioio-plugin'
    import importlib.util
    if importlib.util.find_spec("zarr") and not importlib.util.find_spec("bioio_ome_zarr"):
        print("[DEBUG] Base 'zarr' is installed, but 'bioio-ome-zarr' plugin is missing!")

#: まとめて読む1ブロックの上限バイト数。RAM に載るのはこの1ブロックぶんだけ。
_STREAM_BLOCK_BYTES = 256 * 1024**2

#: 1ブロックを読むときの同時読み取り数。ネットワークドライブでは1ファイルの時間は
#: ほぼ往復の待ちなので (実測: ヘッダ読みは CPU 0.23 ms に対し実時間 320 ms)、
#: 並べれば件数ぶんの時間はかからない。CPU 数ではなく待ちの数で決める値なので、
#: dask の既定 (CPU 数) より大きくとる。
_STREAM_READERS = 32


#: 出力名を作るときに「拡張子」として置き換えてよい終わり方。**長いものを先に**
#: 並べること (``.ome.tif`` を ``.tif`` より先に見ないと ``.ome`` が名前の一部と
#: して残る)。
_TIFF_SUFFIXES = (".ome.tiff", ".ome.tif", ".tiff", ".tif")
_ZARR_SUFFIXES = (".ome.zarr", ".zarr")


def _with_ome_suffix(path: Path, suffix: str, known: Sequence[str]) -> Path:
    """``path`` に OME の拡張子を付けた宛先を返す。

    **``Path.with_suffix`` を使ってはいけない。** あれは「最後のドット以降」を
    拡張子とみなすので、この用途では 2 つの壊れ方をする (実測):

        volume                       -> volume.ome.tif        (正しい)
        volume.ome.tif               -> volume.ome.ome.tif    <- .ome が二重になる
        stack_coreg_adj_crop.ome.tif -> ...crop.ome.ome.tif    <- 同上
        volume_1.5x                  -> volume_1.ome.tif      <- 名前の一部が消える

    ひとつめのせいで、**出力名を自分で決めている呼び出し側は公開 API を使えない**。
    slice-analysis が ``volume.ome.tif`` のような名前を渡すと ``volume.ome.ome.tif``
    になるので、内部の ``_write_ometiff_streaming`` を直接呼ぶしかなかった。
    ふたつめは、名前にドットを含む取得で **黙って別の場所に書く**。

    ここでは既知の終わり方だけを取り除いて付け直す。知らない終わり方は名前の
    一部として残す。
    """
    name = path.name
    for candidate in known:
        if name.lower().endswith(candidate) and len(name) > len(candidate):
            name = name[: -len(candidate)]
            break
    return path.with_name(name + suffix)


def _read_block(block: Any) -> np.ndarray:
    """遅延ブロックを実体化する。dask なら **同時に** 読む。

    ``np.asarray`` (= ``compute()``) の既定スケジューラはワーカ数が CPU 数なので、
    待ちが支配的なネットワーク読みでは並列度が足りない。ここだけ明示的に増やす。
    """
    if _da is not None and isinstance(block, _da.Array):
        return block.compute(scheduler="threads", num_workers=_STREAM_READERS)
    return np.asarray(block)


def _axis_scales(dim_order: str,
                 physical_pixel_sizes: Optional[tuple[float, float, float]]
                 ) -> Optional[list[float]]:
    """``(Z, Y, X)`` の物理サイズを、``dim_order`` の軸ごとの scale 列にする。

    OME-Zarr の ``coordinateTransformations`` は **軸ごとに 1 つ** scale を持つ
    ので、T/C のように物理長を持たない軸には 1.0 を置く。
    """
    if physical_pixel_sizes is None:
        return None
    by_axis = dict(zip("ZYX", physical_pixel_sizes))
    return [by_axis.get(axis, 1.0) for axis in dim_order]


class BioIOWriter:
    """
    Low-level export engine.

    Accepts already validated TCZYX numpy arrays and writes:
        • OME-TIFF (always)
        • OME-Zarr (optional)

    This class MUST NOT perform experiment logic.
    That belongs to BioIOBuilder.
    """

    def __init__(
        self,
        output_path: Path | str,
        *,
        compression: str = "zlib",
        compression_level: int = 6,
    ) -> None:
        self.output_path = Path(output_path)
        self.compression = compression
        self.compression_level = compression_level

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(
        self,
        data: np.ndarray,
        *,
        dim_order: str = "TCZYX",
        channel_names: Optional[Sequence[str]] = None,
        physical_pixel_sizes: Optional[tuple[float, float, float]] = None,
        # 既定は False。OME-Zarr も書くと同じ遅延スタックをもう一度 compute することに
        # なり、生 TIFF を全部読み直したうえ _write_omezarr 側は volume 全体を RAM に
        # 展開する。安全側を既定にして、必要な呼び出し側だけが明示的に有効化する
        # (ThorlabBioioBuilder.write は以前から save_zarr=False を明示していた)。
        save_zarr: bool = False,
        source_bytes_per_frame: Optional[int] = None,
        stream: Optional[bool] = None,
    ) -> None:
        """
        Write validated dataset.

        Parameters
        ----------
        data:
            5D numpy array (TCZYX). May be a lazy (dask) array.
        source_bytes_per_frame:
            How many bytes of *source* data one output time point costs to read.
            **Pass it whenever the array reduces its input** (a Z projection reads
            Z planes per output plane), or the streaming writer sizes its blocks
            from the output and under-counts the read by a factor of Z. See
            ``_write_ometiff_streaming``.
        stream:
            Force the streaming writer on (True) or off (False). The default
            (None) streams a lazy array once it is larger than 2 GiB, which is
            the right call when the caller does not care either way.

        両方とも、内部の ``_write_ometiff_streaming`` を直接呼ぶ以外に渡す手が
        無かったもの。呼び出し側が非公開のメソッドへ手を伸ばすのは、公開 API に
        必要な口が無いということなので、ここに出す。
        """

        self._validate_array(data, dim_order)

        self._write_ometiff(
            data,
            dim_order=dim_order,
            channel_names=channel_names,
            physical_pixel_sizes=physical_pixel_sizes,
            source_bytes_per_frame=source_bytes_per_frame,
            stream=stream,
        )

        if save_zarr:
            if not _HAS_ZARR:
                print("[BioIOWriter] bioio-ome-zarr not installed: skipping.")
            else:
                self._write_omezarr(
                    data,
                    dim_order=dim_order,
                    channel_names=channel_names,
                    physical_pixel_sizes=physical_pixel_sizes,
                )

    # ------------------------------------------------------------------
    # Internal validation
    # ------------------------------------------------------------------

    def _validate_array(self, data: np.ndarray, dim_order: str) -> None:
        if data.ndim != len(dim_order):
            raise ValueError(
                f"Array dimension mismatch. Got {data.ndim}D but dim_order={dim_order}"
            )

        if dim_order != "TCZYX":
            raise ValueError("BioIOWriter currently requires TCZYX ordering.")

    # ------------------------------------------------------------------
    # OME-TIFF writer
    # ------------------------------------------------------------------

    def _write_ometiff(
        self,
        data: np.ndarray,
        *,
        dim_order: str,
        channel_names: Optional[Sequence[str]],
        physical_pixel_sizes: Optional[tuple[float, float, float]],
        source_bytes_per_frame: Optional[int] = None,
        stream: Optional[bool] = None,
    ) -> None:
        out_file = _with_ome_suffix(self.output_path, ".ome.tif", _TIFF_SUFFIXES)
        print(f"DEBUG WRITER: values actually sent to writer: {physical_pixel_sizes}")

        # bioio's OmeTiffWriter.save() computes dask arrays fully into RAM
        # ("assumes it fits in memory"), which OOMs on large volumes (e.g. a
        # 62000x1024x1024 uint16 stack = 121 GiB). For a large lazy (dask) array,
        # stream it to disk plane-by-plane instead so peak memory stays bounded.
        # Small arrays keep the proven OmeTiffWriter path.
        lazy = _da is not None and isinstance(data, _da.Array)
        nbytes = prod(data.shape) * np.dtype(data.dtype).itemsize
        if stream is None:
            stream = lazy and nbytes > 2 * 1024**3
        if stream:
            if not lazy:
                # 実体配列は既に RAM にある。流し書きしても減るものが無く、
                # 経路だけが変わる。頼まれても黙って従わずに言う。
                raise ValueError(
                    "stream=True needs a lazy (dask) array; this one is already "
                    "in memory, so streaming it would change the write path "
                    "without bounding anything.")
            self._write_ometiff_streaming(
                data,
                out_file,
                channel_names=channel_names,
                physical_pixel_sizes=physical_pixel_sizes,
                source_bytes_per_frame=source_bytes_per_frame,
            )
            return

        pps = None
        if physical_pixel_sizes is not None:
            pps = PhysicalPixelSizes(
            Z=physical_pixel_sizes[0],
            Y=physical_pixel_sizes[1],
            X=physical_pixel_sizes[2],
        )

        OmeTiffWriter.save(
            data,
            out_file,
            dim_order=dim_order,
            channel_names=list(channel_names) if channel_names else None,
            physical_pixel_sizes=pps,
            tifffile_kwargs={
                "compression": self.compression,
                "compressionargs": {"level": self.compression_level},
            },
        )

        print(f"[BioIOWriter] OME-TIFF written → {out_file}")

    # ------------------------------------------------------------------
    # Streaming OME-TIFF writer (bounded memory, for large dask arrays)
    # ------------------------------------------------------------------

    def _write_ometiff_streaming(
        self,
        data: Any,
        out_file: Path,
        *,
        channel_names: Optional[Sequence[str]],
        physical_pixel_sizes: Optional[tuple[float, float, float]],
        source_bytes_per_frame: Optional[int] = None,
    ) -> None:
        """Stream a large TCZYX dask array to OME-TIFF without materializing it.

        Planes are computed in bounded blocks (~256 MB) and written one Y/X page
        at a time via tifffile, so peak memory stays ~one block instead of the
        whole volume. Assumes TCZYX order and that the source dask array is chunked
        finely enough (per plane/scene) that reading a block does not pull the
        entire volume — a monolithic single-chunk source cannot be streamed.

        Args:
            source_bytes_per_frame: how many bytes of *source* data one output
                time point costs to read. Defaults to the output's own size,
                which is right only when the array is read straight through.
                **Pass it whenever the array reduces its input** — a Z projection
                reads Z planes per output plane, so sizing blocks from the output
                underestimates the read by a factor of Z. See the comment on
                ``t_block`` below for the failure this caused.
        """
        import tifffile

        T, C, Z, Y, X = (int(n) for n in data.shape)
        dtype = np.dtype(data.dtype)
        nbytes = prod((T, C, Z, Y, X)) * dtype.itemsize
        bigtiff = nbytes > 3_900_000_000  # standard TIFF caps out near 4 GB

        metadata: dict[str, Any] = {"axes": "TCZYX"}
        if physical_pixel_sizes is not None:
            pz, py, px = physical_pixel_sizes
            if px:
                metadata["PhysicalSizeX"] = float(px)
                metadata["PhysicalSizeXUnit"] = "µm"
            if py:
                metadata["PhysicalSizeY"] = float(py)
                metadata["PhysicalSizeYUnit"] = "µm"
            if pz:
                metadata["PhysicalSizeZ"] = float(pz)
                metadata["PhysicalSizeZUnit"] = "µm"
        if channel_names:
            metadata["Channel"] = {"Name": list(channel_names)}

        plane_bytes = max(Y * X * dtype.itemsize, 1)
        # まとめて読む単位は「時点」で数える。以前は Z 方向だけをブロックにしていたが、
        # Z=1 の取得 (連続撮影) ではブロックが 1 面になり、6000 面を **1 面ずつ順番に**
        # 読むことになっていた。生データはネットワークドライブ上にあり、1 ファイル開くのに
        # 往復が数回 (実測 ~320 ms) かかるので、直列だと 6000 x 0.32 = 32 分かかる。
        # 実際に 2.5 面/秒・ETA 2350 秒で走っていた。
        #
        # 時点をまとめて 1 回で読めば、その中のファイルは dask のスケジューラが同時に
        # 読む。待ち時間なので重ねられる (ヘッダ読みと同じ理屈)。
        #
        # ブロックの大きさは「**元をどれだけ読むか**」で決める。出力の大きさで
        # 決めると、入力を畳む配列 (z 投影など) で読む量を大きく見誤る。実際に
        # 起きた例: 出力 Z=1・C=2・1024x1024 の z 投影で t_block=64 が選ばれたが、
        # 1 時点を作るのに元の面が Z=41 枚要るため、1 ブロックで 64x2x41 = 5248 面
        # = 10.5 GiB を一度に読み、メモリ不足で落ちた (書きかけの 1 KB が残った)。
        per_frame = source_bytes_per_frame or (max(C * Z, 1) * plane_bytes)
        t_block = max(1, (_STREAM_BLOCK_BYTES) // max(int(per_frame), 1))

        print(f"[BioIOWriter] Streaming OME-TIFF "
              f"(T={T},C={C},Z={Z},Y={Y},X={X}, {dtype}, ~{nbytes / 1024**3:.1f} GiB, "
              f"bigtiff={bigtiff}, tblock={t_block}) → {out_file}")

        # 遅延配列の画素を実際に読むのはここ (np.asarray) なので、取り込み全体の中で
        # 最も長く、入力(生データ)と出力の両方がネットワークドライブ越しになる。数十分
        # 沈黙しうる区間なので、ブロック単位で進捗を刻んで「どこまで書けたか」を残す。
        with timed_step("ometiff.stream_write", total=T * C * Z,
                        target=str(out_file), n_bytes=nbytes) as step:

            def planes() -> Any:
                for t0 in range(0, T, t_block):
                    t1 = min(t0 + t_block, T)
                    # 読む前に記録する。止まったときに掴んだままのブロックが分かる。
                    step.advance(0, item=f"reading T={t0}:{t1}")
                    chunk = _read_block(data[t0:t1])    # (t1-t0, C, Z, Y, X), bounded
                    for i in range(t1 - t0):
                        for c in range(C):
                            for z in range(Z):
                                step.advance(item=f"T={t0 + i} C={c} Z={z}")
                                yield chunk[i, c, z]

            with tifffile.TiffWriter(out_file, bigtiff=bigtiff, ome=True) as tif:
                tif.write(
                    planes(),
                    shape=(T, C, Z, Y, X),
                    dtype=dtype,
                    photometric="minisblack",
                    metadata=metadata,
                    compression=self.compression,
                    compressionargs={"level": self.compression_level},
                )

        print(f"[BioIOWriter] OME-TIFF (streamed) written → {out_file}")

    # ------------------------------------------------------------------
    # OME-Zarr writer
    # ------------------------------------------------------------------

    def _write_omezarr(
        self,
        data: np.ndarray,
        *,
        dim_order: str,
        channel_names: Optional[Sequence[str]],
        physical_pixel_sizes: Optional[tuple[float, float, float]],
    ) -> None:
        out_dir = _with_ome_suffix(self.output_path, ".ome.zarr", _ZARR_SUFFIXES)

        # bioio-ome-zarr の書き出しは「writer を作って全ボリュームを流す」形。
        # 以前ここは存在しない ``OmeZarrWriter.save(...)`` を呼んでいた (綴りも
        # 上流と違い、import が常に失敗して zarr 出力が黙って無効になっていた)。
        # 解像度ピラミッドは作らない —— 元の呼び出しも 1 段だった。
        writer = OMEZarrWriter(
            str(out_dir),
            level_shapes=[list(data.shape)],
            dtype=data.dtype,
            axes_names=list(dim_order),
            # color は Channel の必須項目。灰色画像なので白を置く
            # (見た目の既定であって、画素値には影響しない)。
            channels=([Channel(label=name, color="FFFFFF") for name in channel_names]
                      if channel_names else None),
            physical_pixel_size=_axis_scales(dim_order, physical_pixel_sizes),
        )
        writer.write_full_volume(data)

        print(f"[BioIOWriter] OME-Zarr written → {out_dir}")


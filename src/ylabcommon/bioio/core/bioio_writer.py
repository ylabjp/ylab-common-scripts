from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence
from math import prod
import numpy as np
from bioio import PhysicalPixelSizes

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
    from bioio_ome_zarr.writers import OmeZarrWriter
    _HAS_ZARR = True
except ImportError:
    _HAS_ZARR = False
    # Helpful debug to distinguish between 'no zarr' and 'no bioio-plugin'
    import importlib.util
    if importlib.util.find_spec("zarr") and not importlib.util.find_spec("bioio_ome_zarr"):
        print("[DEBUG] Base 'zarr' is installed, but 'bioio-ome-zarr' plugin is missing!")

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
        save_zarr: bool = True,
    ) -> None:
        """
        Write validated dataset.

        Parameters
        ----------
        data:
            5D numpy array (TCZYX).
        """

        self._validate_array(data, dim_order)

        self._write_ometiff(
            data,
            dim_order=dim_order,
            channel_names=channel_names,
            physical_pixel_sizes=physical_pixel_sizes,
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
    ) -> None:
        out_file = self.output_path.with_suffix(".ome.tif")
        print(f"DEBUG WRITER: values actually sent to writer: {physical_pixel_sizes}")

        # bioio's OmeTiffWriter.save() computes dask arrays fully into RAM
        # ("assumes it fits in memory"), which OOMs on large volumes (e.g. a
        # 62000x1024x1024 uint16 stack = 121 GiB). For a large lazy (dask) array,
        # stream it to disk plane-by-plane instead so peak memory stays bounded.
        # Small arrays keep the proven OmeTiffWriter path.
        nbytes = prod(data.shape) * np.dtype(data.dtype).itemsize
        if _da is not None and isinstance(data, _da.Array) and nbytes > 2 * 1024**3:
            self._write_ometiff_streaming(
                data,
                out_file,
                channel_names=channel_names,
                physical_pixel_sizes=physical_pixel_sizes,
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
        data,
        out_file: Path,
        *,
        channel_names: Optional[Sequence[str]],
        physical_pixel_sizes: Optional[tuple[float, float, float]],
    ) -> None:
        """Stream a large TCZYX dask array to OME-TIFF without materializing it.

        Planes are computed in bounded Z-blocks (~256 MB) and written one Y/X page
        at a time via tifffile, so peak memory stays ~one block instead of the
        whole volume. Assumes TCZYX order and that the source dask array is chunked
        finely enough (per plane/scene) that reading a block does not pull the
        entire volume — a monolithic single-chunk source cannot be streamed.
        """
        import tifffile

        T, C, Z, Y, X = (int(n) for n in data.shape)
        dtype = np.dtype(data.dtype)
        nbytes = prod((T, C, Z, Y, X)) * dtype.itemsize
        bigtiff = nbytes > 3_900_000_000  # standard TIFF caps out near 4 GB

        metadata = {"axes": "TCZYX"}
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
        block = max(1, (256 * 1024**2) // plane_bytes)  # ~256 MB worth of Z planes

        def planes():
            for t in range(T):
                for c in range(C):
                    for z0 in range(0, Z, block):
                        z1 = min(z0 + block, Z)
                        chunk = np.asarray(data[t, c, z0:z1])  # (z1-z0, Y, X), bounded
                        for k in range(z1 - z0):
                            yield chunk[k]

        print(f"[BioIOWriter] Streaming OME-TIFF "
              f"(T={T},C={C},Z={Z},Y={Y},X={X}, {dtype}, ~{nbytes / 1024**3:.1f} GiB, "
              f"bigtiff={bigtiff}, zblock={block}) → {out_file}")

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
        out_dir = self.output_path.with_suffix(".ome.zarr")

        OmeZarrWriter.save(
            data,
            out_dir,
            dim_order=dim_order,
            channel_names=list(channel_names) if channel_names else None,
            physical_pixel_sizes=physical_pixel_sizes,
        )

        print(f"[BioIOWriter] OME-Zarr written → {out_dir}")


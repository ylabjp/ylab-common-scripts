from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence
import numpy as np
from bioio import PhysicalPixelSizes

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
        print(f"DEBUG WRITER: values actually sent to OmeTiffWriter: {physical_pixel_sizes}")

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


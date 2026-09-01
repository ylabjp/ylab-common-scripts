from __future__ import annotations
from typing import Any, Optional
from pathlib import Path
import numpy as np
import warnings

xr: Any
try:
    import xarray as xr
except Exception:  # pragma: no cover - xarray is a hard dep in practice
    xr = None

from bioio import BioImage, DimensionNames
#from .bioio_metadata import BioIOMetadataExtractor
#from bioio.thorlab.thorlab_metadata_extractor import ThorlabMetadataExtractor

class BioIOReader:
    """
    Wrapper around BioImage.

    Responsibilities:
    - Accept numpy array OR file path
    - Normalize output to TCZYX numpy array
    - Expose dimensions (NO metadata interpretation here)
    """
    '''
    def __init__(self, image_input):
        """
        image_input:
            - numpy array (preferred)
            - path string / Path
        """

        self.image_input = image_input

        # BioImage supports BOTH arrays and paths
        try:
            #self._img = BioImage(image_input)
            self._img = BioImage(
                image_input,
                ims="TCZYX"
               )

        except Exception as e:
            raise RuntimeError(f"[BioIOReader] Cannot initialize BioImage: {e}")
       '''
    def __init__(self, image_data: Any) -> None:
        """
         image_data can be:
          - a lazy xarray.DataArray (dask-backed, already TCZYX) — preferred
          - a dask or numpy array (already TCZYX)
          - a file path (str or Path)
          - an existing BioImage
        """

        self.image_data = image_data
        try:
            if isinstance(image_data, BioImage):
                self._img = image_data

            # Already-normalized lazy stack: wrap the underlying (dask) array so
            # laziness is preserved. Accessing `.data` on a dask-backed DataArray
            # returns the dask array WITHOUT reading any pixels.
            elif xr is not None and isinstance(image_data, xr.DataArray):
                self._img = BioImage(image_data.data, dims="".join(map(str, image_data.dims)))

            # numpy array → explicitly define dimension order
            elif isinstance(image_data, np.ndarray):
                self._img = BioImage(image_data, dims="TCZYX")

            # file path → let BioIO detect normally
            elif isinstance(image_data, (str, Path)):
                self._img = BioImage(str(image_data))

            # dask array / other array-like already in TCZYX order
            else:
                self._img = BioImage(image_data, dims="TCZYX")

        except Exception as e:
            raise RuntimeError(f"[BioIOReader] Cannot initialize BioImage: {e}")

    def read(self) -> Any:
        """遅延(dask)の TCZYX 配列を返す (:meth:`get_data` と同じ)。"""
        return self.get_data()

    # ---------------------------
    # Returns a LAZY (dask) TCZYX array
    # ---------------------------
    def get_data(self) -> Any:
        """TCZYX の *遅延* (dask) 配列を返す。画素は読まない。

        以前は bioio の EAGER アクセサ ``self._img.data`` を返していた。これは
        volume 全体を RAM へ展開する (T=2000, Z=31, 1024x1024 uint16 で 121 GiB) うえ、
        bioio 側が結果をキャッシュして ``_xarray_dask_data`` を numpy 実体から作り直す
        ため、以降その BioImage は「遅延」に見えて実体を抱え続ける。

        取り込みは RAM に載らない volume を日常的に扱う (だから書き出しは
        ``BioIOWriter._write_ometiff_streaming`` でブロック単位に流している) ので、
        既定は遅延にして、実体化は :meth:`get_data_eager` の明示呼び出しに限る。

        戻り値は dask 配列なので ``.shape`` / ``.dtype`` はそのまま使え、画素が要る
        ときだけ必要な範囲をスライスしてから ``np.asarray`` する。
        """
        try:
            return self._img.dask_data
        except Exception as e:
            warnings.warn(f"[BioIO] Unable to build the lazy pixel view: {e}")
            return None

    def get_data_eager(self) -> Any:
        """全画素を RAM へ読み込んだ numpy 配列を返す (**volume 全体が載る場合のみ**)。

        巨大な volume では MemoryError / OOM kill になる。ストリーミング書き出しや
        ブロック単位のスライスで足りるなら、そちらを使うこと。

        例外は握り潰さずそのまま送出する。ここで MemoryError を warning に変えて
        None を返すと、呼び出し側が原因の分からない失敗をするため
        (MemoryError は Exception のサブクラスなので、素の except Exception では
        メモリ不足も飲み込んでしまう)。
        """
        return self._img.data

    # ---------------------------
    # xarray access (lazy)
    # ---------------------------
    def get_xarray(self) -> Any:
        """遅延(dask)裏付けの xarray ビューを返す。

        ``self._img.xarray_data`` は bioio の EAGER アクセサで、参照しただけで
        volume 全体を展開する (同じ注意書きが thorlab_metadata_extractor にもある)。
        遅延版の ``xarray_dask_data`` を返す。
        """
        try:
            return self._img.xarray_dask_data
        except Exception as e:
            warnings.warn(f"[BioIO] xarray view unavailable: {e}")
            return None

    # ---------------------------
    # Dimensions
    # ---------------------------
    def get_dims(self) -> Any:
        try:
            return self._img.dims
        except Exception as e:
            warnings.warn(f"[BioIO] dims unavailable: {e}")
            return None

    # ---------------------------
    # Dimension order (TCZYX)
    # ---------------------------
    def get_dim_order(self) -> Optional[str]:
        try:
            return self._img.dims.order
        except Exception as e:
            warnings.warn(f"[BioIO] dim order unavailable: {e}")
            return None

    # ---------------------------
    # Shape
    # ---------------------------
    def get_shape(self) -> Optional[tuple]:
        try:
            return self._img.shape
        except Exception as e:
            warnings.warn(f"[BioIO] shape unavailable: {e}")
            return None

    def get_size(self, axis: str) -> Optional[int]:
        try:
            return getattr(self._img.dims, axis)
        except Exception:
            warnings.warn(f"[BioIO] axis '{axis}' not present")
            return None

    # ---------------------------
    # Metadata
    # ---------------------------
    def get_standard_metadata(self) -> Any:
        try:
            return self._img.standard_metadata
        except Exception:
            warnings.warn("[BioIO] standard metadata unavailable")
            return None

    def get_physical_pixel_sizes(self) -> Any:
        try:
            return self._img.physical_pixel_sizes
        except Exception as e:
            warnings.warn(f"[BioIO] pixel size metadata unavailable: {e}")
            return None

    def get_scale(self) -> Any:
        try:
            return self._img.scale
        except Exception as e:
            warnings.warn(f"[BioIO] Scale unavailable: {e}")
            return None

    # ---------------------------
    # Channel metadata
    # ---------------------------
    def get_channel_info(self) -> list:
        channels = []

        try:
            dims = self._img.dims
            size_c = dims.C if hasattr(dims, "C") else 1
        except Exception:
            size_c = 1

        try:
            ch_meta = getattr(self._img, "channel_names", None)

            if ch_meta:
                for i, name in enumerate(ch_meta):
                    channels.append({
                        "index": i,
                        "name": str(name) if name else f"C{i}",
                    })

        except Exception:
            pass

        if not channels:
            for i in range(size_c):
                channels.append({
                    "index": i,
                    "name": f"C{i}",
                })

        return channels


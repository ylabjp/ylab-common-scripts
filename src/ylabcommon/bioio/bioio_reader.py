from __future__ import annotations
import numpy as np
import warnings

from bioio import BioImage, DimensionNames
#from .bioio_metadata import BioIOMetadataExtractor
from .thorlab_metadata_extractor import ThorlabMetadataExtractor

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
    def __init__(self, image_data):
        """
         image_data can be:
          - numpy array (already TCZYX)
         - file path (str or Path)
        """

        self.image_data = image_data
        try:
            # If numpy array → explicitly define dimension order
            if isinstance(image_data, np.ndarray):
                self._img = BioImage(image_data, dims="TCZYX")

            # If file path → let BioIO detect normally
            else:
                self._img = BioImage(str(image_data))
        
        except Exception as e:
            raise RuntimeError(f"[BioIOReader] Cannot initialize BioImage: {e}") 

    def read(self):
        return self.get_data()  
    # ---------------------------
    # Returns TCZYX numpy array
    # ---------------------------
    def get_data(self):
        try:
            return self._img.data
        except Exception as e:
            warnings.warn(f"[BioIO] Unable to read pixel data: {e}")
            return None

    # ---------------------------
    # xarray access
    # ---------------------------
    def get_xarray(self):
        try:
            return self._img.xarray_data
        except Exception as e:
            warnings.warn(f"[BioIO] xarray view unavailable: {e}")
            return None

    # ---------------------------
    # Dimensions
    # ---------------------------
    def get_dims(self):
        try:
            return self._img.dims
        except Exception as e:
            warnings.warn(f"[BioIO] dims unavailable: {e}")
            return None

    # ---------------------------
    # Dimension order (TCZYX)
    # ---------------------------
    def get_dim_order(self):
        try:
            return self._img.dims.order
        except Exception as e:
            warnings.warn(f"[BioIO] dim order unavailable: {e}")
            return None

    # ---------------------------
    # Shape
    # ---------------------------
    def get_shape(self):
        try:
            return self._img.shape
        except Exception as e:
            warnings.warn(f"[BioIO] shape unavailable: {e}")
            return None

    def get_size(self, axis: str):
        try:
            return getattr(self._img.dims, axis)
        except Exception:
            warnings.warn(f"[BioIO] axis '{axis}' not present")
            return None

    # ---------------------------
    # Metadata
    # ---------------------------
    def get_standard_metadata(self):
        try:
            return self._img.standard_metadata
        except Exception:
            warnings.warn("[BioIO] standard metadata unavailable")
            return None

    def get_physical_pixel_sizes(self):
        try:
            return self._img.physical_pixel_sizes
        except Exception as e:
            warnings.warn(f"[BioIO] pixel size metadata unavailable: {e}")
            return None

    def get_scale(self):
        try:
            return self._img.scale
        except Exception as e:
            warnings.warn(f"[BioIO] Scale unavailable: {e}")
            return None

    # ---------------------------
    # Channel metadata
    # ---------------------------
    def get_channel_info(self):

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

    # ---------------------------
    # Unified metadata access
    # ---------------------------
    def get_metadata_all(self):

        if not hasattr(self, "_metadata_obj"):
            self._metadata_obj = ThorlabMetadataExtractor(self._img)

        return self._metadata_obj


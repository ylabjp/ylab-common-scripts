from dataclasses import dataclass
from typing import Optional, Tuple, Dict, List
from datetime import datetime, timedelta
import warnings
from ylabcommon.bioio.core.metadata_extractor_base import MicroscopeMetadataExtractor


@dataclass
class ImagePhysicalMetadata:

    dimension_order: Optional[str]
    shape: Optional[Tuple[int, int, int, int, int]]

    size_t: Optional[int]
    size_c: Optional[int]
    size_z: Optional[int]
    size_y: Optional[int]
    size_x: Optional[int]

    pixel_size: Optional[Tuple[float, float, float]]  # Z,Y,X
    scale: Optional[Tuple]

    imaging_datetime: Optional[datetime]
    timelapse_interval: Optional[timedelta]
    objective: Optional[str]

    channel_names_index: Optional[List[str]]

    def to_dict(self):
        return self.__dict__


class ThorlabMetadataExtractor(MicroscopeMetadataExtractor):

    def __init__(self, reader, pixel_size_tuple=None, channel_names_index=None):
        self._img = reader._img
        self._pixel_size = pixel_size_tuple
        self._channel_names_index = channel_names_index

    def extract(self) -> ImagePhysicalMetadata:

        # Dimension order
        try:
            dim_order = self._img.dims.order
        except Exception:
            dim_order = None
            warnings.warn("Dimension order unavailable")

        # Shape
        try:
            shape = self._img.shape
        except Exception:
            shape = None

        # sizes from dims (safe)
        dims = getattr(self._img, "dims", None)

        def safe_get(attr):
            try:
                return getattr(dims, attr)
            except:
                return None

        size_t = safe_get("T")
        size_c = safe_get("C")
        size_z = safe_get("Z")
        size_y = safe_get("Y")
        size_x = safe_get("X")

        # Physical pixel size
        ''' 
        try:
            pps = self._img.physical_pixel_sizes
            pixel_size = (pps.Z, pps.Y, pps.X) if pps else None
        except:
            pixel_size = None
        '''

        if hasattr(self._img, "xarray_data"):
            # If it's a BioImage object
            pixel_size = self._img.xarray_data.attrs.get("pixel_size_xyz")
        elif hasattr(self._img, "attrs"):
            # If it's already an Xarray DataArray
            pixel_size = self._img.attrs.get("pixel_size_xyz")
        else:
            pixel_size = None
        pixel_size = self._pixel_size if self._pixel_size else (1.0, 1.0, 1.0)
        print(f"DEBUG EXTRACTOR: Using fixed pixel size: {pixel_size}")
        '''
        try:
            # Remap Thorlabs XML names to our (Z, Y, X) standard
            pixel_size = (
                float(self._params.get("PixelSizeZ", 1.0)),
                float(self._params.get("PixelSizeY", 1.0)),
                float(self._params.get("PixelSizeX", 1.0))
            )
        except (TypeError, ValueError):
            print("[WARN] Could not parse XML pixel sizes. Defaulting to 1.0")
            pixel_size = (1.0, 1.0, 1.0)
        '''

        # Scale
        try:
            scale_obj = self._img.scale
            scale = tuple(scale_obj) if scale_obj else None
        except:
            scale = None

        # Standard metadata
        std = getattr(self._img, "standard_metadata", None)

        # Channel names
        try:
            channel_names_index = self._img.channel_names_index
        except:
            channel_names_index = None

        if self._channel_names_index:
            clean_names_index  = [str(c).split(':')[0] for c in self._channel_names_index]
        else:
            num_c = self._img.shape[1]
            clean_names_index = [f"Channel {i}" for i in range(num_c)]

    # If splitting left us with just 'Channel', let's add the index back for clarity
        final_names_index = []
        for i, name in enumerate(clean_names_index):
            if name == "Channel":
                final_names_index.append(f"Channel {i}")
            else:
                final_names_index.append(name)

        print(f"[DEBUG Final Names for OME] {final_names_index}")


        return ImagePhysicalMetadata(
            dimension_order = dim_order,
            shape=shape,
            size_t=size_t,
            size_c=size_c,
            size_z=size_z,
            size_y=size_y,
            size_x=size_x,
            pixel_size=pixel_size,
            scale=scale,
            imaging_datetime=getattr(std, "imaging_datetime", None),
            timelapse_interval=getattr(std, "timelapse_interval", None),
            objective=getattr(std, "objective", None),
            #channel_names_index = channel_names_index,
            channel_names_index=final_names_index,
        )


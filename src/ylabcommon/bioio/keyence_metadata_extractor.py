import numpy as np
from ylabcommon.bioio.keyence_metainfo import ImageMetadata
from ylabcommon.bioio.metadata_extractor_base import MicroscopeMetadataExtractor

"""
"X": int(self.image_positions[0]/self.nm_per_pixel_values),
            "Y": int(self.image_positions[1]/self.nm_per_pixel_values),
            "W": self.dimensions[0],
            "H": self.dimensions[1],
            "Z_position": int(self.z_position) if self.z_position is not None else 0,
            "LensName": self.lens_name,
            "ExposureTimeInS": self.exposure_time,
            "umPerPixel": self.nm_per_pixel_values / 1000,  # Convert nm to um,
            "Sectioning": self.sectioning,
            "CameraHardwareGain": int(self.gain[1]),
            "CameraGain": int(self.gain[0]),
        }

"""

class KeyenceMetadataExtractor(MicroscopeMetadataExtractor):

    def extract(self):

        metas = [ImageMetadata(f).get_dict() for f in self.files]

        first = metas[0]

        # ----------------------------
        # Build metadata object
        # ----------------------------

        class ImageMeta:
            pass

        # ----------------------------
        # Compute Z spacing
        # ----------------------------

        z_positions = sorted({m["Z_position"] for m in metas})

        if len(z_positions) > 1:
            diffs = np.diff(z_positions)
            z_step = np.median(diffs) / 1000  # nm → µm
        else:
            z_step = first["umPerPixel"]

        image_meta = ImageMeta()

        image_meta.dim_order = "TCZYX"
        image_meta.shape = self.data_shape

        px = first["umPerPixel"]

        image_meta.pixel_size = (z_step, px, px)
        image_meta.lens = first["LensName"]
        image_meta.exposure = first["ExposureTimeInS"]
        image_meta.sectioning = first["Sectioning"]


        image_meta.stage = {
             "X": first["X"],
             "Y": first["Y"],
             "Z_position": first["Z_position"]
        }

        image_meta.image = {
             "width": first["W"],
             "height": first["H"]
         }

        # ----------------------------
        # Validation checks
        # ----------------------------

        image_meta.pixel_sizes = {m["umPerPixel"] for m in metas}
        image_meta.dimensions = {(m["W"], m["H"]) for m in metas}
        image_meta.lens_names = {m["LensName"] for m in metas}

        # ----------------------------
        # Compute Z spacing
        # ----------------------------

        image_meta.z_positions = sorted({m["Z_position"] for m in metas})

        return image_meta

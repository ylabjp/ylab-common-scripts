from bioio.thorlab.thorlab_metadata_extractor import ThorlabMetadataExtractor

class UnifiedMeta :

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
 
        # ---------------------------
        # Unified metadata access
        #---------------------------
        def get_metadata_all(self):

            if not hasattr(self, "_metadata_obj"):
                self._metadata_obj = ThorlabMetadataExtractor(self._img)

            return self._metadata_obj

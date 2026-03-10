class KeyenceMetadataAdapter(BaseMetadataAdapter):
    def __init__(self, tiff_file):
        self.tiff_file = tiff_file

    def extract(self):
        # parse embedded XML inside TIFF
        return {
            "mode": "Z",
            "SizeZ": ???,
            "PixelSizeX": ???,
            ...
        }

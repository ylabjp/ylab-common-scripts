from abc import ABC, abstractmethod


class MicroscopeMetadataExtractor(ABC):
    """
    Common base class for all microscope metadata extractors.
    Ensures a unified interface for writers.
    """

    def __init__(self, files, data_shape, channel_names=None):
        self.files = files
        self.data_shape = data_shape
        self.channel_names = channel_names

    @abstractmethod
    def extract(self):
        """Return an image_meta object compatible with the writer."""
        pass

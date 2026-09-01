from typing import Any
from abc import ABC, abstractmethod


class MicroscopeMetadataExtractor(ABC):
    """
    Common base class for all microscope metadata extractors.
    Ensures a unified interface for writers.
    """

    def __init__(self, files: Any, data_shape: Any, channel_names: Any = None) -> None:
        self.files = files
        self.data_shape = data_shape
        self.channel_names = channel_names

    @abstractmethod
    def extract(self) -> Any:
        """Return an image_meta object compatible with the writer."""
        pass

class BaseParamsAdapter:
    """
    Abstract parameters for all microscope .
    Ensures uniform output for validation and OME writing.
    """

    def extract(self) -> dict:
        raise NotImplementedError("Subclasses must implement extract()")

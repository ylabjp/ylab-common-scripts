import xarray as xr
from bioio import BioImage


# ---------------------------------------------------------
# Normalize ANY BioImage to TCZYX xarray
# ---------------------------------------------------------
def normalize_to_tczyx(img: BioImage) -> xr.DataArray:
    """Standardizes any input to a 5D TCZYX xarray without conflicting coords."""

    data = img.xarray_data
    target_dims = ["T", "C", "Z", "Y", "X"]

    for d in target_dims:
        if d not in data.dims:
            data = data.expand_dims(d)

    data = data.transpose(*target_dims)

    #Keep scaling: If the input data already has coordinates like spatial scaling in microns
    data = data.reset_coords(drop=True)
    #data = data.drop_vars(data.coords.keys())
    return data

import numpy as np
import tifffile
import pytest


import numpy as np
import tifffile
import pytest
from pathlib import Path


def test_fake_generator_fixture(tmp_path):
    """
    Generate a synthetic TIFF dataset with real image data.
    Useful for testing BioIO readers/writers safely.
    """

    dataset_dir = tmp_path / "tiff_dataset"
    dataset_dir.mkdir()

    # dataset dimensions
    channels = ["ChanA", "ChanB"]
    x_pos = 1
    y_pos = 1
    z_slices = 5
    timepoints = 1

    shape = (128, 128)

    for ch in channels:
        for z in range(1, z_slices + 1):
            for t in range(1, timepoints + 1):

                data = np.random.randint(
                    0, 65535, size=shape, dtype=np.uint16
                )

                filename = f"{ch}_{x_pos:03d}_{y_pos:03d}_{z:03d}_{t:03d}.tif"

                tifffile.imwrite(dataset_dir / filename, data)

    assert dataset_dir

    BLUE = '\033[94m'
    print(f"\n\n{BLUE}[INFORMATION]: Verified dimension and name \n")

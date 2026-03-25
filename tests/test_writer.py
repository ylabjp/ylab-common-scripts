from ylabcommon.bioio.core.bioio_writer import BioIOWriter

import numpy as np
from bioio import BioImage


def test_writer_with_ome(generate_fake_ome_stack, tmp_path):
    _, data = generate_fake_ome_stack

    out = tmp_path / "output.tif"

    writer = BioIOWriter(
        str(out),
        compression=None,
        compression_level=None
    )

    writer.write(
        data,
        dim_order="TCZYX",
        channel_names=None,
        physical_pixel_sizes=None,
    )

    # writer will create: output.ome.tif
    final_file = tmp_path / "output.ome.tif"

    assert final_file.exists(), f("File not created: {final_file}")

    img = BioImage(str(final_file))
    assert img.get_image_data().shape == data.shape
 
    BLUE = '\033[94m'
    print(f"\n {BLUE}[INFORMATION]: Verified Bioio Writer \n")



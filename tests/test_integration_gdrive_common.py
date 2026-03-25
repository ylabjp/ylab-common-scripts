import pytest
from pathlib import Path
from thorlab_loader.backends.bioio_thorlab_builder import ThorlabBioioBuilder

@pytest.mark.gdrive
def test_gdrive_thorlab(gdrive_dataset, tmp_path):
    dataset_dir = Path(gdrive_dataset)

    xml_path = next(dataset_dir.rglob("Experiment.xml"))
    tiff_dir = xml_path.parent

    out_dir = tmp_path / "gdrive_thorlab"

    builder = ThorlabBioioBuilder(
                  str(tiff_dir), 
                  str(xml_path),
                  out_dir,
                  compression=None,
                  compression_level=None
              )

    assert out_dir.exists()


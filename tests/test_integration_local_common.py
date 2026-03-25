import pytest
from thorlab_loader.backends.bioio_thorlab_builder import ThorlabBioioBuilder


@pytest.mark.integration_bioio
def test_full_pipeline_bioio_common(local_real_dataset_common):

    tiff_dir, xml_file = local_real_dataset_common

    if not tiff_dir:
        pytest.skip("No local dataset provided")

    if not xml_file:
        raise FileNotFoundError("No Experiment.xml found")

    xml_path = xml_file/"Experiment.xml"

    builder = ThorlabBioioBuilder(
        tiff_dir = str(tiff_dir),
        xml_file = str(xml_path),
        output_dir = "Ptest_output",
        compression=None,
        compression_level=None
    )

    builder.build()

    #assert True


"""
@pytest.mark.integration
def test_keyence_local(fake_dataset, tmp_path):
    out_dir = tmp_path / "keyence_output"

    parser = KeyenceParser(fake_dataset)

    builder = KeyenceBioioBuilder(
        str(tiff_dir),
        str(xml_path),
        out_dir,
        compression=None,
        compression_level=None
    )


    assert out_dir.exists()
"""

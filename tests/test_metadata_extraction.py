from bioio import BioImage


def test_metadata_extraction(generate_fake_ome_stack):

    ome_file, _ = generate_fake_ome_stack

    img = BioImage(str(ome_file))

    meta = img.metadata

    assert meta is not None
   
    assert ome_file.is_file(), f"{ome_file} is not a file"

    BLUE = '\033[94m'
    print(f"\n\n{BLUE}[INFORMATION]: Verified meta data extraction, BioImage\n")

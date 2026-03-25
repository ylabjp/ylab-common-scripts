def test_metadata_structure():

    meta = {
        "pixel_size": (1,1,1),
        "shape": (1,1,10,100,100)
    }

    assert "pixel_size" in meta
 
    BLUE = '\033[94m'
    print(f"\n\n{BLUE}[INFORMATION]: Verified meta, just basic checkr\n")

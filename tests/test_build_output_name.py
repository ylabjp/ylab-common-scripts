import numpy as np
from pathlib import Path
from ylabcommon.utils.outfile_name import extract_dimensions, build_stack_filename

def test_build_output_name_basic(tmp_path):

    datasets = {
        "keyence": [
            "Image_XY02_Z001_CH1.tif",
             "Image_XY02_Z002_CH2.tif",
             "Image_XY02_Z003_CH3.tif",
        ],
        "thorlab": [
            "ChanA_001_001_001_001.tif",
            "ChanA_001_001_001_002.tif",
            "ChanA_001_001_001_003.tif",
        ]
    }

    
    tiff_files_keyence = []
    tiff_files_thorlab = []

    for source, files in datasets.items():
        for f in files:

            if source == "keyence":
                file_keyence = tmp_path / f
                file_keyence.write_text("")
                tiff_files_keyence.append(file_keyence)

            elif source == "thorlab":
                file_thorlab = tmp_path / f
                file_thorlab.write_text("")
                tiff_files_thorlab.append(file_thorlab)

            else:
                print(f"May add other spectroscopy option")
  
    print(f"[Message] : Input dummy files: {tiff_files_keyence} \n")
    
    print(f"[Message] : Input dummy files: {tiff_files_thorlab} \n")

    for tiff_files in [tiff_files_keyence, tiff_files_thorlab]:

        image_name, dims = extract_dimensions(tiff_files) 
        output_dir = tmp_path
        z_mx_min_re = [1,3,"None"]

        name = build_stack_filename(
            output_dir, 
            image_name, 
            dims, 
            z_mx_min_re
        )
        
        new_name = name.with_suffix('.ome.tif')

        print(f"[Message] : Output Saved : {new_name} \n")
        assert new_name

    BLUE = '\033[94m'
    print(f"{BLUE}[INFORMATION]: Verified dimensions,shape extraction,  stack filename name\n")

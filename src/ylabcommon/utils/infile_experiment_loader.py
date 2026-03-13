import yaml
from pathlib import Path
import io
import zipfile
from typing import List, Optional

from pathlib import Path
import yaml
import zipfile


def extract_zip_and_find_tiffs(datase_ymal_file: str):

    dataset_dirs = []

    with open(datase_ymal_file) as f:
        config = yaml.safe_load(f)

    for zip_path in config["datasets"]:

        zip_path = Path(zip_path)

        if not zip_path.exists():
            raise FileNotFoundError(f"Dataset not found: {zip_path}")

        # extract ZIP next to the zip file
        extract_root = zip_path.parent
        
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(extract_root)

        # dataset root folder
        dataset_root = extract_root / zip_path.stem

        # find directories containing TIFF files
        for tif in dataset_root.rglob("*.tif"):
            dataset_dirs.append(tif.parent)

    # remove duplicates
    dataset_dirs = sorted(set(dataset_dirs))

    # convert Path → string directories
    dataset_dirs = [str(p) for p in dataset_dirs]

    return dataset_dirs


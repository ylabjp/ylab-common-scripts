from datetime import datetime
from pathlib import Path

"""
Handeling Outout directory name according to input tiff's directoy
"""

def sanitize(name: str):
    return name.replace("-", "_").replace(".", "p")

def get_base_path(dataset_dir: str, input_root: str) -> Path:
    dataset_dir = Path(dataset_dir)
    input_root = Path(input_root)
    return dataset_dir.relative_to(input_root)

def build_output_dir_name(microscope_name=None, output_dir=None, dataset_name=None, change_output_dir_path=None, extra_txt=None):

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
 
    if extra_txt:
        basedir_name = f"{microscope_name}_{output_dir}_{extra_txt}_{timestamp}"
    else:
        basedir_name = f"{microscope_name}_{output_dir}_{timestamp}"

    # Base directory
    dataset_dir = Path(dataset_name)

    # Top directory
    clean_parts = [sanitize(p) for p in dataset_dir.parts]
    if change_output_dir_path: 
        output_dir = Path(change_output_dir_path) / basedir_name
        output_dir = output_dir.joinpath(*clean_parts)
    else:
    # Output directory
        output_dir = Path(basedir_name).joinpath(*clean_parts)

    output_dir.mkdir(parents=True, exist_ok=True)

    return output_dir



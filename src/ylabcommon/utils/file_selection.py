from pathlib import Path
from ..utils.utils import find_tiff_files, log_info, log_warn


def collect_valid_tiffs(tiff_dir: Path):

    tiff_dir = Path(tiff_dir)

    if not tiff_dir.exists():
        raise FileNotFoundError(f"Input folder does not exist: {tiff_dir}")

    all_tiffs = find_tiff_files(str(tiff_dir))

    if len(all_tiffs) == 0:
        raise FileNotFoundError(f"No TIFF files found in folder {tiff_dir}")

    log_info(f"Found {len(all_tiffs)} TIFF files in {tiff_dir}")

    tiff_files = [
        f for f in all_tiffs
        if ("Chan" in Path(f).name or "CH" in Path(f).name)
    ]

    skipped = len(all_tiffs) - len(tiff_files)

    log_info(f"Loaded {len(tiff_files)} valid channel TIFF files")

    if skipped > 0:
        log_warn(f"Skipped {skipped} non-standard TIFF files")

    if not tiff_files:
        raise FileNotFoundError("No valid Chan*.tif files found")

    print(f"[DISCOVERY] Found {len(tiff_files)} usable TIFF files")

    return sorted(tiff_files)


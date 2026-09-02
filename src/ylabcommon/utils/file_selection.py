from pathlib import Path
from ..utils.utils import scan_tiff_dir, log_info, log_warn


def collect_valid_tiffs(tiff_dir: Path) -> tuple[list, dict]:
    """取得ディレクトリの生 TIFF を列挙し、``(files, sizes)`` を返す。

    サイズを一緒に返すのは、後段のサイズフィルタが同じディレクトリをもう一度
    列挙しなくて済むようにするため (:func:`scan_tiff_dir` 参照)。SMB 越しでは
    ディレクトリ列挙 1 回ぶんの往復がまるごと浮く。
    """
    tiff_dir = Path(tiff_dir)

    if not tiff_dir.exists():
        raise FileNotFoundError(f"Input folder does not exist: {tiff_dir}")

    all_tiffs, sizes = scan_tiff_dir(str(tiff_dir))

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

    # scan_tiff_dir が既に自然順 (Z9 < Z10) で返している。ここで素の sorted() を
    # かけ直すと辞書順に戻ってしまい、ゼロ埋めされていない取得で面の順序が狂う。
    return tiff_files, sizes

"""
Simple runner for BioIO; Can move with CLI also 

Usage:

uv run python bioio_run_process_experiment.py \
    --tiff-dir ./data \
    --xml ./Experiment.xml \
    --output-dir ./out
    ........................
"""

from pathlib import Path
import argparse

from ylabcommon.bioio.thorlab.builder import ThorlabBioioBuilder
from ylabcommon.utils.utils import get_theme, style_print



# =============================================================================
# Argument Parser
# =============================================================================

def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        prog="thorlab-bioio",
        description="Convert Thorlabs TIFF + Experiment.xml into validated OME datasets using BioIO",
    )

    parser.add_argument(
        "--tiff-dir",
        type=Path,
        help="Folder containing raw Thorlabs TIFF files",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default="Output",
        help="Directory to write OME outputs rather than Tiff directory",
    )

    parser.add_argument(
        "--compression",
        default="zlib",
        help="Compression type (zlib, lzw, etc.)",
    )

    parser.add_argument(
        "--compression-level",
        type=int,
        default=6,
        help="Compression level (0–9)",
    )

    parser.add_argument(
        "--dry_run", 
        action="store_true", 
        help="Run full pipeline without writing any output files"
    )

    parser.add_argument("--verbose", action="store_true")

    return parser

# =============================================================================
# Main Execution
# =============================================================================

def main() -> None:

    parser = build_parser()
    args = parser.parse_args()
    
    dataset_name = args.tiff_dir.name
    print("DATASET NAME:", dataset_name)
   

    output_fname = args.output_dir / f"{dataset_name}.ome.tiff"

    theme = get_theme()

    style_print("\n========== Thorlab BioIO Processing ======================\n", "header")
    style_print(f"Started at: {theme['timestamp']}", "info")
    style_print(f"TIFF directory  : {args.tiff_dir}", "info")
    style_print(f"Output: {output_fname}", "info")
    style_print("\n==========================================================", "header")

    builder = ThorlabBioioBuilder(
        tiff_dir=args.tiff_dir,
        compression=args.compression,
        compression_level=args.compression_level,
        dry_run=args.dry_run,
    )

    builder.build()
    builder.write(output_fname)

    print("=============================================================================")
    style_print("[Builder] DONE. Processing completed successfully : success")
    style_print("Check Summary and validation Report.\n", "info")
    print("================================================================================")

# =============================================================================

if __name__ == "__main__":
    main()

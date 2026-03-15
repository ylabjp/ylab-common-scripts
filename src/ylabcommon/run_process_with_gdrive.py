#!/usr/bin/env python3
"""
run_process_with_gdrive.py

Supports:
 - Drive mode  : --drive_folder

Output handling:
 - Drive default  : <extracted_root>/output_<dataset>
 - Override       : --diff_outdirpath
"""

import argparse
import json
import logging
import sys
import time
import threading
from pathlib import Path
import datetime

from util_download_drive_folder import download_and_extract_drive_folder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger(__name__)


# -------------------------------------------------
# CLI
# -------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser("Gdrive Extract")

    # Drive mode
    p.add_argument("--drive_folder", type=str)
    p.add_argument("--auth_mode", choices=["service_account", "oauth"], default="service_account")
    p.add_argument("--service_account", type=str)
    p.add_argument("--client_secret", type=str)

    # Directories
    p.add_argument("--work_dir", type=str, default="./drive_work")
    p.add_argument("--output_dir", type=str, default="./output")  # kept for backward compatibility
    p.add_argument(
        "--diff_outdirpath",
        type=str,
        default=None,
        help="Optional different base output directory"
    )

    p.add_argument("--verbose", action="store_true")

    return p.parse_args()


# -------------------------------------------------
# Helpers
# -------------------------------------------------
def write_summary(summary: dict, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    summary_path = outdir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    logger.info(f"Summary written → {summary_path}")

# -------------------------------------------------
# DRIVE MODE
# -------------------------------------------------
def process_drive_mode(args):
    print("%s")
    logger.info("Running in DRIVE mode")

    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    extracted_root = download_and_extract_drive_folder(
        folder_url=args.drive_folder,
        work_dir=work_dir,
        auth_mode=args.auth_mode,
        service_account_json=args.service_account,
        client_secret_json=args.client_secret,
    )

    datasets = [p for p in extracted_root.iterdir() if p.is_dir()]
    logger.info(f"Found {len(datasets)} extracted datasets")

    for dataset in datasets:
        xml_files = list(dataset.glob("**/Experiment.xml"))
        if not xml_files:
            logger.warning(f"No Experiment.xml found in {dataset}, skipping")
            continue

        xml_path = xml_files[0]
        dataset_name = dataset.name

        if args.diff_outdirpath:
            output_dir = Path(args.diff_outdirpath) / f"output_{dataset_name}"
        else:
            output_dir = extracted_root / f"output_{dataset_name}"

        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Processing dataset: {dataset_name}")
        start = time.time()

        """
        #-------------------------------------------------------------------------------------------------------- 
        #NOTE: Not need here just kep it here when you need, replace with the needed funtion, builder etcc...
        #-------------------------------------------------------------------------------------------------------- 
        try:
            builder = ThorlabBuilder(str(dataset), str(xml_path))
            saved_files = builder.run_and_save(str(output_dir), save_raw=args.save_raw)
            status = "success"
        except Exception as e:
            logger.exception(f"Failed dataset {dataset_name}")
            saved_files = []
            status = "failed"
            error_msg = str(e)
        #-------------------------------------------------------------------------------------------------------- 
        #-------------------------------------------------------------------------------------------------------- 
        """

        elapsed = time.time() - start

        summary = {
            "mode": "drive",
            "dataset": dataset_name,
            "input_dir": str(dataset),
            "xml": str(xml_path),
            "output_dir": str(output_dir),
            #"n_files_written": len(saved_files),
            "runtime_sec": round(elapsed, 2),
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            #"status": status,
        }


        write_summary(summary, output_dir)


# -------------------------------------------------
# MAIN
# -------------------------------------------------
def main():
    args = parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if not args.drive_folder:
        sys.exit("Must use --drive_folder")

    process_drive_mode(args)

if __name__ == "__main__":
    main()


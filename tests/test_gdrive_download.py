import pytest
from pathlib import Path
import ylabcommon.utils.util_download_drive_folder as dl


def fake_download(folder_url, work_dir, **kwargs):
    dataset_dir = work_dir / "fake_dataset"
    dataset_dir.mkdir(parents=True, exist_ok=True)

    files = [
        "ChanA_001_001_001_001.tif",
        "ChanA_001_001_001_002.tif",
    ]

    for f in files:
        (dataset_dir / f).touch()

    return dataset_dir


def test_pipeline(monkeypatch, tmp_path):
    monkeypatch.setattr(
        dl,
        "download_and_extract_drive_folder",
        fake_download
    )

    dataset_dir = dl.download_and_extract_drive_folder(
        "fake_url",
        tmp_path
    )

    files = list(dataset_dir.glob("Chan*.tif"))

    assert len(files) > 0
    
    BLUE = '\033[94m'
    print(f"\n {BLUE}[INFORMATION]: Verified Drive extration \n")

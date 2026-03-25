# tests/conftest.py

import pytest
from pathlib import Path
import numpy as np
import tifffile

# --------------------------------------------------
# Pytest CLI options
# --------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--gdrive-folder",
        action="store",
        default=None,
        help="Google Drive folder URL for integration tests",
    )
    parser.addoption(
        "--gdrive-sa-json",
        action="store",
        default=None,
        help="Service account JSON path for GDrive tests",
    )
    parser.addoption(
        "--local-dataset",
        action="store",
        default=None,
        help="Local dataset directory for integration tests",
    )
    parser.addoption(
        "--local-tiff-dir",
        action="store",
        default=None,
        help="Local directory containing real TIFF files",
    )
    parser.addoption(
        "--local-xml",
        action="store",
        default=None,
        help="Local Experiment.xml file path",
    )



def pytest_configure(config):

    markers = [
        "unit: fast synthetic unit tests",
        "local: requires --local-dataset",
        "local: requirespath --local-tiff-dir and --local-xml",
        "gdrive: requires Google Drive service account",
        "integration: full pipeline using real data",
        "integration_bioio: full BioIO integration test",
        "slow: heavy dataset or stress test",
        "regression: scientific reproducibility tests",
    ]

    for m in markers:
        config.addinivalue_line("markers", m)


# --------------------------------------------------
# Temporary output directory
# --------------------------------------------------

@pytest.fixture
def tmp_output_root(tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    return out


# --------------------------------------------------
# Fake dataset for CI-safe unit tests
# --------------------------------------------------

@pytest.fixture
def fake_dataset(tmp_path):
    """
    Minimal synthetic dataset for unit tests
    """

    ds = tmp_path / "dataset"
    ds.mkdir()

    for i in range(3):
        (ds / f"fake_{i:03d}.tif").write_bytes(b"FAKE_TIFF")

    return ds


# --------------------------------------------------
# Local real dataset (opt-in integration)
# --------------------------------------------------

@pytest.fixture(scope="session")
def local_dataset(pytestconfig):

    dataset = pytestconfig.getoption("--local-dataset")

    if not dataset:
        pytest.skip("Local dataset not provided")

    dataset = Path(dataset)

    if not dataset.exists():
        raise FileNotFoundError(f"Dataset not found: {dataset}")

    return dataset

# --------------------------------------------------
# Real local dataset (opt-in integration)
# --------------------------------------------------

@pytest.fixture(scope="session")
def local_real_dataset_common(pytestconfig):
    tiff_dir = pytestconfig.getoption("--local-tiff-dir")
    xml_path = pytestconfig.getoption("--local-xml")
    
    if not tiff_dir or not xml_path:
        pytest.skip("Local real dataset not provided")
        
    tiff_dir = Path(tiff_dir)
    xml_path = Path(xml_path)

    if not tiff_dir.exists():
        raise FileNotFoundError(f"TIFF directory not found: {tiff_dir}")
    if not xml_path.exists():
        raise FileNotFoundError(f"XML file not found: {xml_path}")
        
    return tiff_dir, xml_path


# --------------------------------------------------
# Google Drive dataset (opt-in integration)
# --------------------------------------------------

@pytest.fixture(scope="session")
def gdrive_dataset(pytestconfig, tmp_path_factory):

    folder = pytestconfig.getoption("--gdrive-folder")
    sa_json = pytestconfig.getoption("--gdrive-sa-json")

    if not folder or not sa_json:
        pytest.skip("GDrive credentials not provided")

    from ylabcommon.utils.util_download_drive_folder import (
        download_and_extract_drive_folder,
    )

    work_dir = tmp_path_factory.mktemp("gdrive_work")

    extracted = download_and_extract_drive_folder(
        folder_url=folder,
        work_dir=work_dir,
        auth_mode="service_account",
        service_account_json=sa_json,
    )

    return extracted

@pytest.fixture
def generate_fake_ome_stack(tmp_path):
    """
    Generate a realistic OME-style microscopy stack (TCZYX).
    """

    # dimensions
    T = 2
    C = 2
    Z = 4
    Y = 64
    X = 64

    stack = np.random.randint(
        0, 65535,
        size=(T, C, Z, Y, X),
        dtype=np.uint16
    )

    ome_file = tmp_path / "test.ome.tif"

    tifffile.imwrite(
        ome_file,
        stack,
        metadata={
            "axes": "TCZYX"
        }
    )

    return ome_file, stack

@pytest.fixture
def generate_fake_file(tmp_path):
    d = tmp_path / "thorlab_dataset"
    d.mkdir()

    files = [
        "ChanA_001_001_001_001.tif",
        "ChanA_001_001_001_002.tif",
        "ChanA_001_001_001_003.tif",
    ]

    for f in files:
        (d / f).touch()

    return d

@pytest.fixture
def output_dir(tmp_path):
    return tmp_path

@pytest.fixture
def output_filename():
    return "test"

import os
from pathlib import Path

import pandas as pd
import pytest

from ylabcommon import pd_util


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def sample_hdf_path(tmp_path: Path) -> Path:
    """
    Create a dummy HDF5 file path that matches the naming convention
    expected by `get_prj_name` and `read_and_cache`.
    """
    nested = tmp_path / "projA" / "paradigmB"
    nested.mkdir(parents=True)
    file_path = nested / "df_individual_analyzed_merged.h5"
    file_path.touch()  # empty placeholder – we monkey-patch pandas anyway
    return file_path


# --------------------------------------------------------------------------- #
# get_prj_name                                                                #
# --------------------------------------------------------------------------- #

def test_get_prj_name_valid(sample_hdf_path: Path):
    expected = "projA_paradigmB"
    assert pd_util.get_prj_name(str(sample_hdf_path)) == expected


def test_get_prj_name_invalid(tmp_path: Path):
    wrong_file = tmp_path / "random.h5"
    wrong_file.touch()
    with pytest.raises(ValueError):
        pd_util.get_prj_name(str(wrong_file))


# --------------------------------------------------------------------------- #
# read_and_cache – cached path branch                                         #
# --------------------------------------------------------------------------- #

def test_read_and_cache_uses_cache(monkeypatch, sample_hdf_path: Path, tmp_path: Path):
    """
    When the cache file already exists, read_and_cache should load from that
    cache instead of the original HDF5.
    """
    sentinel_df = pd.DataFrame({"x": [1, 2, 3]})

    # Build the expected cache name
    cache_dir = tmp_path
    expected_cache = cache_dir / ("_cache_" + pd_util.get_prj_name(str(sample_hdf_path)) + ".h5")
    expected_cache.touch()  # pretend cache exists

    # Monkey-patch os.path.exists so the function believes the cache is present
    monkeypatch.setattr(os.path, "exists", lambda p: Path(p) == expected_cache)

    # Monkey-patch pandas.read_hdf so we don’t need actual HDF5 support
    def fake_read_hdf(path, key=None):
        assert Path(path) == expected_cache  # must read from cache
        return sentinel_df

    monkeypatch.setattr(pd, "read_hdf", fake_read_hdf)

    out_df = pd_util.read_and_cache(
        fname_df=str(sample_hdf_path),
        cache_path=str(cache_dir),
        cond_map={},  # not used in this branch
    )
    pd.testing.assert_frame_equal(out_df, sentinel_df)

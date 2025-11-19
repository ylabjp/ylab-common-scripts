import os
import platform
from pathlib import Path

import pytest

import ylabcommon.file_util as fu


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def dir_tree(tmp_path: Path):
    """Create a nested directory tree with a marker file in the root."""
    deep_dir = tmp_path / "level1" / "level2"
    deep_dir.mkdir(parents=True)
    marker = tmp_path / "target_marker"
    marker.touch()
    return {"deep": deep_dir, "marker": marker}


# --------------------------------------------------------------------------- #
# find_parents_for_dir                                                        #
# --------------------------------------------------------------------------- #

def test_find_parents_for_dir_finds_target(dir_tree):
    found = fu.find_parents_for_dir(dir_tree["deep"], "target_marker")
    assert found == dir_tree["marker"]


def test_find_parents_for_dir_returns_none(dir_tree):
    missing = fu.find_parents_for_dir(dir_tree["deep"], "does_not_exist")
    assert missing is None


# --------------------------------------------------------------------------- #
# replace_yen_in_path                                                         #
# --------------------------------------------------------------------------- #

def test_replace_yen_in_path_handles_backslashes():
    in_path = r"some\\folder\\file.txt"
    out_path = fu.replace_yen_in_path(None, in_path)  # self arg is unused
    assert out_path == "some//folder//file.txt"


# --------------------------------------------------------------------------- #
# init_base_drive                                                             #
# --------------------------------------------------------------------------- #

def test_init_base_drive_selects_correct_prefix(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    prefix_map = {"Windows": "C:\\Data", "Linux": str(tmp_path)}
    assert fu.init_base_drive(prefix_map) == str(tmp_path)

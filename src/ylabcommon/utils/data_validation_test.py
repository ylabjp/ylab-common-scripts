import pandas as pd
import pytest

from ylabcommon.utils.data_validation import (
    DataStructureError,
    require_columns,
    require_h5_path,
    require_h5_paths,
    require_keys,
)


# --------------------------------------------------------------------------- #
# h5 (file-like) path checks                                                  #
# --------------------------------------------------------------------------- #
class _FakeH5:
    """``"a/b" in obj`` を実装した h5py.File 相当のダミー。"""

    def __init__(self, paths):
        self._paths = set(paths)

    def __contains__(self, key):
        return key in self._paths


def test_require_h5_paths_passes_when_all_present():
    h5 = _FakeH5(["param_general", "trial_data", "param_task/general"])
    # 例外が出なければOK
    require_h5_paths(h5, ["param_general", "param_task/general"])


def test_require_h5_paths_raises_listing_missing():
    h5 = _FakeH5(["param_general"])
    with pytest.raises(DataStructureError) as exc:
        require_h5_paths(h5, ["param_general", "trial_data", "param_task"], context="a.h5")
    msg = str(exc.value)
    assert "trial_data" in msg
    assert "param_task" in msg
    assert "param_general" not in msg  # 存在するものは列挙しない
    assert "a.h5" in msg  # context がメッセージに含まれる


def test_require_h5_path_single():
    h5 = _FakeH5([])
    with pytest.raises(DataStructureError):
        require_h5_path(h5, "trial_data")


def test_data_structure_error_is_value_error():
    # 既存の except ValueError / except Exception で捕捉できること
    assert issubclass(DataStructureError, ValueError)


# --------------------------------------------------------------------------- #
# DataFrame column checks                                                     #
# --------------------------------------------------------------------------- #
def test_require_columns_passes():
    df = pd.DataFrame({"type": [1], "variable_interval_in_s": [2]})
    require_columns(df, ["type", "variable_interval_in_s"])


def test_require_columns_raises_with_available_listed():
    df = pd.DataFrame({"type": [1]})
    with pytest.raises(DataStructureError) as exc:
        require_columns(df, ["type", "variable_interval_in_s"], context="task_table")
    msg = str(exc.value)
    assert "variable_interval_in_s" in msg
    assert "task_table" in msg
    assert "type" in msg  # available columns に含まれる


# --------------------------------------------------------------------------- #
# mapping/dict key checks                                                     #
# --------------------------------------------------------------------------- #
def test_require_keys_passes():
    require_keys({"a": 1, "b": 2}, ["a"])


def test_require_keys_raises():
    with pytest.raises(DataStructureError) as exc:
        require_keys({"a": 1}, ["a", "b"], context="config")
    assert "b" in str(exc.value)

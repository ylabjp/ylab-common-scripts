import pytest
from pydantic import ValidationError

from ylabcommon.models.parameters import analysis_behavior as ab


# --------------------------------------------------------------------------- #
# DLCConfig                                                                   #
# --------------------------------------------------------------------------- #

def test_dlc_config_validation():
    cfg = ab.DLCConfig(config_path="conf.yml", bodyparts_version=2, is_dynamic=True)
    assert cfg.bodyparts_version == 2
    assert cfg.is_dynamic is True


def test_dlc_config_validation_error():
    with pytest.raises(ValidationError):
        # bodyparts_version must be int, not str
        ab.DLCConfig(config_path="x", bodyparts_version="two", is_dynamic=False)

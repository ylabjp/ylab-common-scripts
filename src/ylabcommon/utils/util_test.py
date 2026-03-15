import json
import yaml
from pathlib import Path

import pytest
from ylabcommon import util


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def original_data():
    """Python object that will be written to JSON, then round-tripped."""
    return {
        "alpha": 1,
        "beta": ["x", "y", "z"],
        "gamma": {"nested": True},
    }


@pytest.fixture()
def json_file(tmp_path: Path, original_data):
    """Temporary JSON file containing *original_data*."""
    jf = tmp_path / "sample.json"
    with jf.open("w", encoding="utf-8") as fp:
        json.dump(original_data, fp)
    return jf


# --------------------------------------------------------------------------- #
# deepupdate                                                                  #
# --------------------------------------------------------------------------- #
def test_deepupdate_merges_nested_dicts():
    base = {"a": 1, "b": {"c": 2}}
    other = {"b": {"d": 3}, "e": 4}

    result = util.deepupdate(base.copy(), other)

    assert result == {
        "a": 1,
        "b": {"c": 2, "d": 3},
        "e": 4,
    }, "deepupdate should merge dictionaries recursively"


# --------------------------------------------------------------------------- #
# convert_json_to_yaml                                                        #
# --------------------------------------------------------------------------- #
def test_convert_json_to_yaml_roundtrip(json_file: Path, original_data):
    """
    YAML produced by convert_json_to_yaml should translate back to
    the same Python object as the original JSON content.
    """
    yaml_str = util.convert_json_to_yaml(str(json_file))
    roundtrip_obj = yaml.safe_load(yaml_str)

    assert roundtrip_obj == original_data


@pytest.fixture(params=[
    "{'bad': True}",       # single quotes are invalid in JSON
    "not even json",       # random string
])
def invalid_json_file(tmp_path: Path, request):
    bad = tmp_path / "bad.json"
    bad.write_text(request.param, encoding="utf-8")
    return bad


def test_convert_json_to_yaml_invalid(invalid_json_file: Path):
    output = util.convert_json_to_yaml(str(invalid_json_file))
    assert output.startswith("Error"), "Should return an error message for invalid JSON"



"""standard_arg_parser の --gpu は、gpu=True を渡したコマンドだけが受け付ける。"""
import sys

import pytest

from ylabcommon.models.parameters.general import ArgModel, standard_arg_parser

pytestmark = pytest.mark.unit


def _parse(monkeypatch, argv, **kwargs) -> ArgModel:
    monkeypatch.setattr(sys, "argv", ["prog", *argv])
    return standard_arg_parser(**kwargs)


def test_gpu_rejected_by_default(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _parse(monkeypatch, ["--gpu", "1"])
    assert exc.value.code == 2  # argparse usage error


def test_gpu_parsed_when_enabled(monkeypatch):
    assert _parse(monkeypatch, ["--gpu", "1"], gpu=True).gpu == 1


def test_gpu_omitted_is_none(monkeypatch):
    assert _parse(monkeypatch, [], gpu=True).gpu is None
    assert _parse(monkeypatch, []).gpu is None


@pytest.mark.parametrize("gpu", [False, True])
def test_overwrite_and_subfolder_unchanged(monkeypatch, gpu):
    args = _parse(monkeypatch, ["-o", "-s", "sub"], gpu=gpu)
    assert args.overwrite is True
    assert _parse(monkeypatch, [], gpu=gpu).overwrite is False

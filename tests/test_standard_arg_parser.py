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


def test_mouse_rejected_by_default(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _parse(monkeypatch, ["-m", "ET484"])
    assert exc.value.code == 2


def test_mouse_parsed_when_enabled(monkeypatch):
    args = _parse(monkeypatch, ["-m", "ET484", "ET485"], mouse=True)
    assert args.overwrite_mice == frozenset({"ET484", "ET485"})
    assert args.overwrite is False


def test_mouse_omitted_is_empty(monkeypatch):
    assert _parse(monkeypatch, [], mouse=True).overwrite_mice == frozenset()
    assert _parse(monkeypatch, []).overwrite_mice == frozenset()


def test_mouse_and_overwrite_are_exclusive(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _parse(monkeypatch, ["-o", "-m", "ET484"], mouse=True)
    assert exc.value.code == 2


def test_video_rejected_by_default(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _parse(monkeypatch, ["--video"])
    assert exc.value.code == 2


def test_video_parsed_when_enabled(monkeypatch):
    assert _parse(monkeypatch, ["--video"], video=True).video is True
    assert _parse(monkeypatch, [], video=True).video is False
    assert _parse(monkeypatch, []).video is False


def test_argv_given_explicitly():
    args = standard_arg_parser(gpu=True, mouse=True, video=True,
                               argv=["--video", "-m", "ET1", "--gpu", "0"])
    assert (args.video, args.overwrite_mice, args.gpu) == (True, frozenset({"ET1"}), 0)

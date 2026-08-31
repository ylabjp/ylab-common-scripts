"""走査オフセット (`LSM/@offsetX` `@offsetY`) が 0 でない取得を知らせることのテスト。

``<LSM offsetX="-52" offsetY="36" .../>`` は走査の中心を視野の中心からずらす設定で、
既定は 0。0 でない取得は視野がステージ座標の中心と一致しないので、他の取得と並べた
ときに位置が合わない。画素も画素サイズも変わらないため取り込みは通るが、黙って
通すと、あとで「位置が合わない」とだけ言われて原因を追えなくなる。
"""
from __future__ import annotations

import pytest

from ylabcommon.bioio.thorlab.xml_parser import (
    ExperimentXMLParser,
    scan_offset,
    scan_offset_known,
    warn_if_scan_offset,
)


def _xml(tmp_path, offset_x, offset_y):
    p = tmp_path / "Experiment.xml"
    p.write_text(
        '<?xml version="1.0"?><ThorImageExperiment>'
        '<LSM name="ThorDAQ RGG" pixelX="512" pixelY="512" '
        'pixelWidthUM="0.17" pixelHeightUM="0.17" '
        f'offsetX="{offset_x}" offsetY="{offset_y}" />'
        '<Streaming enable="1" frames="3000" zFastEnable="0" />'
        '</ThorImageExperiment>', encoding="utf-8")
    return p


@pytest.mark.parametrize("x,y", [(-52, 36), (-52, 0), (0, 36), (1, -1)])
def test_a_shifted_scan_is_reported(tmp_path, capsys, x, y):
    meta = ExperimentXMLParser(_xml(tmp_path, x, y)).extract_metadata()

    assert scan_offset(meta) == (x, y)
    assert warn_if_scan_offset(meta, tmp_path / "Experiment.xml") is True

    out = capsys.readouterr().out
    assert "Scan offset is not centred" in out
    assert "offsetX=%d, offsetY=%d" % (x, y) in out


def test_a_centred_scan_is_silent(tmp_path, capsys):
    """0 なら何も出さない。毎回鳴る警告は読まれなくなる。"""
    meta = ExperimentXMLParser(_xml(tmp_path, 0, 0)).extract_metadata()

    assert scan_offset(meta) == (0, 0)
    assert warn_if_scan_offset(meta, tmp_path / "Experiment.xml") is False
    assert capsys.readouterr().out == ""


def _xml_without_offset(tmp_path, lsm='<LSM pixelX="512" pixelY="512" />'):
    p = tmp_path / "Experiment.xml"
    p.write_text('<?xml version="1.0"?><ThorImageExperiment>'
                 '%s</ThorImageExperiment>' % lsm, encoding="utf-8")
    return p


def test_a_missing_offset_does_not_crash_the_parser(tmp_path):
    """属性が無い XML でも落ちない (古い ThorImage を想定)。

    数値がほしいだけの呼び出し側のために ``scan_offset`` は 0 を返し続ける。
    """
    meta = ExperimentXMLParser(_xml_without_offset(tmp_path)).extract_metadata()

    assert scan_offset(meta) == (0, 0)
    assert warn_if_scan_offset(meta, tmp_path / "Experiment.xml") is False


@pytest.mark.parametrize("lsm", [
    '<LSM pixelX="512" pixelY="512" />',                 # 属性が無い
    '<LSM pixelX="512" offsetX="0" />',                  # 片方だけ (もう片方は不明)
    '<LSM pixelX="512" offsetX="" offsetY="" />',        # 空
    '<LSM pixelX="512" offsetX="n/a" offsetY="n/a" />',  # 数として読めない
    '<Streaming enable="1" />',                          # LSM ごと無い
])
def test_an_unrecorded_offset_is_not_reported_as_zero(tmp_path, lsm):
    """回帰: 記録の無い取得が「オフセット 0」として合格していた。

    ``extract_metadata`` が ``or 0`` で倒していたので、``ScanOffsetX`` は必ず
    int になり、**書かれていなかったことが呼び出し側から見えなかった**。
    0 であることを取り込みの合否に使う道具 (slice-analysis の QC) では、
    確かめられなかったものが黙って合格する。
    """
    meta = ExperimentXMLParser(_xml_without_offset(tmp_path, lsm)).extract_metadata()

    assert scan_offset_known(meta) is False
    # 記録の無いほうが None として残っていること (0 に倒されていない)
    assert None in (meta.get("ScanOffsetX"), meta.get("ScanOffsetY"))


def test_a_recorded_zero_is_told_apart_from_no_record(tmp_path):
    """負のコントロール: 0 と **書いてある** 取得は「記録あり」。"""
    meta = ExperimentXMLParser(_xml(tmp_path, 0, 0)).extract_metadata()

    assert scan_offset_known(meta) is True
    assert meta["ScanOffsetX"] == 0 and meta["ScanOffsetY"] == 0
    assert scan_offset(meta) == (0, 0)


def test_an_unrecorded_offset_still_says_something(tmp_path, capsys):
    """黙って通さない。ただし「ずれている」とは言わない。"""
    p = _xml_without_offset(tmp_path)
    meta = ExperimentXMLParser(p).extract_metadata()

    shifted = warn_if_scan_offset(meta, p)

    out = capsys.readouterr().out
    assert "not recorded" in out, out
    assert "not centred" not in out, "ずれていると分かったわけではない"
    # 戻り値で分岐している呼び出し側に「ずれている」と言ってはいけない
    assert shifted is False


def test_the_values_reach_better_stack_as_fields(tmp_path, monkeypatch):
    """値は本文だけでなく構造化フィールドとしても送る (後から集計できるように)。"""
    from ylabcommon.utils import betterstack_log as bs

    sent = []
    monkeypatch.setattr(bs, "send", lambda level, message, **f: sent.append((level, f)))

    meta = ExperimentXMLParser(_xml(tmp_path, -52, 36)).extract_metadata()
    warn_if_scan_offset(meta, tmp_path / "Experiment.xml")

    assert len(sent) == 1
    level, fields = sent[0]
    assert level == "warning"
    assert fields["scan_offset_x"] == -52
    assert fields["scan_offset_y"] == 36
    assert fields["stage"] == "thorlab.xml"


def test_the_console_colour_never_leaks_into_the_sent_message(tmp_path, monkeypatch):
    """制御文字は端末に出す文字にだけ付ける。ログ本文には混ぜない。"""
    from ylabcommon.utils import betterstack_log as bs
    from ylabcommon.utils import util

    monkeypatch.setattr(util, "supports_color", lambda stream=None: True)
    sent = []
    monkeypatch.setattr(bs, "send", lambda level, message, **f: sent.append(message))

    meta = ExperimentXMLParser(_xml(tmp_path, -52, 36)).extract_metadata()
    warn_if_scan_offset(meta, tmp_path / "Experiment.xml")

    assert "\033[" not in sent[0]


def test_colour_is_dropped_when_the_output_is_not_a_terminal(monkeypatch, capsys):
    """端末でなければ色を付けない (``←[33m`` が本文に見えるのを防ぐ)。"""
    from ylabcommon.utils.betterstack_log import log_warning

    log_warning("plain please", console=True)
    out = capsys.readouterr().out          # capsys の下では isatty() が False

    assert out == "[WARNING] plain please\n"
    assert "\033[" not in out


def test_a_half_recorded_shift_is_still_called_a_shift(tmp_path, capsys):
    """片方しか記録が無くても、その片方が 0 でなければ「ずれている」。

    「記録が無い」を先に見ると、確定している事実が「確認できません」に隠れる。
    文面では、記録の無いほうを 0 と書かない。
    """
    p = _xml_without_offset(tmp_path, '<LSM pixelX="512" offsetX="-52" />')
    meta = ExperimentXMLParser(p).extract_metadata()

    assert warn_if_scan_offset(meta, p) is True

    out = capsys.readouterr().out
    assert "not centred" in out
    assert "offsetX=-52" in out
    assert "offsetY=not recorded" in out, out
    assert "offsetY=0" not in out, "記録の無い値を 0 と書いている"

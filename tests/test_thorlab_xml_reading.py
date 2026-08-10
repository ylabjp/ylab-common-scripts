"""Experiment.xml の読み方 (仮説) をコードに固定するテスト。

ThorImage の XML に公開仕様は無く、読み方は実データとの突き合わせで決めている。
仮説と根拠は docs/thorlabs_experiment_xml.md にあり、ここはその仮説が
コードから消えないようにするためのもの。

とくに Streaming/ZStage/Timelapse の関係は、間違えると時系列が丸ごと Z 軸へ
潰れる (実際に 3001 時点が深さ 1500 um のスタックになった)。
"""
from __future__ import annotations

import pytest

from ylabcommon.bioio.thorlab.xml_parser import ExperimentXMLParser


def write_xml(d, *, streaming=None, z_steps=61, z_enable="1", timepoints=3000,
              interval=60, channels=("ChanA", "ChanB"), channel_set=3,
              pixel_x=512, pixel_y=512):
    """実データの XML から、構造の判定に関わるノードだけを抜き出した最小形。"""
    wl = "".join(f'<Wavelength name="{c}" exposureTimeMS="0" />' for c in channels)
    if channel_set is not None:
        wl += f'<ChannelEnable Set="{channel_set}" />'
    stream = ""
    if streaming is not None:
        enable, z_fast = streaming
        stream = (f'<Streaming enable="{enable}" frames="3000" zFastEnable="{z_fast}" '
                  f'zFastMode="1" flybackFrames="1" previewIndex="1" '
                  f'triggerMode="1" />')
    (d / "Experiment.xml").write_text(
        "<ThorImageExperiment>"
        '<Date date="07/29/2026 12:52:07" uTime="1785297127" />'
        f'<LSM pixelX="{pixel_x}" pixelY="{pixel_y}" pixelWidthUM="0.17" '
        'pixelHeightUM="0.17" pixelSizeUM="2.93" widthUM="86.96" heightUM="86.96" '
        'frameRate="45.638" dwellTime="0.12" averageMode="0" averageNum="10" '
        'NumberOfPlanes="1" />'
        f'<ZStage steps="{z_steps}" stepSizeUM="0.5" enable="{z_enable}" '
        'zStreamFrames="1" zStreamMode="0" />'
        f'<Timelapse timepoints="{timepoints}" intervalSec="{interval}" />'
        f"<Wavelengths>{wl}</Wavelengths>"
        '<Magnification mag="27.777" name="25xOLY" />'
        f"{stream}"
        "</ThorImageExperiment>",
        encoding="utf-8",
    )
    return d / "Experiment.xml"


# ---- Streaming が Z/T の読み方を決める ---------------------------------------

def test_streaming_without_fast_z_is_a_single_plane_time_series(tmp_path):
    """Streaming enable=1 / zFastEnable=0 なら Z=1、T は Streaming の frames。

    これが報告された不具合そのもの。ZStage steps=61 / enable=1 が残っていても、
    連続取得中に低速な Z ステージは動かせないので Z は 1 面である。
    steps=61 を信じると 3001 時点が Z 軸へ潰れて深さ 1500 um になる。
    """
    xml = write_xml(tmp_path, streaming=("1", "0"), z_steps=61, z_enable="1")
    m = ExperimentXMLParser(xml).extract_metadata()

    assert m["SizeZ"] == 1
    assert m["SizeT"] == 3000              # Timelapse ではなく Streaming の frames
    assert m["ZStackEnabled"] is False
    assert (m["Streaming"], m["ZFastEnabled"]) == (True, False)


def test_fast_z_streaming_splits_the_frames_into_volumes(tmp_path):
    """zFastEnable=1 なら ZStage の段数が効き、frames は面数なので段数で割る。"""
    xml = write_xml(tmp_path, streaming=("1", "1"), z_steps=60)
    m = ExperimentXMLParser(xml).extract_metadata()

    assert m["SizeZ"] == 60
    assert m["SizeT"] == 50                # 3000 面 / 60 段
    assert m["ZStackEnabled"] is True


def test_a_non_streaming_z_stack_reads_zstage_and_timelapse(tmp_path):
    """Streaming enable=0 なら従来どおり ZStage と Timelapse を読む。"""
    xml = write_xml(tmp_path, streaming=("0", "0"), z_steps=61, timepoints=5)
    m = ExperimentXMLParser(xml).extract_metadata()

    assert (m["SizeZ"], m["SizeT"]) == (61, 5)
    assert m["ZStackEnabled"] is True


def test_a_missing_streaming_node_behaves_like_streaming_off(tmp_path):
    """Streaming ノードごと無い XML (古い版) でも壊れない。"""
    xml = write_xml(tmp_path, streaming=None, z_steps=61, timepoints=5)
    m = ExperimentXMLParser(xml).extract_metadata()

    assert (m["SizeZ"], m["SizeT"]) == (61, 5)
    assert m["Streaming"] is False


def test_z_enable_off_means_no_stack_even_without_streaming(tmp_path):
    xml = write_xml(tmp_path, streaming=("0", "0"), z_enable="0")
    assert ExperimentXMLParser(xml).extract_metadata()["ZStackEnabled"] is False


def test_zfastmode_is_not_zfastenable(tmp_path):
    """zFastMode は fast-Z の *方式* であって有効/無効ではない。

    実データは zFastEnable="0" zFastMode="1"。取り違えると Z=61 になる。
    """
    xml = write_xml(tmp_path, streaming=("1", "0"))
    assert ExperimentXMLParser(xml).extract_metadata()["SizeZ"] == 1


# ---- チャンネル --------------------------------------------------------------

def test_enabled_channels_come_from_the_bitmask(tmp_path):
    """<ChannelEnable Set> が有効チャンネルを決める (Set=3 なら1番目と2番目)。"""
    xml = write_xml(tmp_path, streaming=("1", "0"), channel_set=3)
    assert ExperimentXMLParser(xml).extract_metadata()["Channels"] == ["ChanA", "ChanB"]


def test_a_disabled_channel_is_not_counted(tmp_path):
    """Set=1 なら <Wavelength> が2つ並んでいても有効なのは1つ。

    数え違えると「XML は2波長だが実データは1チャンネル」という食い違いが出る。
    """
    xml = write_xml(tmp_path, streaming=("1", "0"), channel_set=1)
    assert ExperimentXMLParser(xml).extract_metadata()["Channels"] == ["ChanA"]


def test_the_second_channel_alone_is_read_from_the_bit_position(tmp_path):
    """ビット位置で選ぶ (先頭から N 個ではない)。"""
    xml = write_xml(tmp_path, streaming=("1", "0"), channel_set=2)
    assert ExperimentXMLParser(xml).extract_metadata()["Channels"] == ["ChanB"]


def test_without_a_bitmask_all_wavelengths_are_used(tmp_path):
    """<ChannelEnable> が無い XML では全波長を採る (従来どおり)。"""
    xml = write_xml(tmp_path, streaming=("1", "0"), channel_set=None)
    assert ExperimentXMLParser(xml).extract_metadata()["Channels"] == ["ChanA", "ChanB"]


# ---- 画素サイズの罠 ----------------------------------------------------------

def test_frame_size_comes_from_pixelx_not_the_nonexistent_width(tmp_path):
    """LSM/@width は存在しない。以前はそれを読んで常に 512 になっていた。"""
    xml = write_xml(tmp_path, streaming=("1", "0"), pixel_x=1024, pixel_y=256)
    m = ExperimentXMLParser(xml).extract_metadata()
    assert (m["SizeX"], m["SizeY"]) == (1024, 256)


def test_pixel_size_is_not_the_misleading_pixelsizeum_attribute(tmp_path):
    """LSM/@pixelSizeUM (2.93) は um/px ではない。pixelWidthUM (0.17) を使う。"""
    xml = write_xml(tmp_path, streaming=("1", "0"))
    m = ExperimentXMLParser(xml).extract_metadata()
    assert m["PixelSizeX"] == pytest.approx(0.17)
    assert m["PixelSizeY"] == pytest.approx(0.17)


def test_the_z_step_is_taken_as_a_positive_length(tmp_path):
    """下向き取得で stepSizeUM が負でも、物理サイズは正で扱う。"""
    xml = write_xml(tmp_path, streaming=("0", "0"))
    xml.write_text(xml.read_text(encoding="utf-8").replace('stepSizeUM="0.5"',
                                                           'stepSizeUM="-0.5"'),
                   encoding="utf-8")
    assert ExperimentXMLParser(xml).extract_metadata()["PixelSizeZ"] == 0.5


# ---- as_params (取り込み用の見方) --------------------------------------------

def test_params_fill_defaults_without_hiding_the_structure(tmp_path):
    xml = write_xml(tmp_path, streaming=("1", "0"))
    p = ExperimentXMLParser(xml).as_params()

    assert (p["SizeZ"], p["SizeT"]) == (1, 3000)
    assert p["Streaming"] is True and p["ZFastEnabled"] is False
    assert p["Objective"] == "25xOLY"
    assert p["TimeStamp"] == "07/29/2026 12:52:07"


def test_an_empty_xml_still_yields_usable_params(tmp_path):
    (tmp_path / "Experiment.xml").write_text("<ThorImageExperiment/>", encoding="utf-8")
    p = ExperimentXMLParser(tmp_path / "Experiment.xml").as_params()

    assert (p["SizeZ"], p["SizeT"]) == (1, 1)
    assert p["PixelSizeX"] == 1.0 and p["PixelSizeZ"] == 1.0
    assert p["Streaming"] is False

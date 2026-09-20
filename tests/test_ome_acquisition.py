"""撮像条件が OME-XML に残り、次元のラベルを壊さないこと。

`axes` の回帰が一番大事: 下流は ``series[0].axes`` を見て分岐している
(histology の plot_image_n_histogram は ``ZCYX`` / ``CYX`` / ``ZYX``)。
自作 OME を書き込み側へ渡すとここが ``IYX`` に化けるので、条件を足すのは
**画素を書いたあと**にしてある。その前提が崩れたら気付けるようにする。
"""
import numpy as np
import pytest
import tifffile
from ome_types import from_xml

from ylabcommon.bioio.core.bioio_writer import BioIOWriter
from ylabcommon.bioio.core.ome_acquisition import (
    ACQUISITION_NAMESPACE,
    AcquisitionConditions,
    write_acquisition_metadata,
)

T, C, Z, Y, X = 1, 2, 1, 8, 8


@pytest.fixture
def conditions():
    return AcquisitionConditions(
        channel_names=["CH1", "CH3"],
        exposure_by_channel={"CH1": 1 / 20, "CH3": 1 / 2},
        gain_by_channel={
            "CH1": {"CameraGain": 120, "CameraHardwareGain": 336},
            "CH3": {"CameraGain": 200, "CameraHardwareGain": 400},
        },
        lens="PlanApo 4x 0.20/20.00mm :Default",
        extra={"Sectioning": "None"},
    )


def _write(path, acquisition=None):
    BioIOWriter(str(path)).write(
        np.zeros((T, C, Z, Y, X), np.uint16),
        dim_order="TCZYX",
        channel_names=["CH1", "CH3"],
        acquisition=acquisition,
    )
    return path.parent / (path.stem + ".ome.tif")


def test_axes_survive_the_enrichment(tmp_path, conditions):
    """条件を足しても ``axes`` が変わらないこと。

    ここが落ちたら、下流の軸分岐が「未対応の軸」で止まる。
    """
    plain = _write(tmp_path / "plain")
    before = tifffile.TiffFile(str(plain)).series[0].axes

    enriched = _write(tmp_path / "enriched", conditions)
    after = tifffile.TiffFile(str(enriched)).series[0].axes

    assert after == before
    assert "C" in after


def test_exposure_lands_on_each_plane(tmp_path, conditions):
    out = _write(tmp_path / "a", conditions)

    ome = from_xml(tifffile.TiffFile(str(out)).ome_metadata)
    got = {pl.the_c: pl.exposure_time for pl in ome.images[0].pixels.planes}

    assert got == {0: 1 / 20, 1: 1 / 2}
    assert all(str(pl.exposure_time_unit).endswith("SECOND")
               for pl in ome.images[0].pixels.planes)


def test_camera_gain_lands_on_detector_settings(tmp_path, conditions):
    """``CameraGain`` は「この撮像でカメラに与えたゲイン」なので型付きの枠へ。"""
    out = _write(tmp_path / "a", conditions)

    ome = from_xml(tifffile.TiffFile(str(out)).ome_metadata)
    got = {ch.name: ch.detector_settings.gain for ch in ome.images[0].pixels.channels}

    assert got == {"CH1": 120.0, "CH3": 200.0}


def test_hardware_gain_is_kept_verbatim_not_reinterpreted(tmp_path, conditions):
    """``CameraHardwareGain`` の OME 対応先は未確認なので、元のキー名のまま残す。

    ゲインは 2 つとも注釈にも入れる。型付き側の解釈が後で変わっても、
    元の値が消えていないようにするため。
    """
    out = _write(tmp_path / "a", conditions)

    ome = from_xml(tifffile.TiffFile(str(out)).ome_metadata)
    annotation = ome.structured_annotations.map_annotations[0]
    got = {m.k: m.value for m in annotation.value.ms}

    assert annotation.namespace == ACQUISITION_NAMESPACE
    assert got["CH1/CameraHardwareGain"] == "336"
    assert got["CH3/CameraHardwareGain"] == "400"
    assert got["CH1/CameraGain"] == "120"  # 型付き側にも入れたが、ここにも残す
    assert got["Sectioning"] == "None"


def test_lens_is_not_parsed_into_numbers(tmp_path, conditions):
    """``PlanApo 4x 0.20/20.00mm`` を倍率や NA に割るのは解釈であって記録ではない。"""
    out = _write(tmp_path / "a", conditions)

    ome = from_xml(tifffile.TiffFile(str(out)).ome_metadata)

    assert ome.instruments[0].objectives[0].model == "PlanApo 4x 0.20/20.00mm :Default"


def test_a_channel_whose_value_is_missing_is_left_empty(tmp_path):
    """割れて None になった値は、**既定値で埋めずに**書かないこと。"""
    out = _write(
        tmp_path / "a",
        AcquisitionConditions(
            channel_names=["CH1", "CH3"],
            exposure_by_channel={"CH1": 1 / 20, "CH3": None},
            gain_by_channel={"CH1": {"CameraGain": 120}, "CH3": {"CameraGain": None}},
        ),
    )

    ome = from_xml(tifffile.TiffFile(str(out)).ome_metadata)
    planes = {pl.the_c: pl.exposure_time for pl in ome.images[0].pixels.planes}
    channels = ome.images[0].pixels.channels

    assert planes[0] == 1 / 20
    assert planes[1] is None
    assert channels[0].detector_settings.gain == 120.0
    assert channels[1].detector_settings is None


def test_without_conditions_the_file_is_untouched(tmp_path):
    """``acquisition`` を渡さない既存の呼び出しは、いままでどおり。"""
    out = _write(tmp_path / "a")

    ome = from_xml(tifffile.TiffFile(str(out)).ome_metadata)

    assert ome.structured_annotations is None or not ome.structured_annotations.map_annotations
    assert not ome.instruments


def test_empty_conditions_write_nothing(tmp_path):
    before = tifffile.tiffcomment(str(_write(tmp_path / "a")))
    out = _write(tmp_path / "b")

    write_acquisition_metadata(out, AcquisitionConditions())

    assert tifffile.tiffcomment(str(out)) == before.replace("a.ome.tif", "b.ome.tif")

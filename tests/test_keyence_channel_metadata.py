"""チャネルごとの撮像条件 (露光時間・検出器ゲイン) が畳み潰されないこと。

Keyence はチャネルごとに露光とゲインを変えて撮る。抽出器が 1 ファイル目の値を
代表にすると、**残りのチャネルの条件が黙って消える**。消えた値は「記録されて
いない」ようには見えず、記録された値と見分けがつかないので、あとから条件を
照合できなくなる。

``ImageMetadata`` は TIFF を**バイナリとして読んで ``<Data>..</Data>`` を
正規表現で拾う**だけなので、ここでは本物の TIFF を作らずに XML を埋めた
ファイルで足りる (test_Stich.py も同じ作り)。
"""
import pytest

from ylabcommon.bioio.keyence.keyence_metadata_extractor import (
    KeyenceMetadataExtractor,
    channel_of,
)

#: 露光は Numerator/Denominator、ゲインは整数。どちらも XML の値をそのまま使う。
_XML = """<Data>
    <XyStageRegion>
        <X>{x}</X>
        <Y>63107000</Y>
        <Width>3623424</Width>
    </XyStageRegion>
    <SavingImageSize>
        <Width>480</Width>
        <Height>360</Height>
    </SavingImageSize>
    <StageLocationZ>{z}</StageLocationZ>
    <LensName>PlanApo 4x 0.20/20.00mm :Default</LensName>
    <ExposureTime>
        <Numerator>1</Numerator>
        <Denominator>{denominator}</Denominator>
    </ExposureTime>
    <CameraGain>{gain}</CameraGain>
    <CameraHardwareGain>{hardware_gain}</CameraHardwareGain>
</Data>"""


def _write_tile(tmp_path, name, *, denominator, gain, hardware_gain, x=65594000, z=2721720):
    path = tmp_path / name
    path.write_bytes(
        b"II*\x00"
        + _XML.format(
            x=x, z=z, denominator=denominator, gain=gain, hardware_gain=hardware_gain
        ).encode("utf-8")
    )
    return str(path)


@pytest.fixture
def two_channel_tiles(tmp_path):
    """CH1 と CH3 を、**それぞれ違う露光とゲイン**で 2 タイルずつ。"""
    files = []
    for xy in (1, 2):
        files.append(
            _write_tile(
                tmp_path,
                "Image_XY%02d_Z001_CH1.tif" % xy,
                denominator=20,  # 0.05 s
                gain=120,
                hardware_gain=336,
            )
        )
        files.append(
            _write_tile(
                tmp_path,
                "Image_XY%02d_Z001_CH3.tif" % xy,
                denominator=2,  # 0.5 s
                gain=200,
                hardware_gain=400,
            )
        )
    return sorted(files)


def test_channel_of_normalizes_zero_padding():
    """``get_channel_names`` が作る ``CH1`` 形に揃える (``CH01`` も同じ)。"""
    assert channel_of("Image_XY01_Z001_CH1.tif") == "CH1"
    assert channel_of("Image_XY01_Z001_CH01.tif") == "CH1"
    assert channel_of("/some/dir/Image_XY02_Z010_CH3.tif") == "CH3"
    assert channel_of("no_channel_here.tif") is None


def test_exposure_and_gain_are_kept_per_channel(two_channel_tiles):
    """チャネルごとの露光とゲインが両方とも残ること。

    ここが回帰したら、片方のチャネルの撮像条件が出力から消えている。
    """
    meta = KeyenceMetadataExtractor(two_channel_tiles, (1, 2, 1, 360, 480)).extract()

    # ゲインは `get_dict()` が `int(...)` でキャストする (keyence_metainfo.py:151-152)。
    assert meta.exposure_by_channel == {"CH1": 1 / 20, "CH3": 1 / 2}
    assert meta.gain_by_channel == {
        "CH1": {"CameraGain": 120, "CameraHardwareGain": 336},
        "CH3": {"CameraGain": 200, "CameraHardwareGain": 400},
    }
    # 全タイルで揃っているので、割れの報告は無い。
    assert meta.inconsistent == {}


def test_disagreeing_tiles_yield_no_value_rather_than_the_first(tmp_path):
    """同じチャネルのタイルで値が割れたら、**片方を選ばず** None にすること。

    どちらが正しいかはファイルからは分からない。選べば、測った値と区別の
    つかない嘘になる。無い値のほうがましなので None を返し、観測された値は
    ``inconsistent`` に残して呼び出し側が拒否できるようにする。
    """
    files = [
        _write_tile(tmp_path, "Image_XY01_Z001_CH1.tif", denominator=20, gain=120, hardware_gain=336),
        _write_tile(tmp_path, "Image_XY02_Z001_CH1.tif", denominator=20, gain=999, hardware_gain=336),
    ]

    meta = KeyenceMetadataExtractor(sorted(files), (1, 1, 1, 360, 480)).extract()

    assert meta.gain_by_channel["CH1"]["CameraGain"] is None
    assert meta.inconsistent == {"CH1/CameraGain": {120, 999}}
    # 割れていない項目は巻き添えにしない。
    assert meta.exposure_by_channel["CH1"] == 1 / 20
    assert meta.gain_by_channel["CH1"]["CameraHardwareGain"] == 336


def test_legacy_scalar_exposure_is_unchanged(two_channel_tiles):
    """``exposure`` は 1 ファイル目の値のまま (後方互換)。

    slice-analysis / slice-controller が読んでいる可能性があるので、既存の
    属性の意味は変えない。正しい値は ``exposure_by_channel`` 側にある。
    """
    meta = KeyenceMetadataExtractor(two_channel_tiles, (1, 2, 1, 360, 480)).extract()

    assert meta.exposure == 1 / 20
    assert meta.lens == "PlanApo 4x 0.20/20.00mm :Default"

"""モザイクの並べ方。**移植元と画素が合うこと**が一番の約束。

``keyenceutils.stich.StichedImage`` (archived) からの移植なので、配置・回転・
重なりの勝ち負けが 1 画素でもずれたら、これまでに作った出力と比べられなくなる。
ここでは小さな合成タイルで規則を 1 つずつ留める。

``ImageMetadata`` は TIFF を**バイナリとして読んで ``<Data>..</Data>`` を正規表現で
拾う**が、画素は ``tifffile`` が読むので、タイルは**本物の TIFF に XML を足した
もの**にしてある (XML だけのファイルでは画素が読めない)。
"""
import numpy as np
import pytest
import tifffile

from ylabcommon.bioio.keyence.keyence_mosaic import (
    Mosaic,
    list_tiles,
    stitch_mosaic,
)

#: 1 画素 = 1000 nm になるように選んである (Width 4000 nm / 4 px)。
#: そのおかげで「X=2000 nm のタイルは x=2 に載る」と読める。
_XML = """<Data>
    <XyStageRegion><X>{x}</X><Y>{y}</Y><Width>4000</Width></XyStageRegion>
    <SavingImageSize><Width>4</Width><Height>3</Height></SavingImageSize>
    <StageLocationZ>{z}</StageLocationZ>
    <LensName>{lens}</LensName>
    <ExposureTime><Numerator>1</Numerator><Denominator>{denominator}</Denominator></ExposureTime>
    <CameraGain>{gain}</CameraGain>
    <CameraHardwareGain>336</CameraHardwareGain>
    <Sectioning><Enabled>False</Enabled></Sectioning>
</Data>"""


def _tile(folder, name, pixels, *, x=0, y=0, z=0, denominator=20, gain=120,
          lens="PlanApo 4x", dtype=np.uint16):
    """本物の TIFF を書いてから、Keyence の XML を末尾に足す。"""
    path = folder / name
    tifffile.imwrite(str(path), np.asarray(pixels, dtype))
    with open(path, "ab") as f:
        f.write(_XML.format(x=x, y=y, z=z, denominator=denominator, gain=gain,
                            lens=lens).encode("utf-8"))
    return path


def _ramp(value):
    """3x4 の、値が一目で分かるタイル。"""
    return np.full((3, 4), value, np.uint16)


def test_one_tile_lands_whole(tmp_path):
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", _ramp(7))

    mosaic = stitch_mosaic(tmp_path)

    assert isinstance(mosaic, Mosaic)
    assert mosaic.data.shape == (1, 1, 1, 3, 4)  # TCZYX
    assert np.all(mosaic.data[0, 0, 0] == 7)


def test_a_tile_is_rotated_180_degrees(tmp_path):
    """移植元がそうしている (ステージの座標系が画像と反転)。**外すと全部裏返る。**"""
    pixels = np.array([[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]], np.uint16)
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", pixels)

    mosaic = stitch_mosaic(tmp_path)

    assert np.array_equal(mosaic.data[0, 0, 0], np.flipud(np.fliplr(pixels)))


def test_tiles_are_placed_by_their_stage_position(tmp_path):
    """X=4000 nm = 4 px 離れた 2 枚が、重ならずに横に並ぶ。"""
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", _ramp(1), x=0)
    _tile(tmp_path, "Image_XY02_Z001_CH1.tif", _ramp(2), x=4000)

    mosaic = stitch_mosaic(tmp_path)

    assert mosaic.data.shape == (1, 1, 1, 3, 8)
    assert np.all(mosaic.data[0, 0, 0, :, :4] == 1)
    assert np.all(mosaic.data[0, 0, 0, :, 4:] == 2)


def test_the_overlap_is_won_by_whichever_tile_is_written_last(tmp_path):
    """混ぜない・合わせ直さない。**継ぎ目の補正は別の課題。**

    ここが変わったら、その時点から出力の継ぎ目が変わる。いつ変えたかを
    追えるように、規則としてここに留めておく。
    """
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", _ramp(1), x=0)
    _tile(tmp_path, "Image_XY02_Z001_CH1.tif", _ramp(2), x=2000)  # 2 px ずらし

    mosaic = stitch_mosaic(tmp_path)

    assert mosaic.data.shape == (1, 1, 1, 3, 6)
    assert np.all(mosaic.data[0, 0, 0, :, :2] == 1)   # 1 枚目だけ
    assert np.all(mosaic.data[0, 0, 0, :, 2:4] == 2)  # 重なり: 後が勝つ
    assert np.all(mosaic.data[0, 0, 0, :, 4:] == 2)   # 2 枚目だけ


def test_channels_become_the_c_axis_in_number_order(tmp_path):
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", _ramp(1))
    _tile(tmp_path, "Image_XY01_Z001_CH3.tif", _ramp(3))

    mosaic = stitch_mosaic(tmp_path)

    assert mosaic.channel_names == ["CH1", "CH3"]
    assert mosaic.data.shape == (1, 2, 1, 3, 4)
    assert np.all(mosaic.data[0, 0] == 1)
    assert np.all(mosaic.data[0, 1] == 3)


def test_ten_channels_sort_by_number_not_by_string(tmp_path):
    """移植元は文字列順だったので ``CH10`` が ``CH2`` の前に来ていた。"""
    for n in (1, 2, 10):
        _tile(tmp_path, "Image_XY01_Z001_CH%d.tif" % n, _ramp(n))

    mosaic = stitch_mosaic(tmp_path)

    assert mosaic.channel_names == ["CH1", "CH2", "CH10"]


def test_the_z_index_comes_from_the_file_name_and_is_one_based(tmp_path):
    """``_Z00001_`` が添字 0。"""
    _tile(tmp_path, "Image_XY01_Z00001_CH1.tif", _ramp(1), z=0)
    _tile(tmp_path, "Image_XY01_Z00002_CH1.tif", _ramp(2), z=500)

    mosaic = stitch_mosaic(tmp_path)

    assert mosaic.data.shape == (1, 1, 2, 3, 4)
    assert np.all(mosaic.data[0, 0, 0] == 1)
    assert np.all(mosaic.data[0, 0, 1] == 2)
    assert mosaic.z_interval_um == 0.5  # 500 nm


def test_a_single_plane_has_no_z_interval(tmp_path):
    """1 枚しか無ければ間隔は **無い**。0 と書くと測った 0 と区別がつかない。"""
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", _ramp(1))

    assert stitch_mosaic(tmp_path).z_interval_um is None


def test_exposure_and_gain_come_out_per_channel(tmp_path):
    """移植元はゲインを 1 タイル目の値に畳んでいた。"""
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", _ramp(1), denominator=20, gain=120)
    _tile(tmp_path, "Image_XY01_Z001_CH3.tif", _ramp(3), denominator=2, gain=200)

    conditions = stitch_mosaic(tmp_path).conditions

    assert conditions.exposure_by_channel == {"CH1": 1 / 20, "CH3": 1 / 2}
    assert conditions.gain_by_channel["CH1"]["CameraGain"] == 120
    assert conditions.gain_by_channel["CH3"]["CameraGain"] == 200
    assert conditions.lens == "PlanApo 4x"


def test_a_value_that_differs_between_tiles_of_one_channel_is_left_out(tmp_path):
    """どちらが正しいか分からないものは埋めない。"""
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", _ramp(1), gain=120)
    _tile(tmp_path, "Image_XY02_Z001_CH1.tif", _ramp(2), x=4000, gain=999)

    conditions = stitch_mosaic(tmp_path).conditions

    assert conditions.gain_by_channel["CH1"]["CameraGain"] is None
    assert conditions.exposure_by_channel["CH1"] == 1 / 20  # 揃っているものは残る


def test_each_tile_is_read_exactly_once(tmp_path, monkeypatch):
    """移植元は同じ DataFrame を 2 回 append していて、全タイルを 2 回読んでいた。

    絵は変わらないが、ネットワーク越しでは読み込みが実行時間のほとんどを占める。
    """
    for xy in (1, 2, 3):
        _tile(tmp_path, "Image_XY%02d_Z001_CH1.tif" % xy, _ramp(xy), x=4000 * (xy - 1))

    reads = []
    original = tifffile.imread

    def counting(path, *args, **kwargs):
        reads.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(tifffile, "imread", counting)
    stitch_mosaic(tmp_path)

    assert len(reads) == 3, reads
    assert len(set(reads)) == 3


def test_overlay_files_are_not_tiles(tmp_path):
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", _ramp(1))
    _tile(tmp_path, "Image_XY01_Z001_Overlay.tif", _ramp(9))

    assert list_tiles(tmp_path) == ["Image_XY01_Z001_CH1.tif"]


def test_an_empty_folder_says_so(tmp_path):
    with pytest.raises(ValueError, match="No Image_"):
        stitch_mosaic(tmp_path)


def test_tiles_taken_at_different_settings_are_refused(tmp_path):
    """1 枚のモザイクは 1 つの設定で撮ったものから作る。選んで進まない。"""
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", _ramp(1), lens="PlanApo 4x")
    _tile(tmp_path, "Image_XY02_Z001_CH1.tif", _ramp(2), x=4000, lens="PlanApo 20x")

    with pytest.raises(ValueError, match="LensName differs"):
        stitch_mosaic(tmp_path)


def test_a_tile_that_is_not_16_bit_is_refused(tmp_path):
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", np.full((3, 4), 5), dtype=np.uint8)

    with pytest.raises(ValueError, match="not uint16"):
        stitch_mosaic(tmp_path)


def test_physical_pixel_sizes_are_ready_for_the_writer(tmp_path):
    _tile(tmp_path, "Image_XY01_Z00001_CH1.tif", _ramp(1), z=0)
    _tile(tmp_path, "Image_XY01_Z00002_CH1.tif", _ramp(2), z=500)

    mosaic = stitch_mosaic(tmp_path)

    assert mosaic.physical_pixel_sizes == (0.5, 1.0, 1.0)  # (Z, Y, X) µm


def test_an_unmeasured_z_size_stays_unknown(tmp_path):
    """Z が測れていないとき、**XY の画素サイズで埋めない**。

    ``KeyenceMetadataExtractor`` は Z 位置が 1 つのとき XY の画素サイズを Z に
    入れている (``z_step = first["umPerPixel"]``)。それは測った間隔と区別が
    つかない。``BioIOWriter`` は ``Z=None`` を受けて OME の ``PhysicalSizeZ`` を
    空のまま書く (次の ``test_an_unknown_z_size_reaches_the_file_as_an_empty_field``
    が確かめる) ので、埋める必要は無い。
    """
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", _ramp(1))

    assert stitch_mosaic(tmp_path).physical_pixel_sizes == (None, 1.0, 1.0)


def test_an_unknown_z_size_reaches_the_file_as_an_empty_field(tmp_path):
    """上の ``None`` が、書き出しで 0 や 1.0 に化けないこと。"""
    from ome_types import from_xml

    from ylabcommon.bioio.core.bioio_writer import BioIOWriter

    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", _ramp(1))
    mosaic = stitch_mosaic(tmp_path)

    out = tmp_path / "merged"
    BioIOWriter(str(out)).write(
        mosaic.data,
        dim_order="TCZYX",
        channel_names=mosaic.channel_names,
        physical_pixel_sizes=mosaic.physical_pixel_sizes,
    )

    pixels = from_xml(tifffile.TiffFile(
        str(tmp_path / "merged.ome.tif")).ome_metadata).images[0].pixels
    assert pixels.physical_size_z is None
    assert pixels.physical_size_x == 1.0


# --------------------------------------------------------------------------
# 継ぎ目: 重なりから測れる分だけ当てる
# --------------------------------------------------------------------------

_RNG = np.random.default_rng(20260920)


def _field(height=40, width=200):
    """位相相関が効く程度のテクスチャ。"""
    coarse = _RNG.normal(3000, 500, (height // 4 + 2, width // 4 + 2))
    grown = np.repeat(np.repeat(coarse, 4, axis=0), 4, axis=1)[:height, :width]
    return np.clip(grown + _RNG.normal(0, 60, (height, width)), 0, 65535).astype(np.uint16)


def _textured_tile(folder, name, pixels, *, claimed_x):
    """``claimed_x`` [px] を名乗るタイル。中身は呼び出し側が切り出して渡す。

    ``stitch_mosaic`` は 180 度回して貼るので、**回した結果が意図した中身に
    なるように**ここで先に回しておく。
    """
    height, width = pixels.shape
    xml = ("<Data><XyStageRegion><X>{x}</X><Y>0</Y><Width>{w}</Width></XyStageRegion>"
           "<SavingImageSize><Width>{pw}</Width><Height>{ph}</Height></SavingImageSize>"
           "<StageLocationZ>0</StageLocationZ><LensName>PlanApo 4x</LensName>"
           "<ExposureTime><Numerator>1</Numerator><Denominator>20</Denominator></ExposureTime>"
           "<CameraGain>120</CameraGain><CameraHardwareGain>336</CameraHardwareGain>"
           "<Sectioning><Enabled>False</Enabled></Sectioning></Data>").format(
               x=claimed_x * 1000, w=width * 1000, pw=width, ph=height)
    path = folder / name
    tifffile.imwrite(str(path), np.flipud(np.fliplr(pixels)))
    with open(path, "ab") as f:
        f.write(xml.encode("utf-8"))
    return path


def _two_tiles(folder, *, true_step, claimed_step, tile_width=80):
    """同じ視野から 2 枚切り出し、ステージ座標は ``claimed_step`` を名乗らせる。

    ``true_step != claimed_step`` なら、その差が registration が見つけるべき
    ずれである。
    """
    field = _field(40, 200)
    _textured_tile(folder, "Image_XY01_Z001_CH1.tif",
                   field[:, :tile_width], claimed_x=0)
    _textured_tile(folder, "Image_XY02_Z001_CH1.tif",
                   field[:, true_step:true_step + tile_width], claimed_x=claimed_step)
    return field


def test_a_stage_coordinate_that_is_off_is_corrected(tmp_path):
    """ステージは 40 px と言っているが、中身は 45 px ずれている。"""
    _two_tiles(tmp_path, true_step=45, claimed_step=40)

    mosaic = stitch_mosaic(tmp_path, register=True)
    registration = mosaic.registration

    assert registration.accepted == 1
    assert registration.offsets["x40_y0"] == (0, 5)
    assert mosaic.data.shape[-1] == 125   # 45 + 80、名乗った 120 ではない


def test_registration_can_be_turned_off_to_get_the_stage_placement(tmp_path):
    """移植元と同じ置き方。既存の出力と突き合わせるときに要る。"""
    _two_tiles(tmp_path, true_step=45, claimed_step=40)

    mosaic = stitch_mosaic(tmp_path, register=False)

    assert mosaic.registration is None
    assert mosaic.data.shape[-1] == 120   # 名乗ったとおり 40 + 80


def test_a_correct_stage_coordinate_is_left_alone(tmp_path):
    """合っているものを動かさない。"""
    _two_tiles(tmp_path, true_step=40, claimed_step=40)

    registration = stitch_mosaic(tmp_path, register=True).registration

    assert registration.offsets["x40_y0"] == (0, 0)
    assert registration.moved == 0


def test_an_overlap_with_no_texture_does_not_move_anything(tmp_path):
    """動かす根拠が無いときは動かさない。**でたらめに合わせない。**"""
    for index, x in enumerate((0, 40), start=1):
        _textured_tile(tmp_path, "Image_XY%02d_Z001_CH1.tif" % index,
                       np.full((40, 80), 1200, np.uint16), claimed_x=x)

    registration = stitch_mosaic(tmp_path, register=True).registration

    assert registration.moved == 0
    assert registration.rejected == 1
    assert not registration.pairs[0].accepted


def test_what_was_done_to_the_seam_is_recorded(tmp_path):
    """出力を見ただけで、どう並べたものか分かること。

    置き直しは画素の位置を変えるので、記録が無いと別々の日に作った出力を
    突き合わせられない。
    """
    _two_tiles(tmp_path, true_step=45, claimed_step=40)

    extra = stitch_mosaic(tmp_path, register=True).conditions.extra

    assert extra["Seam/Registration"] == "phase-correlation"
    assert extra["Seam/PairsAccepted"] == "1/1"
    assert extra["Seam/Offset/x40_y0"] == "+0,+5"
    assert extra["Seam/Overlap"] == "last"
    # 明るさは**測るだけで直さない**。次の段 (キャリブレーション) の材料。
    assert "Seam/IntensityRatio/x0_y0|x40_y0" in extra
    assert "Seam/EdgeRatio/x0_y0" in extra


def test_registration_costs_one_extra_read_per_overlapping_tile(tmp_path):
    """**読み込みが増えるのはここだけ**、という約束。

    推定は Z 1 面ぶん・全チャネルしか読まない。Z を積んだ取得では誤差の範囲だが
    (C=2 Z=40 なら 80 枚中 2 枚)、Z が 1 枚の取得では 2 倍になる。それが嫌な
    呼び出し側は ``register=False`` にできる。
    """
    _two_tiles(tmp_path, true_step=45, claimed_step=40)

    reads = []
    original = tifffile.imread

    def counting(path, *args, **kwargs):
        reads.append(str(path))
        return original(path, *args, **kwargs)

    tifffile.imread = counting
    try:
        stitch_mosaic(tmp_path, register=False)
        without = len(reads)
        reads.clear()
        stitch_mosaic(tmp_path, register=True)
        with_registration = len(reads)
    finally:
        tifffile.imread = original

    assert without == 2                    # 貼るぶんだけ
    assert with_registration == 4          # + 推定のぶん (Z 1 面 x 1 チャネル)


def test_the_mean_policy_averages_the_overlap(tmp_path):
    """継ぎ目は消えないが (実測)、選べるようにはしてある。値が変わるので記録する。"""
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", np.full((3, 4), 100, np.uint16), x=0)
    _tile(tmp_path, "Image_XY02_Z001_CH1.tif", np.full((3, 4), 200, np.uint16), x=2000)

    mosaic = stitch_mosaic(tmp_path, register=False, overlap="mean")

    assert np.all(mosaic.data[0, 0, 0, :, :2] == 100)
    assert np.all(mosaic.data[0, 0, 0, :, 2:4] == 150)   # 重なり = 平均
    assert np.all(mosaic.data[0, 0, 0, :, 4:] == 200)
    assert mosaic.conditions.extra["Seam/Overlap"] == "mean"


def test_an_unknown_overlap_policy_is_refused(tmp_path):
    _tile(tmp_path, "Image_XY01_Z001_CH1.tif", _ramp(1))

    with pytest.raises(ValueError, match="unknown overlap policy"):
        stitch_mosaic(tmp_path, overlap="feather")

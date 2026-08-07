"""ThorLabs 取り込みの組み立てアルゴリズムのテスト。

入力側は Experiment.xml + ファイル名 + ヘッダ1枚で決まるので bioio を経由しない、
という前提が壊れていないこと (= 画素を読まずにスタックが組めること) と、
以前あった「黙ってデータを捨てる」経路が塞がっていることを固定する。
"""
from __future__ import annotations

import dask.array as da
import numpy as np
import pytest
import tifffile

from ylabcommon.bioio.thorlab.builder import ThorlabBioioBuilder
from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import (
    stack_thorlab_with_bioio_calibrated,
)
from ylabcommon.bioio.thorlab.xml_parser import ExperimentXMLParser


def write_experiment_xml(d, *, pixel_x=64, pixel_y=64, steps=40, z_enable="1",
                         timepoints=1, channels=("ChanA",)):
    wl = "".join(f'<Wavelength name="{c}"/>' for c in channels)
    (d / "Experiment.xml").write_text(
        "<ThorImageExperiment>"
        '<Date date="11/20/2025 14:03:22"/>'
        f'<LSM pixelX="{pixel_x}" pixelY="{pixel_y}" pixelWidthUM="0.5" '
        'pixelHeightUM="0.5" frameRate="30" dwellTime="2"/>'
        f'<ZStage steps="{steps}" stepSizeUM="1.5" enable="{z_enable}"/>'
        f'<Timelapse timepoints="{timepoints}" intervalSec="2.5"/>'
        f"<Wavelengths>{wl}</Wavelengths>"
        '<Magnification name="20x"/>'
        "</ThorImageExperiment>"
    )
    return d / "Experiment.xml"


def plane(seed, y=64, x=64):
    return np.random.default_rng(seed).integers(0, 4096, (y, x), dtype=np.uint16)


#: Builder 経由のテスト用。ThorlabBioioBuilder は既定で 100 KB 未満のファイルを
#: メタデータ扱いで落とすので、その閾値を超える大きさにしておく (256x256 uint16 = 128 KB)。
BIG = 256


def big_plane(seed):
    return plane(seed, y=BIG, x=BIG)


# ---- XML が唯一の読み取り口になっていること --------------------------------

def test_params_read_the_frame_size_from_pixelx_not_a_missing_width(tmp_path):
    """SizeX/SizeY は LSM の pixelX/pixelY から来る。

    以前は存在しない ``width`` / ``height`` 属性を見ていたため、XML が何を宣言して
    いようと常に既定値の 512 が返っていた。
    """
    xml = write_experiment_xml(tmp_path, pixel_x=64, pixel_y=48)

    params = ExperimentXMLParser(xml).as_params()

    assert (params["SizeX"], params["SizeY"]) == (64, 48)


def test_params_keep_defaults_when_the_xml_has_no_such_node(tmp_path):
    """ノードが無くても既定値で埋まる (None を返して下流を壊さない)。"""
    (tmp_path / "Experiment.xml").write_text("<ThorImageExperiment/>")

    params = ExperimentXMLParser(tmp_path / "Experiment.xml").as_params()

    assert params["SizeZ"] == 1 and params["SizeT"] == 1
    assert params["PixelSizeX"] == 1.0 and params["PixelSizeZ"] == 1.0
    assert params["mode"] == "T"


def test_the_experiment_xml_is_parsed_exactly_once_per_build(tmp_path, monkeypatch):
    """1回の build で Experiment.xml を開くのは1回だけ。

    以前は params アダプタ / チャンネル名の取得 / 検証用パーサが別々に開いていた。
    """
    import ylabcommon.bioio.thorlab.xml_parser as xml_mod

    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 4):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif", big_plane(i))
    write_experiment_xml(d, pixel_x=BIG, pixel_y=BIG, steps=3)

    real_parse = xml_mod.etree.parse
    calls = {"n": 0}

    def counting_parse(*a, **kw):
        calls["n"] += 1
        return real_parse(*a, **kw)

    monkeypatch.setattr(xml_mod.etree, "parse", counting_parse)

    ThorlabBioioBuilder(d).build()

    assert calls["n"] == 1


# ---- 多ページのファイルでデータを落とさないこと ------------------------------

def test_a_multipage_file_no_longer_discards_its_sibling_files(tmp_path):
    """多ページのファイルが混ざっても、他のファイルは捨てられない。

    以前は「面数が最大の1ファイル」だけを残して同じチャンネルの残りを黙って捨てて
    いた。40面あるのに Z=10 のスタックが出来上がり、例外も警告も出なかった。
    """
    d = tmp_path / "img01"
    d.mkdir()
    tifffile.imwrite(d / "ChanA_001_001_001_001.tif",
                     np.stack([plane(i) for i in range(10)]))
    for i in range(2, 32):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif", plane(100 + i))
    xml = write_experiment_xml(d, steps=40)
    params = ExperimentXMLParser(xml).as_params()

    files = sorted(str(p) for p in d.glob("*.tif"))
    stacked, kept = stack_thorlab_with_bioio_calibrated(files, xml, params, min_kb=0)

    assert stacked.shape == (1, 1, 40, 64, 64)     # 10 (多ページ) + 30 (単一)
    assert len(kept) == 31


def test_multipage_planes_keep_their_order(tmp_path):
    """多ページの中の面が、ファイルをまたいで正しい順に並ぶ。"""
    d = tmp_path / "img01"
    d.mkdir()
    first = np.stack([plane(0), plane(1)])
    second = plane(2)
    tifffile.imwrite(d / "ChanA_001_001_001_001.tif", first)
    tifffile.imwrite(d / "ChanA_001_001_001_002.tif", np.stack([second, plane(3)]))
    xml = write_experiment_xml(d, steps=4)
    params = ExperimentXMLParser(xml).as_params()

    files = sorted(str(p) for p in d.glob("*.tif"))
    stacked, _ = stack_thorlab_with_bioio_calibrated(files, xml, params, min_kb=0)

    got = np.asarray(stacked[0, 0])
    assert got.shape == (4, 64, 64)
    assert np.array_equal(got[0], first[0])
    assert np.array_equal(got[1], first[1])
    assert np.array_equal(got[2], second)


# ---- 遅延であること ----------------------------------------------------------

def test_building_the_stack_reads_no_pixels(tmp_path, monkeypatch):
    """スタックの組み立て中に画素は1バイトも読まれない。

    ``tifffile.imread`` (画素を読む唯一の入口) を爆発させても組み立てが通ることで
    確かめる。実際に読むのは書き出し / 解析側が compute したときの1回だけ。
    """
    import ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder as mod

    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 6):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif", plane(i))
    xml = write_experiment_xml(d, steps=5)
    params = ExperimentXMLParser(xml).as_params()

    def explode(*a, **kw):
        raise AssertionError("pixels must not be read while building the stack")

    monkeypatch.setattr(mod.tifffile, "imread", explode)

    files = sorted(str(p) for p in d.glob("*.tif"))
    stacked, _ = stack_thorlab_with_bioio_calibrated(files, xml, params, min_kb=0)

    assert isinstance(stacked, da.Array)
    assert stacked.shape == (1, 1, 5, 64, 64)


def test_the_builder_leaves_the_stack_lazy(tmp_path):
    """build() を通っても stacked_data は dask のまま (sorter がそれを前提にしている)。"""
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 4):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif", big_plane(i))
    write_experiment_xml(d, pixel_x=BIG, pixel_y=BIG, steps=3)

    b = ThorlabBioioBuilder(d)
    b.build()

    assert isinstance(b.stacked_data, da.Array)
    assert b.image_meta.shape == (1, 1, 3, BIG, BIG)
    assert b.image_meta.pixel_size == (1.5, 0.5, 0.5)


# ---- チャンネルの扱い --------------------------------------------------------

def test_channels_are_ordered_naturally_not_lexically(tmp_path):
    """CH10 が CH2 より前に来ない (辞書順だと CH1 < CH10 < CH2 になる)。"""
    d = tmp_path / "img01"
    d.mkdir()
    for seed, ch in enumerate(("CH1", "CH2", "CH10")):
        tifffile.imwrite(d / f"{ch}_001_001_001_001.tif", plane(seed))
    xml = write_experiment_xml(d, steps=1, z_enable="0",
                               channels=("CH1", "CH2", "CH10"))
    params = ExperimentXMLParser(xml).as_params()

    files = sorted(str(p) for p in d.glob("*.tif"))
    stacked, _ = stack_thorlab_with_bioio_calibrated(files, xml, params, min_kb=0)

    assert stacked.shape[1] == 3
    got = np.asarray(stacked[0, :, 0])
    expected = [np.asarray(tifffile.imread(str(d / f"{ch}_001_001_001_001.tif")))
                for ch in ("CH1", "CH2", "CH10")]
    for i, exp in enumerate(expected):
        assert np.array_equal(got[i], exp), f"channel {i} is out of order"


def test_ragged_channels_are_truncated_with_a_warning(tmp_path):
    """取得が途中で終わってチャンネルの面数が揃わなくても、バッチを落とさない。"""
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 4):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif", plane(i))
    for i in range(1, 3):                       # ChanB は1枚少ない
        tifffile.imwrite(d / f"ChanB_001_001_001_{i:03d}.tif", plane(10 + i))
    xml = write_experiment_xml(d, steps=3, channels=("ChanA", "ChanB"))
    params = ExperimentXMLParser(xml).as_params()

    files = sorted(str(p) for p in d.glob("*.tif"))
    with pytest.warns(UserWarning, match="揃っていません"):
        stacked, _ = stack_thorlab_with_bioio_calibrated(files, xml, params, min_kb=0)

    assert stacked.shape == (1, 2, 2, 64, 64)   # 共通する2面まで


def test_channel_names_come_from_the_xml(tmp_path):
    """OME に書くチャンネル名が Experiment.xml の Wavelength 名から来る。"""
    d = tmp_path / "img01"
    d.mkdir()
    for ch in ("ChanA", "ChanB"):
        tifffile.imwrite(d / f"{ch}_001_001_001_001.tif", big_plane(1))
    write_experiment_xml(d, pixel_x=BIG, pixel_y=BIG, steps=1, z_enable="0",
                         channels=("GFP: green", "RFP: red"))

    b = ThorlabBioioBuilder(d)
    b.build()

    assert b.image_meta.channel_names_index == ["GFP", "RFP"]


# ---- XML には無い情報だけを実データから採ること ------------------------------

def test_the_frame_size_from_the_files_wins_over_a_stale_xml(tmp_path):
    """XML の宣言と実データの縦横が食い違ったら、実データを採って警告する。"""
    d = tmp_path / "img01"
    d.mkdir()
    tifffile.imwrite(d / "ChanA_001_001_001_001.tif", plane(1, y=32, x=32))
    xml = write_experiment_xml(d, pixel_x=64, pixel_y=64, steps=1, z_enable="0")
    params = ExperimentXMLParser(xml).as_params()

    files = sorted(str(p) for p in d.glob("*.tif"))
    with pytest.warns(UserWarning, match="TIFF header"):
        stacked, _ = stack_thorlab_with_bioio_calibrated(files, xml, params, min_kb=0)

    assert stacked.shape == (1, 1, 1, 32, 32)


def test_a_frame_size_mismatch_between_files_fails_loudly(tmp_path):
    """途中のファイルだけ大きさが違ったら、どのファイルかを言って落ちる。"""
    from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import _read_file_planes

    d = tmp_path / "img01"
    d.mkdir()
    odd = d / "ChanA_001_001_001_002.tif"
    tifffile.imwrite(odd, plane(2, y=32, x=32))

    with pytest.raises(ValueError, match="Frame size mismatch"):
        _read_file_planes(str(odd), 1, 64, 64, np.uint16)


def test_metadata_now_carries_the_acquisition_time_and_objective(tmp_path):
    """XML にしか無い情報 (撮影日時・タイムラプス間隔・対物レンズ) が埋まる。

    BioImage 経由だったころは、渡していたのが生の配列だったのでいずれも常に None
    だった。
    """
    d = tmp_path / "img01"
    d.mkdir()
    tifffile.imwrite(d / "ChanA_001_001_001_001.tif", big_plane(1))
    write_experiment_xml(d, pixel_x=BIG, pixel_y=BIG, steps=1, z_enable="0")

    b = ThorlabBioioBuilder(d)
    b.build()

    assert b.image_meta.imaging_datetime.year == 2025
    assert b.image_meta.timelapse_interval.total_seconds() == 2.5
    assert b.image_meta.objective == "20x"


def test_an_io_error_survives_dask_with_its_errno_and_filename(tmp_path, monkeypatch):
    """compute 時の EIO が、errno とファイル名を保ったまま dask を抜けてくる。

    sorter 側はこの errno で「ネットワークドライブ切断」を判定し、案内文を添える
    (slice_analysis.data_curation.sorter._is_io_error)。遅延にしたことで読み取りが
    dask の中へ移ったので、そこを通っても errno が消えないことを固定する。
    """
    import errno

    import ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder as mod

    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 4):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif", plane(i))
    xml = write_experiment_xml(d, steps=3)
    params = ExperimentXMLParser(xml).as_params()

    files = sorted(str(p) for p in d.glob("*.tif"))
    stacked, _ = stack_thorlab_with_bioio_calibrated(files, xml, params, min_kb=0)

    # グラフを組んだ後で読み取りだけを落とす (= 取り込み中にドライブが切れた状況)
    def drive_gone(*a, **kw):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(mod.tifffile, "imread", drive_gone)

    with pytest.raises(OSError) as excinfo:
        np.asarray(stacked)

    assert excinfo.value.errno == errno.EIO       # sorter の EIO 判定が効く
    assert "ChanA_001_001_001_" in str(excinfo.value)   # どのファイルかが残る

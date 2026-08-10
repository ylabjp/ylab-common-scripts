"""タイル (mosaic) 取得を黙って積まないことのテスト。

複数の XY ステージ位置で撮った取得をそのまま積むと、タイルが Z/T 軸へ潰れて
「1枚の深いスタック」になる。画素は全部入っているので出力は正常に見え、
解析まで進んでから初めておかしいと気付く。だから取り込みは明示的に失敗させる。

問題は、その安全確認が **確認できなかったときに素通りしていた** こと。

    try:
        _, _dims = extract_dimensions(filtered_files)
        _is_mosaic = is_mosaic(_dims)
    except Exception:
        _dims, _is_mosaic = {}, False      # <- 落ちたら「mosaic ではない」

しかも extract_dimensions は実際に落ちた。``Z`` / ``T`` / ``XY`` / ``CH`` の分岐だけ
``isdigit()`` の確認が無く、``Timelapse`` や ``Zstack`` のようなトークンを含む
ファイル名で ``int()`` が ValueError を投げていた (``X`` / ``Y`` の分岐にはあった)。

つまり「名前に Timelapse が入っているタイル取得」は、警告も例外もなしに
Z/T へ潰されていた。ここではその組み合わせを直接固定する。
"""
from __future__ import annotations

import numpy as np
import pytest
import tifffile

import ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder as mod
from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import (
    stack_thorlab_with_bioio_calibrated,
)
from ylabcommon.utils.outfile_name import extract_dimensions, is_mosaic


PARAMS = {"mode": "T", "SizeZ": 1, "SizeT": 4, "SizeY": 8, "SizeX": 8,
          "PixelSizeX": .5, "PixelSizeY": .5, "PixelSizeZ": 1.}


# ---- extract_dimensions は例外を投げない --------------------------------------

@pytest.mark.parametrize("name", [
    "ChanA_Zstack_001_001_001.tif",       # Z + 非数字
    "ChanA_Timelapse_001.tif",            # T + 非数字
    "ChanA_XYscan_001.tif",               # XY + 非数字
    "ChanA_CHmerge_001.tif",              # CH + 非数字
    "ChanA_Xa_Yb_001_001.tif",            # X/Y + 非数字
    "T.tif", "Z.tif", "XY.tif", "CH.tif",  # 接頭辞だけで数字が無い
    "ChanA.tif",                          # 連番なし
    "ChanA_001_abc_def_ghi.tif",          # 5トークンだが数字でない
])
def test_odd_tokens_are_skipped_not_raised(name):
    """読めないトークンは黙って飛ばす (呼び出し側が except で包まなくて済むように)。

    投げる実装だと、呼び出し側が except Exception で包み、そこで安全確認が
    無効化される。「投げない」こと自体がこの関数の契約である。
    """
    _n, dims = extract_dimensions(["/x/" + name])
    assert is_mosaic(dims) is False


def test_the_labelled_prefixes_are_matched_longest_first():
    """``XY01`` は X ではなく XY として読む (``CH1`` も同様)。"""
    _n, dims = extract_dimensions(["/x/img_XY01_Z02_CH1_T03.tif"])
    assert dict(dims) == {"XY": {1}, "Z": {2}, "CH": {1}, "T": {3}}


# ---- 本物の mosaic は見逃さない ------------------------------------------------

@pytest.mark.parametrize("files", [
    # ThorLabs の数値形式で X が動く
    ["ChanA_001_001_001_001.tif", "ChanA_002_001_001_001.tif"],
    # Y が動く
    ["ChanA_001_001_001_001.tif", "ChanA_001_002_001_001.tif"],
    # ラベル付き形式の XY
    ["img_XY01_Z02_CH1_T03.tif", "img_XY02_Z02_CH1_T03.tif"],
])
def test_real_mosaics_are_detected(files):
    _n, dims = extract_dimensions(["/x/" + f for f in files])
    assert is_mosaic(dims) is True


# ---- 取り込みが実際に拒否する --------------------------------------------------

def _write(d, name):
    tifffile.imwrite(d / name, np.zeros((8, 8), dtype=np.uint16))


def test_a_mosaic_acquisition_is_rejected(tmp_path):
    d = tmp_path / "img01"
    d.mkdir()
    for xy in (1, 2):
        for t in (1, 2):
            _write(d, f"ChanA_00{xy}_001_001_{t:03d}.tif")
    files = sorted(str(p) for p in d.glob("*.tif"))

    with pytest.raises(RuntimeError, match="mosaic"):
        stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", PARAMS, min_kb=0)


def test_a_mosaic_is_rejected_even_when_a_name_used_to_crash_the_parser(tmp_path):
    """**これが回帰テストの本体。**

    ``CHmerge`` は ``CH`` 接頭辞のあとが数字でないので、以前は
    ``int("merge")`` で ValueError になっていた。呼び出し側の except Exception が
    それを「mosaic ではない」に変換するので、タイル取得が黙って Z/T へ潰れる。

    この名前が机上の空論でないことに注意: collect_valid_tiffs は名前に ``Chan``
    または ``CH`` を含むファイルを通すので、``CH`` で始まる名前は取り込みまで
    そのまま届く。つまり「落ちるトークンを含み、かつ取り込みに到達し、かつ
    タイル取得」という組み合わせは実際に起こりうる。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for xy in (1, 2):
        for t in (1, 2):
            _write(d, f"CHmerge_00{xy}_001_001_{t:03d}.tif")
    files = sorted(str(p) for p in d.glob("*.tif"))

    _n, dims = extract_dimensions(files)
    assert is_mosaic(dims) is True, "mosaic を見落としている: %s" % dict(dims)

    with pytest.raises(RuntimeError, match="mosaic"):
        stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", PARAMS, min_kb=0)


def test_the_guard_does_not_swallow_an_unexpected_failure(tmp_path, monkeypatch):
    """判定そのものが壊れたら、黙って通さずに落ちる。

    「確認できなかったので通す」は安全確認として成立しない。
    extract_dimensions は投げない契約なので、投げたらそれは本物の不具合である。
    """
    d = tmp_path / "img01"
    d.mkdir()
    for t in (1, 2):
        _write(d, f"ChanA_001_001_001_{t:03d}.tif")
    files = sorted(str(p) for p in d.glob("*.tif"))

    def boom(_files):
        raise ValueError("dimension parsing is broken")

    monkeypatch.setattr(mod, "extract_dimensions", boom)

    with pytest.raises(ValueError, match="dimension parsing is broken"):
        stack_thorlab_with_bioio_calibrated(
            files, d / "Experiment.xml", PARAMS, min_kb=0)


def test_a_normal_acquisition_still_passes_the_guard(tmp_path):
    """単一位置の取得は素通りする (ガードを厳しくしすぎていないこと)。"""
    d = tmp_path / "img01"
    d.mkdir()
    for t in range(1, 5):
        _write(d, f"ChanA_001_001_001_{t:03d}.tif")
    files = sorted(str(p) for p in d.glob("*.tif"))

    stacked, kept = stack_thorlab_with_bioio_calibrated(
        files, d / "Experiment.xml", PARAMS, min_kb=0)

    assert stacked.shape == (4, 1, 1, 8, 8)
    assert len(kept) == 4

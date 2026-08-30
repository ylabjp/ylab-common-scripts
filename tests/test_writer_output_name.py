"""出力先の名前の決め方。

``BioIOWriter`` は渡された ``output_path`` に OME の拡張子を付けて書く。以前は
``Path.with_suffix(".ome.tif")`` で作っていたが、あれは「最後のドット以降」を
拡張子とみなすので 2 つの壊れ方をする:

  * ``volume.ome.tif`` -> ``volume.ome.ome.tif`` (``.ome`` が二重になる)
  * ``volume_1.5x``    -> ``volume_1.ome.tif``   (名前の一部が消える)

ひとつめのせいで、**出力名を自分で決めている呼び出し側は公開 API を使えなかった**
(slice-analysis は内部の ``_write_ometiff_streaming`` を直接呼んでいた)。
ふたつめは、名前にドットを含む取得で **黙って別の場所に書く**。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import ylabcommon.bioio.core.bioio_writer as W


def _name(path_str: str) -> str:
    return W._with_ome_suffix(Path(path_str), ".ome.tif", W._TIFF_SUFFIXES).name


@pytest.mark.parametrize("given,want", [
    # 拡張子が無ければ足すだけ (build_output_name が返すのはこの形)
    ("Output_ChanA_X000_Y000_Z001_T001", "Output_ChanA_X000_Y000_Z001_T001.ome.tif"),
    # TIFF の拡張子は置き換える (従来どおり)
    ("output.tif", "output.ome.tif"),
    ("output.tiff", "output.ome.tif"),
    ("OUTPUT.TIF", "OUTPUT.ome.tif"),
    # 既に OME-TIFF の名前なら、そのまま
    ("volume.ome.tif", "volume.ome.tif"),
    ("volume.ome.tiff", "volume.ome.tif"),
    ("stack_coreg_adj_crop.ome.tif", "stack_coreg_adj_crop.ome.tif"),
    # 拡張子ではないドットは名前の一部として残す
    ("volume_1.5x", "volume_1.5x.ome.tif"),
    ("STDP_2026-001.raw", "STDP_2026-001.raw.ome.tif"),
    # 名前が拡張子そのものだけ、という形で消してしまわない
    (".tif", ".tif.ome.tif"),
    # **削るのは 1 回だけ。** 削った残りがまた拡張子で終わっていても、それは
    # 名前の一部。止めないと data.tif.ome.tiff が data.ome.tif になる。
    ("data.tif.ome.tiff", "data.tif.ome.tif"),
    ("scan.tiff.ome.tif", "scan.tiff.ome.tif"),
])
def test_the_output_name_is_built_without_with_suffix(given, want):
    assert _name(given) == want


def test_a_dot_in_the_directory_does_not_reach_the_file_name():
    got = W._with_ome_suffix(Path("prj1.rare/volume"), ".ome.tif", W._TIFF_SUFFIXES)

    assert got == Path("prj1.rare/volume.ome.tif")


def test_the_zarr_name_follows_the_same_rule():
    for given, want in (("out", "out.ome.zarr"),
                        ("out.zarr", "out.ome.zarr"),
                        ("out.ome.zarr", "out.ome.zarr"),
                        ("out_1.5x", "out_1.5x.ome.zarr")):
        got = W._with_ome_suffix(Path(given), ".ome.zarr", W._ZARR_SUFFIXES)
        assert got.name == want, (given, got.name)


def test_the_zarr_writer_uses_that_rule_for_its_own_destination(tmp_path, monkeypatch):
    """規則があるだけでなく、zarr の書き出しが実際にそれを通ること。

    ``_with_ome_suffix`` を直接呼ぶだけのテストでは、呼び出し側が
    ``with_suffix`` に戻っても気づけない (実際それで変異が生き残った)。
    bioio-ome-zarr がこの環境に無くても書き出し先の決め方は確かめられるので、
    保存そのものは差し替えて名前だけ見る。
    """
    seen = []
    monkeypatch.setattr(
        W, "OmeZarrWriter",
        type("_Fake", (), {"save": staticmethod(
            lambda *a, **k: seen.append(a[1] if len(a) > 1 else k.get("uri")))}),
        raising=False)

    out = tmp_path / "volume.ome.zarr"
    W.BioIOWriter(out)._write_omezarr(
        np.zeros((1, 1, 1, 2, 2), dtype=np.uint16),
        dim_order="TCZYX", channel_names=None, physical_pixel_sizes=None)

    assert seen == [out], seen
    assert "ome.ome" not in str(seen[0])


def test_a_caller_that_names_the_file_itself_gets_that_exact_file(tmp_path):
    """回帰: 公開 API に ``volume.ome.tif`` を渡すと ``volume.ome.ome.tif`` になった。

    これが理由で slice-analysis は内部メソッドを直接呼んでいた。
    """
    out = tmp_path / "volume.ome.tif"
    data = np.zeros((2, 1, 2, 8, 8), dtype=np.uint16)

    W.BioIOWriter(out, compression="zlib", compression_level=1).write(data)

    assert out.exists(), sorted(p.name for p in tmp_path.iterdir())
    assert not (tmp_path / "volume.ome.ome.tif").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["volume.ome.tif"]

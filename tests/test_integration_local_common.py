"""手元の実データ 1 式を最後まで通す (opt-in)。

``--local-tiff-dir`` と ``--local-xml`` を渡したときだけ走る。渡さなければ
``local_real_dataset_common`` が skip する。
"""
import pytest

from ylabcommon.bioio.thorlab.builder import ThorlabBioioBuilder


@pytest.mark.integration_bioio
def test_full_pipeline_bioio_common(local_real_dataset_common, tmp_path):
    tiff_dir, xml_path = local_real_dataset_common

    # ``ThorlabBioioBuilder`` は Experiment.xml を tiff_dir から自分で決める
    # (以前は xml_file / output_dir も引数だった)。渡された XML と食い違って
    # いたら、そのまま進めても別のものを読むことになるので先に止める。
    builder = ThorlabBioioBuilder(str(tiff_dir), compression=None,
                                  compression_level=None)
    assert builder.xml_file == xml_path, (
        "the builder reads %s but the dataset points at %s"
        % (builder.xml_file, xml_path))

    builder.build()

    out = tmp_path / "local_common"
    out.mkdir()
    builder.write(out / "volume")

    # **何か書けたことまで見る。** 以前は ``build()`` を呼ぶだけで
    # ``#assert True`` が残っており、例外が出ないこと以外は何も確かめていなかった。
    written = sorted(out.glob("*.ome.tif"))
    assert written, sorted(p.name for p in out.iterdir())
    assert written[0].stat().st_size > 0

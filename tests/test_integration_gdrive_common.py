"""Google Drive から落としたデータ 1 式を最後まで通す (opt-in)。

``--gdrive-folder`` と ``--gdrive-sa-json`` を渡したときだけ走る。渡さなければ
``gdrive_dataset`` が skip する。
"""
from pathlib import Path

import pytest

from ylabcommon.bioio.thorlab.builder import ThorlabBioioBuilder


@pytest.mark.gdrive
def test_gdrive_thorlab(gdrive_dataset, tmp_path):
    dataset_dir = Path(gdrive_dataset)

    xml_path = next(dataset_dir.rglob("Experiment.xml"))
    tiff_dir = xml_path.parent

    out_dir = tmp_path / "gdrive_thorlab"
    out_dir.mkdir()

    # ``ThorlabBioioBuilder`` は tiff_dir だけを取り、Experiment.xml はそこから
    # 決める (以前は xml_path と出力先も引数だった)。
    builder = ThorlabBioioBuilder(str(tiff_dir), compression=None,
                                  compression_level=None)
    builder.build()
    builder.write(out_dir / "volume")

    # **書けたファイルを見る。** 以前は ``assert out_dir.exists()`` だったが、
    # out_dir を作るものが無かったので、import さえ通っていれば落ちる主張だった
    # (しかも builder は組み立てるだけで一度も走っていなかった)。
    written = sorted(out_dir.glob("*.ome.tif"))
    assert written, sorted(p.name for p in out_dir.iterdir())
    assert written[0].stat().st_size > 0

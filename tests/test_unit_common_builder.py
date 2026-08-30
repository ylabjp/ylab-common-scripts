# tests/test_unit_common_builder.py
"""``thorlab_loader.ThorlabBuilder`` の単体テスト。

**このリポジトリでは走らない。** ``thorlab_loader`` は ylabcommon の前身の
別パッケージで、依存にも入っていない。``ThorlabBuilder`` に当たるものは
ylabcommon には無く (``ThorlabBioioBuilder`` は API が別物で、ここが差し替える
``ThorlabMetadata`` / ``read_stack`` / ``save_ome_tiff`` も存在しない)、
そのままでは付け替えられない。

以前は import で **収集エラー**になっていた。エラーは「テストが 1 本落ちた」と
同じ見え方をするので、走らないものは走らないと言って skip する。
``thorlab_loader`` が入った環境では今までどおり走る。
"""

import pytest
import numpy as np
from pathlib import Path

pytest.importorskip(
    "thorlab_loader",
    reason="thorlab_loader is a separate package and is not a dependency here")

from thorlab_loader.builder import ThorlabBuilder
from ylabcommon.parser.keyence_parser import KeyenceParser

@pytest.mark.unit
def test_thorlab_unit(fake_local_dataset, tmp_output_root, monkeypatch):
    tiff_dir, xml_path = fake_local_dataset

    import thorlab_loader.builder as module

    # --- mock metadata ---
    class FakeMetadata:
        def __init__(self, xml_meta, tiff_files):
            pass

        def validate_integrity(self):
            return True

        def groups(self):
            import pandas as pd
            df = pd.DataFrame({
                "path": [str(Path(tiff_dir) / "ChanA_000.tif")],
                "filename": ["ChanA_000.tif"],
                "z": [0],
            })
            return [(("A", 0, 0, 0), df)]

    monkeypatch.setattr(module, "ThorlabMetadata", FakeMetadata)

    monkeypatch.setattr(module, "read_stack", lambda paths: np.zeros((1, 10, 10)))
    monkeypatch.setattr(module, "save_ome_tiff", lambda stack, path: Path(path).touch())

    out_dir = tmp_output_root / "thorlab_unit"

    builder = ThorlabBuilder(str(tiff_dir), str(xml_path))
    outputs = builder.run_and_save(str(out_dir))

    assert len(outputs) == 1
    assert outputs[0].endswith(".ome.tif")


@pytest.mark.unit
def test_keyence_unit(fake_dataset, tmp_output_root, monkeypatch):

    parser = KeyenceParser(fake_dataset)

    import thorlab_loader.builder as module

    monkeypatch.setattr(module, "read_stack", lambda paths: np.zeros((3, 10, 10)))
    monkeypatch.setattr(module, "save_ome_tiff", lambda stack, path: Path(path).touch())

    out_dir = tmp_output_root / "keyence_unit"

    assert out_dir

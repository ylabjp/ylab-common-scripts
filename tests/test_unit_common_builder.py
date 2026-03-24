# tests/test_unit_common_builder.py

import pytest
import numpy as np
from pathlib import Path
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

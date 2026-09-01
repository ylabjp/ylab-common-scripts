# ylabcommon/parser/keyence_parser.py

from typing import Any
from pathlib import Path
import pandas as pd


class KeyenceParser:
    def __init__(self, tiff_dir: Any) -> None:
        self.tiff_dir = Path(tiff_dir)

    def build_groups(self) -> list[tuple]:
        files = sorted(self.tiff_dir.glob("*.tif"))

        df = pd.DataFrame({
            "path": [str(f) for f in files],
            "filename": [f.name for f in files],
            "z": list(range(len(files))),
        })

        group_key = ("KEYENCE", 0, 0, 0)
        return [(group_key, df)]

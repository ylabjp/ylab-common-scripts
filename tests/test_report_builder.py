from ylabcommon.utils.report_builder import ReportBuilder
from datetime import datetime, UTC
from pathlib import Path
import json


def test_report_builder(tmp_output_root):
    report = ReportBuilder()

    report.collect_dataset("dataset", "test", 10)
    report.finalize_validation()

    report.write(tmp_output_root, Path("test"))

    files = list(tmp_output_root.glob("*.json"))

    assert Path("test.validation.json").exists()

    BLUE = '\033[94m'
    print(f"\n\n{BLUE}[INFORMATION]: Verified ReporttBuilder, writer\n")

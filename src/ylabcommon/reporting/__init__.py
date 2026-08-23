"""参照・照会・追跡できる解析出力(addressable analysis outputs)。

仕様は `docs/reporting-spec.md`。複数ページのPDFしか出さないパイプラインは、図を
**参照**できず(図ごとのアドレスが無い)、数値を**照会**できず(p 値がピクセルに
しか無い)、**追跡**もできない(どのコード・データで作ったか残らない)。ここでは
図1枚ごとに SVG/PNG と manifest レコードを残し、PDF は「まとめて見るためのビュー」に
降格する。既存のPDF出力は変えないので、呼び出し箇所は1つずつ移行できる。

    from ylabcommon.reporting import FigureStore, render_report

    with FigureStore(prj_dir, pdf_name="psth_ga.pdf") as store:
        store.save(fig, key="prj1-2-3_conda_psth", caption="…", stats=[…])
    render_report(prj_dir)
"""
from ylabcommon.reporting.manifest import (  # noqa: F401
    CONTENT_TOKEN_HASH_LEN,
    CONTENT_TOKEN_MAX_LEN,
    FIGURES_DIRNAME,
    MANIFEST_NAME,
    REPORT_DIRNAME,
    FigureRecord,
    StatRecord,
    append_record,
    content_token,
    figure_id,
    figures_dir,
    manifest_path,
    read_manifest,
    report_dir,
    slug,
    split_figure_id,
    validate_figure_id,
    write_manifest,
)
from ylabcommon.reporting.report import REPORT_NAME, render_report  # noqa: F401
from ylabcommon.reporting.source import SourceInfo  # noqa: F401
from ylabcommon.reporting.store import DEFAULT_PNG_DPI, FigureStore  # noqa: F401

__all__ = [
    "FigureStore",
    "FigureRecord",
    "StatRecord",
    "SourceInfo",
    "render_report",
    "figure_id",
    "content_token",
    "split_figure_id",
    "validate_figure_id",
    "slug",
    "read_manifest",
    "write_manifest",
    "append_record",
    "manifest_path",
    "figures_dir",
    "report_dir",
    "FIGURES_DIRNAME",
    "REPORT_DIRNAME",
    "MANIFEST_NAME",
    "REPORT_NAME",
    "CONTENT_TOKEN_MAX_LEN",
    "CONTENT_TOKEN_HASH_LEN",
    "DEFAULT_PNG_DPI",
]

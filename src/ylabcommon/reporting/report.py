"""`render_report` — manifest から `report/report.md` を生成する。

`docs/reporting-spec.md` の report.md 節の実装。group ごとに1節、PNG を相対リンクで
埋め、caption と stats の表を出す。**manifest が正本**なので何度でも作り直せる。

数値を表にするのがこの機能の眼目。今は p 値が図のピクセルとしてしか存在せず、
「どの図のどの比較が有意だったか」を後から一覧できない。
"""
from __future__ import annotations

import os
from pathlib import Path

from ylabcommon.reporting.manifest import read_manifest, report_dir

REPORT_NAME = "report.md"

#: source.commit は40桁で記録する。表示はこの桁数に縮める(読みやすさのため)。
_SHORT_SHA = 8


def _fmt_p(value) -> str:
    """p 値の表示。None は「計算したが値なし」なので空欄にせず明示する。"""
    if value is None:
        return "n/a"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v < 1e-4:
        return f"{v:.1e}"
    return f"{v:.4g}"


def _fmt_cell(value) -> str:
    """Markdown の表に入れられる形へ。`|` は表を壊すのでエスケープする。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    return str(value).replace("|", "\\|")


def _source_line(source: dict | None) -> str | None:
    """`repo@commit (dirty) — script` の1行。分からない項目は出さない。"""
    if not source:
        return None
    parts = []
    repo, commit = source.get("repo"), source.get("commit")
    if repo or commit:
        head = repo or "?"
        if commit:
            head += f"@{str(commit)[:_SHORT_SHA]}"
        if source.get("dirty"):
            head += " (dirty)"
        parts.append(head)
    if source.get("script"):
        parts.append(str(source["script"]))
    if source.get("params_hash"):
        parts.append(f"params={source['params_hash']}")
    return " — ".join(parts) if parts else None


def _stats_table(stats) -> list[str]:
    if not stats:
        return []
    lines = [
        "| name | test | n | statistic | p | params |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for s in stats:
        lines.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                _fmt_cell(s.get("name")), _fmt_cell(s.get("test")),
                _fmt_cell(s.get("n")), _fmt_cell(s.get("statistic")),
                _fmt_p(s.get("p")), _fmt_cell(s.get("params")),
            )
        )
    return lines


def render_report(prj_dir: Path | str, title: str | None = None) -> Path:
    """manifest を読んで `prj_dir/report/report.md` を書き、そのパスを返す。

    節は group ごと。group の並びと節の中の図の並びは **manifest の登場順**にする
    (図IDのソートだと 01,02,... が無い図で並びが崩れるうえ、レポートの読み順は
    書き出した順であることが多い)。
    """
    prj_dir = Path(prj_dir)
    records = read_manifest(prj_dir)
    out_dir = report_dir(prj_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / REPORT_NAME

    lines: list[str] = [f"# {title or prj_dir.name}", ""]
    if not records:
        lines += ["(no figures recorded yet)", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(str(r.get("group", "")), []).append(r)

    lines.append(f"{len(records)} figure(s) in {len(groups)} group(s).")
    lines.append("")
    for group, items in groups.items():
        lines += [f"## {group}", ""]
        for r in items:
            lines += [f"### {r['id']}", ""]
            if r.get("caption"):
                lines += [str(r["caption"]), ""]
            png = (r.get("files") or {}).get("png")
            if png:
                # report.md は report/ の中、図は figures/ の中なので相対で辿る。
                rel = os.path.relpath(prj_dir / png, out_dir).replace(os.sep, "/")
                lines += [f"![{r['id']}]({rel})", ""]
            lines += _stats_table(r.get("stats"))
            if r.get("stats"):
                lines.append("")
            meta: list[str] = []
            files = r.get("files") or {}
            # 数値へのリンクを先頭に。AI が図を見ずに値を確認できるようにするのが目的。
            if files.get("csv"):
                rel = os.path.relpath(prj_dir / files["csv"], out_dir).replace(os.sep, "/")
                meta.append(f"Data: [{Path(files['csv']).name}]({rel})")
            if files.get("svg"):
                rel = os.path.relpath(prj_dir / files["svg"], out_dir).replace(os.sep, "/")
                meta.append(f"Vector: [{Path(files['svg']).name}]({rel})")
            for note in (r.get("notes") or []):
                meta.append(f"Note: {note}")
            pdf = r.get("pdf") or {}
            if pdf.get("file"):
                meta.append(f"PDF: `{pdf['file']}` p.{pdf.get('page')}")
            src = _source_line(r.get("source"))
            if src:
                meta.append(f"Source: {src}")
            if r.get("data"):
                meta.append("Input: " + ", ".join(f"`{d}`" for d in r["data"]))
            if r.get("created_at"):
                meta.append(f"Created: {r['created_at']}")
            if meta:
                lines += ["<sub>" + "<br>".join(meta) + "</sub>", ""]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path

"""`FigureStore` — 1図を SVG/PNG + manifest レコード + PDF の1ページとして保存する。

`docs/reporting-spec.md` の "API sketch" の実装。

    store = FigureStore(prj_dir, pdf_name="psth_ga.pdf")
    store.save(fig, key="prj1-2-3_conda_psth", caption="…", stats=[…])
    store.pdf      # 未移行の呼び出し箇所のための素の PdfPages
    store.close()

**互換の約束**: PDF の中身は従来の `PdfPages` 直書きと同じ。図ファイルと manifest は
純粋な追加で、呼び出し箇所は1つずつ移行できる。`save()` に渡した savefig の引数
(`bbox_inches="tight"` など)は PDF / SVG / PNG の3つに同じように渡すので、
per-figure ファイルと PDF のページが食い違わない。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from ylabcommon.reporting.manifest import (
    FigureRecord,
    StatRecord,
    append_record,
    figures_dir,
    read_manifest,
    split_figure_id,
    write_manifest,
)
from ylabcommon.reporting.source import SourceInfo

#: PNG(Markdown 埋め込み用ラスタ)の既定 dpi。SVG が正本なので閲覧に足りればよい。
DEFAULT_PNG_DPI = 200


class FigureStore:
    """1つのレポート単位(= 1 PDF)ぶんの図を保存する。

    prj_dir: 解析出力の置き場。`figures/` と `report/` をこの下に作る。
    pdf_name: 従来どおり出す束ねPDFのファイル名(prj_dir 直下)。None なら PDF を作らない。
    scope: manifest の書き換え単位。既定は pdf_name。**再実行したときに自分が前回
        書いたレコードだけを差し替える**ためのキーで、他のレポートのレコードは残る。
        pdf_name も scope も無いと再実行のたびにレコードが二重になるので ValueError。
    source: 出所情報。省略時は呼び出し元のリポジトリから取得する。
    """

    def __init__(
        self,
        prj_dir: Path | str,
        pdf_name: str | None = None,
        *,
        scope: str | None = None,
        source: SourceInfo | None = None,
        png_dpi: int = DEFAULT_PNG_DPI,
    ) -> None:
        self.prj_dir = Path(prj_dir)
        self.pdf_name = pdf_name
        self.scope = scope if scope is not None else pdf_name
        if not self.scope:
            raise ValueError(
                "FigureStore needs pdf_name or scope: it identifies which manifest "
                "records this run owns, so a rerun replaces them instead of appending "
                "duplicates."
            )
        self.png_dpi = png_dpi
        self.source = source if source is not None else SourceInfo.capture()

        self._closed = False
        self._records: list[FigureRecord] = []
        # 自分の scope の古いレコードを落として書き直してから始める。以降は1図ごとに
        # 追記するので、run が途中で落ちてもそこまでの図は manifest に残る。
        kept = [r for r in read_manifest(self.prj_dir) if r.get("scope") != self.scope]
        self._foreign_ids = {r.get("id") for r in kept}
        write_manifest(self.prj_dir, kept)

        self._pdf = None
        if pdf_name:
            from matplotlib.backends.backend_pdf import PdfPages

            self.prj_dir.mkdir(parents=True, exist_ok=True)
            self._pdf = PdfPages(self.prj_dir / pdf_name)

    # --- PDF passthrough ------------------------------------------------- #
    @property
    def pdf(self):
        """素の `PdfPages`。未移行の呼び出し箇所がそのまま `savefig` できる。

        ここへ直接書いたページは manifest に載らない(移行の途中段階なので当然)。
        ページ番号は PdfPages 自身の数え上げから取るので、直書きが混ざっても
        `save()` が記録するページ番号はずれない。
        """
        if self._pdf is None:
            raise ValueError("this FigureStore was created without pdf_name")
        return self._pdf

    @property
    def has_pdf(self) -> bool:
        return self._pdf is not None

    @property
    def records(self) -> list[FigureRecord]:
        """この run で保存した図のレコード(保存順)。"""
        return list(self._records)

    # --- 保存 -------------------------------------------------------------- #
    def save(
        self,
        fig,
        key: str,
        caption: str | None = None,
        stats: Iterable[Any] | None = None,
        data: Sequence[str] | None = None,
        **savefig_kwargs,
    ) -> FigureRecord:
        """図を SVG/PNG と(あれば)PDF の1ページとして保存し、manifest へ追記する。

        key: 図ID `{prj}_{group}_{kind}[_{seq}]`。prj/group/kind はここから復元する。
        stats: 検定結果。**図に描かなかったものも渡すこと**(これが目的)。
        data: 入力データの **prj_dir 相対**パス。
        savefig_kwargs: `bbox_inches="tight"` など。3つの出力に同じものを渡す。
        """
        if self._closed:
            raise ValueError("FigureStore is closed")
        prj, group, kind, _seq = split_figure_id(key)
        if key in self._foreign_ids:
            raise ValueError(
                f"figure id {key!r} is already used by another report in this project "
                "(figure ids must be unique within prj_dir). Give this figure a "
                "different group/kind/seq."
            )
        if any(r.id == key for r in self._records):
            raise ValueError(f"figure id {key!r} was already saved in this run")

        fig_dir = figures_dir(self.prj_dir)
        fig_dir.mkdir(parents=True, exist_ok=True)
        svg = fig_dir / f"{key}.svg"
        png = fig_dir / f"{key}.png"
        fig.savefig(svg, **savefig_kwargs)
        fig.savefig(png, **{"dpi": self.png_dpi, **savefig_kwargs})

        pdf_ref = None
        if self._pdf is not None:
            self._pdf.savefig(fig, **savefig_kwargs)
            # ページ番号は PdfPages 自身の数から取る(passthrough 直書きが混ざっても
            # ずれない)。get_pagecount は今書いたページを含む1始まりの総数。
            pdf_ref = {"file": self.pdf_name, "page": self._pdf.get_pagecount()}

        record = FigureRecord(
            id=key, prj=prj, group=group, kind=kind, scope=self.scope, caption=caption,
            files={
                "svg": svg.relative_to(self.prj_dir).as_posix(),
                "png": png.relative_to(self.prj_dir).as_posix(),
            },
            pdf=pdf_ref,
            stats=tuple(StatRecord.coerce(s) for s in (stats or ())),
            source=self.source.to_dict(),
            data=tuple(data or ()),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        self._records.append(record)
        append_record(self.prj_dir, record.to_dict())
        return record

    # --- 後始末 ------------------------------------------------------------ #
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pdf is not None:
            self._pdf.close()

    def __enter__(self) -> "FigureStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

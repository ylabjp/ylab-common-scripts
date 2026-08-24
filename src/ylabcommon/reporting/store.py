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
    ensure_jsonable,
    figures_dir,
    rewrite_dropping_scope,
    split_figure_id,
)
from ylabcommon.reporting.source import SourceInfo

#: PNG(Markdown 埋め込み / AI が読む用のラスタ)の既定 dpi。
DEFAULT_PNG_DPI = 200

#: 既定の保存形式。**PNG が既定で SVG は opt-in**。
#: マルチモーダルLLM の画像入力は PNG/JPEG/GIF/WebP で、SVG は画像として読めない
#: (matplotlib の SVG は path の座標列なので、テキストとして渡してもグラフの形は読めない)。
#: 「AI が読んで Markdown に組み込む」用途では PNG が必須。SVG はベクタ編集(論文figure化)
#: 用の正本で、必要なのは通常ごく一部の図なので既定には入れない。
DEFAULT_FORMATS = ("png",)
SUPPORTED_FORMATS = ("png", "svg")

#: 図の数値を CSV にするときの行数上限。これを超える表(生の時系列など)は書かずに
#: 理由を record の notes に残す(黙って出さないと「なぜ無いのか」が分からない)。
DEFAULT_MAX_TABLE_ROWS = 20000


class FigureStore:
    """1つのレポート単位(= 1 PDF)ぶんの図を保存する。

    prj_dir: 解析出力の置き場。`figures/` と `report/` をこの下に作る。
    pdf_name: 従来どおり出す束ねPDFのファイル名(prj_dir 直下)。None なら PDF を作らない。
    scope: manifest の書き換え単位。既定は pdf_name。**再実行したときに自分が前回
        書いたレコードだけを差し替える**ためのキーで、他のレポートのレコードは残る。
        pdf_name も scope も無いと再実行のたびにレコードが二重になるので ValueError。
    source: 出所情報。省略時は呼び出し元のリポジトリから取得する。
    house_style: ラボ共通の rcParams(Arial / fonttype 42)を当てる。既定 True。
        `matplot_util` を import したときと同じ見た目にするためのもので、
        **グローバルな rcParams を書き換える**ので、自前で管理したいときは False。
    """

    def __init__(
        self,
        prj_dir: Path | str,
        pdf_name: str | None = None,
        *,
        scope: str | None = None,
        source: SourceInfo | None = None,
        formats: tuple = DEFAULT_FORMATS,
        png_dpi: int = DEFAULT_PNG_DPI,
        house_style: bool = True,
        max_table_rows: int = DEFAULT_MAX_TABLE_ROWS,
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
        self.max_table_rows = max_table_rows
        bad = [f for f in formats if f not in SUPPORTED_FORMATS]
        if bad:
            raise ValueError(
                f"unsupported figure format(s) {bad}; supported: {list(SUPPORTED_FORMATS)}"
            )
        if not formats:
            raise ValueError("formats must not be empty")
        self.formats = tuple(formats)
        self.source = source if source is not None else SourceInfo.capture()

        self._closed = False
        self._records: list[FigureRecord] = []

        # **PDF に書けることを先に確かめる。** 後回しにすると、PDF がロックされて
        # いた場合に「前回の manifest を消したあとで例外」になり、図も PDF も
        # 無いのに前回の記録だけ失う。
        #
        # `PdfPages(...)` は**ファイルを遅延オープンする**ので、構築しただけでは
        # 検査にならない(実際に開くのは最初の savefig)。追記モードで開いて確かめる。
        # `matplot_util.create_pdf_pages` が書き込み可否をこの方法で見ているのと同じ。
        self._pdf = None
        if pdf_name:
            from matplotlib.backends.backend_pdf import PdfPages

            self.prj_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = self.prj_dir / pdf_name
            with open(pdf_path, "ab"):
                pass
            self._pdf = PdfPages(pdf_path)

        if house_style:
            # 図の出力経路が matplot_util 以外にも増えたので、同じ rcParams を
            # ここでも当てる。当てないと create_pdf_pages から移行した瞬間、
            # PDF が Type-42/Arial から Type-3/DejaVu に黙って変わる。
            from ylabcommon.utils.mpl_style import apply_house_style

            apply_house_style()

        # 自分の scope の古いレコードだけを落とす。以降は1図ごとに追記するので、
        # run が途中で落ちてもそこまでの図は manifest に残る。
        self._foreign_ids = rewrite_dropping_scope(self.prj_dir, self.scope)

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
        close_figure: bool = False,
        table=None,
        pdf_page: int | None = None,
        **savefig_kwargs,
    ) -> FigureRecord:
        """図を保存し、manifest へ追記する。

        key: 図ID `{prj}_{group}_{kind}[_{seq}]`。prj/group/kind はここから復元する。
        stats: 検定結果。**図に描かなかったものも渡すこと**(これが目的)。
        data: 入力データの **prj_dir 相対**パス。
        table: **その図に描かれた数値**(DataFrame)。`figures/{key}.csv` に書いて
            `files["csv"]` から辿れるようにする。図を見なくても値を照会できるのが
            この層の目的の半分なので、可能な限り渡すこと。行数が max_table_rows を
            超える表(生の時系列など)は書かず、理由を notes に残す。
        pdf_page: **PDFのページは呼び出し側が既に書いた**場合に、そのページ番号。
            複数のパネルが1ページに載るレイアウト(縦積みのbar/PSTH)で、パネルごとに
            レコードを作りつつ PDF は従来どおり束ねたページのまま出すために使う。
            None なら従来どおりこの図を新しい1ページとして PDF に書く。
        savefig_kwargs: `bbox_inches="tight"` など。全ての出力に同じものを渡す。
        close_figure: 保存後に figure を閉じる。既定 False。
            **`matplot_util.close_fig` は閉じていた**ので、そこから移行して図を
            大量に出すスクリプトは True にしないと figure が溜まり続ける。
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

        # **ファイルを書く前に**直列化できることを確かめる。あとで失敗すると
        # 図だけ残って manifest に行が無い孤児になる(numpy の int が典型)。
        stat_records = tuple(StatRecord.coerce(s) for s in (stats or ()))
        ensure_jsonable({
            "caption": caption,
            "stats": [r.to_dict() for r in stat_records],
            "data": list(data or ()),
        })

        fig_dir = figures_dir(self.prj_dir)
        fig_dir.mkdir(parents=True, exist_ok=True)
        files: dict = {}
        for fmt in self.formats:
            path = fig_dir / f"{key}.{fmt}"
            kwargs = dict(savefig_kwargs)
            if fmt == "png":
                kwargs.setdefault("dpi", self.png_dpi)
            fig.savefig(path, **kwargs)
            files[fmt] = path.relative_to(self.prj_dir).as_posix()

        notes: list[str] = []
        if table is not None:
            csv_rel, note = self._save_table(fig_dir, key, table)
            if csv_rel:
                files["csv"] = csv_rel
            if note:
                notes.append(note)

        pdf_ref = None
        if pdf_page is not None:
            pdf_ref = {"file": self.pdf_name, "page": int(pdf_page)}
        elif self._pdf is not None:
            self._pdf.savefig(fig, **savefig_kwargs)
            # ページ番号は PdfPages 自身の数から取る(passthrough 直書きが混ざっても
            # ずれない)。get_pagecount は今書いたページを含む1始まりの総数。
            pdf_ref = {"file": self.pdf_name, "page": self._pdf.get_pagecount()}

        record = FigureRecord(
            id=key, prj=prj, group=group, kind=kind, scope=self.scope, caption=caption,
            files=files,
            pdf=pdf_ref,
            stats=stat_records,
            source=self.source.to_dict(),
            data=tuple(data or ()),
            notes=tuple(notes),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        self._records.append(record)
        append_record(self.prj_dir, record.to_dict())
        if close_figure:
            import matplotlib.pyplot as plt

            plt.close(fig)
        return record

    def _save_table(self, fig_dir: Path, key: str, table) -> tuple[str | None, str | None]:
        """図の数値を `figures/{key}.csv` に書き、(相対パス, 注記) を返す。

        list を値に持つ列(個体別ベクタ等)は `;` 連結の文字列にする(集計CSVと同じ流儀)。
        大きすぎる表は書かずに理由だけ返す。失敗しても図は落とさない。
        """
        try:
            n_rows = int(getattr(table, "shape", (0,))[0])
            if n_rows > self.max_table_rows:
                return None, (
                    f"table not written: {n_rows} rows exceed max_table_rows="
                    f"{self.max_table_rows}"
                )
            out = table.reset_index() if getattr(table, "index", None) is not None else table
            out = out.copy()
            for col in out.columns:
                if out[col].map(lambda v: isinstance(v, (list, tuple))).any():
                    out[col] = out[col].map(
                        lambda v: ";".join(str(x) for x in v)
                        if isinstance(v, (list, tuple)) else v
                    )
            path = fig_dir / f"{key}.csv"
            out.to_csv(path, index=False, encoding="utf-8-sig")
            return path.relative_to(self.prj_dir).as_posix(), None
        except Exception as e:
            return None, f"table not written: {type(e).__name__}: {e}"

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

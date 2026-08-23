# Reporting Spec: Addressable Analysis Outputs

Status: **Phase 1 implemented** (`ylabcommon.reporting`). Phases 2–4 (adoption and
promotion) are open — see ylabjp/ylab-common-scripts#68.
Norm source: ylabjp/general research guidelines — `80-operations/ai-skills.md`, applied example "Addressable Analysis Outputs".

## Motivation

Pipelines that emit only multi-page PDFs produce results that cannot be
**referenced** (no per-figure address), **queried** (numbers exist only as
pixels), or **traced** (no code/data provenance). This spec makes every figure
an addressable, provenance-carrying record. The PDF is kept — demoted to a
bundled *view* of those records. Adoption is incremental: existing PDF output
is unchanged.

## Prior art

This is established reproducible-research practice applied to our
matplotlib/PdfPages reporting layer:

* Sandve GK, Nekrutenko A, Taylor J, Hovig E (2013) Ten simple rules for
  reproducible computational research. *PLoS Comput Biol* 9(10):e1003285.
  [doi:10.1371/journal.pcbi.1003285](https://doi.org/10.1371/journal.pcbi.1003285)
  — Rule 1: "for every result, keep track of how it was produced".
* Experiment/run tracking — Davison AP (2012) Automated capture of experiment
  context for easier reproducibility in computational research. *Comput Sci
  Eng* 14(4):48–56 (Sumatra); MLflow; DVC — one record per run with params,
  metrics, artifacts.
* Generated reports — Knuth DE (1984) Literate programming. *Comput J*
  27(2):97–111; knitr/[Quarto](https://quarto.org/);
  [showyourwork](https://show-your.work/) — the report is built from code;
  every figure is traceable to code + data.
* Convention-named layouts — Gorgolewski KJ et al. (2016) The brain imaging
  data structure. *Sci Data* 3:160044.
  [doi:10.1038/sdata.2016.44](https://doi.org/10.1038/sdata.2016.44) — naming
  makes a dataset machine-navigable.
* Machine-actionability as a goal — Wilkinson MD et al. (2016) The FAIR
  Guiding Principles for scientific data management and stewardship. *Sci
  Data* 3:160018.
  [doi:10.1038/sdata.2016.18](https://doi.org/10.1038/sdata.2016.18)
* Tracked pipelines — Köster J, Rahmann S (2012) Snakemake. *Bioinformatics*
  28(19):2520–2522.
  [doi:10.1093/bioinformatics/bts480](https://doi.org/10.1093/bioinformatics/bts480);
  Di Tommaso P et al. (2017) Nextflow. *Nat Biotechnol* 35(4):316–319.
  [doi:10.1038/nbt.3820](https://doi.org/10.1038/nbt.3820)
* Code+data versioning — Halchenko YO et al. (2021) DataLad. *J Open Source
  Softw* 6(63):3262. [doi:10.21105/joss.03262](https://doi.org/10.21105/joss.03262)

## Directory layout (relative to a project output directory)

```text
prj_dir/
  figures/{figure_id}.svg      # vector, for reading and later figure work
  figures/{figure_id}.png      # raster, for embedding in Markdown
  report/manifest.jsonl        # one JSON record per figure (see below)
  report/report.md             # generated from the manifest
  *.pdf                        # existing bundled reports, unchanged (views)
```

## Figure ID

`{prj}_{group}_{kind}[_{seq}]` — lowercase; `-` inside a field, `_` between
fields; filesystem-safe; unique within `prj_dir`.

Examples: `prj1-2-3_conda_psth`, `prj1-2-3_all_event-raster_p02`.

## Manifest (JSON Lines, one record per figure)

```json
{
  "id": "prj1-2-3_conda_psth",
  "prj": "prj1-2-3",
  "group": "conda",
  "kind": "psth",
  "scope": "psth_ga.pdf",
  "caption": "PSTH around cue onset, cond A vs B",
  "files": {"svg": "figures/prj1-2-3_conda_psth.svg",
            "png": "figures/prj1-2-3_conda_psth.png"},
  "pdf": {"file": "psth_ga.pdf", "page": 3},
  "stats": [
    {"name": "conda_vs_condb", "test": "mannwhitneyu",
     "n": [12, 11], "p": 0.031, "params": {"alternative": "two-sided"}}
  ],
  "source": {"repo": "behavior-analysis", "commit": "<40-char sha>", "dirty": false,
             "script": "pipeline/group_report.py", "params_hash": "…"},
  "data": ["cond_a/…", "cond_b/…"],
  "created_at": "2026-08-21T12:00:00+09:00"
}
```

Rules:

* **Every computed statistic is recorded**, whether or not it is drawn into
  the figure (today p-values exist only as pixel labels).
* `source.dirty: true` when the analysis repo working tree had uncommitted
  changes at run time.
* `data` paths are **relative to `prj_dir`** — no machine-specific roots in
  the manifest. The same holds for `files`.
* `prj`/`group`/`kind` are **derived from the id**, not written independently, so a
  record can never disagree with its own address.
* `scope` names the run that owns the record (defaults to the PDF file name). A rerun
  replaces the records of its own scope and leaves every other scope alone.
* `commit` is the **full 40-character sha**. The short form is a display concern
  (`report.md` shortens it); short shas can collide and cannot be expanded again.
* `stats[].p` stays present as `null` when a test was attempted but produced no value
  (reason goes in `params`, e.g. `{"skipped": "n<2"}`). Dropping the key would make
  "not computed" and "computed, undefined" indistinguishable.

## report.md

Generated from the manifest: one section per `group`, PNG embeds via relative
links, captions, and a stats table. Regenerable at any time; PDF export of the
report is optional.

## API (`ylabcommon.reporting`)

```python
from ylabcommon.reporting import FigureStore, figure_id, render_report

with FigureStore(prj_dir, pdf_name="psth_ga.pdf") as store:   # source is captured
    store.save(                                               # automatically
        fig,
        key=figure_id("prj1-2-3", "conda", "psth"),
        caption="PSTH around cue onset, cond A vs B",
        stats=[{"name": "conda_vs_condb", "test": "mannwhitneyu",
                "n": [12, 11], "p": 0.031, "params": {"alternative": "two-sided"}}],
        data=["cond_a/…", "cond_b/…"],
        bbox_inches="tight",          # savefig kwargs reach SVG, PNG and the PDF page
    )
    store.pdf     # underlying PdfPages, for call sites not yet ported

render_report(prj_dir)   # manifest -> report/report.md
```

* `figure_id(prj, group, kind, seq=None)` builds a conforming id; `save` derives
  `prj`/`group`/`kind` by splitting it.
* `FigureStore(prj_dir, scope="…")` without `pdf_name` records figures with no bundled
  PDF. One of `pdf_name` / `scope` is required — it is what a rerun replaces.
* Pages written straight to `store.pdf` are not recorded (that is what "not yet ported"
  means), but they do not shift the page numbers `save` records.

Compatibility contract: PDF output is byte-for-byte the same behavior as
today's `PdfPages` usage; per-figure files and manifest records are pure
additions. Call sites adopt one by one.

## Migrating a call site off `create_pdf_pages` / `close_fig`

The PDF *content* is unchanged, but three things that came free with
`matplot_util` do not come free with `FigureStore`. Two are handled for you; the
third is a deliberate difference.

| | `create_pdf_pages` + `close_fig` | `FigureStore` |
| --- | --- | --- |
| house rcParams (Arial, fonttype 42) | applied as an import side effect of `matplot_util` | applied by `FigureStore` (`house_style=True`, the default). Pass `house_style=False` to manage them yourself |
| closing the figure | `close_fig` called `plt.close()` | `save(..., close_figure=True)`. **Default is False** — a batch script that emits hundreds of figures must pass it or they accumulate |
| `subplots_adjust` margins | `close_fig` applied fixed margins (`wspace=0.5, hspace=1.5, bottom=0.15, top=0.85, left=0.07, right=0.93`) | **not applied.** Call `plt.subplots_adjust(...)` yourself, or pass `bbox_inches="tight"` to `save` |

Unwritable output is a hard error rather than a silent rename:
`create_pdf_pages` fell back to `{base}_{timestamp}.pdf` on `PermissionError`
without telling the caller, which would put a filename in `pdf.file` that is not
the file that was written. `FigureStore` checks the PDF is writable up front and
raises. (`PdfPages` alone is not that check — it opens the file lazily, at the
first `savefig`.)

## Serialisation rules

* The manifest is **strict** JSON Lines: `json.dumps(..., allow_nan=False)`.
  Non-finite statistics (`ttest_ind` on zero-variance groups, `pearsonr` on
  constant input, …) are stored as `p: null` with the field names listed in
  `params.nonfinite`. Writing bare `NaN` would leave the file unreadable by
  strict parsers — and worse, `jq` and `pandas.read_json` silently coerce it to
  `null`, which is the token reserved above for "computed, no value", so the two
  cases would become indistinguishable without any error.
* numpy scalars are coerced (`np.int64` does not subclass `int`, so `n =
  [np.sum(mask), …]` would otherwise raise).
* Caller-supplied values are validated **before** any figure file is written, so
  a serialisation failure cannot leave figures on disk with no manifest row.

## Storage and access

Outputs live under `prj_dir` on lab storage (analyzed data). AI-assisted work
on analysis contexts runs as local Claude Code on a machine that mounts that
storage. Figures under discussion are **promoted** (copied) into the
project-management repository; cloud-drive copies are disposable exports.
(Policy: ylabjp/general `80-operations/documentation.md`.)

## Settled in Phase 1

* **Manifest lifecycle**: rewrite per run, scoped by the `scope` field. On open, a
  `FigureStore` drops the manifest records carrying its own scope and keeps the rest;
  each figure is then appended as it is saved, so a run that dies part-way still leaves
  a truthful manifest of what it managed to draw.
* **Figure-id uniqueness**: enforced within `prj_dir`. Reusing an id that another
  report already owns raises rather than silently overwriting that figure's record.

## Open questions

* Stability of `seq`/page numbers across reruns (proposal: `seq` derives from
  content keys, not enumeration order). Phase 1 takes `seq` from the caller and
  does not enumerate.
* `params_hash`: which parameters enter the hash, and their canonical form.
  Phase 1 accepts it from the caller rather than inventing a scheme.
* Orphaned figure files: a rerun that stops emitting a figure removes its manifest
  record but leaves `figures/*.svg|png` on disk. The manifest is the index, so the
  orphan is invisible to `report.md`; deleting files under `prj_dir` was judged too
  destructive to do implicitly.

# Reporting Spec: Addressable Analysis Outputs (draft)

Status: **draft for review** (Phase 0 of the reporting plan).
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

* Sandve et al. 2013, *Ten Simple Rules for Reproducible Computational
  Research* (PLOS Comput Biol) — "for every result, keep track of how it was
  produced".
* Experiment/run tracking — Sumatra (Davison 2012), MLflow, DVC: one record
  per run with params, metrics, artifacts.
* Generated reports — knitr/Quarto, showyourwork: the report is built from
  code; every figure is traceable to code + data.
* Convention-named layouts — BIDS (Gorgolewski et al. 2016): naming makes a
  dataset machine-navigable.

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
  "prj": "1-2-3",
  "group": "conda",
  "kind": "psth",
  "caption": "PSTH around cue onset, cond A vs B",
  "files": {"svg": "figures/prj1-2-3_conda_psth.svg",
            "png": "figures/prj1-2-3_conda_psth.png"},
  "pdf": {"file": "psth_ga.pdf", "page": 3},
  "stats": [
    {"name": "conda_vs_condb", "test": "mannwhitneyu",
     "n": [12, 11], "p": 0.031, "params": {"alternative": "two-sided"}}
  ],
  "source": {"repo": "behavior-analysis", "commit": "abc1234", "dirty": false,
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
  the manifest.

## report.md

Generated from the manifest: one section per `group`, PNG embeds via relative
links, captions, and a stats table. Regenerable at any time; PDF export of the
report is optional.

## API sketch (`ylabcommon.reporting`)

```python
store = FigureStore(prj_dir, pdf_name="psth_ga.pdf", source=SourceInfo.capture())
store.save(fig, key="prj1-2-3_conda_psth", caption="…", stats=[…], data=[…])
store.pdf     # underlying PdfPages, for call sites not yet ported
store.close()
render_report(prj_dir)   # manifest -> report/report.md
```

Compatibility contract: PDF output is byte-for-byte the same behavior as
today's `PdfPages` usage; per-figure files and manifest records are pure
additions. Call sites adopt one by one.

## Storage and access

Outputs live under `prj_dir` on lab storage (analyzed data). AI-assisted work
on analysis contexts runs as local Claude Code on a machine that mounts that
storage. Figures under discussion are **promoted** (copied) into the
project-management repository; cloud-drive copies are disposable exports.
(Policy: ylabjp/general `80-operations/documentation.md`.)

## Open questions

* Stability of `seq`/page numbers across reruns (proposal: `seq` derives from
  content keys, not enumeration order).
* `params_hash`: which parameters enter the hash, and their canonical form.
* Manifest lifecycle: append vs rewrite (proposal: rewrite per run, scoped to
  the PDF being regenerated).

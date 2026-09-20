# Describe the shared, common framework
  ylab-common-scripts (ylabcommon)

---

## Suggested structure:

ylabcommon a shared utilities, hybrid classes for microscopy dataset reconstruction pipelines developed by YLab.

This framework provides share classes, codesi, and Microscope-specific loaders such as:

- Thorlab microscopy pipeline

- Keyence microscopy pipeline

- Reuse for others only in future

---

## The framework is built on top of BioIO and provides standardized tools for:

- image stacking

- metadata extraction

- dataset validation

- OME output writing

- dataset reporting

Repository Structure

---

## Features:

- BioIO-based microscopy IO

- automatic stack reconstruction

- metadata standardization

- dataset validation

- OME-TIFF writing

- dataset summary reports

- electrophysiology trace analysis (`ylabcommon.ephys`)

---

## `ylabcommon.ephys` — EPSC 様イベントの検出 (取得側と解析側の共有)

取得側 (slice-controller の viewer) と解析側 (slice-analysis) が **同じ計算で同じ値**
を出すために置いてある。画面 (PyQt6 / pyqtgraph) は入っていない。

| モジュール | 役割 |
| --- | --- |
| `ephys.filters` | 4 次ゼロ位相 Butterworth の低域通過 (`apply_lowpass_filter`) |
| `ephys.event_detection` | 二重指数関数テンプレートとの相関で内向きイベントを拾う (`detect_epsc_events` / `detect` / `summarize`) |
| `ephys.event_record` | 検出結果と、その値が出た条件の記録 (`<記録名>_mepsc_result.json` / `<記録名>_mepsc_events.csv`) |
| `ephys.sorted_trace` | sorter が書いた `*_df.h5` と相方の json を読む (`SortedRecording`) |

使い方 (仕分け後の記録 1 つを掛けて記録まで):

```python
from ylabcommon.ephys.event_detection import DetectionParams, detect
from ylabcommon.ephys.event_record import build_result, save_result
from ylabcommon.ephys.sorted_trace import SortedRecording

rec = SortedRecording.open(session_dir / "V-test_260904-001_df.h5")
params = DetectionParams()                       # 既定は取得側の画面と同じ
events = detect(rec.values(), rec.dt_s, params)  # values() は全 train の連結
save_result(session_dir, build_result(
    events, source_file=rec.path.name, sampling_rate_khz=rec.sampling_rate_khz,
    analysed_seconds=rec.duration_s(), params=params, config_name=rec.config_name))
```

決めごと:

* **頻度は記録長が無いと出せない。** `summarize` / `build_result` は掛けた波形の
  長さ (`analysed_seconds`) を必ず受け取り、0 以下なら断る
* **採用イベントが無いときの平均は `None`。** 0 で埋めると 0 pA のイベントが
  並んでいるのと見分けが付かない
* **記録には値だけでなく条件も入れる。** どのファイルの、どの train を、表示用の
  どのフィルタと、どの検出パラメータで掛けたのか。無いと後から確かめられない
* **保存名は元の記録から決まる** (`<記録名>_mepsc_result.json`)。集計 (crawl) は
  `*_mepsc_result.json` を探すので、人がその場で選んだ名前ではセッションと結び付かない。
  1 つのセルに V-test と STDP のように記録が 2 つあっても上書きにならない
* **train の平均は検出に掛けない。** 平均するとランダムなイベントは消える

検出そのものは slice-controller から **振る舞いを変えずに** 移した。持ち越して
いる未確認の点 (`corr_threshold` が `find_peaks` の `threshold` である点、
`onset_time_s` が窓の先頭でありベースラインぶん早い点、振幅が下位 1 パーセンタイル
である点、山の最小間隔が無い点) は `ephys/event_detection.py` の説明に書いてある。
直すには実験者の判断が要る。

---

## Documentation

- [ThorImage `Experiment.xml` の読み方](docs/thorlabs_experiment_xml.md) —
  公開仕様が無いためリバースエンジニアリングした結果。各項目に確度
  (確認済 / 推定 / 仕様外) を付けてあり、**コードのコメントではなくこの文書が正典**。
  未確認の仮説は冒頭の「宿題」節にまとめてあるので、新しい取得条件のサンプルが
  手に入ったらそこから確認すること。

---

## Installation

### Clone repository:

```bash

git clone https://github.com/ylabjp/ylab-common-scripts.git
cd ylab-common-scripts

```
---

### Setup virtual environment using uv

```bash
uv sync
```
### Install package and dependencies in editable mode

```bash

uv pip install -e .

```

---

## Dependency Note

This package is intended to be used as a dependency of microscope-specific pipelines.

Example dependency:

ylabcommon = { git = "https://github.com/ylabjp/ylab-common-scripts" }

During development/work locally use local path:



ylabcommon = { path = "../YlabCommonScripts/ylab-common-scripts", editable = true }

---

# If environment issues occur:, don't worry run diagnostic script 

```bash

source env_common_fix.sh

```

---

## Run Unit/pytest

 -**Unit tests (default)**

```bash
pytest \
  --ignore src/ylabcommon/analysis/ \
  --ignore=src/ylabcommon/utils       % Just ignore these folder
```
-**Local dataset validation**

```bash
uv run pytest tests/ -m integration\_bioio
  --local-tiff-dir "Your local tiff's directory path"
  --local-xml \
```
-**Google Drive dataset**

```bash

uv run pytest tests \
  -v -m gdrive -s --gdrive-folder \
  --gdrive-folder "URL" \
  --gdrive-sa-json "/credentials.json"
```

---

## Future Work

- extended validation tools

- improved metadata handling

- additional report utilities

---



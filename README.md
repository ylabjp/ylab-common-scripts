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



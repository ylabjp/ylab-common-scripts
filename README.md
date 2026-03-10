# Ddescribe the shared, common framework
#ylab-common-scripts (ylabcommon)

---

## Suggested structure:

ylabcommon a shared utilities, hybrid classes for microscopy dataset reconstruction pipelines developed by YLab.

This framework provides share classes, codesi, and Microscope-specific loaders such as:

-- Thorlab microscopy pipeline

-- Keyence microscopy pipeline

-- Reuse for others only in future

---

## The framework is built on top of BioIO and provides standardized tools for:**

-- image stacking

-- metadata extraction

-- dataset validation

-- OME output writing

-- dataset reporting

Repository Structure

---

## Features:**

-- BioIO-based microscopy IO

-- automatic stack reconstruction

-- metadata standardization

-- dataset validation

== OME-TIFF writing

-- dataset summary reports

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

##  Dependency

This package is intended to be used as a dependency of microscope-specific pipelines.

Example dependency:

ylabcommon = { git = "https://github.com/ylabjp/ylab-common-scripts" } **NOTE** : Once 1st PR will merge, continue to  use this one

During development/work locally use local path:

ylabcommon = { path = "../YlabCommonScripts/ylab-common-scripts", editable = true }

---

# If environment issues occur:, don't worry run diagnostic script 

```bash

source env_common_fix.sh

```

---


## Future Work

-- extended validation tools

-- improved metadata handling

-- additional report utilities

---



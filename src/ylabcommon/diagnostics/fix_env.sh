#!/usr/bin/env bash

set -e

echo "=========================================="
echo " Thorlab BioIO Environment Repair Script"
echo "=========================================="

echo ""
echo "Checking uv installation..."
if ! command -v uv &> /dev/null; then
    echo "ERROR: uv not installed."
    echo "Install via:"
    echo "curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
echo "uv OK"
echo ""

# -------------------------------------------------------
# Recreate environment
# -------------------------------------------------------

echo "Recreating virtual environment (.venv)..."

if [ -d ".venv" ]; then
    echo "Removing existing .venv..."
    rm -rf .venv
fi

echo "Running uv sync..."
uv sync

echo ""

# -------------------------------------------------------
# Verify pip exists
# -------------------------------------------------------

echo "Checking pip inside uv environment..."

if uv run python -m pip --version &> /dev/null; then
    echo "pip exists ✔"
else
    echo "pip missing — repairing..."

    uv run python -m ensurepip --upgrade
    uv run python -m pip install --upgrade pip

    echo "pip repaired"
fi

echo ""

echo "Installed packages:"
uv run python -m pip list
echo ""

# -------------------------------------------------------
# Verify BioIO plugins
# -------------------------------------------------------

echo "Checking bioio plugins..."

PLUGIN_OK=true

if uv run python -c "import bioio_tifffile" &> /dev/null; then
    echo "bioio_tifffile OK ✔"
else
    echo "Missing bioio_tifffile — installing..."
    uv add bioio-tifffile || uv pip install bioio-tifffile
    PLUGIN_OK=false
fi

if uv run python -c "import bioio_ome_tiff" &> /dev/null; then
    echo "bioio_ome_tiff OK ✔"
else
    echo "Missing bioio_ome_tiff — installing..."
    uv add bioio-ome-tiff || uv pip install bioio-ome-tiff
    PLUGIN_OK=false
fi

if uv run python -c "import bioio_ome_zarr" &> /dev/null; then
    echo "bioio_ome_zarr OK ✔"
else
    echo "OME-Zarr plugin not found (optional). Installing..."
    uv add bioio-ome-zarr || uv pip install bioio-ome-zarr
fi

echo ""

# -------------------------------------------------------
# Ultra debug info
# -------------------------------------------------------

echo "Environment debug info"

echo ""
echo "Python executable:"
uv run which python

echo ""
echo "Python version:"
uv run python --version

echo ""
echo "Installed BioIO plugins:"
uv run python - <<'PY'
from bioio.plugins import dump_plugins
dump_plugins()
PY

echo ""
echo "Test import:"
uv run python -c "import bioio, bioio_tifffile; print('BioIO import OK ✔')"

echo ""
echo "=========================================="
echo " Environment check COMPLETE"
echo "=========================================="

if [ "$PLUGIN_OK" = false ]; then
    echo "NOTE: Some plugins were installed during repair."
    echo "You may want to re-run your pipeline."
fi


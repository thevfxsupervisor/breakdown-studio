#!/bin/bash
# ============================================================================
# Advanced: separate detection env (GPU/CUDA users)
#
# The default install.sh installs AI features (detection + OCR) straight into
# bs_env, which is right for almost everyone. Use THIS script only if you want
# a specific CUDA torch wheel for GPU detection, kept in its own venv so it does
# not affect bs_env. Install the matching torch wheel from https://pytorch.org
# into transnet_env AFTER it is created (or edit requirements-transnet.txt first).
# ============================================================================
set -e
cd "$(dirname "$0")"

echo "[1/2] Checking for a working Python 3 interpreter ..."
if ! command -v python3 >/dev/null; then
    echo
    echo "============================================================================"
    echo " ERROR: python3 was not found on PATH."
    echo
    echo " Fix:"
    echo "   1. Install Python 3.9+ from https://www.python.org/downloads/"
    echo "      (or via your package manager, e.g. 'brew install python' / 'apt install python3')"
    echo "   2. Open a new terminal and re-run install-transnet.sh."
    echo "============================================================================"
    exit 1
fi

if ! python3 -c "import sys; print(sys.version)" >/dev/null 2>&1; then
    echo
    echo "============================================================================"
    echo " ERROR: python3 was found but failed to run (import sys check failed)."
    echo " Install Python 3.9+ from https://www.python.org/downloads/ and try again."
    echo "============================================================================"
    exit 1
fi
PYVER="$(python3 -c 'import sys; print(sys.version.split()[0])')"
echo "  Using Python $PYVER ($(command -v python3))"

echo
echo "[2/2] Creating TransNetV2 environment (transnet_env) ..."
python3 -m venv transnet_env
./transnet_env/bin/python -m pip install --upgrade pip
./transnet_env/bin/python -m pip install -r requirements-transnet.txt

cat <<EOF

============================================================================
 Done. TransNetV2 Python is:  $(pwd)/transnet_env/bin/python
 Open Breakdown Studio - Settings - "TransNetV2 Python" and set it to that path.
============================================================================
EOF

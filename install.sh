#!/bin/bash
# ============================================================================
# Breakdown Studio - one-shot installer (macOS / Linux)
#   1) validates the Python interpreter
#   2) creates the worker venv (Pillow, numpy, Google libs)
#   3) optionally creates the TransNetV2 venv (large; torch)
#   4) checks ffmpeg and Tkinter (warnings only, non-fatal)
# ============================================================================
set -e
cd "$(dirname "$0")"

echo "[1/4] Checking for a working Python 3 interpreter ..."
if ! command -v python3 >/dev/null; then
    echo
    echo "============================================================================"
    echo " ERROR: python3 was not found on PATH."
    echo
    echo " Fix:"
    echo "   1. Install Python 3.9+ from https://www.python.org/downloads/"
    echo "      (or via your package manager, e.g. 'brew install python' / 'apt install python3')"
    echo "   2. Open a new terminal and re-run install.sh."
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
echo "[2/4] Creating worker environment (bs_env) ..."
python3 -m venv bs_env
./bs_env/bin/python -m pip install --upgrade pip
./bs_env/bin/python -m pip install -r requirements-worker.txt

echo
echo "  Checking Tkinter in the new environment (needed for the desktop GUI) ..."
if ./bs_env/bin/python -c "import tkinter" >/dev/null 2>&1; then
    echo "  OK: Tkinter is available."
else
    echo "  WARNING: Tkinter is not available in this Python."
    echo "           The Tkinter GUI (breakdown_studio.py) will not run."
    echo "           On macOS, install a python.org build (its installers include Tkinter)."
    echo "           On Linux, install your distro's tk package (e.g. 'apt install python3-tk')."
    echo "           Or use the Qt GUI instead: breakdown_studio_qt.py (pip install PySide6)."
fi

echo
echo "  Checking for ffmpeg on PATH ..."
if command -v ffmpeg >/dev/null 2>&1; then
    echo "  OK: ffmpeg found on PATH."
else
    echo "  WARNING: ffmpeg was not found on PATH."
    echo "           Frames, cuts, and reference clips will not work until it is available."
    echo "           Install it (e.g. 'brew install ffmpeg' / 'apt install ffmpeg') from"
    echo "           https://ffmpeg.org, or point Settings - ffmpeg / ffprobe at the full path."
fi

echo
read -r -p "Install the TransNetV2 detection env now? It is large (torch). [y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
  echo "[3/4] Creating TransNetV2 environment (transnet_env) ..."
  python3 -m venv transnet_env
  ./transnet_env/bin/python -m pip install --upgrade pip
  ./transnet_env/bin/python -m pip install -r requirements-transnet.txt
else
  echo "Skipped TransNetV2 env."
fi

cat <<EOF

============================================================================
 Done. Launch the app (python3 breakdown_studio.py), open Settings, and set:
   Worker Python     = $(pwd)/bs_env/bin/python
   TransNetV2 Python = $(pwd)/transnet_env/bin/python   (if installed)
   ffmpeg / ffprobe  = your ffmpeg binaries (https://ffmpeg.org)
   Google OAuth client secret = your client_secret.json (see README)
============================================================================
EOF

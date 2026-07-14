#!/bin/bash
# ============================================================================
# Breakdown Studio - one-shot installer (macOS / Linux)
#   1) validates the Python interpreter
#   2) creates the worker environment (bs_env): Pillow, numpy, Google libs
#   3) asks ONE plain-language question about AI features (shot detection, OCR)
#      and installs them into the SAME bs_env if wanted
#   4) checks ffmpeg (prints the install line for your platform if missing)
#   5) writes config.json for you, makes breakdown_studio.command runnable,
#      and (Linux only) offers a .desktop launcher
# ============================================================================
set -e
cd "$(dirname "$0")"

echo "[1/5] Checking for a working Python 3 interpreter ..."
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
echo "[2/5] Creating worker environment (bs_env) ..."
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
echo "[3/5] AI features ..."
AI_INSTALLED=""
read -r -p "Install AI features (shot detection + burn-in OCR)? ~2 GB download, recommended. [Y/n] " aiq
if [[ "$aiq" =~ ^[Nn]$ ]]; then
    echo "  Skipped. These stages will be unavailable until you install AI features:"
    echo "    - Detect shots"
    echo "    - Slate OCR"
    echo "    - VFX-note OCR"
    echo "    - Boundary QC"
    echo "  Re-run install.sh later and answer Y to add them, or run install-transnet.sh"
    echo "  for the advanced (separate GPU/CUDA environment) path."
else
    echo "  Installing AI features into bs_env (this can take a while) ..."
    ./bs_env/bin/python -m pip install -r requirements-ai.txt
    AI_INSTALLED="1"
fi

echo
echo "[4/5] Checking for ffmpeg ..."
if command -v ffmpeg >/dev/null 2>&1; then
    echo "  OK: ffmpeg found on PATH."
else
    echo "  WARNING: ffmpeg was not found on PATH."
    echo "           Frames, cuts, and reference clips will not work until it is available."
    if [[ "$(uname)" == "Darwin" ]]; then
        echo "           Install it with:  brew install ffmpeg"
    else
        echo "           Install it with:  sudo apt install ffmpeg   (Debian/Ubuntu)"
        echo "                        or:  sudo dnf install ffmpeg   (Fedora)"
    fi
    echo "           Or point Settings - ffmpeg / ffprobe at the full path to your binaries."
fi

echo
echo "[5/5] Writing config.json ..."
if [ ! -f config.json ]; then
    cp config.example.json config.json
fi

WORKER_PY="$(pwd)/bs_env/bin/python"
TRANSNET_PY=""
if [ -n "$AI_INSTALLED" ]; then
    TRANSNET_PY="$WORKER_PY"
fi
FFMPEG_PATH="$(command -v ffmpeg 2>/dev/null || true)"
FFPROBE_PATH="$(command -v ffprobe 2>/dev/null || true)"

CFGPY="$(mktemp -t bs_write_config.XXXXXX).py"
cat > "$CFGPY" <<'PYEOF'
import json, sys, os

path = sys.argv[1]
pairs = sys.argv[2:]
updates = {}
i = 0
while i < len(pairs):
    updates[pairs[i]] = pairs[i + 1]
    i += 2

with open(path, "r", encoding="utf-8") as f:
    cfg = json.load(f)

def is_placeholder(key, value):
    if not isinstance(value, str) or value.strip() == "":
        return True
    v = value.strip()
    if v.startswith("/path/to"):
        return True
    for frag in ("worker_env", "transnet_env"):
        if frag in v and not os.path.exists(v):
            return True
    return False

changed = False
for key, value in updates.items():
    if value == "":
        continue
    current = cfg.get(key, "")
    if is_placeholder(key, current):
        cfg[key] = value
        changed = True

if changed:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print("config.json updated")
else:
    print("config.json already configured, no changes needed")
PYEOF

./bs_env/bin/python "$CFGPY" "$(pwd)/config.json" \
    worker_python "$WORKER_PY" \
    transnet_python "$TRANSNET_PY" \
    ffmpeg "$FFMPEG_PATH" \
    ffprobe "$FFPROBE_PATH"
rm -f "$CFGPY"

chmod +x breakdown_studio.command 2>/dev/null || true

if [[ "$(uname)" != "Darwin" ]]; then
    echo
    read -r -p "Create a Linux desktop launcher (breakdown-studio.desktop)? [y/N] " deskans
    if [[ "$deskans" =~ ^[Yy]$ ]]; then
        DESKTOP_DIR="$HOME/.local/share/applications"
        mkdir -p "$DESKTOP_DIR" 2>/dev/null || true
        DESKTOP_FILE="$DESKTOP_DIR/breakdown-studio.desktop"
        {
            echo "[Desktop Entry]"
            echo "Type=Application"
            echo "Name=Breakdown Studio"
            echo "Exec=$WORKER_PY $(pwd)/breakdown_studio.py"
            echo "Path=$(pwd)"
            echo "Terminal=false"
            echo "Categories=Graphics;"
        } > "$DESKTOP_FILE" 2>/dev/null || echo "  Could not write $DESKTOP_FILE (non-fatal, skipping)."
        if [ -f "$DESKTOP_FILE" ]; then
            chmod +x "$DESKTOP_FILE" 2>/dev/null || true
            echo "  Created $DESKTOP_FILE"
        fi
    fi
fi

cat <<EOF

============================================================================
 Done. Settings are pre-filled (config.json written).
 Launch the app: ./breakdown_studio.command   or   python3 breakdown_studio.py
 Google OAuth client secret still needs to be set in Settings (see README).
============================================================================
EOF

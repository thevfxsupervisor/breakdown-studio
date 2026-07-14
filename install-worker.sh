#!/bin/bash
# Create a self-contained worker environment (bs_env: Pillow, numpy, Google libs)
# next to the app. Most people should use install.sh instead; this is here for
# re-running just the worker step (e.g. after deleting bs_env).
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
    echo "   2. Open a new terminal and re-run install-worker.sh."
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
echo "[2/2] Creating worker environment (bs_env) ..."
python3 -m venv bs_env
./bs_env/bin/python -m pip install --upgrade pip
./bs_env/bin/python -m pip install -r requirements-worker.txt

echo
echo "  Writing config.json ..."
if [ ! -f config.json ]; then
    cp config.example.json config.json
fi

WORKER_PY="$(pwd)/bs_env/bin/python"
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

./bs_env/bin/python "$CFGPY" "$(pwd)/config.json" worker_python "$WORKER_PY"
rm -f "$CFGPY"

echo
echo "Worker Python is: $WORKER_PY"
echo "config.json has been updated (if worker_python was still a placeholder)."

#!/bin/bash
# macOS / Linux launcher. Double-click on macOS, or run ./breakdown_studio.command
cd "$(dirname "$0")"
PY="./bs_env/bin/python"
[ -x "$PY" ] || PY="python3"
exec "$PY" breakdown_studio.py

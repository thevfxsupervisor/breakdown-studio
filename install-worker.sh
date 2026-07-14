#!/bin/bash
# Create a self-contained worker venv (Pillow + numpy) next to the app.
set -e
cd "$(dirname "$0")"
python3 -m venv bs_env
./bs_env/bin/python -m pip install --upgrade pip
./bs_env/bin/python -m pip install -r requirements-worker.txt
echo
echo "Worker Python is: $(pwd)/bs_env/bin/python"
echo 'Put that path in Breakdown Studio -> Settings -> "Worker Python".'

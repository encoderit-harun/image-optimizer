#!/bin/bash
# macOS: double-click to start Image Optimizer.
# Works from any location. First run (or after moving the folder) sets up Python + Pillow once.
cd "$(dirname "$0")" || exit 1
if ! .venv/bin/python -c "import PIL" >/dev/null 2>&1; then
  echo "Setting up (one time only, needs internet)…"
  rm -rf .venv
  python3 -m venv .venv && .venv/bin/python -m pip install --upgrade pip -q && .venv/bin/python -m pip install -r requirements.txt -q || {
    echo "Setup failed. Make sure Python 3.9+ is installed (https://www.python.org/downloads/)."; read -r -p "Press Enter to close"; exit 1; }
fi
.venv/bin/python app.py

#!/usr/bin/env bash
# ===========================================================================
#  AI Schedule Assistant - one click launcher (macOS / Linux)
# ===========================================================================
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 not found. Please install Python 3.10+."
    exit 1
fi

if [ ! -f ".venv/bin/python" ]; then
    echo "[1/3] Creating virtual environment..."
    python3 -m venv .venv
    echo "[2/3] Installing dependencies (this may take a few minutes)..."
    .venv/bin/python -m pip install --upgrade pip >/dev/null
    .venv/bin/python -m pip install -r requirements.txt
else
    echo "[1/3] Virtual environment found, skipping install."
fi

echo ""
echo "[3/3] Starting app... open http://localhost:8501"
echo "      Press Ctrl+C to stop."
echo ""
exec .venv/bin/python -m streamlit run app.py

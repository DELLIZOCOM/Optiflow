#!/bin/bash
# OptiFlow AI — one-command startup script
# Usage: ./start.sh  (or  bash start.sh)

set -e
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
PYTHON="python3"

# ── 1. Check Python ───────────────────────────────────────────────────────────
if ! command -v "$PYTHON" &>/dev/null; then
    PYTHON="python"
    if ! command -v "$PYTHON" &>/dev/null; then
        echo "ERROR: Python 3 is not installed. Install it from https://python.org" >&2
        exit 1
    fi
fi

PYVER=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
if [ "$PYVER" -lt 3 ]; then
    echo "ERROR: Python 3 is required (found Python $PYVER)." >&2
    exit 1
fi

# ── 2. Create virtual environment if missing ──────────────────────────────────
if [ ! -d ".venv" ]; then
    echo "→ Creating virtual environment..."
    "$PYTHON" -m venv .venv
fi

# ── 3. Activate ───────────────────────────────────────────────────────────────
# shellcheck disable=SC1091
source .venv/bin/activate

# ── 4. Install / sync dependencies ───────────────────────────────────────────
MARKER=".venv/.deps_installed"
REQ_HASH=$(md5 -q requirements.txt 2>/dev/null || md5sum requirements.txt 2>/dev/null | awk '{print $1}')

if [ ! -f "$MARKER" ] || [ "$(cat "$MARKER" 2>/dev/null)" != "$REQ_HASH" ]; then
    echo "→ Installing dependencies..."
    pip install -r requirements.txt --quiet
    echo "$REQ_HASH" > "$MARKER"
    echo "  Done."
fi

# ── 5. Launch ─────────────────────────────────────────────────────────────────
echo ""
echo "  OptiFlow AI is starting on http://localhost:${PORT}"
echo "  Open your browser and navigate there."
echo "  Press Ctrl+C to stop."
echo ""

uvicorn app:app --host 0.0.0.0 --port "$PORT"

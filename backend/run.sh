#!/bin/bash
set -euo pipefail

# Move to backend directory (this script's directory)
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"

# Ensure dependencies are installed
REQ_FILE="$SCRIPT_DIR/requirements.txt"
python3 -m pip install -r "$REQ_FILE"

# Run server from backend dir so 'app' module is importable
UVICORN_CMD="uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"
exec $UVICORN_CMD 
#!/usr/bin/env bash
# Start the OD inference console on http://127.0.0.1:8404
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "creating virtual environment…"
  python3 -m venv --system-site-packages .venv
  .venv/bin/python -m pip install -q polars fastapi "uvicorn[standard]"
fi

exec .venv/bin/python -m app.cli serve --port "${PORT:-8404}" "$@"

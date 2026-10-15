#!/bin/bash
D="$(cd "$(dirname "$0")" && pwd)"
PY="$D/venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" "$D/install.py" start --dest "$D" "$@"

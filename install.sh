#!/bin/bash
# 通用安装入口（Mac/Linux）：调用 install.py
cd "$(dirname "$0")"
PY="$(command -v python3.12 || command -v python3)"
[ -n "$PY" ] || { echo "需要 Python 3.10+"; exit 1; }
exec "$PY" install.py install "$@"

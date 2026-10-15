#!/bin/bash
# macOS 安装：确保有 Python 3.10+（必要时用 Homebrew 安装），然后调用 install.py
set -e
cd "$(dirname "$0")"
PY=""
for c in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$(command -v "$c")"; break; fi
done
if [ -z "$PY" ]; then
  command -v brew >/dev/null 2>&1 || { echo "!! 请先安装 Homebrew：https://brew.sh ，再重新运行"; exit 1; }
  brew install python@3.12; PY="$(brew --prefix)/bin/python3.12"
fi
exec "$PY" install.py install --dest "${1:-$HOME/KeDuoDuo}"

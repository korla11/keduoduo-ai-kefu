#!/bin/bash
# Linux 安装：调用 install.py（root 运行时注册 systemd 服务 keduoduo；--no-autostart 跳过）
set -e
cd "$(dirname "$0")"
PY="$(command -v python3.12 || command -v python3 || true)"
[ -n "$PY" ] || { echo "需要 Python 3.10+（apt install python3 python3-venv）"; exit 1; }
DEST="${1:-$HOME/KeDuoDuo}"; shift || true
exec "$PY" install.py install --dest "$DEST" "$@"

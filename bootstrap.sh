#!/usr/bin/env bash
# Linux / macOS 终端入口: bash bootstrap.sh   (或 ./bootstrap.sh)
#
# 与 bootstrap.command 的区别: 这个不暂停等待按键, 适合在终端里直接跑 / 脚本调用。
set -u

cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    PY="$c"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "[X] 未找到 Python 解释器。"
  echo "    请先安装 Python 3.10 及以上版本, 然后重新运行本脚本。"
  echo "    Debian/Ubuntu: sudo apt install python3.12"
  echo "    Fedora/RHEL:   sudo dnf install python3.12"
  exit 1
fi

exec "$PY" bootstrap.py "$@"

#!/usr/bin/env bash
# macOS 一键入口: 在 Finder 里双击本文件即可(它会自动打开终端)。
# 也可以: bash bootstrap.sh
#
# 本脚本只负责"找到一个 Python 解释器再把向导跑起来"。
# 版本是否够新(需要 3.10+)由 bootstrap.py 自己判断 —— 所以这里用最宽松的搜寻顺序,
# 找不到 3.11 时也会用 3.9 起步, 让向导给出可读的安装指引。
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
  echo "未找到 Python 解释器。"
  echo "请先安装 Python 3.10 及以上版本: https://www.python.org/downloads/macos/"
  echo ""
  read -n 1 -s -r -p "按任意键关闭这个窗口 ..."
  exit 1
fi

"$PY" bootstrap.py "$@"
code=$?

echo ""
echo "--------------------------------------------"
echo "向导退出码: $code"
if [ "$code" -eq 0 ]; then
  echo "服务已正常停止。"
else
  echo "没有正常结束 —— 请把上面的输出截图反馈给作者。"
fi
echo "--------------------------------------------"
read -n 1 -s -r -p "按任意键关闭这个窗口 ..."
exit "$code"

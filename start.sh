#!/bin/bash
# OpenXiaZai 启动脚本
# 用法: ./start.sh  （优先用项目内 venv，系统 python3 兜底）
cd "$(dirname "$0")"
if [ -f ".venv/bin/python3" ]; then
  .venv/bin/python3 launcher.py
elif [ -f "venv/bin/python3" ]; then
  venv/bin/python3 launcher.py
else
  python3 launcher.py
fi

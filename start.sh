#!/bin/bash
# OpenXiaZai 启动脚本
# 用法: ./start.sh  （优先运行当前源码，避免旧 dist 应用缺少新功能）
cd "$(dirname "$0")"
if [ -f ".venv/bin/python3" ]; then
  .venv/bin/python3 launcher.py
elif [ -f "venv/bin/python3" ]; then
  venv/bin/python3 launcher.py
else
  if [[ "$OSTYPE" == "darwin"* && -d "dist/OpenXiaZai.app" ]]; then
    open "dist/OpenXiaZai.app"
  else
    python3 launcher.py
  fi
fi

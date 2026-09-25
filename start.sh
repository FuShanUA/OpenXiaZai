#!/bin/bash
# OpenXiaZai 启动脚本
# 用法: ./start.sh  （macOS 优先启动打包后的 .app，其他系统走 venv）
cd "$(dirname "$0")"
if [[ "$OSTYPE" == "darwin"* && -d "dist/OpenXiaZai.app" ]]; then
  open "dist/OpenXiaZai.app"
  exit 0
fi
if [ -f ".venv/bin/python3" ]; then
  .venv/bin/python3 launcher.py
elif [ -f "venv/bin/python3" ]; then
  venv/bin/python3 launcher.py
else
  python3 launcher.py
fi

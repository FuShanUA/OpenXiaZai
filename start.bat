@echo off
REM OpenXiaZai 启动脚本 (Windows)
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" launcher.py
) else if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" launcher.py
) else (
  python launcher.py
)
pause

@echo off
title KartCut
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo KartCut is not installed yet. Run setup_windows.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run.py %*
pause

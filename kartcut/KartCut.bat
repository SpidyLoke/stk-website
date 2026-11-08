@echo off
title KartCut
cd /d "%~dp0"
set PYTHONNOUSERSITE=1
set "PY=python\python.exe"
if not exist "%PY%" set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo KartCut is not installed yet. Run setup_windows.bat first.
  pause
  exit /b 1
)
"%PY%" run.py %*
pause

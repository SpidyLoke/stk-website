@echo off
title KartCut setup
echo Setting up KartCut. This downloads a few GB the first time; keep the laptop on mains power.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
echo.
pause

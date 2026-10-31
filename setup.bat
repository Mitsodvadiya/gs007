@echo off
title GastroAGI Chunk Uploader setup
cd /d "%~dp0program"
python --version >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install Python 3.10 or newer from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Setup failed. Read the error above.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create-shortcut.ps1"
echo.
echo Setup finished. Next, follow the remaining steps in SETUP.md.
pause

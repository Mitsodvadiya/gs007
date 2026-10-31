@echo off
title GastroAGI Chunk Uploader
cd /d "%~dp0program"
netstat -ano | findstr /r /c:"127\.0\.0\.1:8765 .*LISTENING" >nul
if not errorlevel 1 (
  echo GastroAGI Chunk Uploader is already running. You can close this window.
  echo Click the extension icon in Chrome to use it.
  pause
  exit /b 0
)
echo Starting GastroAGI Chunk Uploader...
echo.
echo When it says "running", click the extension icon in Chrome.
echo Keep this window open. Closing it stops the program.
echo.
python server.py
pause

@echo off
rem ============================================================
rem  Knowledge Base Dashboard - browse your recorded days and
rem  search them by meaning. A browser tab opens automatically.
rem  Keep this window open while using it; close it (or press
rem  Ctrl+C) to stop the dashboard.
rem ============================================================
title Knowledge Base Dashboard
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo Starting the dashboard...
echo A browser tab will open automatically.
echo Keep this window open while you browse.
echo Close this window (or press Ctrl+C) to stop.
echo.
python.exe "%~dp0serve.py"

echo.
echo Dashboard stopped.
pause

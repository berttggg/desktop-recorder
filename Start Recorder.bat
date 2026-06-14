@echo off
rem ============================================================
rem  Desktop Recorder - records your screen + audio, then makes
rem  an AI summary of what you did. Double-click to open the app.
rem ============================================================
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem Pre-flight check so a problem shows a clear message instead of
rem failing silently behind the windowless GUI launch.
python.exe -c "import recorder_app" 2>"%TEMP%\recorder_launch_error.txt"
if errorlevel 1 goto failed

rem All good - launch the GUI with no console window.
start "" pythonw.exe "%~dp0recorder_app.py"
exit /b 0

:failed
echo.
echo Could not start the recorder. Details below:
echo ------------------------------------------------------------
type "%TEMP%\recorder_launch_error.txt"
echo ------------------------------------------------------------
echo.
echo Tip: make sure Python is installed and these packages are present:
echo   pip install faster-whisper pyaudiowpatch fastembed anthropic numpy
echo.
pause
exit /b 1

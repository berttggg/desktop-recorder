@echo off
rem ============================================================
rem  Stop Desktop Recorder from starting at Windows login.
rem ============================================================
set "VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\DesktopRecorder.vbs"
if exist "%VBS%" (
  del "%VBS%"
  echo Removed. Desktop Recorder will no longer start at login.
) else (
  echo Nothing to remove - auto-start was not enabled.
)
echo.
pause

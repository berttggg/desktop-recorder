@echo off
rem ============================================================
rem  Make Desktop Recorder start automatically at Windows login.
rem  Drops a tiny launcher in your Startup folder that opens the
rem  recorder GUI hidden (no console window). Safe to re-run.
rem  Run "Uninstall Autostart.bat" to turn this off.
rem ============================================================
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS=%STARTUP%\DesktopRecorder.vbs"
> "%VBS%" echo CreateObject("WScript.Shell").Run """%~dp0Start Recorder.bat"" --minimized", 0, False
if exist "%VBS%" (
  echo Done. Desktop Recorder will now start automatically when you log in.
  echo Launcher created at:
  echo   "%VBS%"
) else (
  echo Failed to create the startup launcher. Check folder permissions.
)
echo.
pause

@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "VCS_ISO=%~1"
if not defined VCS_ISO (
    echo Enter path to GTA Vice City Stories PS2 US ISO.
    set /p "VCS_ISO=ISO path: "
)
if not exist "%VCS_ISO%" (
    echo ERROR: ISO file not found: "%VCS_ISO%"
    pause
    exit /b 2
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0verify_vcs_us_iso.py" "%VCS_ISO%"
) else (
    python "%~dp0verify_vcs_us_iso.py" "%VCS_ISO%"
)
set "CHECK_STATUS=%ERRORLEVEL%"
echo.
if "%CHECK_STATUS%"=="0" (
  echo Completed: no structural anomalies reported.
) else if "%CHECK_STATUS%"=="1" (
  echo Completed WITH ANOMALIES: review report before patching.
) else (
  echo Verification failed to complete. Code %CHECK_STATUS%.
)
echo Report files are written to this helper folder, not to the ISO.
pause
exit /b %CHECK_STATUS%

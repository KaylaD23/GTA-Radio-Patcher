@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "LCS_ISO=%~1"
if not defined LCS_ISO (
    echo Enter the full path to your ORIGINAL, unmodified US GTA LCS PS2 ISO.
    set /p "LCS_ISO=ISO path: "
)
if not exist "%LCS_ISO%" (
    echo ERROR: ISO not found: "%LCS_ISO%"
    pause
    exit /b 2
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 run_lcs_retail_iso_tests.py "%LCS_ISO%"
) else (
    python run_lcs_retail_iso_tests.py "%LCS_ISO%"
)
set "TEST_CODE=%ERRORLEVEL%"
echo.
if "%TEST_CODE%"=="0" (echo Retail ISO checks PASSED.) else (echo Test did not pass. Exit code: %TEST_CODE%)
pause
exit /b %TEST_CODE%

@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 gta_radio_patcher_gui.py
) else (
    python gta_radio_patcher_gui.py
)
if errorlevel 1 (
    echo GTA Radio Patcher could not start. Install Python 3 with Tkinter.
    pause
)
endlocal

@echo off
rem  Same thing, with the log on screen. Use this one when something is wrong.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Erebus is not installed yet in this folder.
    echo Run:  python install.py
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m erebus %*
echo.
echo Erebus has stopped. Press any key to close.
pause >nul

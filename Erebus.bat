@echo off
rem  Double-click to start Erebus with no console window.
rem
rem  %~dp0 is this file's own folder, so the shortcut works from anywhere -
rem  the desktop, the taskbar, the Start menu - rather than only from a shell
rem  already sitting in the repository.
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Erebus is not installed yet in this folder.
    echo Run:  python install.py
    pause
    exit /b 1
)

rem  pythonw, not python: no console window at all. Use "Erebus (console).bat"
rem  when you want to watch the log.
start "Erebus" ".venv\Scripts\pythonw.exe" -m erebus

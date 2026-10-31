@echo off
setlocal
cd /d "%~dp0"
set "PYTHONDONTWRITEBYTECODE=1"
set "TEMP=%~dp0data\tmp\runtime"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%"
".venv\Scripts\python.exe" -B -X utf8 tools\open_ui.py --port 8773
if errorlevel 1 pause

@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
setlocal
cd /d "%~dp0"
set "TEMP=%~dp0data\tmp\runtime"
set "TMP=%TEMP%"
set "TMPDIR=%TEMP%"
set "MEDIA_DATA_DIR=%~dp0data"
set "PIP_CACHE_DIR=%~dp0data\cache\pip"
set "PYTHONDONTWRITEBYTECODE=1"
if not exist "%TEMP%" mkdir "%TEMP%"
if not exist ".venv\Scripts\python.exe" (
  echo First run setup.bat to prepare the Python environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B -X utf8 -m framekeep --port 8773 --open %*
if errorlevel 1 pause

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
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or newer is required. Install Python and enable Add to PATH.
  pause
  exit /b 1
)
python -B -X utf8 tools\bootstrap.py %*
if errorlevel 1 (
  pause
  exit /b 1
)
echo Setup complete. Double-click start.bat to open the app.
pause

# SPDX-License-Identifier: GPL-3.0-or-later
$ErrorActionPreference = 'Stop'
$env:TEMP = Join-Path $PSScriptRoot 'data\tmp\runtime'
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:MEDIA_DATA_DIR = Join-Path $PSScriptRoot 'data'
$env:PIP_CACHE_DIR = Join-Path $PSScriptRoot 'data\cache\pip'
$env:PYTHONDONTWRITEBYTECODE = '1'
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
Push-Location -LiteralPath $PSScriptRoot
try {
    $framekeepPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $framekeepPython)) {
        throw 'Run python tools/bootstrap.py first to prepare the Python environment.'
    }
    & $framekeepPython -B -X utf8 -m framekeep --port 8773 --open @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

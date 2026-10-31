#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ ! -x .venv/bin/python ]; then
    echo 'Run python3 tools/bootstrap.py first to prepare the Python environment.' >&2
    exit 1
fi
exec .venv/bin/python -m framekeep --port 8765 --open "$@"

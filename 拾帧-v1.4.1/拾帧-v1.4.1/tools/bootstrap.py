# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare an isolated Python environment without copying dependency source."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

# Configure before venv/pip or any third-party module can allocate temp files.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from framekeep.storage import configure_storage
configure_storage()
import venv


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Framekeep's local runtime.")
    parser.add_argument("--ffmpeg", type=Path, help="Path to ffmpeg executable")
    parser.add_argument("--ffprobe", type=Path, help="Path to ffprobe executable")
    parser.add_argument("--node", type=Path, help="Optional Node.js executable")
    parser.add_argument("--with-ejs", action="store_true", help="Install yt-dlp's default dependencies for additional website support")
    parser.add_argument("--with-browser", action="store_true", help="Enable browser discovery using locally installed Chrome or Edge")
    args = parser.parse_args()

    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required.")

    root = Path(__file__).resolve().parent.parent
    config_path = root / "runtime.local.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else {}
    for key in ("ffmpeg", "ffprobe", "node"):
        supplied = getattr(args, key)
        if supplied is not None:
            supplied = supplied.expanduser().resolve()
            if not supplied.is_file():
                parser.error(f"{key} executable does not exist: {supplied}")
            config[key] = str(supplied)

    env_dir = root / ".venv"
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("Creating isolated Python environment...", flush=True)
        venv.EnvBuilder(with_pip=True).create(env_dir)
    print("Installing the website adapter from PyPI...", flush=True)
    subprocess.run([str(python), "-m", "pip", "install", "--no-deps", "-r", str(root / "requirements.txt")], check=True)
    if args.with_ejs:
        subprocess.run([str(python), "-m", "pip", "install", "yt-dlp[default]==2026.8.19"], check=True)
    if args.with_browser:
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(root / "requirements-browser.txt")], check=True)

    if config:
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Ready. Run start.bat (Windows) or sh run.sh (macOS/Linux).")
    print("FFmpeg and ffprobe must be installed separately; see README.md.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

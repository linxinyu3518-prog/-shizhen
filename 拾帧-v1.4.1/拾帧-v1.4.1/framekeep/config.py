# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit runtime discovery; no registry edits or global package changes."""
import importlib.util
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .storage import default_data_dir, storage_paths

ROOT = Path(__file__).resolve().parent.parent
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def resolve_binary(name, local):
    configured = os.environ.get("MEDIA_" + name.upper()) or local.get(name)
    if configured:
        return shutil.which(str(configured)) or (str(Path(configured).resolve()) if Path(configured).is_file() else None)
    bundled = ROOT / "tools" / "bin" / (name + (".exe" if os.name == "nt" else ""))
    return str(bundled) if bundled.is_file() else shutil.which(name)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=default_data_dir)
    ffmpeg: str | None = None
    ffprobe: str | None = None
    node: str | None = None
    browser: str | None = None
    playwright: bool = False
    yt_dlp: bool = False
    h264: bool = False
    aac: bool = False
    interpolation: bool = False
    rtx_python: str | None = None
    nvenc: bool = False
    interpolation_python: str | None = None
    interpolation_root: str | None = None
    interpolation_model: str | None = None
    max_download_bytes: int = 10 * 1024**3
    demo_url: str | None = None

    @classmethod
    def discover(cls, *, data_dir=None):
        paths = storage_paths(default_data_dir() if data_dir is None else data_dir)
        local_file = ROOT / "runtime.local.json"
        local = json.loads(local_file.read_text(encoding="utf-8-sig")) if local_file.is_file() else {}
        settings = cls(data_dir=Path(paths["data"]), ffmpeg=resolve_binary("ffmpeg", local), ffprobe=resolve_binary("ffprobe", local), node=resolve_binary("node", local),
                       yt_dlp=importlib.util.find_spec("yt_dlp") is not None)
        settings.playwright = importlib.util.find_spec("playwright") is not None
        settings.rtx_python = resolve_binary("rtx_python", local)
        settings.interpolation_python = resolve_binary("interpolation_python", local)
        settings.interpolation_root = local.get("interpolation_root")
        settings.interpolation_model = local.get("interpolation_model")
        settings.browser = resolve_binary("browser", local)
        if not settings.browser:
            candidates = [shutil.which(name) for name in ("google-chrome", "chromium", "chromium-browser", "msedge")]
            if os.name == "nt":
                for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
                    if base:
                        candidates += [str(Path(base) / "Google/Chrome/Application/chrome.exe"), str(Path(base) / "Microsoft/Edge/Application/msedge.exe")]
            settings.browser = next((path for path in candidates if path and Path(path).is_file()), None)
        if settings.ffmpeg:
            try:
                result = subprocess.run([settings.ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                                        text=True, timeout=10, creationflags=NO_WINDOW)
                settings.h264 = "libx264 " in result.stdout
                settings.aac = " aac " in result.stdout
                if "h264_nvenc " in result.stdout:
                    # Encoder listing alone does not establish driver/device
                    # compatibility. Keep this optional probe independent of
                    # the baseline software encoder checks.
                    try:
                        check = subprocess.run(
                            [settings.ffmpeg, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=s=320x180:r=30",
                             "-frames:v", "1", "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
                             "-rc", "vbr", "-cq", "16", "-b:v", "0", "-f", "null", "-"],
                            capture_output=True, timeout=15, creationflags=NO_WINDOW)
                        settings.nvenc = check.returncode == 0
                    except (OSError, subprocess.SubprocessError):
                        settings.nvenc = False
                filters = subprocess.run([settings.ffmpeg, "-hide_banner", "-filters"], capture_output=True,
                                         text=True, timeout=10, creationflags=NO_WINDOW)
                names = {line.split()[1] for line in filters.stdout.splitlines() if len(line.split()) >= 2}
                settings.interpolation = {"minterpolate", "tpad", "trim", "fps"}.issubset(names)
            except (OSError, subprocess.SubprocessError):
                settings.ffmpeg = None
        if settings.ffprobe:
            try:
                subprocess.run([settings.ffprobe, "-version"], capture_output=True, check=True,
                               timeout=10, creationflags=NO_WINDOW)
            except (OSError, subprocess.SubprocessError):
                settings.ffprobe = None
        for folder in ("downloads", "tmp", "demo"):
            (settings.data_dir / folder).mkdir(parents=True, exist_ok=True)
        return settings

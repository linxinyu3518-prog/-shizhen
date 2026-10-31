"""Launch the local UI with its own profile and download directory."""
import json
import os
import shutil
import subprocess
from pathlib import Path

from .storage import configure_storage, storage_paths


def build_browser_command(browser, data_dir, url):
    paths = storage_paths(data_dir)
    profile = Path(paths["browser_profile"])
    preferences = profile / "Default" / "Preferences"
    if not preferences.resolve().is_relative_to(Path(paths["data"])):
        raise RuntimeError("浏览器配置文件不能链接到数据目录之外。")
    downloads = Path(paths["browser_downloads"])
    disk_cache = Path(paths["cache"]) / "browser"
    crashes = Path(paths["cache"]) / "browser-crashes"
    for directory in (preferences.parent, downloads, disk_cache, crashes):
        if not directory.resolve().is_relative_to(Path(paths["data"])):
            raise RuntimeError("浏览器工作目录不能链接到数据目录之外。")
        directory.mkdir(parents=True, exist_ok=True)
    # Only seed a new profile. Never rewrite a live browser's preferences or
    # touch the user's daily browser profile.
    if not preferences.exists():
        initial = {"download": {"default_directory": str(downloads), "prompt_for_download": False,
                                 "directory_upgrade": True},
                   "savefile": {"default_directory": str(downloads)}}
        try:
            with preferences.open("x", encoding="utf-8") as target:
                json.dump(initial, target, ensure_ascii=False)
        except FileExistsError:
            pass  # A simultaneous launcher already created this profile.
    return [str(browser), f"--user-data-dir={profile}", f"--disk-cache-dir={disk_cache}",
            f"--crash-dumps-dir={crashes}", "--no-first-run", "--no-default-browser-check",
            "--disable-background-mode", "--new-window", url]


def desktop_binary(configured=None):
    candidates = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
            if base:
                candidates.append(str(Path(base) / "Microsoft/Edge/Application/msedge.exe"))
    candidates += [configured, *(shutil.which(name) for name in ("msedge", "chromium", "google-chrome"))]
    return next((value for value in candidates if value and Path(value).is_file()), None)


def open_page(url, data_dir, configured=None):
    configure_storage(data_dir)
    browser = desktop_binary(configured)
    if not browser:
        print(f"未找到受控浏览器，请安装 Edge 或配置 browser；本地页面：{url}", flush=True)
        return False
    try:
        subprocess.Popen(build_browser_command(browser, data_dir, url), cwd=Path(data_dir).parent,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError:
        print(f"无法启动独立浏览器，请检查安装；本地页面：{url}", flush=True)
        return False
    return True

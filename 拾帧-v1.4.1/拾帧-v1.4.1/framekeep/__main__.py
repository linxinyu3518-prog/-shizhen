# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import json
import threading
import urllib.request

from .config import Settings
from .server import create_server
from .desktop_browser import open_page
from .storage import configure_storage


def open_existing(port, settings):
    """A repeated launch opens this application's existing local instance."""
    url = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url + "/api/health", timeout=2) as response:
            if not response.headers.get("Server", "").startswith("Framekeep/"):
                return False
            health = json.load(response)
        if not health.get("version") or not health.get("token"):
            return False
        return open_page(url, settings.data_dir, settings.browser)
    except (OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description="拾帧 · 本地视频与音频提取工具")
    parser.add_argument("--port", type=int, default=8765, help="本地端口，默认 8765")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0 到 65535 之间")
    configure_storage()
    settings = Settings.discover()
    try:
        server = create_server(settings, args.port)
    except OSError as exc:
        if args.open and open_existing(args.port, settings):
            print("拾帧已在运行，已打开本地页面。", flush=True)
            return
        parser.exit(1, f"启动失败：端口可能已被占用，请使用 --port 指定其他端口。\n{exc}\n")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"拾帧已启动：{url}\n下载保存位置：{settings.data_dir / 'downloads'}\n按 Ctrl+C 停止。", flush=True)
    if args.open:
        threading.Timer(0.6, lambda: open_page(url, settings.data_dir, settings.browser)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n正在停止任务并清理临时文件……", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

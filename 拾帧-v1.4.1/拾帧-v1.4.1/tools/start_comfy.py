# SPDX-License-Identifier: GPL-3.0-or-later
"""Start the separately installed ComfyUI with only built-in local nodes."""
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import ProxyHandler, build_opener

root = Path(__file__).resolve().parents[1]
config_path = root / "runtime.local.json"
try:
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    python = Path(config.get("comfyui_python") or config.get("rtx_python") or "")
    comfy = Path(config.get("comfyui_root") or "")
    if not python.is_file() or not (comfy / "main.py").is_file():
        raise ValueError("请在 runtime.local.json 配置 comfyui_python 和 comfyui_root，指向独立的 ComfyUI 安装。")
    # The launcher only uses the fixed local port; it never installs dependencies.
    try:
        with build_opener(ProxyHandler({})).open("http://127.0.0.1:8188/system_stats", timeout=2) as response:
            status = json.loads(response.read(1024 * 1024))
        if isinstance(status.get("system"), dict):
            print("ComfyUI 本地接口已运行：http://127.0.0.1:8188")
            raise SystemExit(0)
    except OSError:
        pass
    logs = root / "data" / "comfy-logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "stdout.log").open("ab") as output, (logs / "stderr.log").open("ab") as errors:
        process = subprocess.Popen([str(python.resolve()), str((comfy / "main.py").resolve()), "--listen", "127.0.0.1", "--port", "8188",
                                    "--disable-all-custom-nodes", "--disable-api-nodes", "--disable-auto-launch"], cwd=comfy,
                                   stdout=output, stderr=errors, stdin=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    print(f"已启动 ComfyUI 本地接口，进程 {process.pid}。首次启动可能较慢，请在拾帧 AI 接口设置中检测。")
    print("本次只加载基础节点；日志在 data/comfy-logs。现有安装与依赖没有改动。")
except (OSError, ValueError) as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)

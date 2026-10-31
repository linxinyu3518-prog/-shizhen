"""Open FrameKeep using an E: profile and download folder without restarting it."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from framekeep.storage import configure_storage
from framekeep.desktop_browser import open_page

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    paths = configure_storage()
    config_file = ROOT / "runtime.local.json"
    config = json.loads(config_file.read_text(encoding="utf-8-sig")) if config_file.is_file() else {}
    return 0 if open_page(f"http://127.0.0.1:{args.port}/", paths["data"], config.get("browser")) else 1

if __name__ == "__main__":
    raise SystemExit(main())

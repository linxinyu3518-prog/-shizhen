"""Keep application data and inherited worker caches beside this installation."""
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def default_data_dir():
    directory = (ROOT / "data").resolve()
    if not directory.is_relative_to(ROOT.resolve()):
        raise RuntimeError("项目 data 目录不能链接到项目目录之外。")
    return directory


def storage_paths(data_dir):
    root = Path(data_dir).resolve()
    if os.name == "nt" and root.drive.upper() == "C:":
        raise RuntimeError("拾帧工作数据不能保存到 C 盘，请从 E 盘项目目录启动。")
    paths = {"data": root, "downloads": root / "downloads", "temporary": root / "tmp" / "runtime",
             "cache": root / "cache", "browser_profile": root / "browser" / "ui",
             "browser_downloads": root / "downloads" / "browser"}
    for name, path in paths.items():
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise RuntimeError(f"工作目录 {name} 不能链接到数据目录之外。")
        paths[name] = str(resolved)
    return paths


def worker_environment(data_dir=None):
    paths = storage_paths(default_data_dir() if data_dir is None else data_dir)
    for path in paths.values():
        Path(path).mkdir(parents=True, exist_ok=True)
    cache = Path(paths["cache"])
    environment = dict(os.environ)
    cache_dirs = {
        "XDG_CACHE_HOME": "xdg", "TORCH_HOME": "torch", "TORCH_EXTENSIONS_DIR": "torch-extensions",
        "TORCHINDUCTOR_CACHE_DIR": "torch-inductor", "CUDA_CACHE_PATH": "cuda",
        "TRITON_CACHE_DIR": "triton", "TRITON_DUMP_DIR": "triton-dump", "TRITON_OVERRIDE_DIR": "triton-override",
        "HF_HOME": "huggingface", "HF_HUB_CACHE": "huggingface/hub",
        "HUGGINGFACE_HUB_CACHE": "huggingface/hub", "TRANSFORMERS_CACHE": "huggingface/transformers",
        "MPLCONFIGDIR": "matplotlib", "PIP_CACHE_DIR": "pip", "PLAYWRIGHT_BROWSERS_PATH": "playwright",
        "NUMBA_CACHE_DIR": "numba", "PYTHONPYCACHEPREFIX": "pycache",
    }
    for key, relative in cache_dirs.items():
        destination = (cache / relative).resolve()
        if not destination.is_relative_to(cache):
            raise RuntimeError(f"缓存目录 {key} 不能链接到数据目录之外。")
        destination.mkdir(parents=True, exist_ok=True)
        environment[key] = str(destination)
    for name in ("TEMP", "TMP", "TMPDIR"):
        environment[name] = paths["temporary"]
    environment["MEDIA_DATA_DIR"] = paths["data"]
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def configure_storage(data_dir=None):
    paths = storage_paths(default_data_dir() if data_dir is None else data_dir)
    os.environ.update(worker_environment(data_dir))
    sys.dont_write_bytecode = True
    tempfile.tempdir = paths["temporary"]
    return paths

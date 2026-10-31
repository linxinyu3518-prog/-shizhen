# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated AI providers and timestamped frames; no Torch imports in the app."""
import json
import math
import queue
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path

from .config import NO_WINDOW
from .media import number
from .network import Cancelled, MediaError

MAX_FRAMES = 18000
MAX_TEMP_BYTES = 20 * 1024**3
MAX_PIXELS = 16_777_216


def target_size(metadata, height):
    width, source_height = metadata.get("width"), metadata.get("height")
    if not width or not source_height or type(height) is not int or height not in (360, 480, 720, 1080, 1440, 2160):
        raise MediaError("AI 超分需要可读取的来源尺寸和明确的输出分辨率。")
    sar = 1.0
    try:
        a, b = metadata.get("sample_aspect_ratio", "1:1").split(":")
        sar = float(a) / float(b)
    except (AttributeError, ValueError, ZeroDivisionError):
        pass
    if not math.isfinite(sar) or sar <= 0:
        sar = 1.0
    result = (max(2, round(width * sar * height / source_height / 2) * 2), height)
    if result[0] * result[1] > MAX_PIXELS or result[0] > 8192:
        raise MediaError("目标画面超过本地 AI 超分尺寸限制，请降低分辨率。")
    return result


def png_size(path):
    with Path(path).open("rb") as image:
        header = image.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise MediaError("超分接口没有返回有效的 PNG 图像。")
    return struct.unpack(">II", header[16:24])


def run_bounded(command, folder, cancel, name, timeout=300, monitor=None):
    log = folder / (name + ".log")
    output = folder / (name + ".json")
    with log.open("wb") as errors, output.open("wb") as stdout:
        process = subprocess.Popen(command, stdout=stdout, stderr=errors, creationflags=NO_WINDOW)
        began = time.monotonic()
        try:
            while process.poll() is None:
                if cancel.wait(.15):
                    raise Cancelled("任务已取消。")
                if time.monotonic() - began > timeout:
                    raise MediaError("AI 预处理超过时间限制，请缩短视频后重试。")
                if output.stat().st_size > 32 * 1024**2:
                    raise MediaError("来源帧信息超过本地处理限制。")
                if monitor:
                    monitor()
            if process.returncode:
                raise MediaError("AI 预处理失败：" + log.read_text(encoding="utf-8", errors="replace")[-1200:])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    return output


class RTXProcess:
    """Fixed worker protocol, in a separately configured Python environment."""
    def __init__(self, python, folder):
        self.log = (folder / "rtx-worker.log").open("wb")
        try:
            self.process = subprocess.Popen([python, "-u", str(Path(__file__).with_name("rtx_worker.py")), "--serve"],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                            text=True, encoding="utf-8", creationflags=NO_WINDOW)
        except OSError as exc:
            self.log.close()
            raise MediaError("无法启动 RTX 独立运行环境，请检查配置的 Python 路径。") from exc
        self.messages = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    self.messages.put(value)
            except ValueError:
                pass

    def upscale(self, source, output, width, height, cancel):
        try:
            self.process.stdin.write(json.dumps({"input": str(source), "output": str(output), "width": width, "height": height}) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MediaError("RTX 超分进程已退出，请检查显存和运行库。") from exc
        began = time.monotonic()
        while True:
            if cancel.is_set():
                raise Cancelled("任务已取消。")
            if time.monotonic() - began > 120:
                raise MediaError("RTX 单帧处理超过 120 秒，请降低分辨率或检查显卡占用。")
            try:
                result = self.messages.get(timeout=.1)
                if not result.get("ok"):
                    raise MediaError("RTX 超分失败：" + str(result.get("reason") or result.get("error") or "运行库错误")[:1000])
                return result
            except queue.Empty:
                if self.process.poll() is not None:
                    raise MediaError("RTX 超分进程意外退出，请检查 NVIDIA 驱动、显存与运行库。")

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        self.process.stdin.close()
        self.reader.join(2)
        self.process.stdout.close()
        self.log.close()


class UpscaleService:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.slot = threading.BoundedSemaphore(1)
        self.path = settings.data_dir / "upscale.json"
        self.config = {"comfyui_url": "http://127.0.0.1:8188", "comfyui_model": ""}
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self.config.update({k: stored[k] for k in self.config if isinstance(stored.get(k), str)})
        except (OSError, ValueError):
            pass
        self.cached = None
        self.cached_at = 0
        self.rtx_cached = None
        self.rtx_cached_at = 0

    def status(self, refresh=False):
        from .comfy_client import ComfyClient
        with self.lock:
            if not refresh and self.cached and time.monotonic() - self.cached_at < 120:
                return self.cached
            rtx = {"id": "rtx", "name": "NVIDIA RTX 超分", "available": False,
                   "reason": "尚未配置独立 RTX 运行环境（runtime.local.json 的 rtx_python）。"}
            if self.rtx_cached and time.monotonic() - self.rtx_cached_at < (600 if self.rtx_cached["available"] else 30):
                rtx = dict(self.rtx_cached)
            elif self.settings.rtx_python and self.slot.acquire(blocking=False):
                try:
                    completed = subprocess.run([self.settings.rtx_python, str(Path(__file__).with_name("rtx_worker.py")), "--probe"],
                                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                                               creationflags=NO_WINDOW)
                    records = []
                    for line in completed.stdout.splitlines():
                        try:
                            records.append(json.loads(line))
                        except ValueError:
                            pass
                    report = next((r for r in reversed(records) if isinstance(r, dict) and "available" in r), {})
                    rtx.update(available=completed.returncode == 0 and report.get("available") is True,
                               reason=str(report.get("reason", "RTX 运行库探测失败，请检查环境。")))
                except (OSError, subprocess.SubprocessError):
                    rtx["reason"] = "RTX 独立环境无法启动或探测超时。"
                finally:
                    self.slot.release()
                self.rtx_cached, self.rtx_cached_at = dict(rtx), time.monotonic()
            try:
                comfy = ComfyClient(self.config["comfyui_url"]).inspect()
            except MediaError as exc:
                comfy = {"available": False, "reason": str(exc), "models": []}
            models = comfy.get("models", [])
            if not self.config["comfyui_model"] and models:
                self.config["comfyui_model"] = "4x-UltraSharp.pth" if "4x-UltraSharp.pth" in models else models[0]
            self.cached = {**self.config, "protocol_version": 1, "models": models,
                           "providers": [rtx, {"id": "comfyui", "name": "本地 ComfyUI", "available": bool(comfy.get("available")),
                                               "reason": str(comfy.get("reason", ""))}]}
            self.cached_at = time.monotonic()
            return self.cached

    def configure(self, payload):
        from .comfy_client import ComfyClient
        url, model = payload.get("comfyui_url"), payload.get("comfyui_model", "")
        if not isinstance(url, str) or not isinstance(model, str) or len(model) > 240 or any(ord(c) < 32 for c in model):
            raise MediaError("请输入有效的本地地址和模型名称。")
        client = ComfyClient(url)
        with self.lock:
            self.config = {"comfyui_url": client.url, "comfyui_model": model}
            self.cached = None
            temporary = self.path.with_suffix(".part")
            temporary.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        return self.status(refresh=True)

    def select(self, options, metadata):
        requested = options.get("upscale", "none")
        if requested == "none" or (metadata.get("height") or 0) >= options["height"]:
            return None
        status = self.status()
        ready = {p["id"]: p for p in status["providers"]}
        provider = next((p for p in ("rtx", "comfyui") if ready[p]["available"]), None) if requested == "auto" else requested
        if provider not in ready or not ready[provider]["available"]:
            reason = ready.get(provider, {}).get("reason", "RTX 与本地 ComfyUI 均未就绪，请打开 AI 接口设置。")
            raise MediaError("AI 超分不可用：" + reason, code="upscale_unavailable")
        if provider == "comfyui" and status["comfyui_model"] not in status["models"]:
            raise MediaError("请在 AI 接口设置中选择 ComfyUI 实际安装的超分模型。")
        return {"provider": provider, "model": "NVIDIA RTX Video" if provider == "rtx" else status["comfyui_model"],
                "url": status["comfyui_url"], "rtx_python": self.settings.rtx_python}

    def prepare(self, source, options, metadata, folder, cancel, progress, selection):
        """Decode once, upscale each original frame, keep original timestamps/audio."""
        from .comfy_client import ComfyClient
        width, height = target_size(metadata, options["height"])
        if options["height"] <= metadata["height"]:
            return None, {"upscale_provider": "none", "upscale_note": "目标分辨率不高于来源，未执行 AI 超分。"}
        if not selection:
            raise MediaError("AI 提供者未就绪，请重新配置并创建任务。")
        if metadata.get("color_transfer") in ("smpte2084", "arib-std-b67"):
            raise MediaError("当前 AI 链路输出 SDR 8-bit；HDR 素材请先做正确色调映射，或关闭 AI 超分。", code="upscale_hdr_unsupported")
        if (metadata.get("rotation") or 0) % 360:
            raise MediaError("来源带有旋转显示标记，请先将画面旋转固化后再执行 AI 超分。", code="upscale_rotation_unsupported")
        if metadata.get("frame_count", 0) and metadata["frame_count"] > MAX_FRAMES:
            raise MediaError("单次 AI 超分最多处理 18,000 个来源画面，请先拆分视频。")
        if metadata.get("duration", 0) and metadata["duration"] > 1800:
            raise MediaError("单次 AI 超分最多处理 30 分钟来源，请先拆分视频。")
        original = folder / "frames-in"
        enhanced = folder / "frames-out"
        original.mkdir()
        enhanced.mkdir()
        last_disk_check = [0.0]
        def disk_guard(size=None):
            if size is None:
                if time.monotonic() - last_disk_check[0] < 1:
                    return
                last_disk_check[0] = time.monotonic()
                size = sum(p.stat().st_size for directory in (original, enhanced) for p in directory.glob("*.png"))
            if size > MAX_TEMP_BYTES or shutil.disk_usage(folder).free < 512 * 1024**2:
                raise MediaError("AI 临时帧超过 20 GiB 或磁盘剩余不足 512 MiB，请缩短视频或降低分辨率。")
        timing = run_bounded([self.settings.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "v:0",
                              "-show_frames", "-show_entries", "frame=best_effort_timestamp_time,pkt_duration_time", "-of", "json", str(source)],
                             folder, cancel, "ai-timing")
        frames = json.loads(timing.read_text(encoding="utf-8"))["frames"]
        if not frames or len(frames) > MAX_FRAMES:
            raise MediaError("来源画面为空或超过 18,000 帧的 AI 处理限制。")
        times = [number(frame.get("best_effort_timestamp_time")) for frame in frames]
        if any(t is None for t in times) or any(b <= a for a, b in zip(times, times[1:])):
            raise MediaError("来源时间戳缺失或不连续，无法保证 AI 导出的音画同步。")
        tail = number(metadata.get("video_duration"), 0) - (times[-1] - times[0])
        if tail <= 0:
            tail = number(frames[-1].get("pkt_duration_time")) or (1 / metadata["fps"] if metadata.get("fps") else None)
        if not tail or tail <= 0:
            raise MediaError("无法确定最后一帧时长，停止 AI 处理以避免截短视频。")
        # FFmpeg adjusts non-square sample pixels before the image-only providers.
        command = [self.settings.ffmpeg, "-v", "error", "-nostdin", "-y", "-noautorotate", "-protocol_whitelist", "file,pipe", "-i", str(source),
                   "-map", "0:v:0", "-an", "-vf", "scale=trunc(iw*sar/2)*2:trunc(ih/2)*2,setsar=1", "-fps_mode", "passthrough",
                   "-frames:v", str(len(frames)), "-threads", "2", str(original / "%08d.png")]
        progress(56, "正在为 AI 超分读取原始画面")
        run_bounded(command, folder, cancel, "ai-decode", monitor=disk_guard)
        inputs = sorted(original.glob("*.png"))
        if len(inputs) != len(frames):
            raise MediaError("解码帧数与时间戳数量不一致，停止 AI 处理。")
        temporary_bytes = sum(path.stat().st_size for path in inputs)
        worker = RTXProcess(selection["rtx_python"], folder) if selection["provider"] == "rtx" else None
        client = ComfyClient(selection["url"]) if selection["provider"] == "comfyui" else None
        began = time.monotonic()
        actual_model, maximum_passes = selection["model"], 1
        manifest = enhanced / "frames.ffconcat"
        try:
            with manifest.open("w", encoding="utf-8", newline="\n") as listing:
                listing.write("ffconcat version 1.0\n")
                for index, input_path in enumerate(inputs):
                    if cancel.is_set():
                        raise Cancelled("任务已取消。")
                    if time.monotonic() - began > 4 * 3600:
                        raise MediaError("AI 超分超过 4 小时限制，请拆分视频。")
                    output = enhanced / input_path.name
                    if worker:
                        frame_result = worker.upscale(input_path, output, width, height, cancel)
                        if isinstance(frame_result, dict):
                            actual_model = str(frame_result.get("model") or actual_model)
                            maximum_passes = max(maximum_passes, int(frame_result.get("passes", 1)))
                    else:
                        frame_result = client.upscale(input_path, output, selection["model"], width, height, cancel, folder.name)
                        if isinstance(frame_result, dict):
                            maximum_passes = max(maximum_passes, int(frame_result.get("passes", 1)))
                    if png_size(output) != (width, height):
                        raise MediaError("AI 返回的实际尺寸与目标不符，未将普通放大冒充超分结果。")
                    duration = times[index + 1] - times[index] if index + 1 < len(times) else tail
                    listing.write(f"file '{output.name}'\noption framerate 1000000\nduration {duration:.9f}\n")
                    temporary_bytes += output.stat().st_size - input_path.stat().st_size
                    input_path.unlink()
                    disk_guard(temporary_bytes)
                    progress(57 + 26 * (index + 1) / len(inputs), f"{selection['model']} 超分：{index + 1} / {len(inputs)} 帧")
                # Sentinel supplies the final duration; the encoder trims it away.
                listing.write(f"file '{inputs[-1].name}'\noption framerate 1000000\n")
        finally:
            if worker:
                worker.close()
        report = {"upscale_provider": selection["provider"], "upscale_model": actual_model, "upscale_max_passes": maximum_passes,
                  "upscale_width": width, "upscale_height": height, "source_width": metadata["width"], "source_height": metadata["height"],
                  "upscale_note": "已逐帧执行 AI 超分，再按原始时间戳编码；音轨取自来源。", "upscale_frames": len(frames)}
        return {"manifest": manifest, "width": width, "height": height}, report

# SPDX-License-Identifier: GPL-3.0-or-later
"""GPU capability selection and supervision of the bounded video pipeline.

Torch stays in an external process. Only timestamps and control messages cross
this boundary; decoded/output images flow directly through bounded worker pipes.
"""
import json
import math
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from .config import NO_WINDOW
from .media import build_encode_command, number
from .network import Cancelled, MediaError
from .stream_timing import (BoundedErrorLog, GPU_TIMEOUT_SECONDS, MAX_DURATION_SECONDS,
                            MAX_OUTPUT_FRAMES, MAX_SOURCE_FRAMES, STALL_TIMEOUT_SECONDS,
                            read_pipe_lines, scan_timestamps, validate_limits)


def worker_command(settings, *arguments):
    return [settings.interpolation_python, "-B", "-u",
            str(Path(__file__).with_name("interpolation_worker.py")),
            "--root", str(settings.interpolation_root), "--model", str(settings.interpolation_model), *arguments]


def output_size(metadata, options, prepared=None):
    if prepared:
        return int(prepared["width"]), int(prepared["height"])
    width, height = int(metadata.get("width") or 0), int(metadata.get("height") or 0)
    if min(width, height) <= 0:
        raise MediaError("无法确定视频画面尺寸。")
    sar = 1.0
    try:
        a, b = str(metadata.get("sample_aspect_ratio")).split(":")
        sar = float(a) / float(b)
    except (ValueError, ZeroDivisionError):
        pass
    if not math.isfinite(sar) or sar <= 0:
        sar = 1.0
    target = min(height, options.get("height") or height)
    target = max(2, target // 2 * 2)
    return max(2, int(width * sar * target / height / 2) * 2), target


def stop_worker_tree(process):
    """Kill this live task tree, including FFmpeg children blocked on a pipe."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


class ComputeService:
    def __init__(self, settings, slot=None):
        self.settings = settings
        self.slot = slot if slot is not None else threading.BoundedSemaphore(1)
        self.lock = threading.RLock()
        self.cached = None
        self.cached_at = 0.0

    def status(self, refresh=False, cancel=None):
        with self.lock:
            if cancel is not None and cancel.is_set():
                raise Cancelled("任务已取消。")
            if not refresh and self.cached and time.monotonic() - self.cached_at < (600 if self.cached["gpu_interpolation"] else 30):
                return dict(self.cached)
            result = {"gpu_interpolation": False, "gpu_encoding": bool(getattr(self.settings, "nvenc", False)),
                      "reason": "未配置独立 GPU 插帧环境和 RIFE 模型。", "device": "", "model": "",
                      "default_mode": "auto", "pipeline": "streaming"}
            configured = all(getattr(self.settings, name, None) for name in
                             ("interpolation_python", "interpolation_root", "interpolation_model"))
            if configured:
                if not self.slot.acquire(blocking=False):
                    return {**(self.cached or result), "busy": True,
                            "reason": "GPU 正在处理任务，空闲后可重新检测。"}
                try:
                    process = subprocess.Popen(worker_command(self.settings, "--probe"), stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                               creationflags=NO_WINDOW)
                    began = time.monotonic()
                    try:
                        while True:
                            if cancel is not None and cancel.is_set():
                                raise Cancelled("任务已取消。")
                            if time.monotonic() - began > 120:
                                raise subprocess.TimeoutExpired(process.args, 120)
                            try:
                                stdout, _ = process.communicate(timeout=.2)
                                break
                            except subprocess.TimeoutExpired:
                                pass
                    finally:
                        if process.poll() is None:
                            stop_worker_tree(process)
                        process.wait()
                    completed = process
                    report = {}
                    for line in stdout.splitlines():
                        try:
                            value = json.loads(line)
                            if isinstance(value, dict) and "available" in value:
                                report = value
                        except ValueError:
                            pass
                    result.update(gpu_interpolation=completed.returncode == 0 and report.get("available") is True,
                                  reason=str(report.get("reason") or "GPU 插帧自检失败，请检查独立环境和模型。")[:1500],
                                  device=str(report.get("device") or ""), model=str(report.get("model") or ""))
                except (OSError, subprocess.SubprocessError):
                    result["reason"] = "GPU 插帧环境无法启动或自检超过 120 秒。"
                finally:
                    self.slot.release()
            self.cached, self.cached_at = dict(result), time.monotonic()
            return result

    def select(self, options, metadata, plan, cancel=None):
        requested = options.get("processing_mode", "auto")
        if requested not in ("auto", "gpu", "cpu"):
            raise MediaError("请选择 GPU 优先、仅 GPU 或 CPU 兼容模式。")
        interpolation = plan.get("effective_frame_rate_mode") == "interpolate"
        encoder, backend = "libx264", "cpu" if interpolation else "none"
        notes, model = [], ""
        if requested != "cpu":
            width, height = output_size(metadata, options)
            if options.get("upscale", "none") != "none" and options.get("height", 0) and options["height"] > metadata["height"]:
                width, height = int(width * options["height"] / height), options["height"]
            can_encode = bool(getattr(self.settings, "nvenc", False)) and width >= 160 and height >= 128 and max(width, height) <= 4096
            if can_encode:
                encoder = "h264_nvenc"
            elif requested == "gpu":
                raise MediaError("GPU 编码自检未通过或画面超出当前 H.264 NVENC 支持范围。", code="gpu_unavailable")
            else:
                notes.append("NVENC 不可用或画面尺寸不支持，本次编码使用 CPU。")
            if interpolation:
                unsupported = None
                if metadata.get("color_transfer") in ("smpte2084", "arib-std-b67"):
                    unsupported = "GPU 插帧暂不支持 HDR 色调映射。"
                elif (metadata.get("rotation") or 0) % 360:
                    unsupported = "GPU 插帧暂不支持带旋转显示标记的来源。"
                if not can_encode:
                    unsupported = "GPU 流水线需要可用的 NVENC 编码。"
                status = {"gpu_interpolation": False, "reason": unsupported} if unsupported else self.status(cancel=cancel)
                if status["gpu_interpolation"]:
                    backend, model = "gpu", status.get("model") or "RIFE 4.25 lite"
                elif requested == "gpu":
                    raise MediaError("GPU 插帧不可用：" + status["reason"], code="gpu_interpolation_unavailable")
                else:
                    notes.append(str(status["reason"]) + "流畅插帧使用 CPU。")
        if backend == "gpu":
            notes.append("CUDA RIFE 插帧与 NVENC 编码连续处理；CPU 负责解码、音频和有界队列。")
        elif backend == "cpu":
            notes.append("FFmpeg 运动补偿在 CPU 生成中间画面。")
        notes.append("视频使用 NVIDIA NVENC 编码。" if encoder == "h264_nvenc" else "视频使用 CPU x264 编码。")
        gpu_used = backend == "gpu" or encoder == "h264_nvenc"
        cpu_used = backend == "cpu" or encoder == "libx264"
        return {"video_encoder": encoder, "interpolation_backend": backend, "interpolation_model": model,
                "compute_device": "mixed" if gpu_used and cpu_used else "gpu" if gpu_used else "cpu",
                "compute_note": "".join(notes), "pipeline": "streaming" if backend == "gpu" else "ffmpeg"}

    def encode(self, source, destination, options, metadata, folder, cancel, progress, plan, compute, prepared=None):
        if cancel.is_set():
            raise Cancelled("任务已取消。")
        began = time.monotonic()
        fps, duration = options.get("fps"), number(metadata.get("video_duration"))
        if not duration:
            raise MediaError("GPU 插帧需要有效的目标帧率和视频时长。")
        output_count = validate_limits(duration, fps, metadata.get("frame_count") or 0)
        folder = Path(folder)
        destination = Path(destination)
        directory = folder / "gpu-stream"
        directory.mkdir(exist_ok=True)
        last_disk_check = None
        def monitor_disk(force=False):
            nonlocal last_disk_check
            now = time.monotonic()
            if force or last_disk_check is None or now - last_disk_check >= 1:
                if shutil.disk_usage(folder).free < 512 * 1024**2:
                    raise MediaError("GPU 导出磁盘剩余不足 512 MiB。")
                last_disk_check = now
        monitor_disk()
        progress(84 if prepared else 55, "正在读取来源时间信息，准备 GPU 连续插帧")
        def timing_progress(count, elapsed):
            base = 84 if prepared else 55
            progress(base + min(1, elapsed / duration), f"正在读取来源时间信息：{count:,} 帧，{elapsed / 60:.1f} / {duration / 60:.1f} 分钟")
        timing = scan_timestamps(self.settings.ffprobe, source, directory, duration, cancel,
                                 progress=timing_progress, monitor=monitor_disk)
        source_count = timing["source_count"]
        width, height = output_size(metadata, options, prepared)
        decoder = [self.settings.ffmpeg, "-v", "error", "-nostdin", "-noautorotate", "-threads", "4",
                   "-protocol_whitelist", "file,pipe"]
        if prepared:
            decoder += ["-f", "concat", "-safe", "0", "-i", str(prepared["manifest"])]
        else:
            decoder += ["-i", str(source)]
        decoder += ["-map", "0:v:0", "-an", "-vf", f"scale={width}:{height}:flags=lanczos,setsar=1",
                    "-fps_mode", "passthrough", "-frames:v", str(source_count), "-threads", "2",
                    "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
        raw = {"raw": True, "width": width, "height": height, "fps": fps, "interpolated": True}
        encoder = build_encode_command(source, destination, options, self.settings, metadata, plan, raw, compute=compute)
        spec = {"decoder_command": decoder, "encoder_command": encoder, "width": width, "height": height,
                "fps": fps, **timing, "duration": duration, "output_count": output_count, "log_dir": str(directory)}
        specification = directory / "stream.json"
        specification.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        messages, reader_stop = queue.Queue(maxsize=32), threading.Event()
        monitor_disk(force=True)
        process = subprocess.Popen(worker_command(self.settings, "--stream", str(specification)), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=NO_WINDOW, start_new_session=os.name != "nt")
        log = BoundedErrorLog(process.stderr, directory / "worker.log")
        try:
            reader = threading.Thread(target=read_pipe_lines, args=(process.stdout, messages, reader_stop), daemon=True)
            reader.start()
            report, failure = None, None
            encoding_began = last_event = time.monotonic()
            try:
                while process.poll() is None or reader.is_alive() or not messages.empty():
                    if cancel.is_set():
                        raise Cancelled("任务已取消。")
                    if time.monotonic() - encoding_began > GPU_TIMEOUT_SECONDS:
                        raise MediaError("GPU 处理超过 24 小时限制。")
                    if time.monotonic() - last_event > STALL_TIMEOUT_SECONDS:
                        raise MediaError("GPU 流水线超过 600 秒未响应，请检查显存或降低分辨率。")
                    monitor_disk()
                    try:
                        kind, payload = messages.get(timeout=.2)
                    except queue.Empty:
                        continue
                    if kind == "error":
                        raise MediaError("GPU 进程消息读取失败：" + payload)
                    if kind == "eof":
                        continue
                    try:
                        message = json.loads(payload)
                    except ValueError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    last_event = time.monotonic()
                    if message.get("event") == "progress":
                        done = max(0, min(output_count, int(message.get("output_frames", 0))))
                        base = 84 if prepared else 56
                        progress(base + (98 - base) * done / output_count, f"GPU 插帧与编码：{done} / {output_count} 帧")
                    elif message.get("event") == "result" and message.get("ok"):
                        report = message
                    elif message.get("event") == "error":
                        failure = str(message.get("reason") or "GPU 进程异常")[:1500]
                if process.returncode or not report or failure:
                    log.thread.join(timeout=3)
                    details = failure or log.details()
                    raise MediaError("GPU 流水线失败，未自动改用 CPU：" + details, code="gpu_pipeline_failed")
                if not destination.is_file() or report.get("output_frames") != output_count:
                    raise MediaError("GPU 输出文件或真实输出帧数不完整。")
            finally:
                reader_stop.set()
                stop_worker_tree(process)
                process.wait()
                reader.join(timeout=3)
                if not reader.is_alive():
                    process.stdout.close()
        finally:
            log.close()
        return {"pipeline": "streaming", "interpolation_backend": "gpu",
                "interpolation_model": report.get("model"), "gpu_device": report.get("device"),
                "interpolation_generated_frames": report.get("generated_frames", 0),
                "interpolation_held_frames": report.get("held_frames", 0),
                "interpolation_scene_cut_frames": report.get("cut_frames", 0),
                "interpolation_output_frames": output_count, "interpolation_source_frames": source_count,
                "gpu_peak_memory_bytes": report.get("gpu_peak_memory_bytes"),
                "pipeline_queue_capacity": report.get("queue_capacity"),
                "gpu_worker_seconds": report.get("elapsed_seconds"),
                "gpu_pipeline_seconds": round(time.monotonic() - began, 3),
                "interpolation_temp_frame_files": 0}

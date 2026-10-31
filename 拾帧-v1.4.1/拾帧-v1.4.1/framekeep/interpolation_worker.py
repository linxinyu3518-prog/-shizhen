# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded raw-frame pipeline for a separately installed MIT RIFE CUDA runtime.

FrameKeep owns this adapter, not the external network or its weights. This
process imports Torch in the explicitly configured Python environment only.
CPU decoding and pipe I/O overlap CUDA interpolation and hardware encoding;
there are no intermediate image files and no request per output frame.
"""
import argparse
from dataclasses import dataclass
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

if __package__:
    from .stream_timing import DoubleTimeline, MAX_DURATION_SECONDS, MAX_SOURCE_FRAMES, MAX_OUTPUT_FRAMES
else:
    from stream_timing import DoubleTimeline, MAX_DURATION_SECONDS, MAX_SOURCE_FRAMES, MAX_OUTPUT_FRAMES


QUEUE_CAPACITY = 3
_END = object()


@dataclass(frozen=True)
class StreamSpec:
    decoder_command: list
    encoder_command: list
    width: int
    height: int
    fps: float
    times: object
    output_count: int
    log_dir: Path

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict):
            raise ValueError("GPU 流式任务必须是 JSON 对象。")
        for name in ("decoder_command", "encoder_command"):
            command = value.get(name)
            if not isinstance(command, list) or not command or any(
                not isinstance(part, str) or not part or "\x00" in part for part in command
            ):
                raise ValueError(f"{name} 必须是非空命令参数数组。")
        for name in ("width", "height", "output_count"):
            if type(value.get(name)) is not int or value[name] < 1:
                raise ValueError(f"{name} 必须是正整数。")
        width, height = value["width"], value["height"]
        if min(width, height) < 8 or max(width, height) > 8192 or width * height > 8192 * 4608:
            raise ValueError("GPU 插帧来源尺寸超出范围。")
        fps = value.get("fps")
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 0 < fps <= 120:
            raise ValueError("输出帧率必须在 0–120 fps 范围内。")
        log_dir = value.get("log_dir")
        if not isinstance(log_dir, str) or not log_dir:
            raise ValueError("GPU 流式任务缺少日志目录。")
        log_dir = Path(log_dir).resolve()
        if value["output_count"] > MAX_OUTPUT_FRAMES or value["output_count"] / fps > MAX_DURATION_SECONDS + 1e-7:
            raise ValueError("GPU 插帧最多支持 3 小时及 1,296,000 个输出画面。")
        times = value.get("times")
        mapped = False
        try:
            if "times_file" in value:
                path = Path(value["times_file"])
                if path.is_symlink() or path.resolve().parent != log_dir or not path.is_file():
                    raise ValueError("来源时间戳文件必须位于当前任务目录。")
                count = value.get("source_count")
                if type(count) is not int or not 0 < count <= MAX_SOURCE_FRAMES:
                    raise ValueError("来源时间戳数量超出三小时插帧范围。")
                times = DoubleTimeline(path, count)
                mapped = True
            elif not isinstance(times, list) or not times or len(times) > MAX_SOURCE_FRAMES:
                raise ValueError("来源时间戳必须是有界的非空数字列表。")
            previous = None
            for timestamp in times:
                if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or
                        not math.isfinite(timestamp) or timestamp < 0 or timestamp >= MAX_DURATION_SECONDS or
                        (previous is None and abs(timestamp) > 1e-7) or
                        (previous is not None and timestamp <= previous)):
                    raise ValueError("来源时间戳必须从零开始、有限且严格递增。")
                previous = timestamp
            if "duration" in value:
                duration = value["duration"]
                if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or
                        not math.isfinite(duration) or not 0 < duration <= MAX_DURATION_SECONDS or
                        times[-1] >= duration + .001 or
                        max(1, math.floor(duration * fps + .5)) != value["output_count"]):
                    raise ValueError("视频时长、来源时间戳或输出帧数不一致。")
            return cls(value["decoder_command"], value["encoder_command"], width, height,
                       float(fps), times, value["output_count"], log_dir)
        except BaseException:
            if mapped:
                times.close()
            raise

    def close(self):
        if isinstance(self.times, DoubleTimeline):
            self.times.close()


class GPUInterpolator:
    def __init__(self, root, model):
        runtime, model_dir = Path(root).resolve(), Path(model).resolve()
        definition, weights = model_dir / "IFNet_HDv3.py", model_dir / "flownet.pkl"
        for required in (definition, weights, runtime / "model" / "warplayer.py"):
            if not required.is_file():
                raise FileNotFoundError(f"GPU 插帧运行库或模型缺失：{required.name}")
        import torch
        import numpy

        if not torch.cuda.is_available():
            raise RuntimeError("此 Python 环境没有可用的 NVIDIA CUDA GPU，请选择 CPU 模式。")
        self.torch, self.numpy = torch, numpy
        self.device = torch.cuda.get_device_name(0)
        self.model_name = "RIFE 4.25 lite"
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        sys.path.insert(0, str(runtime))
        module_spec = importlib.util.spec_from_file_location("framekeep_external_ifnet", definition)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        self.network = module.IFNet()
        state = torch.load(weights, map_location="cpu", weights_only=True)
        if not isinstance(state, dict):
            raise ValueError("RIFE 权重不是预期的参数字典。")
        state = {key.removeprefix("module."): tensor for key, tensor in state.items()}
        # The published lite checkpoint retains training-only branches, while
        # its inference definition omits them. Validate every inference weight.
        state = {key: tensor for key, tensor in state.items() if not key.startswith(("teacher.", "caltime."))}
        self.network.load_state_dict(state, strict=True)
        self.network.eval().requires_grad_(False).to("cuda")
        torch.cuda.reset_peak_memory_stats()

    def prepare(self, pixels, width, height):
        """Upload and pad each source frame once; the pipeline retains two."""
        torch = self.torch
        array = self.numpy.frombuffer(pixels, dtype=self.numpy.uint8).reshape(height, width, 3).copy()
        tensor = torch.from_numpy(array).to(device="cuda", dtype=torch.float32)
        tensor = tensor.permute(2, 0, 1).unsqueeze(0).contiguous().div_(255)
        scale = 0.5 if max(height, width) > 2048 else 1.0
        alignment = int(128 / scale)
        tensor = torch.nn.functional.pad(tensor, (0, (-width) % alignment, 0, (-height) % alignment))
        return tensor

    def pair_kind(self, raw0, raw1, first, second, width, height):
        """Evaluate static/cut decisions once per source pair, not per output."""
        if raw0 == raw1:
            return "static"
        torch = self.torch
        small0 = torch.nn.functional.interpolate(first[..., :height, :width], (48, 48), mode="area")
        small1 = torch.nn.functional.interpolate(second[..., :height, :width], (48, 48), mode="area")
        if (small0 - small1).abs().mean().item() < 0.2:
            return "interpolate"
        distances = []
        for channel in range(3):
            hist0 = torch.histc(small0[0, channel], bins=16, min=0, max=1)
            hist1 = torch.histc(small1[0, channel], bins=16, min=0, max=1)
            distances.append((hist0 - hist1).abs().sum() / (2 * 48 * 48))
        return "cut" if torch.stack(distances).mean().item() > 0.55 else "interpolate"

    def infer(self, first, second, timestep, width, height):
        torch = self.torch
        scale = 0.5 if max(height, width) > 2048 else 1.0
        prediction = self.network(
            torch.cat((first, second), dim=1), timestep=float(timestep),
            scale_list=[32 / scale, 16 / scale, 8 / scale, 4 / scale, 1 / scale],
        )[2][-1][..., :height, :width]
        if not prediction.is_cuda or not torch.isfinite(prediction).all().item():
            raise RuntimeError("GPU 插帧返回了无效画面。")
        return prediction[0].mul(255).round().clamp_(0, 255).to(torch.uint8).permute(1, 2, 0).cpu().numpy().tobytes()

    def probe(self):
        torch, numpy = self.torch, self.numpy
        with torch.inference_mode():
            ramp = numpy.tile(numpy.arange(256, dtype=numpy.uint8), (128, 1))
            first = numpy.repeat(ramp[..., None], 3, axis=2)
            first[32:96, 50:110, :] = 230
            second = numpy.roll(first, 12, axis=1)
            raw0, raw1 = first.tobytes(), second.tobytes()
            middle = self.infer(self.prepare(raw0, 256, 128), self.prepare(raw1, 256, 128), 0.37, 256, 128)
            pixels = numpy.frombuffer(middle, numpy.uint8).astype(numpy.float32)
            if any(numpy.abs(pixels - frame.reshape(-1).astype(numpy.float32)).mean() < 0.0255 for frame in (first, second)):
                raise RuntimeError("GPU 自检没有生成新的中间画面。")
        return {
            "available": True, "provider": "rife", "device": self.device,
            "model": self.model_name, "reason": "已通过实际 CUDA 中间帧生成自检。",
            "torch": torch.__version__, "cuda": torch.version.cuda,
            "python": sys.version.split()[0], "arbitrary_timestep": True,
            "max_dimension": 8192, "scene_cut_detection": True,
            "max_duration_seconds": MAX_DURATION_SECONDS,
            "max_source_frames": MAX_SOURCE_FRAMES, "max_output_frames": MAX_OUTPUT_FRAMES,
            "streaming": True, "queue_capacity": QUEUE_CAPACITY,
        }

    def close(self):
        self.network = None
        self.torch.cuda.empty_cache()


class FramePipes:
    """Two bounded queues overlap pipe I/O and GPU work with failure propagation."""
    def __init__(self, spec, emit):
        self.spec, self.emit = spec, emit
        self.incoming, self.outgoing = queue.Queue(QUEUE_CAPACITY), queue.Queue(QUEUE_CAPACITY)
        self.errors = queue.Queue()
        self.stop = threading.Event()
        self.reader_done, self.writer_done = threading.Event(), threading.Event()
        self.decoder = self.encoder = None
        self.reader = self.writer = None
        self.logs = []

    def check(self):
        try:
            failure = self.errors.get_nowait()
        except queue.Empty:
            return
        raise failure

    def _put(self, channel, item):
        while not self.stop.is_set():
            try:
                channel.put(item, timeout=0.1)
                return True
            except queue.Full:
                pass
        return False

    def start(self):
        self.spec.log_dir.mkdir(parents=True, exist_ok=True)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        for name in ("decoder", "encoder"):
            self.logs.append(open(self.spec.log_dir / f"gpu-{name}.log", "wb"))
        self.decoder = subprocess.Popen(self.spec.decoder_command, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=self.logs[0], bufsize=0,
                                        creationflags=creationflags)
        # Announce immediately, even if starting the second child subsequently fails.
        self.emit({"event": "processes", "decoder_pid": self.decoder.pid, "encoder_pid": None})
        self.encoder = subprocess.Popen(self.spec.encoder_command, stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=self.logs[1], bufsize=0,
                                        creationflags=creationflags)
        self.emit({"event": "processes", "decoder_pid": self.decoder.pid, "encoder_pid": self.encoder.pid})
        self.reader = threading.Thread(target=self._read, name="framekeep-decode", daemon=True)
        self.writer = threading.Thread(target=self._write, name="framekeep-encode", daemon=True)
        self.reader.start()
        self.writer.start()
        return self

    def _read(self):
        frame_size = self.spec.width * self.spec.height * 3
        count = 0
        try:
            while not self.stop.is_set():
                pixels = bytearray()
                while len(pixels) < frame_size and not self.stop.is_set():
                    chunk = self.decoder.stdout.read(frame_size - len(pixels))
                    if not chunk:
                        break
                    pixels.extend(chunk)
                if self.stop.is_set():
                    return
                if not pixels:
                    break
                if len(pixels) != frame_size:
                    raise RuntimeError("视频解码返回了不完整的 RGB 画面。")
                count += 1
                if count > len(self.spec.times):
                    raise RuntimeError("实际解码帧数超过来源时间戳数量。")
                if not self._put(self.incoming, bytes(pixels)):
                    return
            code = self.decoder.wait()
            if code != 0:
                raise RuntimeError(f"视频解码失败（退出码 {code}），请查看 gpu-decoder.log。")
            if count != len(self.spec.times):
                raise RuntimeError(f"实际解码帧数 {count} 与来源时间戳数量 {len(self.spec.times)} 不符。")
        except Exception as exc:
            if not self.stop.is_set():
                self.errors.put(exc)
                self.stop.set()
        finally:
            self.reader_done.set()

    def _write(self):
        try:
            while not self.stop.is_set():
                try:
                    pixels = self.outgoing.get(timeout=0.1)
                except queue.Empty:
                    if self.encoder.poll() is not None:
                        raise RuntimeError("视频编码进程提前退出，请查看 gpu-encoder.log。")
                    continue
                if pixels is _END:
                    self.encoder.stdin.close()
                    code = self.encoder.wait()
                    if code != 0:
                        raise RuntimeError(f"视频编码失败（退出码 {code}），请查看 gpu-encoder.log。")
                    return
                view = memoryview(pixels)
                while view and not self.stop.is_set():
                    written = self.encoder.stdin.write(view)
                    if not written:
                        raise RuntimeError("视频编码管道未能写入完整画面。")
                    view = view[written:]
        except Exception as exc:
            if not self.stop.is_set():
                self.errors.put(RuntimeError(f"视频编码失败：{exc}；请查看 gpu-encoder.log。"))
                self.stop.set()
        finally:
            self.writer_done.set()

    def read(self):
        while True:
            self.check()
            try:
                return self.incoming.get(timeout=0.1)
            except queue.Empty:
                if self.reader_done.is_set():
                    self.check()
                    return None

    def write(self, pixels):
        self.check()
        if not self._put(self.outgoing, pixels):
            self.check()
            raise RuntimeError("GPU 流式输出已停止。")
        self.check()

    def finish(self):
        self.write(_END)
        last_report = time.monotonic()
        while not self.writer_done.wait(0.1):
            self.check()
            # A large MP4 can spend minutes flushing/relocating its index.
            # Keep the supervisor informed while retaining its hard deadline.
            if time.monotonic() - last_report >= 2:
                self.emit({"event": "progress", "phase": "finalizing", "progress": 1,
                           "output_frames": self.spec.output_count, "output_count": self.spec.output_count})
                last_report = time.monotonic()
        self.check()

    def close(self):
        self.stop.set()
        # Kill blocked children before joining threads: read/write may be inside
        # a full OS pipe and cannot observe a Python event until the pipe closes.
        for process in (self.decoder, self.encoder):
            if process is not None and process.poll() is None:
                try:
                    if os.name == "nt":
                        # A Windows runtime launcher may own another executable;
                        # stopping only its PID leaves inherited pipe/log handles.
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                    if process.poll() is None:
                        process.kill()
                except OSError:
                    pass
                except subprocess.TimeoutExpired:
                    process.kill()
        for process in (self.decoder, self.encoder):
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        for thread in (self.reader, self.writer):
            if thread is not None:
                thread.join(timeout=5)
        for process in (self.decoder, self.encoder):
            if process is not None:
                for handle in (process.stdin, process.stdout):
                    if handle is not None:
                        try:
                            handle.close()
                        except OSError:
                            pass
        for handle in self.logs:
            handle.close()


def stream_frames(spec, processor, emit):
    """Map CFR output timestamps to original PTS without changing video speed."""
    started = time.monotonic()
    pipes = FramePipes(spec, emit)
    stats = {"source_frames": 0, "output_frames": 0, "generated_frames": 0,
             "held_frames": 0, "cut_frames": 0, "static_frames": 0, "source_output_frames": 0}
    last_report = 0.0

    def take():
        pixels = pipes.read()
        if pixels is None:
            raise RuntimeError("来源视频在所需时间戳之前结束。")
        stats["source_frames"] += 1
        return pixels

    try:
        pipes.start()
        with processor.torch.inference_mode():
            index = 0
            first_raw = take()
            second_raw = take() if len(spec.times) > 1 else None
            first_tensor = second_tensor = None
            pair_kind = None
            for output_index in range(spec.output_count):
                pipes.check()
                timestamp = output_index / spec.fps
                while index + 1 < len(spec.times) and timestamp + 1e-7 >= spec.times[index + 1]:
                    index += 1
                    first_raw, first_tensor = second_raw, second_tensor
                    second_raw = take() if index + 1 < len(spec.times) else None
                    second_tensor = None
                    pair_kind = None
                if abs(timestamp - spec.times[index]) <= 1e-7:
                    pixels = first_raw
                    stats["source_output_frames"] += 1
                elif second_raw is None:
                    pixels = first_raw
                    stats["held_frames"] += 1
                else:
                    if first_tensor is None:
                        first_tensor = processor.prepare(first_raw, spec.width, spec.height)
                    if second_tensor is None:
                        second_tensor = processor.prepare(second_raw, spec.width, spec.height)
                    if pair_kind is None:
                        pair_kind = processor.pair_kind(first_raw, second_raw, first_tensor, second_tensor,
                                                        spec.width, spec.height)
                    if pair_kind in ("cut", "static"):
                        pixels = first_raw
                        stats["held_frames"] += 1
                        stats["cut_frames" if pair_kind == "cut" else "static_frames"] += 1
                    else:
                        relative = (timestamp - spec.times[index]) / (spec.times[index + 1] - spec.times[index])
                        pixels = processor.infer(first_tensor, second_tensor, relative, spec.width, spec.height)
                        stats["generated_frames"] += 1
                pipes.write(pixels)
                stats["output_frames"] += 1
                now = time.monotonic()
                if now - last_report >= 0.5 or stats["output_frames"] == spec.output_count:
                    emit({"event": "progress", "output_frames": stats["output_frames"],
                          "output_count": spec.output_count,
                          "progress": stats["output_frames"] / spec.output_count})
                    last_report = now
            # Downsampling and short output durations can leave unread sources.
            # Drain them so decoder failures or timestamp mismatches cannot hide.
            while pipes.read() is not None:
                stats["source_frames"] += 1
            if stats["source_frames"] != len(spec.times):
                raise RuntimeError("GPU 流式处理未完整读取来源画面。")
            pipes.finish()
        return {"event": "result", "ok": True, "provider": "rife", **stats,
                "model": processor.model_name, "device": processor.device,
                "queue_capacity": QUEUE_CAPACITY, "elapsed_seconds": round(time.monotonic() - started, 3),
                "gpu_peak_memory_bytes": processor.torch.cuda.max_memory_allocated()}
    finally:
        pipes.close()
        spec.close()


def error_result(exc):
    reason = str(exc).strip() or type(exc).__name__
    if "out of memory" in reason.lower():
        reason = "GPU 显存不足，请关闭其他 GPU 任务、降低分辨率或选择 CPU 模式。"
    return {"event": "error", "ok": False, "available": False, "provider": "rife",
            "reason": reason[:1500], "error": type(exc).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--model", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe", action="store_true")
    mode.add_argument("--stream", type=Path)
    arguments = parser.parse_args()
    # Native model output also goes to stderr; stdout is JSON events only.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    sys.dont_write_bytecode = True
    processor = None
    spec = None

    def emit(value):
        protocol.write(json.dumps(value, ensure_ascii=False) + "\n")

    try:
        spec = StreamSpec.parse(json.loads(arguments.stream.read_text(encoding="utf-8"))) if arguments.stream else None
        processor = GPUInterpolator(arguments.root, arguments.model)
        emit(processor.probe() if arguments.probe else stream_frames(spec, processor, emit))
        return 0
    except Exception as exc:
        emit(error_result(exc))
        return 1
    finally:
        if spec is not None:
            spec.close()
        if processor is not None:
            processor.close()
        protocol.close()


if __name__ == "__main__":
    raise SystemExit(main())

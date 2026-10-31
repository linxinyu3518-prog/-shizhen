# SPDX-License-Identifier: GPL-3.0-or-later
"""Original, optional NVIDIA VFX client running in a separate Python process.

The NVIDIA packages and models are installed separately, under their own
licenses. Nothing from those packages is copied or distributed here.
Protocol: one JSON request and response per line; SDK diagnostics use stderr.
"""
import argparse
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid
from collections import OrderedDict


def dimension(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 64 <= value <= 8192:
        raise ValueError("RTX 输出尺寸必须为 64–8192 的整数。")
    return value


def stages_for(source_width, source_height, width, height):
    """Keep every AI stage within 4x; round only the working canvas to 8 pixels."""
    width, height = dimension(width), dimension(height)
    if width < source_width or height < source_height:
        raise ValueError("RTX 超分输出不能小于来源尺寸。")
    if width == source_width and height == source_height:
        raise ValueError("RTX 超分需要比来源更大的输出尺寸。")
    final = (math.ceil(width / 8) * 8, math.ceil(height / 8) * 8)
    current = (source_width, source_height)
    result = []
    while final[0] > current[0] * 4 or final[1] > current[1] * 4:
        factor = min(4, final[0] / current[0], final[1] / current[1])
        next_size = tuple(max(8, math.floor(size * factor / 8) * 8) for size in current)
        if next_size[0] <= current[0] or next_size[1] <= current[1]:
            raise ValueError("RTX 超分无法保持此尺寸比例；请选择与来源一致的宽高比。")
        result.append(next_size)
        current = next_size
    if current != final:
        result.append(final)
    return result


class RTXProcessor:
    def __init__(self):
        # Deliberately lazy: the main application's Python never imports Torch.
        import torch
        import nvvfx
        from PIL import Image
        import numpy

        self.torch, self.nvvfx = torch, nvvfx
        self.Image, self.numpy = Image, numpy
        if not torch.cuda.is_available():
            raise RuntimeError("此 Python 环境没有可用的 NVIDIA CUDA GPU。")
        self.name = torch.cuda.get_device_name(0)
        self.version = importlib.metadata.version("nvidia-vfx")
        self.effects = OrderedDict()
        self.checked = False
        torch.set_num_threads(min(4, os.cpu_count() or 1))

    def effect(self, width, height):
        key = (width, height)
        if key in self.effects:
            self.effects.move_to_end(key)
            return self.effects[key]
        while len(self.effects) >= 2:
            _, previous = self.effects.popitem(last=False)
            previous.close()
        effect = self.nvvfx.VideoSuperRes(self.nvvfx.effects.QualityLevel.HIGH)
        try:
            effect.output_width, effect.output_height = width, height
            effect.load()
        except Exception:
            effect.close()
            raise
        self.effects[key] = effect
        return effect

    def run_tensor(self, tensor, width, height):
        torch = self.torch
        result = self.effect(width, height).run(tensor.contiguous())
        # The public SDK result borrows memory; own it before the next frame.
        output = torch.from_dlpack(result.image).clone()
        if tuple(output.shape) != (3, height, width):
            raise RuntimeError("RTX 返回的图像尺寸与请求不一致。")
        if not torch.isfinite(output).all().item():
            raise RuntimeError("RTX 返回了无效像素；请检查驱动和 NVIDIA VFX 运行库。")
        return output

    def probe(self):
        if not self.checked:
            torch = self.torch
            with torch.inference_mode():
                ramp = torch.linspace(0.1, 0.9, 640, device="cuda")
                sample = ramp.view(1, 1, 640).expand(3, 360, 640).contiguous()
                output = self.run_tensor(sample, 1280, 720)
                if output.std().item() < 0.05 or output.mean().item() < 0.05:
                    raise RuntimeError("RTX 自检图像为空或异常，不能确认 AI 超分可用。")
                del output, sample
            self.checked = True
        return {
            "available": True, "reason": "已通过实际 GPU 超分自检。",
            "provider": "rtx", "name": self.name,
            "model": "NVIDIA VideoSuperRes HIGH", "nvidia_vfx": self.version,
            "torch": self.torch.__version__, "cuda": self.torch.version.cuda,
            "python": sys.version.split()[0], "max_scale_per_pass": 4,
            "multiple_passes": True, "max_dimension": 8192,
        }

    def enhance(self, request):
        started = time.monotonic()
        if not isinstance(request.get("input"), str) or not isinstance(request.get("output"), str):
            raise ValueError("RTX 请求必须提供 input 和 output 文件路径。")
        source, destination = Path(request["input"]).resolve(), Path(request["output"]).resolve()
        if source == destination:
            raise ValueError("RTX 输入与输出不能使用同一个文件。")
        if destination.suffix.lower() != ".png":
            raise ValueError("RTX 帧输出必须使用 PNG 文件。")
        with self.Image.open(source) as loaded:
            if loaded.format != "PNG":
                raise ValueError("RTX 帧输入必须使用 PNG 文件。")
            if loaded.width > 8192 or loaded.height > 8192 or min(loaded.size) < 8:
                raise ValueError("RTX 来源帧尺寸超出范围。")
            pixels = self.numpy.array(loaded.convert("RGB"), copy=True)
        source_height, source_width = pixels.shape[:2]
        width, height = dimension(request.get("width")), dimension(request.get("height"))
        stages = stages_for(source_width, source_height, width, height)
        self.probe()
        torch = self.torch
        with torch.inference_mode():
            frame = torch.from_numpy(pixels).to(device="cuda", dtype=torch.float32)
            frame = frame.permute(2, 0, 1).contiguous().div_(255)
            for stage_width, stage_height in stages:
                frame = self.run_tensor(frame, stage_width, stage_height)
            pixels = frame.mul(255).round().clamp_(0, 255).to(torch.uint8).permute(1, 2, 0).cpu().numpy()
        result = self.Image.fromarray(pixels)
        aligned = result.size != (width, height)
        if aligned:
            # At most seven pixels of alignment are removed by a final downscale.
            # All resolution increases above were performed by the AI effect.
            result = result.resize((width, height), self.Image.Resampling.LANCZOS)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            result.save(temporary, format="PNG", compress_level=1)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "ok": True, "provider": "rtx", "model": "NVIDIA VideoSuperRes HIGH",
            "name": self.name, "width": width, "height": height,
            "source_width": source_width, "source_height": source_height,
            "passes": len(stages), "stages": [list(size) for size in stages],
            "alignment_downscale": aligned, "output": str(destination),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    def close(self):
        for effect in self.effects.values():
            effect.close()
        self.effects.clear()


def error_result(exc):
    reason = str(exc).strip() or type(exc).__name__
    return {"ok": False, "available": False, "provider": "rtx", "reason": reason[:1500], "error": type(exc).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    arguments = parser.parse_args()

    # Redirect the underlying stdout descriptor as well as Python's stream:
    # third-party native-library messages must never corrupt the JSON protocol.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    processor = None

    def handle(request):
        nonlocal processor
        if not isinstance(request, dict):
            raise ValueError("RTX 请求必须是 JSON 对象。")
        if processor is None:
            processor = RTXProcessor()
        response = processor.probe() if request.get("op") == "probe" else processor.enhance(request)
        return response

    def respond(request):
        try:
            response = handle(request)
        except Exception as exc:
            response = error_result(exc)
        if isinstance(request, dict) and "id" in request:
            response["id"] = request["id"]
        protocol.write(json.dumps(response, ensure_ascii=False) + "\n")
        return response

    try:
        if arguments.serve:
            # No unsolicited ready line: exactly one reply for each input line.
            while True:
                line = sys.stdin.readline(65537)
                if not line:
                    break
                try:
                    if len(line) > 65536:
                        raise ValueError("RTX 请求超过 64 KiB。")
                    request = json.loads(line)
                except Exception as exc:
                    protocol.write(json.dumps(error_result(exc), ensure_ascii=False) + "\n")
                    if len(line) > 65536:
                        break
                    continue
                respond(request)
            return 0
        request = {"op": "probe"} if arguments.probe else vars(arguments)
        result = respond(request)
        return 0 if result.get("available") or result.get("ok") else 1
    finally:
        if processor is not None:
            processor.close()
        protocol.close()


if __name__ == "__main__":
    raise SystemExit(main())

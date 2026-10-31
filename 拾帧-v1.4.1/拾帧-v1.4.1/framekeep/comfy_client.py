# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded one-way HTTP client for the user's independent local ComfyUI.

Only a fixed, basic image-upscale workflow is submitted. No custom nodes,
Python packages, callback routes, or browser credentials are installed.
"""
import binascii
import json
import math
from pathlib import Path, PurePosixPath
import re
import struct
import time
from urllib import error, parse, request
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from .network import Cancelled, MediaError

MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
FRAME_TIMEOUT = 120
POLL_INTERVAL = 0.25
REQUIRED_NODES = ("LoadImage", "UpscaleModelLoader", "ImageUpscaleWithModel", "ImageScale", "PreviewImage")


def validate_comfy_url(value):
    if not isinstance(value, str) or not value or any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        raise MediaError("ComfyUI 地址应为本机 HTTP 地址，例如 http://127.0.0.1:8188。")
    try:
        parsed = parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MediaError("ComfyUI 地址或端口格式不正确。") from exc
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or "?" in value or "#" in value or port == 0):
        raise MediaError("ComfyUI 仅允许无账号、无路径参数的本机 HTTP 地址。")
    # Avoid local DNS, proxies, and redirect-based changes of destination.
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{port or 80}"


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MediaError("ComfyUI 返回了重定向；请填写实际本机服务地址。")


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled("任务已取消；ComfyUI 已开始的单帧计算可能仍会完成。")


def _png_size(data):
    """Validate the bounded PNG container and chunk CRCs before passing it on."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > MAX_IMAGE_BYTES:
        raise MediaError("ComfyUI 图像不是有效的 PNG，或超过 128 MiB。")
    offset, dimensions, has_data = 8, None, False
    while offset + 12 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + size
        if end > len(data):
            break
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + size]
        checksum = struct.unpack_from(">I", data, offset + 8 + size)[0]
        if (binascii.crc32(kind + body) & 0xffffffff) != checksum:
            break
        if dimensions is None:
            if kind != b"IHDR" or size != 13:
                break
            dimensions = struct.unpack_from(">II", body)
            if not all(0 < dimension <= 16384 for dimension in dimensions) or dimensions[0] * dimensions[1] > 64 * 1024 * 1024:
                break
        elif kind == b"IHDR":
            break
        has_data = has_data or kind == b"IDAT"
        if kind == b"IEND":
            if size == 0 and end == len(data) and has_data:
                return dimensions
            break
        offset = end
    raise MediaError("ComfyUI 图像 PNG 数据不完整或校验失败。")


def _image_reference(value, *, expected_type):
    if not isinstance(value, dict) or value.get("type") != expected_type:
        raise MediaError("ComfyUI 返回的图像类型不正确。")
    name = value.get("name", value.get("filename"))
    folder = value.get("subfolder", "")
    if (not isinstance(name, str) or not name or len(name) > 255
            or not isinstance(folder, str) or len(folder) > 512
            or any(character in name for character in "/\\:\x00\r\n")
            or "\\" in folder or ":" in folder or "\x00" in folder
            or any(part in ("..", ".") for part in folder.split("/") if part)
            or PurePosixPath(folder).is_absolute() or name in (".", "..")):
        raise MediaError("ComfyUI 返回的图像路径不正确。")
    return name, folder


class ComfyClient:
    def __init__(self, url):
        self.url = validate_comfy_url(url)
        self.opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        self.model_scales = {}

    def _request(self, route, *, data=None, content_type="application/json", deadline=None, cancel=None, limit=MAX_JSON_BYTES):
        deadline = deadline if deadline is not None else time.monotonic() + 10
        _check_cancel(cancel)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MediaError("ComfyUI 单帧处理超过 120 秒，请降低输入尺寸或检查队列。")
        headers = {"Content-Type": content_type, "Accept": "application/json" if limit == MAX_JSON_BYTES else "image/png"}
        req = request.Request(self.url + route, data=data, headers=headers)
        try:
            with self.opener.open(req, timeout=min(10, remaining)) as response:
                try:
                    length = int(response.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise MediaError("ComfyUI 响应长度不正确。") from exc
                if length < 0 or length > limit:
                    raise MediaError("ComfyUI 响应超过允许大小。")
                chunks, size = [], 0
                while True:
                    _check_cancel(cancel)
                    if time.monotonic() >= deadline:
                        raise MediaError("ComfyUI 单帧处理超时。")
                    block = response.read(min(256 * 1024, limit + 1 - size))
                    if not block:
                        break
                    size += len(block)
                    if size > limit:
                        raise MediaError("ComfyUI 响应超过允许大小。")
                    chunks.append(block)
                if length and size != length:
                    raise MediaError("ComfyUI 响应未完整传输。")
                return b"".join(chunks)
        except error.HTTPError as exc:
            exc.close()
            raise MediaError(f"ComfyUI 请求失败（HTTP {exc.code}）；请检查服务及基础超分节点。") from exc
        except (error.URLError, OSError, TimeoutError) as exc:
            _check_cancel(cancel)
            raise MediaError("无法连接 ComfyUI 或请求超时，请启动本机 ComfyUI 并检查地址与队列。") from exc

    def _json(self, route, payload=None, **kwargs):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        raw = self._request(route, data=data, **kwargs)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise MediaError("ComfyUI 返回了无效 JSON，请确认地址指向 ComfyUI 服务。") from exc
        if not isinstance(result, dict):
            raise MediaError("ComfyUI 响应结构不正确。")
        return result

    def inspect(self):
        result = {"available": False, "models": [], "reason": "", "url": self.url}
        try:
            deadline = time.monotonic() + 10
            for node in REQUIRED_NODES:
                info = self._json("/object_info/" + node, deadline=deadline)
                if not isinstance(info.get(node), dict):
                    raise MediaError(f"ComfyUI 缺少基础节点 {node}，请检查它的启动日志。")
                if node == "UpscaleModelLoader":
                    try:
                        spec = info[node]["input"]["required"]["model_name"]
                        names = spec[1].get("options") if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict) else spec[0]
                    except (KeyError, IndexError, TypeError) as exc:
                        raise MediaError("ComfyUI 超分模型节点接口不兼容。") from exc
                    if not isinstance(names, list):
                        raise MediaError("ComfyUI 超分模型列表格式不正确。")
                    result["models"] = sorted({name for name in names if isinstance(name, str) and 0 < len(name) <= 512})
            if not result["models"]:
                raise MediaError("ComfyUI 未发现超分模型，请将兼容模型放入 models/upscale_models 后刷新。")
            result.update(available=True, reason="本机 ComfyUI 和基础超分节点已就绪。")
        except MediaError as exc:
            result["reason"] = str(exc)
        return result

    def _cancel_queued(self, prompt_id):
        try:
            # No /interrupt: older servers would globally interrupt unrelated jobs.
            self._request("/queue", data=json.dumps({"delete": [prompt_id]}).encode(), deadline=time.monotonic() + 3)
        except MediaError:
            pass

    def upscale(self, image: Path, output: Path, model: str, width: int, height: int, cancel, job_id: str) -> dict:
        _check_cancel(cancel)
        if (any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 16384 for value in (width, height))
                or width * height > 64 * 1024 * 1024):
            raise MediaError("AI 超分输出尺寸超出允许范围。")
        if not isinstance(model, str) or not model or len(model) > 512 or any(ord(c) < 32 for c in model):
            raise MediaError("请选择有效的 ComfyUI 超分模型。")
        image, output = Path(image), Path(output)
        if image.stat().st_size > MAX_IMAGE_BYTES:
            raise MediaError("AI 超分输入 PNG 超过 128 MiB。")
        pixels = image.read_bytes()
        input_width, input_height = _png_size(pixels)
        deadline = time.monotonic() + FRAME_TIMEOUT
        unique = uuid5(NAMESPACE_URL, "framekeep:" + str(job_id)).hex
        filename = "framekeep-" + unique + ".png"
        boundary = "framekeep" + uuid4().hex
        fields = [("type", "input"), ("subfolder", "framekeep"), ("overwrite", "true")]
        body = bytearray()
        for name, value in fields:
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\nContent-Type: image/png\r\n\r\n'.encode())
        body.extend(pixels)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        raw = self._request("/upload/image", data=bytes(body), content_type="multipart/form-data; boundary=" + boundary,
                            deadline=deadline, cancel=cancel)
        try:
            uploaded = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise MediaError("ComfyUI 上传响应不是有效 JSON。") from exc
        name, folder = _image_reference(uploaded, expected_type="input")
        image_name = str(PurePosixPath(folder) / name)
        workflow = {
            "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
            "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model}},
            "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
            "4": {"class_type": "ImageScale", "inputs": {"image": ["3", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "disabled"}},
            "5": {"class_type": "PreviewImage", "inputs": {"images": ["4", 0]}},
            "6": {"class_type": "PreviewImage", "inputs": {"images": ["3", 0]}},
        }
        def build_passes(scale):
            if not math.isfinite(scale) or scale <= 1:
                raise MediaError("ComfyUI 模型未放大画面，请选择真正的超分模型。")
            ratio = max(width / input_width, height / input_height)
            count = max(1, math.ceil(math.log(max(1, ratio), scale) - 1e-7))
            if count > 3:
                raise MediaError("目标需要超过三级模型超分，请降低分辨率或选择 RTX。")
            previous = "3"
            current_width, current_height = input_width * scale, input_height * scale
            for stage in range(1, count):
                # Downsize only; the next model pass performs every enlargement.
                pre_width = math.ceil(width / scale ** (count - stage))
                pre_height = math.ceil(height / scale ** (count - stage))
                if pre_width <= current_width and pre_height <= current_height:
                    resizer = str(10 + stage * 2)
                    workflow[resizer] = {"class_type": "ImageScale", "inputs": {"image": [previous, 0], "upscale_method": "lanczos",
                                                                                 "width": pre_width, "height": pre_height, "crop": "disabled"}}
                    previous = resizer
                    current_width, current_height = pre_width, pre_height
                enhancer = str(11 + stage * 2)
                workflow[enhancer] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": [previous, 0]}}
                previous = enhancer
                current_width, current_height = current_width * scale, current_height * scale
            workflow["4"]["inputs"]["image"] = [previous, 0]
            workflow["6"]["inputs"]["images"] = [previous, 0]
            return count
        passes = build_passes(self.model_scales[model]) if model in self.model_scales else 1
        prompt_id = None
        partial = output.with_name(output.name + ".part")
        try:
            response = self._json("/prompt", {"prompt": workflow, "client_id": "framekeep-" + unique}, deadline=deadline, cancel=cancel)
            prompt_id = response.get("prompt_id")
            try:
                if not isinstance(prompt_id, str) or str(UUID(prompt_id)) != prompt_id:
                    raise ValueError
            except ValueError as exc:
                prompt_id = None
                raise MediaError("ComfyUI 未接受超分任务，请检查模型和基础节点。") from exc
            while True:
                _check_cancel(cancel)
                history = self._json("/history/" + prompt_id, deadline=deadline, cancel=cancel)
                entry = history.get(prompt_id)
                if isinstance(entry, dict):
                    status = entry.get("status", {})
                    if isinstance(status, dict) and status.get("status_str") == "error":
                        raise MediaError("ComfyUI 超分执行失败，请查看其日志中的模型、显存或节点错误。")
                    outputs = entry.get("outputs", {})
                    images = outputs.get("5", {}).get("images", []) if isinstance(outputs, dict) and isinstance(outputs.get("5", {}), dict) else []
                    if images:
                        if not isinstance(images, list) or len(images) != 1:
                            raise MediaError("ComfyUI 应返回一张超分图像。")
                        raw_images = outputs.get("6", {}).get("images", [])
                        if not isinstance(raw_images, list) or len(raw_images) != 1:
                            raise MediaError("ComfyUI 缺少模型原始输出，不能验证 AI 放大倍率。")
                        raw_name, raw_folder = _image_reference(raw_images[0], expected_type="temp")
                        raw_query = parse.urlencode({"filename": raw_name, "subfolder": raw_folder, "type": "temp"})
                        raw_png = self._request("/view?" + raw_query, deadline=deadline, cancel=cancel, limit=MAX_IMAGE_BYTES)
                        raw_width, raw_height = _png_size(raw_png)
                        if model not in self.model_scales:
                            scale = min(raw_width / input_width, raw_height / input_height)
                            if scale <= 1:
                                raise MediaError("所选模型未增加画面尺寸，不能作为 AI 超分模型。")
                            self.model_scales[model] = scale
                        if raw_width < width or raw_height < height:
                            if passes > 1:
                                raise MediaError("模型放大结果仍小于目标，请降低分辨率或选择 RTX。")
                            passes = build_passes(self.model_scales[model])
                            response = self._json("/prompt", {"prompt": workflow, "client_id": "framekeep-" + unique}, deadline=deadline, cancel=cancel)
                            new_id = response.get("prompt_id")
                            if not isinstance(new_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", new_id):
                                raise MediaError("ComfyUI 未接受多级超分任务。")
                            prompt_id = new_id
                            continue
                        out_name, out_folder = _image_reference(images[0], expected_type="temp")
                        query = parse.urlencode({"filename": out_name, "subfolder": out_folder, "type": "temp"})
                        data = self._request("/view?" + query, deadline=deadline, cancel=cancel, limit=MAX_IMAGE_BYTES)
                        if _png_size(data) != (width, height):
                            raise MediaError("ComfyUI 返回尺寸与所选 AI 超分尺寸不一致。")
                        _check_cancel(cancel)
                        partial.write_bytes(data)
                        partial.replace(output)
                        return {"provider": "comfyui", "model": model, "passes": passes}
                    if isinstance(status, dict) and status.get("completed"):
                        raise MediaError("ComfyUI 任务完成但未返回超分图像。")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MediaError("ComfyUI 单帧处理超过 120 秒，请检查队列或降低输入尺寸。")
                if cancel.wait(min(POLL_INTERVAL, remaining)):
                    _check_cancel(cancel)
        except (MediaError, OSError):
            if prompt_id:
                self._cancel_queued(prompt_id)
            raise
        finally:
            partial.unlink(missing_ok=True)

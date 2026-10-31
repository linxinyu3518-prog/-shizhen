# SPDX-License-Identifier: GPL-3.0-or-later
"""Small loopback-only HTTP boundary. No framework or frontend build step."""
import hmac
import json
import mimetypes
import re
import secrets
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit

from . import __version__
from .config import ROOT
from .storage import storage_paths
from .browser import BrowserManager
from .extractors import ExtractorService
from .jobs import JobManager
from .media import create_demo
from .network import MediaError
from .stream_timing import MAX_DURATION_SECONDS, MAX_SOURCE_FRAMES, MAX_OUTPUT_FRAMES


class App:
    def __init__(self, settings):
        self.settings = settings
        self.token = secrets.token_urlsafe(32)
        self.extractors = ExtractorService(settings)
        self.jobs = JobManager(settings, self.extractors)
        self.browser = BrowserManager(settings, self.extractors)
        self.demo_path = create_demo(settings)


class Handler(BaseHTTPRequestHandler):
    server_version = "Framekeep/1.0"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        pass  # Signed source URLs are deliberately not written into access logs.

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        super().end_headers()

    def _json(self, status, payload):
        content = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)
        if status >= 400 and self.command == "POST" and not getattr(self, "_body_read", False):
            # Closing with an unread POST body can reset the TCP connection on
            # Windows before the client receives its 403. Finish the response,
            # then drain a bounded amount for at most 0.2 seconds. Never dispatch
            # the rejected body or wait indefinitely for its claimed size.
            try:
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_WR)
                self.connection.settimeout(.2)
                size = min(max(0, int(self.headers.get("Content-Length", "0"))), 2 * 1024**2)
                if size:
                    self.rfile.read(size)
            except (OSError, ValueError):
                pass

    def _local_request(self):
        port = self.server.server_port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "").lower() not in allowed_hosts:
            self._json(403, {"error": "仅接受本机页面的请求。"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed_hosts}:
            self._json(403, {"error": "不允许其他网站调用本地程序。"})
            return False
        return True

    def _media_error(self, status, error):
        body = {"error": str(error)}
        if error.code:
            body["code"] = error.code
        if error.normalized_url:
            body["normalized_url"] = error.normalized_url
        if error.diagnostics:
            body["diagnostics"] = error.diagnostics
        self._json(status, body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._local_request():
            return
        route = urlsplit(self.path).path
        try:
            if route == "/api/health":
                s = self.app.settings
                self._json(200, {"ok": bool(s.ffmpeg and s.ffprobe and s.h264 and s.aac), "version": __version__,
                                 "storage": storage_paths(s.data_dir), "ffmpeg": bool(s.ffmpeg), "ffprobe": bool(s.ffprobe), "yt_dlp": s.yt_dlp,
                                 "h264": s.h264, "aac": s.aac, "token": self.app.token,
                                 "browser": bool(s.browser and s.playwright),
                                 "interpolation": s.interpolation, "max_fps": 120,
                                 "max_gpu_interpolation_seconds": MAX_DURATION_SECONDS,
                                 "max_gpu_source_frames": MAX_SOURCE_FRAMES,
                                 "max_gpu_output_frames": MAX_OUTPUT_FRAMES,
                                 "gpu_encoding": bool(getattr(s, "nvenc", False)),
                                 "demo_available": bool(self.app.demo_path),
                                 "max_download_gib": s.max_download_bytes / 1024**3,
                                 "max_download_bytes": s.max_download_bytes})
            elif route == "/api/demo":
                if not self.app.demo_path:
                    raise MediaError("演示素材尚不可用，请先配置 FFmpeg。")
                self._json(200, {"url": self.app.settings.demo_url})
            elif route == "/sample.mp4" and self.app.demo_path:
                self._file(self.app.demo_path)
            elif route == "/api/jobs":
                self._json(200, {"jobs": self.app.jobs.list()})
            elif route == "/api/upscale":
                self._json(200, self.app.jobs.upscale.status())
            elif route == "/api/compute":
                self._json(200, self.app.jobs.compute.status())
            elif route == "/api/session/douyin":
                self._json(200, self.app.extractors.sessions.status("douyin"))
            elif match := re.fullmatch(r"/api/browser/analyze/([a-f0-9]{32})", route):
                self._json(200, self.app.browser.get(match[1]))
            elif match := re.fullmatch(r"/api/jobs/([a-f0-9]{32})(/download)?", route):
                if match[2]:
                    self._file(self.app.jobs.download_path(match[1]), attachment=True)
                else:
                    self._json(200, self.app.jobs.get(match[1]))
            elif route in ("/", "/index.html", "/app.js", "/styles.css", "/static/app.js", "/static/styles.css"):
                path = ROOT / "web" / ("index.html" if route == "/" else route.split("/")[-1])
                if path.is_file():
                    self._file(path)
                else:
                    self._json(404, {"error": "界面文件不存在。"})
            else:
                self._json(404, {"error": "没有这个地址。"})
        except MediaError as exc:
            self._media_error(404, exc)
        except (ConnectionError, BrokenPipeError):
            pass

    def do_POST(self):
        if not self._local_request():
            return
        if not hmac.compare_digest(self.headers.get("X-App-Token", "").encode("utf-8"), self.app.token.encode("ascii")):
            self._json(403, {"error": "页面会话已失效，请刷新后重试。"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "请求必须使用 JSON。"})
            return
        route = urlsplit(self.path).path
        try:
            size = int(self.headers.get("Content-Length", "0"))
            body_limit = 2 * 1024**2 if route == "/api/session/douyin" else 16384
            if size <= 0 or size > body_limit:
                self._json(413, {"error": "请求内容大小不正确。"})
                return
            raw = self.rfile.read(size)
            self._body_read = True
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("object required")
        except (ValueError, UnicodeError):
            self._json(400, {"error": "请求内容不是有效的 JSON 对象。"})
            return
        try:
            if route == "/api/analyze":
                self._json(200, self.app.extractors.analyze(payload.get("url")))
            elif route == "/api/browser/analyze":
                self._json(202, self.app.browser.create(payload.get("url")))
            elif match := re.fullmatch(r"/api/browser/analyze/([a-f0-9]{32})/cancel", route):
                self._json(200, self.app.browser.cancel(match[1]))
            elif route == "/api/jobs":
                self._json(202, self.app.jobs.create(payload))
            elif route == "/api/upscale/configure":
                self._json(200, self.app.jobs.upscale.configure(payload))
            elif route == "/api/session/douyin":
                self._json(200, self.app.extractors.sessions.import_text("douyin", payload.get("cookies")))
            elif route == "/api/session/douyin/clear":
                self._json(200, self.app.extractors.clear_session("douyin"))
            elif match := re.fullmatch(r"/api/jobs/([a-f0-9]{32})/cancel", route):
                self._json(200, self.app.jobs.cancel(match[1]))
            else:
                self._json(404, {"error": "没有这个地址。"})
        except MediaError as exc:
            self._media_error(400, exc)
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception:
            self._json(500, {"error": "程序处理请求失败，请检查运行环境后重试。"})

    def _file(self, path, attachment=False):
        size = path.stat().st_size
        start, end, partial = 0, size - 1, False
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or not any(match.groups()):
                self._range_error(size)
                return
            if match[1]:
                start = int(match[1])
                end = min(end, int(match[2])) if match[2] else end
            else:
                start = max(0, size - int(match[2]))
            if start > end or start >= size:
                self._range_error(size)
                return
            partial = True
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if attachment:
            self.send_header("Content-Disposition", f"attachment; filename=\"media{path.suffix}\"; filename*=UTF-8''{quote(path.name)}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as content:
            content.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = content.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _range_error(self, size):
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        super().server_close()
        if hasattr(self, "app"):
            self.app.browser.close()
            self.app.jobs.close()


def create_server(settings, port=8765):
    server = LocalServer(("127.0.0.1", port), Handler)
    settings.demo_url = f"http://127.0.0.1:{server.server_port}/sample.mp4"
    server.app = App(settings)
    return server

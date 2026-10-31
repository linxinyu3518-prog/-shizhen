# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP input validation and bounded, redirect-aware downloads."""
import ipaddress
from copy import deepcopy
from http.cookiejar import CookieJar, DefaultCookiePolicy
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 Framekeep/1.0"


class MediaError(Exception):
    """An actionable error safe to display to the local user."""

    def __init__(self, message, *, code=None, normalized_url=None, diagnostics=None):
        super().__init__(message)
        self.code = code
        self.normalized_url = normalized_url
        self.diagnostics = diagnostics or []


class Cancelled(MediaError):
    pass


def validate_url(value, demo_url=None):
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise MediaError("请输入有效的视频网页地址或媒体直链。")
    value = value.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise MediaError("网址包含不支持的字符。")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MediaError("网址格式不正确。") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise MediaError("仅支持不含账号密码的 HTTP / HTTPS 网址。")
    if value == demo_url:
        return value
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise MediaError("无法解析这个网站的域名，请检查网址和网络。") from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise MediaError("请输入公开网站地址；本地和内网地址仅允许程序自带的演示素材。")
    return value


class ValidatedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, demo_url=None):
        super().__init__()
        self.demo_url = demo_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, self.demo_url)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def cookie_jar(cookies=()):
    jar = CookieJar(policy=DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
    for cookie in cookies:
        jar.set_cookie(deepcopy(cookie))
    return jar


def open_url(url, demo_url=None, headers=None, cookies=()):
    validate_url(url, demo_url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    handlers = [ValidatedRedirect(demo_url)]
    if cookies:
        handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar(cookies)))
    return urllib.request.build_opener(*handlers).open(request, timeout=20)


def download_file(url, destination, settings, progress, cancel, headers=None, cookies=()):
    downloaded = 0
    start = time.monotonic()
    try:
        with open_url(url, settings.demo_url, headers, cookies) as response, destination.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            if total > settings.max_download_bytes:
                raise MediaError(f"源文件超过 {settings.max_download_bytes / 1024**3:g} GiB，当前版本暂不下载此文件。")
            while True:
                if cancel.is_set():
                    raise Cancelled("任务已取消。")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > settings.max_download_bytes or time.monotonic() - start > 4 * 3600:
                    raise MediaError("下载超过大小或时间限制。")
                output.write(chunk)
                progress(min(54, downloaded / total * 54) if total else 10,
                         f"已下载 {downloaded / 1024**2:.1f} MiB" + (f" / {total / 1024**2:.1f} MiB" if total else ""))
            if total and downloaded != total:
                raise MediaError("下载不完整，请重新创建任务。")
            if not downloaded:
                raise MediaError("网站返回了空文件。")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MediaError("下载失败：网站拒绝访问、链接已过期或网络中断。请重新解析。") from exc
    return destination

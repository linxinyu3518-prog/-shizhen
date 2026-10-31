# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicitly imported platform cookies, kept only in process memory.

This module never reads a browser, a file, or an environment variable. It parses
the Netscape interchange format independently using Python's cookie data model.
"""
from copy import deepcopy
from http.cookiejar import Cookie
import re
import threading
import time

from .network import MediaError


_MAX_BYTES = 256 * 1024
_MAX_RECORDS = 3000
_HEADERS = frozenset({"# Netscape HTTP Cookie File", "# HTTP Cookie File"})
_DOMAINS = ("douyin.com", "iesdouyin.com")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_NAME_SEPARATORS = frozenset('()<>@,;:\\"/[]?={} ')


def _platform(platform: str) -> None:
    if platform != "douyin":
        raise MediaError("当前仅支持导入抖音登录状态。")


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _row_error(row: int) -> MediaError:
    # Never include a field's contents; names as well as values can be sensitive.
    return MediaError(f"Cookie 文件第 {row} 行格式不正确，请重新导出 Netscape 格式文件。")


def _parse(text: str) -> tuple[Cookie, ...]:
    if not isinstance(text, str) or not text or len(text) > _MAX_BYTES:
        raise MediaError("请提供不超过 256 KiB 的 Netscape Cookie 文件内容。")
    try:
        if len(text.encode("utf-8")) > _MAX_BYTES:
            raise MediaError("Cookie 文件超过 256 KiB，请仅导出抖音网站的 Cookie。")
    except UnicodeEncodeError as exc:
        raise MediaError("Cookie 文件字符编码不正确，请使用 UTF-8 文本。") from exc

    # Split only on LF: splitlines() also treats control characters embedded in
    # a value as separators, potentially hiding an invalid record.
    lines = text.lstrip("\ufeff").split("\n")
    if not lines or lines[0].rstrip("\r").strip() not in _HEADERS:
        raise MediaError("缺少 Netscape Cookie 文件头，请导入 cookies.txt 格式内容。")

    records = 0
    cookies = {}
    for row, raw in enumerate(lines[1:], start=2):
        line = raw[:-1] if raw.endswith("\r") else raw
        if not line.strip():
            continue
        http_only = line.startswith("#HttpOnly_")
        if line.startswith("#") and not http_only:
            continue
        if http_only:
            line = line[len("#HttpOnly_"):]
        records += 1
        if records > _MAX_RECORDS:
            raise MediaError("Cookie 文件最多允许 3000 条记录，请仅导出抖音网站的 Cookie。")
        fields = line.split("\t")
        if len(fields) != 7:
            raise _row_error(row)
        domain, subdomains, path, secure, expiry, name, value = fields
        if (subdomains not in ("TRUE", "FALSE") or secure not in ("TRUE", "FALSE")
                or not expiry.isascii() or not expiry.isdecimal() or len(expiry) > 19):
            raise _row_error(row)
        expiration = int(expiry)
        if expiration > 2**63 - 1:
            raise _row_error(row)
        if (not name or len(name) > 256 or _has_control(name)
                or any(char in _NAME_SEPARATORS for char in name)
                or len(value) > 16384 or _has_control(value)
                or not path.startswith("/") or len(path) > 4096 or _has_control(path)):
            raise _row_error(row)
        host = domain.removeprefix(".").lower()
        if (len(host) > 253 or not host.isascii()
                or not all(_HOST_LABEL.fullmatch(label) for label in host.split("."))):
            raise _row_error(row)
        if not any(host == allowed or host.endswith("." + allowed) for allowed in _DOMAINS):
            continue

        include_subdomains = subdomains == "TRUE"
        # The explicit flag controls scope, including exports that omit the
        # redundant leading dot. FALSE always produces a host-only cookie.
        cookie_domain = "." + host if include_subdomains else host
        cookie = Cookie(
            version=0, name=name, value=value,
            port=None, port_specified=False,
            domain=cookie_domain, domain_specified=include_subdomains,
            domain_initial_dot=include_subdomains,
            path=path, path_specified=True, secure=secure == "TRUE",
            expires=expiration or None, discard=expiration == 0,
            comment=None, comment_url=None,
            rest={"HTTPOnly": ""} if http_only else {}, rfc2109=False,
        )
        # Last occurrence wins deterministically, including an expired last row.
        cookies[(cookie.domain, cookie.path, cookie.name)] = cookie
    current = time.time()
    live = tuple(cookie for cookie in cookies.values() if not cookie.is_expired(current))
    if not live:
        raise MediaError("文件中没有可用的抖音 Cookie，可能已过期或导出了其他网站。")
    return live


class SessionStore:
    """Thread-safe process-local sessions; snapshots never share mutable cookies."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cookies: tuple[Cookie, ...] = ()

    def _live(self) -> tuple[Cookie, ...]:
        current = time.time()
        return tuple(cookie for cookie in self._cookies if not cookie.is_expired(current))

    def import_text(self, platform: str, cookies_text: str) -> dict:
        _platform(platform)
        cookies = _parse(cookies_text)
        with self._lock:
            self._cookies = cookies
            return self._status()

    def snapshot(self, platform: str) -> tuple[Cookie, ...]:
        _platform(platform)
        with self._lock:
            return tuple(deepcopy(cookie) for cookie in self._live())

    def _status(self) -> dict:
        count = len(self._live())
        return {"configured": count > 0, "count": count, "platform": "douyin", "storage": "memory"}

    def status(self, platform: str) -> dict:
        _platform(platform)
        with self._lock:
            return self._status()

    def clear(self, platform: str) -> dict:
        _platform(platform)
        with self._lock:
            self._cookies = ()
            return self._status()

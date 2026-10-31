# SPDX-License-Identifier: GPL-3.0-or-later
"""Small, original URL adapters; network validation remains in ``network``.

Only page URLs with a known meaning are rewritten. Media CDN URLs and unknown
platforms retain their query strings, including signatures and parameter order.
"""
import re
from urllib.parse import parse_qs, urlsplit

from .network import MediaError


_DOUYIN_HOSTS = frozenset({"douyin.com", "www.douyin.com"})
_SHARE_HOSTS = frozenset({"iesdouyin.com", "www.iesdouyin.com"})
_SHORT_HOSTS = frozenset({"v.douyin.com"})
_PAGE_HOSTS = _DOUYIN_HOSTS | _SHARE_HOSTS | _SHORT_HOSTS | {"live.douyin.com"}
_TEXT_BOUNDARY = re.compile(r'[\s<>"“”‘’「」『』【】]')
_HTTP_URL = re.compile(r'https?://[^\s<>"“”‘’「」『』【】，。！？；、]+', re.IGNORECASE)
_VIDEO_ID = re.compile(r"[0-9]{1,32}\Z")


def extract_share_url(value: str) -> str:
    """Accept one HTTP URL or one URL embedded in a copied share message.

    HTML ampersands and Markdown-escaped ampersands are presentation escapes.
    Other escapes remain unchanged so ``validate_url`` can reject unsafe input.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise MediaError("请输入有效的视频网址，或包含一个视频链接的分享文字。")
    value = value.strip()
    if any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in value):
        raise MediaError("网址包含不支持的字符。")

    # A complete URL needs no text cleanup. In particular, punctuation in signed
    # query strings must not be stripped. Validation still runs before any I/O.
    if re.match(r"https?://", value, re.IGNORECASE) and not _TEXT_BOUNDARY.search(value):
        candidate = value
    else:
        matches = list(_HTTP_URL.finditer(value))
        if not matches:
            raise MediaError("未找到 HTTP / HTTPS 视频链接，请粘贴网址或平台分享文字。")
        if len(matches) != 1:
            raise MediaError("分享文字包含多个网址，请一次只粘贴一个视频链接。")
        candidate = matches[0].group()
        # A closing Markdown delimiter belongs to the surrounding message only
        # when its matching opener is outside the URL.
        before = value[:matches[0].start()]
        if before.endswith("(") and candidate.endswith(")"):
            candidate = candidate[:-1]
        elif before.endswith("[") and candidate.endswith("]"):
            candidate = candidate[:-1]
    return candidate.replace("&amp;", "&").replace("\\&", "&")


def platform_for_url(value: str) -> str | None:
    """Identify exact known page hosts, never a hostname suffix or a CDN."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme in ("http", "https") and parsed.hostname in _PAGE_HOSTS:
            return "douyin"
    except (TypeError, ValueError):
        pass
    return None


def normalize_media_url(value: str) -> str:
    """Return the one pasted URL, canonicalizing supported Douyin video pages.

    This function deliberately does not resolve DNS, follow short links, obtain
    cookies or grant access. Callers must still pass the result to validate_url.
    """
    value = extract_share_url(value)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MediaError("网址格式不正确。") from exc
    # Never remove an unsafe authority or character by rewriting a known host.
    if (parsed.username is not None or parsed.password is not None
            or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise MediaError("请使用不含账号密码或特殊转义字符的公开视频网址。")
    if parsed.hostname not in _PAGE_HOSTS or parsed.hostname in _SHORT_HOSTS:
        return value
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        return value

    video_id = None
    if parsed.hostname in _DOUYIN_HOSTS:
        video_path = re.fullmatch(r"/video/([^/]+)/?", parsed.path)
        if video_path:
            video_id = video_path.group(1)
        elif parsed.path == "/" or parsed.path.startswith("/user/"):
            modal_ids = parse_qs(parsed.query, keep_blank_values=True).get("modal_id")
            if modal_ids:
                if len(modal_ids) != 1:
                    raise MediaError("链接中有多个视频编号，请重新复制单个视频的分享链接。")
                video_id = modal_ids[0]
    elif parsed.hostname in _SHARE_HOSTS:
        share_path = re.fullmatch(r"/share/video/([^/]+)/?", parsed.path)
        if share_path:
            video_id = share_path.group(1)

    if video_id is not None:
        if not _VIDEO_ID.fullmatch(video_id):
            raise MediaError("抖音视频编号不正确，请重新复制该视频的分享链接。")
        return f"https://www.douyin.com/video/{video_id}"
    if (parsed.hostname == "live.douyin.com" or parsed.path.startswith("/live")
            or parsed.path == "/user" or parsed.path.startswith("/user/")):
        raise MediaError("这是抖音个人主页或直播地址，未包含单个视频编号。请打开具体视频，再复制分享链接。")
    return value

# SPDX-License-Identifier: GPL-3.0-or-later
"""Douyin-specific discovery; completed files still use the common job engine."""
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit
import urllib.error

from .douyin_data import parse_embedded_page
from .media import probe
from .network import MediaError, open_url, validate_url

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (Linux; Android 12; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"


@dataclass(frozen=True)
class DouyinRequest:
    original_url: str
    canonical_url: str
    video_id: str
    url_type: str

    @classmethod
    def from_urls(cls, original_url, canonical_url):
        parsed = urlsplit(canonical_url)
        identifier = parsed.path.rstrip("/").split("/")[-1]
        if not parsed.path.startswith("/video/") or not identifier.isascii() or not identifier.isdecimal():
            raise MediaError("无法从这个抖音地址确认单条视频编号，请复制具体作品的分享链接。", code="video_id_not_found")
        original = urlsplit(original_url)
        kind = "modal" if parse_qs(original.query).get("modal_id") else "short" if original.hostname == "v.douyin.com" else "video"
        return cls(original_url, canonical_url, identifier, kind)


def diagnostic(strategy, status, message, http_status=None):
    return {"strategy": strategy, "label": strategy, "status": status, "message": message, "http_status": http_status}


class DouyinAdapter:
    def __init__(self, settings, sessions, website, direct):
        self.settings, self.sessions = settings, sessions
        self.website, self.direct = website, direct

    def page_strategy(self, request, url, user_agent, cookies, diagnostics, label):
        status = None
        try:
            with open_url(url, self.settings.demo_url, {"User-Agent": user_agent}, cookies) as response:
                status = response.status
                body = response.read(5 * 1024**2 + 1)
                if len(body) > 5 * 1024**2:
                    raise MediaError("页面超过解析大小限制。")
                html = body.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            detail = parse_embedded_page(html, request.video_id)
            if detail:
                media = self.from_detail(detail, request, cookies, user_agent)
                diagnostics.append(diagnostic(label, "success", "找到与目标视频编号一致的媒体数据。", status))
                return media
            diagnostics.append(diagnostic(label, "failed", "页面没有提供该视频的有效媒体数据。", status))
        except urllib.error.HTTPError as exc:
            diagnostics.append(diagnostic(label, "failed", "站点拒绝页面请求。", exc.code))
        except (MediaError, OSError, UnicodeError, LookupError) as exc:
            if isinstance(exc, MediaError) and exc.code == "drm_protected":
                raise
            diagnostics.append(diagnostic(label, "failed", "页面或媒体信息无法读取。", status))
        return None

    def analyze(self, request):
        cookies = self.sessions.snapshot("douyin")
        diagnostics = [diagnostic("链接识别", "success", f"已识别 {request.url_type} 类型，视频编号 {request.video_id}。")]
        strategies = [(request.original_url, DESKTOP_UA, "原始页面数据"),
                      (f"https://www.iesdouyin.com/share/video/{request.video_id}/", MOBILE_UA, "分享页结构化数据")]
        for url, ua, label in strategies:
            media = self.page_strategy(request, url, ua, cookies, diagnostics, label)
            if media:
                return self._finish(media, request, diagnostics)
        try:
            media = self.website.analyze(request.canonical_url)
            diagnostics.append(diagnostic("网站适配器", "success", "网站适配器取得媒体信息。"))
            return self._finish(media, request, diagnostics)
        except MediaError as exc:
            diagnostics.append(diagnostic("网站适配器", "failed", str(exc), getattr(exc, "http_status", None)))
            if exc.code != "drm_protected":
                diagnostics.append(diagnostic("浏览器解析", "waiting", "可以选择浏览器解析，在打开的窗口中正常登录并播放目标视频。"))
            exc.normalized_url = request.canonical_url
            exc.diagnostics = diagnostics
            raise

    @staticmethod
    def _finish(media, request, diagnostics):
        media.update(url=request.canonical_url, source_id=request.video_id, platform="douyin", url_type=request.url_type,
                     diagnostics=diagnostics, _original_url=request.original_url)
        return media

    def from_detail(self, detail, request, cookies=(), user_agent=DESKTOP_UA):
        if detail.get("has_drm"):
            raise MediaError("这个视频受到 DRM 保护，无法导出。", code="drm_protected", normalized_url=request.canonical_url)
        streams = sorted(detail.get("streams", []), key=lambda s: ((s.get("width") or 0) * (s.get("height") or 0), s.get("bit_rate") or 0), reverse=True)
        candidates = []
        for stream in streams[:8]:
            try:
                url = validate_url(stream["url"], self.settings.demo_url)
                # Only a source tied to this video ID is probed. Codec test clips,
                # recommendations and arbitrary network media are never accepted.
                metadata = probe(url, self.settings, remote=True, referer=request.canonical_url, user_agent=user_agent)
                if not metadata["has_video"]:
                    continue
                candidates.append((url, metadata))
                # One verified stream is enough to offer a working download.
                break
            except (MediaError, OSError):
                continue
        if not candidates:
            raise MediaError("找到作品数据，但播放地址未能通过媒体检查，可能已过期或受到访问限制。", code="stream_not_found")
        media = self.direct._record(request.canonical_url, detail.get("title") or f"抖音作品 {request.video_id}", candidates,
                                    request.canonical_url)
        media.update(extractor="抖音独立适配器", _cookies=cookies, _user_agent=user_agent)
        if any(item.get("url") == candidates[0][0] and item.get("ext") in ("m3u8", "mpd") for item in streams):
            media.update(_provider="website", _download_url=candidates[0][0],
                         _download_headers={"Referer": request.canonical_url, "User-Agent": user_agent})
        return media

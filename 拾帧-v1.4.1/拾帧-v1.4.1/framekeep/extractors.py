# SPDX-License-Identifier: GPL-3.0-or-later
"""Original direct/HTML parser plus a replaceable, optional yt-dlp adapter.

No extractor source is vendored. Website adapters can implement analyze/download
and be registered in ExtractorService without changing the UI or job engine.
"""
import importlib
import copy
import json
import re
import shutil
import threading
import time
import urllib.error
from html.parser import HTMLParser
from http.cookiejar import DefaultCookiePolicy
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4

from .media import probe
from .network import Cancelled, MediaError, download_file, open_url, validate_url
from .platforms import extract_share_url, normalize_media_url, platform_for_url
from .sessions import SessionStore
from .douyin import DouyinAdapter, DouyinRequest

MEDIA_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mp3", ".aac", ".m4a", ".wav", ".ogg", ".flac", ".ts"}


class VideoHTMLParser(HTMLParser):
    """Read declared media only; never evaluate a website's JavaScript."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []
        self.title = ""
        self._title = False
        self._json = False
        self._json_data = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._title = True
        if tag in ("video", "audio", "source") and attrs.get("src"):
            self.urls.append(attrs["src"])
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream") and attrs.get("content"):
                self.urls.append(attrs["content"])
            if key == "og:title" and attrs.get("content"):
                self.title = attrs["content"]
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self._json = True
            self._json_data = []

    def handle_data(self, data):
        if self._title and not self.title:
            self.title = data.strip()
        if self._json:
            self._json_data.append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self._title = False
        if tag == "script" and self._json:
            self._json = False
            try:
                self._walk(json.loads("".join(self._json_data)))
            except (ValueError, RecursionError):
                pass

    def _walk(self, value):
        if isinstance(value, list):
            for child in value:
                self._walk(child)
        elif isinstance(value, dict):
            if value.get("@type") in ("VideoObject", "AudioObject"):
                if isinstance(value.get("contentUrl"), str):
                    self.urls.append(value["contentUrl"])
                if value.get("name") and not self.title:
                    self.title = str(value["name"])
            for child in value.values():
                if isinstance(child, (list, dict)):
                    self._walk(child)


def filename_title(url):
    return Path(unquote(urlsplit(url).path)).stem[:160] or urlsplit(url).hostname or "媒体文件"


class DirectExtractor:
    name = "原生解析"

    def __init__(self, settings):
        self.settings = settings

    def analyze(self, url):
        with open_url(url, self.settings.demo_url) as response:
            content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
            final_url = response.url
            suffix = Path(urlsplit(final_url).path).suffix.lower()
            if content_type.startswith(("video/", "audio/")) or suffix in MEDIA_SUFFIXES:
                metadata = probe(final_url, self.settings, remote=True)
                return self._record(url, filename_title(url), [(final_url, metadata)])
            if "html" not in content_type:
                raise MediaError("原生解析未发现媒体文件。")
            body = response.read(2 * 1024**2 + 1)
            if len(body) > 2 * 1024**2:
                raise MediaError("网页过大，转交网站解析器处理。")
            charset = response.headers.get_content_charset() or "utf-8"
        parser = VideoHTMLParser()
        try:
            parser.feed(body.decode(charset, errors="replace"))
        except (ValueError, LookupError):
            parser.feed(body.decode("utf-8", errors="replace"))
        candidates = []
        seen = set()
        for item in parser.urls[:12]:
            candidate = urljoin(final_url, item)
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                validate_url(candidate, self.settings.demo_url)
                if Path(urlsplit(candidate).path).suffix.lower() in (".m3u8", ".mpd"):
                    continue  # Adaptive streams are handled by the optional adapter.
                with open_url(candidate, self.settings.demo_url, {"Referer": final_url}) as source_response:
                    candidate = source_response.url
                metadata = probe(candidate, self.settings, remote=True, referer=final_url)
                candidates.append((candidate, metadata))
            except MediaError:
                continue
        if not candidates:
            raise MediaError("网页中没有直接声明可下载的视频或音频。")
        return self._record(url, parser.title or filename_title(url), candidates, final_url)

    def _record(self, url, title, candidates, referer=None):
        candidates.sort(key=lambda item: ((item[1].get("width") or 0) * (item[1].get("height") or 0), item[1].get("fps") or 0), reverse=True)
        formats, sources = [], {}
        for index, (source, metadata) in enumerate(candidates):
            identifier = str(index)
            ext = Path(urlsplit(source).path).suffix.lstrip(".").lower()
            if "." + ext not in MEDIA_SUFFIXES:
                fmt = metadata.get("format_name", "")
                ext = "mp4" if "mp4" in fmt else "webm" if "webm" in fmt else "mkv" if "matroska" in fmt else "bin"
            label = f"{metadata['width']} × {metadata['height']}" if metadata.get("has_video") else "音频"
            formats.append({"id": identifier, "label": f"{label} · {ext.upper()}", "width": metadata.get("width"),
                            "height": metadata.get("height"), "ext": ext, "fps": metadata.get("fps")})
            sources[identifier] = {"url": source, "ext": ext, "metadata": metadata}
        return {**candidates[0][1], "id": uuid4().hex, "url": url, "title": title[:300], "extractor": self.name,
                "formats": formats, "thumbnail": None, "source_size": candidates[0][1].get("size"),
                "_provider": "direct", "_sources": sources, "_referer": referer, "_created": time.time()}

    def download(self, media, options, folder, progress, cancel):
        format_id = options.get("format_id", "best")
        source = media["_sources"]["0" if format_id == "best" else format_id]
        destination = folder / ("source." + source["ext"])
        headers = {"Referer": media["_referer"]} if media.get("_referer") else {}
        if media.get("_user_agent"):
            headers["User-Agent"] = media["_user_agent"]
        return download_file(source["url"], destination, self.settings, progress, cancel, headers, media.get("_cookies", ()))


class QuietLogger:
    def debug(self, message):
        pass
    def warning(self, message):
        pass
    def error(self, message):
        pass


def site_failure(url, error, has_session=False):
    """Translate upstream errors without echoing credentials or signed URLs."""
    reason = str(error).lower()
    if platform_for_url(url) == "douyin":
        if any(word in reason for word in ("fresh cookies", "log in", "login", "sign in", "403", "captcha")):
            if has_session:
                return MediaError("抖音仍拒绝这次访问。已导入会话，但它可能过期、需要在浏览器完成验证，或该视频不可访问。请先确认浏览器能播放，再更新抖音访问设置。",
                                  code="session_rejected", normalized_url=url)
            return MediaError("已识别为抖音单条视频，但抖音拒绝了当前请求。请选择「在浏览器中解析」，在打开的窗口里登录并播放视频；也可以在「抖音访问设置」中导入自己的有效 Cookie 文件后重试。",
                              code="session_required", normalized_url=url)
        if "unsupported url" in reason:
            return MediaError("暂时无法识别这个抖音页面，请从具体视频的分享菜单复制链接。",
                              code="unsupported_url", normalized_url=url)
        return MediaError("已识别抖音链接，但网站没有返回可下载的视频信息。请先确认视频可播放；视频失效、访问限制或站点规则变化都可能导致失败。",
                          code="site_rejected", normalized_url=url)
    return MediaError("网站解析失败：该链接可能需要登录、已失效或暂不受支持。可尝试媒体直链；网站规则变化后可更新 yt-dlp。",
                      code="site_rejected")


class WebsiteExtractor:
    name = "yt-dlp 网站适配器"

    def __init__(self, settings, sessions=None):
        self.settings = settings
        self.sessions = sessions if sessions is not None else SessionStore()

    @staticmethod
    def _apply_session(downloader, cookies):
        if cookies:
            downloader.cookiejar.set_policy(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
        for cookie in cookies:
            downloader.cookiejar.set_cookie(copy.copy(cookie))

    def _options(self):
        options = {"quiet": True, "no_warnings": True, "logger": QuietLogger(), "noplaylist": True,
                   "socket_timeout": 20, "retries": 2, "fragment_retries": 2,
                   "cachedir": False, "skip_unavailable_fragments": False,
                   "ffmpeg_location": str(Path(self.settings.ffmpeg).parent) if self.settings.ffmpeg else None,
                   "js_runtimes": {"node": {"path": self.settings.node}} if self.settings.node else {}}
        return options

    def analyze(self, url):
        if not self.settings.yt_dlp:
            raise MediaError("此网页需要网站适配器，请运行 setup.bat 安装可选的 yt-dlp 依赖。")
        yt_dlp = importlib.import_module("yt_dlp")
        platform = platform_for_url(url)
        cookies = self.sessions.snapshot(platform) if platform else ()
        try:
            with yt_dlp.YoutubeDL(self._options()) as ydl:
                self._apply_session(ydl, cookies)
                info = ydl.extract_info(url, download=False)
            if not info or info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
                raise MediaError("请提供单个视频的网址，当前不处理播放列表。")
            if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
                raise MediaError("当前支持已发布的视频文件，暂不录制直播。")
            if info.get("has_drm"):
                raise MediaError("这个媒体受 DRM 保护，无法提取。", code="drm_protected")
            if platform == "douyin" and str(info.get("id", "")) != urlsplit(url).path.rstrip("/").split("/")[-1]:
                raise MediaError("网站返回的数据未能确认属于目标视频，已停止这次解析。", code="video_id_mismatch", normalized_url=url)
            entries = [f for f in (info.get("formats") or [info]) if not f.get("has_drm") and f.get("url")]
            videos = [f for f in entries if f.get("vcodec") not in ("none", None) and f.get("height")]
            videos.sort(key=lambda f: ((f.get("width") or 0) * (f.get("height") or 0), f.get("fps") or 0, f.get("tbr") or 0), reverse=True)
            audio = next((f for f in reversed(entries) if f.get("acodec") not in ("none", None)), {})
            best = videos[0] if videos else (entries[-1] if entries else {})
            if not best:
                raise MediaError("没有找到可下载的媒体格式。")
            measured = {}
            if not videos or info.get("duration") is None:
                # Generic adapters may provide a valid URL with no codec/dimension metadata.
                # Unknown streams must not be reported as absent before probing the source.
                try:
                    source_url = validate_url(best["url"], self.settings.demo_url)
                    measured = probe(source_url, self.settings, remote=True, referer=url)
                except MediaError:
                    pass
                if not videos and measured.get("has_video"):
                    best = {**best, "width": measured["width"], "height": measured["height"],
                            "fps": measured["fps"], "vcodec": measured["video_codec"]}
                    videos = [best]
            formats = [{"id": str(f["format_id"]), "label": f"{f.get('width', '?')} × {f.get('height', '?')} · {f.get('ext', '').upper()} · {f.get('fps') or '?'} fps",
                        "width": f.get("width"), "height": f.get("height"), "ext": f.get("ext"), "fps": f.get("fps")}
                       for f in videos if f.get("format_id") is not None]
            return {"id": uuid4().hex, "url": url, "title": str(info.get("title") or filename_title(url))[:300],
                    "duration": info.get("duration") or measured.get("duration"), "width": best.get("width"), "height": best.get("height"),
                    "fps": best.get("fps"), "video_codec": best.get("vcodec") if videos else None,
                    "audio_codec": audio.get("acodec") or measured.get("audio_codec"),
                    "has_video": bool(videos), "has_audio": bool(audio) or measured.get("has_audio", False),
                    "extractor": self.name + " · " + str(info.get("extractor_key") or "Generic"),
                    "formats": formats, "thumbnail": None, "source_size": best.get("filesize") or measured.get("size"),
                    "_provider": "website", "_cookies": cookies, "_created": time.time()}
        except MediaError:
            raise
        except Exception as exc:
            raise site_failure(url, exc, bool(cookies)) from exc

    def download(self, media, options, folder, progress, cancel):
        yt_dlp = importlib.import_module("yt_dlp")
        format_id = "best" if media.get("_download_url") else options.get("format_id", "best")
        if options["kind"] == "audio":
            selection = "bestaudio/best"
        elif format_id == "best":
            selection = "bestvideo+bestaudio/best"
        else:
            # IDs are checked against the analyzed format list; only safe ID characters allowed.
            selection = f"{format_id}+bestaudio/{format_id}"
        started = time.monotonic()
        def hook(event):
            if cancel.is_set():
                raise Cancelled("任务已取消。")
            if time.monotonic() - started > 4 * 3600:
                raise MediaError("下载超过 4 小时限制。")
            # yt-dlp emits the merger's finished hook before deleting input
            # tracks. Count the completed source once during that transition.
            excluded = set()
            info = event.get("info_dict") or {}
            merged = info.get("filepath")
            if event.get("status") == "finished" and isinstance(merged, (str, Path)):
                merged = Path(merged).resolve()
                if merged.parent == folder.resolve() and merged.is_file():
                    for item in info.get("__files_to_merge") or []:
                        if isinstance(item, (str, Path)):
                            track = Path(item).resolve()
                            if track.parent == folder.resolve() and track != merged:
                                excluded.add(track)
            size = sum(p.stat().st_size for p in folder.iterdir()
                       if p.is_file() and p.resolve() not in excluded)
            if size > self.settings.max_download_bytes:
                raise MediaError(f"源文件超过 {self.settings.max_download_bytes / 1024**3:g} GiB 限制。")
            total = event.get("total_bytes") or event.get("total_bytes_estimate") or 0
            downloaded = event.get("downloaded_bytes") or 0
            progress(min(54, downloaded / total * 54) if total else 12, f"已下载 {downloaded / 1024**2:.1f} MiB")
        opts = {**self._options(), "format": selection, "outtmpl": str(folder / "source.%(ext)s"),
                "merge_output_format": "mkv", "progress_hooks": [hook], "postprocessor_hooks": [hook],
                "max_filesize": self.settings.max_download_bytes, "windowsfilenames": True,
                "concurrent_fragment_downloads": 1, "overwrites": True}
        if media.get("_download_headers"):
            opts["http_headers"] = media["_download_headers"]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                self._apply_session(ydl, media.get("_cookies", ()))
                ydl.extract_info(media.get("_download_url") or media["url"], download=True)
            if cancel.is_set():
                raise Cancelled("任务已取消。")
            candidates = [p for p in folder.glob("source.*") if p.suffix.lower() not in (".part", ".ytdl", ".temp") and p.is_file()]
            if not candidates:
                raise MediaError(f"网站没有生成完整文件，可能超过 {self.settings.max_download_bytes / 1024**3:g} GiB 限制或链接已过期。")
            source = max(candidates, key=lambda p: p.stat().st_size)
            if source.stat().st_size > self.settings.max_download_bytes:
                raise MediaError(f"源文件超过 {self.settings.max_download_bytes / 1024**3:g} GiB 限制。")
            return source
        except MediaError:
            raise
        except Exception as exc:
            if cancel.is_set():
                raise Cancelled("任务已取消。") from exc
            if platform_for_url(media["url"]) == "douyin":
                raise site_failure(media["url"], exc, bool(media.get("_cookies"))) from exc
            raise MediaError("网站下载失败：链接可能已过期或访问受限。请重新解析后再试。") from exc


class ExtractorService:
    def __init__(self, settings):
        self.settings = settings
        self.sessions = SessionStore()
        self.providers = {"direct": DirectExtractor(settings), "website": WebsiteExtractor(settings, self.sessions)}
        self.douyin = DouyinAdapter(settings, self.sessions, self.providers["website"], self.providers["direct"])
        self.records = {}
        self.lock = threading.Lock()
        self.analysis_slots = threading.BoundedSemaphore(2)

    def prepare_urls(self, value):
        original = extract_share_url(value)
        url = validate_url(normalize_media_url(value), self.settings.demo_url)
        if urlsplit(url).hostname == "v.douyin.com":
            try:
                with open_url(url, self.settings.demo_url) as response:
                    url = validate_url(normalize_media_url(response.url), self.settings.demo_url)
            except (OSError, urllib.error.URLError) as exc:
                raise site_failure(url, exc) from exc
        return original, url

    def analyze(self, value):
        original, url = self.prepare_urls(value)
        if not self.settings.ffprobe:
            raise MediaError("未找到 FFprobe，请先配置 FFmpeg 与 FFprobe。")
        if not self.analysis_slots.acquire(blocking=False):
            raise MediaError("正在解析其他链接，请稍后再试。")
        try:
            if platform_for_url(url):
                # A platform page can contain unrelated background/demo media.
                # Dispatch it to its site adapter instead of treating it as a generic HTML page.
                media = self.douyin.analyze(DouyinRequest.from_urls(original, url))
            else:
                try:
                    media = self.providers["direct"].analyze(url)
                except (MediaError, OSError, urllib.error.URLError):
                    media = self.providers["website"].analyze(url)
            media["url"] = url
            return self.remember(media)
        finally:
            self.analysis_slots.release()

    def remember(self, media):
        with self.lock:
            self.records = {key: item for key, item in self.records.items() if time.time() - item["_created"] < 3600}
            if len(self.records) >= 100:
                self.records.pop(next(iter(self.records)))
            self.records[media["id"]] = media
        return self.public(media)

    def clear_session(self, platform):
        status = self.sessions.clear(platform)
        with self.lock:
            # Existing worker snapshots finish independently; unstarted results are invalidated.
            self.records = {key: record for key, record in self.records.items()
                            if platform_for_url(record.get("url", "")) != platform}
        return status

    def get(self, media_id):
        with self.lock:
            media = self.records.get(media_id)
        if not media or time.time() - media["_created"] > 3600:
            raise MediaError("解析结果已过期，请重新解析链接。")
        return media

    @staticmethod
    def public(media):
        return {key: value for key, value in media.items() if not key.startswith("_")}

    def download(self, media, options, folder, progress, cancel):
        return self.providers[media["_provider"]].download(media, options, folder, progress, cancel)

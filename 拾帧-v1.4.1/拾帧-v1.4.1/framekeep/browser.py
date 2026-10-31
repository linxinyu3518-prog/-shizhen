# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit, user-operated browser discovery with an isolated temporary profile.

No personal browser database is read. Login and verification remain user actions.
Only structured data bearing the requested video ID can create a media result.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from .storage import worker_environment
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .douyin import DouyinRequest, diagnostic
from .douyin_data import parse_embedded_page, parse_video_detail
from .network import Cancelled, MediaError
from .platforms import platform_for_url


def playwright_cookies(cookies):
    result = []
    for cookie in cookies:
        item = {"name": cookie.name, "value": cookie.value, "secure": cookie.secure,
                "httpOnly": cookie.has_nonstandard_attr("HTTPOnly")}
        if cookie.domain_specified:
            item.update(domain=cookie.domain, path=cookie.path)
        else:
            item["url"] = ("https://" if cookie.secure else "http://") + cookie.domain + cookie.path
        if cookie.expires is not None:
            item["expires"] = cookie.expires
        result.append(item)
    return result


def store_browser_session(store, browser_cookies):
    rows = ["# Netscape HTTP Cookie File"]
    for cookie in browser_cookies:
        domain = cookie.get("domain", "")
        host = domain.lstrip(".").lower()
        if not any(host == allowed or host.endswith("." + allowed) for allowed in ("douyin.com", "iesdouyin.com")):
            continue
        rows.append("\t".join([("#HttpOnly_" if cookie.get("httpOnly") else "") + domain,
                              "TRUE" if domain.startswith(".") else "FALSE", cookie.get("path", "/"),
                              "TRUE" if cookie.get("secure") else "FALSE", str(max(0, int(cookie.get("expires", 0)))),
                              cookie["name"], cookie["value"]]))
    try:
        store.import_text("douyin", "\n".join(rows) + "\n")
    except MediaError:
        pass
    return store.snapshot("douyin")


def discover_in_browser(settings, extractors, request, cancel, update, timeout=180, headless=False):
    # headless is an internal testing seam, never a user-controlled API field.
    from playwright.sync_api import sync_playwright
    environment = worker_environment(settings.data_dir)
    candidate = None
    http_status = None
    diagnostics = [diagnostic("链接识别", "success", f"目标视频编号 {request.video_id}。")]
    with sync_playwright() as engine:
        browser = engine.chromium.launch(executable_path=settings.browser, headless=headless, env=environment)
        try:
            context = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 860})
            cookies = extractors.sessions.snapshot("douyin")
            if cookies:
                context.add_cookies(playwright_cookies(cookies))
            page = context.new_page()

            def response_received(response):
                nonlocal candidate, http_status
                parsed = urlsplit(response.url)
                if parsed.hostname not in ("www.douyin.com", "douyin.com", "www.iesdouyin.com"):
                    return
                if "/aweme/" not in parsed.path or not any(marker in parsed.path for marker in
                        ("/detail", "/iteminfo", "/feed", "/favorite", "/post", "/history")):
                    return
                ids = parse_qs(parsed.query).get("aweme_id") or parse_qs(parsed.query).get("item_ids")
                if ids and request.video_id not in ids:
                    return
                http_status = response.status
                if response.status != 200:
                    return
                try:
                    length = int(response.headers.get("content-length", "0"))
                    if length > 5 * 1024**2:
                        return
                    body = response.body()
                    if len(body) <= 5 * 1024**2:
                        result = parse_video_detail(json.loads(body), request.video_id)
                        if result:
                            candidate = result
                except Exception:
                    # A response can be aborted when navigation changes. It is
                    # not a failure of the entire user-operated browser session.
                    pass

            page.on("response", response_received)
            update("waiting", "请在打开的浏览器中登录抖音，并播放这条视频；完成后会自动读取。")
            try:
                page.goto(request.original_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                if page.is_closed():
                    raise Cancelled("浏览器窗口已关闭。")
            started = time.monotonic()
            last_page_read = 0
            while time.monotonic() - started < timeout:
                if cancel.is_set():
                    raise Cancelled("已取消浏览器解析。")
                if page.is_closed():
                    raise Cancelled("浏览器窗口已关闭。")
                if candidate is None and time.monotonic() - last_page_read > 3:
                    last_page_read = time.monotonic()
                    candidate = parse_embedded_page(page.content(), request.video_id)
                if candidate:
                    update("waiting", "已找到目标视频数据，正在检查播放地址。")
                    cookies = store_browser_session(extractors.sessions, context.cookies())
                    user_agent = page.evaluate("navigator.userAgent")
                    media = extractors.douyin.from_detail(candidate, request, cookies, user_agent)
                    diagnostics.append(diagnostic("浏览器解析", "success", "读取到与目标视频编号一致的媒体数据。", http_status))
                    return extractors.douyin._finish(media, request, diagnostics)
                page.wait_for_timeout(350)
            diagnostics.append(diagnostic("浏览器解析", "failed", "等待结束，未取得目标视频的有效媒体数据。", http_status))
            raise MediaError("浏览器解析等待已结束。请确认在打开的窗口中完成登录或验证，并播放这条视频后再试。", code="browser_wait_timeout",
                             normalized_url=request.canonical_url, diagnostics=diagnostics)
        finally:
            try:
                browser.close()
            except Exception:
                pass  # The user may have already closed the window.


class BrowserManager:
    def __init__(self, settings, extractors, runner=discover_in_browser):
        self.settings, self.extractors, self.runner = settings, extractors, runner
        self.lock = threading.RLock()
        self.jobs = {}
        self.events = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="framekeep-browser")

    def create(self, value):
        if not self.settings.playwright or not self.settings.browser:
            raise MediaError("浏览器解析需要 Playwright 和本机 Chrome / Edge。请运行 setup.bat --with-browser 后重启。", code="browser_unavailable")
        original, canonical = self.extractors.prepare_urls(value)
        if platform_for_url(canonical) != "douyin":
            raise MediaError("当前浏览器解析只支持抖音单条视频。其他平台请使用自动解析。", code="unsupported_url")
        request = DouyinRequest.from_urls(original, canonical)
        with self.lock:
            if self.events:
                raise MediaError("已有一个浏览器解析任务，请先完成或取消它。")
            identifier = uuid4().hex
            state = {"id": identifier, "status": "starting", "message": "正在打开独立浏览器窗口。", "media": None, "error": None}
            self.jobs[identifier] = state
            self.events[identifier] = threading.Event()
            if len(self.jobs) > 20:
                self.jobs.pop(next(iter(self.jobs)))
            self.pool.submit(self._run, identifier, request)
            return deepcopy(state)

    def _run(self, identifier, request):
        cancel = self.events[identifier]
        def update(status, message):
            with self.lock:
                if not cancel.is_set():
                    self.jobs[identifier].update(status=status, message=message)
        try:
            if cancel.is_set():
                raise Cancelled("已取消浏览器解析。")
            media = self.runner(self.settings, self.extractors, request, cancel, update)
            with self.lock:
                if cancel.is_set():
                    raise Cancelled("已取消浏览器解析。")
                public = self.extractors.remember(media)
                self.jobs[identifier].update(status="completed", message="解析完成。", media=public)
        except Cancelled as exc:
            with self.lock:
                self.jobs[identifier].update(status="cancelled", message=str(exc))
        except Exception as exc:
            error = exc if isinstance(exc, MediaError) else MediaError("浏览器解析未能完成。窗口可能已关闭、网络中断或页面未提供数据，请重试。", code="browser_error")
            with self.lock:
                if cancel.is_set():
                    self.jobs[identifier].update(status="cancelled", message="已取消浏览器解析。", error=None)
                else:
                    self.jobs[identifier].update(status="failed", message=str(error), error={"error": str(error), "code": error.code,
                        "normalized_url": request.canonical_url, "diagnostics": error.diagnostics})
        finally:
            with self.lock:
                self.events.pop(identifier, None)

    def get(self, identifier):
        with self.lock:
            if identifier not in self.jobs:
                raise MediaError("找不到这个浏览器解析任务。")
            return deepcopy(self.jobs[identifier])

    def cancel(self, identifier):
        with self.lock:
            if identifier not in self.jobs:
                raise MediaError("找不到这个浏览器解析任务。")
            if identifier in self.events:
                self.events[identifier].set()
                self.jobs[identifier].update(status="cancelled", message="已请求取消，浏览器即将关闭。")
            return deepcopy(self.jobs[identifier])

    def close(self):
        with self.lock:
            for event in self.events.values():
                event.set()
        self.pool.shutdown(wait=True)

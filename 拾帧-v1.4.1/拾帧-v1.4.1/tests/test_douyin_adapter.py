# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline adapter regressions with original synthetic page and media fixtures."""
from email.message import Message
from http.cookiejar import CookieJar
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

from framekeep.config import Settings
from framekeep.douyin import DESKTOP_UA, MOBILE_UA, DouyinAdapter, DouyinRequest
from framekeep.extractors import DirectExtractor, ExtractorService, WebsiteExtractor
from framekeep.network import MediaError
from framekeep.sessions import SessionStore


VIDEO_ID = "7670847854776159515"
CANONICAL = f"https://www.douyin.com/video/{VIDEO_ID}"
ORIGINAL = f"https://www.douyin.com/user/self?from_tab_name=main&modal_id={VIDEO_ID}&showTab=like"
SOURCE = "https://cdn.example.org/target.mp4?token=synthetic"
METADATA = {"duration": 12.5, "width": 1080, "height": 1920, "fps": 30,
            "has_video": True, "has_audio": True, "video_codec": "h264",
            "audio_codec": "aac", "size": 12345, "format_name": "mov,mp4"}


class PageResponse:
    def __init__(self, html, status=200, url=CANONICAL):
        self.body = html.encode("utf-8")
        self.status, self.url = status, url
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"

    def read(self, maximum):
        return self.body[:maximum]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def page(video_id=VIDEO_ID, source=SOURCE, drm=False):
    detail = {"aweme_id": video_id, "desc": "目标作品", "video": {
        "width": 1080, "height": 1920, "duration": 12500,
        "is_drm": drm, "play_addr": {"url_list": [source]}}}
    return PageResponse("<script>window._ROUTER_DATA = " + json.dumps(detail) + ";</script>")


class DouyinAdapterTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(ffprobe="synthetic-ffprobe")
        self.sessions = SessionStore()
        self.website = Mock()
        self.adapter = DouyinAdapter(self.settings, self.sessions, self.website, DirectExtractor(self.settings))
        self.request = DouyinRequest.from_urls(ORIGINAL, CANONICAL)
        self.validation = patch("framekeep.douyin.validate_url", side_effect=lambda value, *_: value)
        self.validation.start()
        self.addCleanup(self.validation.stop)
        self.probe = patch("framekeep.douyin.probe", return_value=dict(METADATA)).start()
        self.addCleanup(patch.stopall)

    def test_original_modal_url_and_target_identity_survive_normalization(self):
        self.assertEqual(self.request.original_url, ORIGINAL)
        self.assertEqual(self.request.canonical_url, CANONICAL)
        self.assertEqual(self.request.video_id, VIDEO_ID)
        self.assertEqual(self.request.url_type, "modal")
        short = DouyinRequest.from_urls("https://v.douyin.com/synthetic/", CANONICAL)
        self.assertEqual(short.url_type, "short")

    def test_non_video_or_non_ascii_identity_is_rejected(self):
        for value in ("https://www.douyin.com/user/self", "https://www.douyin.com/video/１２３",
                      "https://www.douyin.com/video/invalid"):
            with self.subTest(value=value), self.assertRaises(MediaError) as caught:
                DouyinRequest.from_urls(ORIGINAL, value)
            self.assertEqual(caught.exception.code, "video_id_not_found")

    def test_first_page_success_stops_fallback_and_returns_common_media(self):
        with patch("framekeep.douyin.open_url", return_value=page()) as opened:
            media = self.adapter.analyze(self.request)
        opened.assert_called_once_with(ORIGINAL, None, {"User-Agent": DESKTOP_UA}, ())
        self.website.analyze.assert_not_called()
        self.assertEqual(media["source_id"], VIDEO_ID)
        self.assertEqual(media["url"], CANONICAL)
        self.assertEqual(media["_original_url"], ORIGINAL)
        self.assertEqual(media["_sources"]["0"]["url"], SOURCE)
        self.assertEqual((media["width"], media["height"], media["duration"]), (1080, 1920, 12.5))
        self.assertEqual(media["diagnostics"][-1]["status"], "success")

    def test_wrong_target_in_original_page_is_ignored_and_share_page_used(self):
        with patch("framekeep.douyin.open_url", side_effect=[page("999", "https://cdn.example.org/wrong.mp4"), page()]) as opened:
            media = self.adapter.analyze(self.request)
        self.assertEqual(opened.call_args_list[0].args[0], ORIGINAL)
        self.assertEqual(opened.call_args_list[1].args[0], f"https://www.iesdouyin.com/share/video/{VIDEO_ID}/")
        self.assertEqual(opened.call_args_list[1].args[2], {"User-Agent": MOBILE_UA})
        self.probe.assert_called_once()
        self.assertEqual(self.probe.call_args.args[0], SOURCE)
        self.assertEqual([item["status"] for item in media["diagnostics"]], ["success", "failed", "success"])
        self.website.analyze.assert_not_called()

    def test_rejected_pages_fall_back_to_canonical_site_adapter(self):
        self.website.analyze.return_value = {"id": "synthetic", "_created": time.time()}
        private_url = ORIGINAL + "&sensitive=not-for-diagnostics"
        denied = HTTPError(private_url, 403, "private-upstream-body", Message(), None)
        with patch("framekeep.douyin.open_url", side_effect=[denied, PageResponse("<html>Login</html>")]):
            media = self.adapter.analyze(self.request)
        self.website.analyze.assert_called_once_with(CANONICAL)
        self.assertEqual(media["diagnostics"][1]["http_status"], 403)
        self.assertEqual(media["diagnostics"][2]["http_status"], 200)
        self.assertEqual(media["diagnostics"][-1]["status"], "success")
        self.assertNotIn("not-for-diagnostics", json.dumps(media["diagnostics"]))
        self.assertNotIn("private-upstream-body", json.dumps(media["diagnostics"]))

    def test_all_automatic_strategies_fail_with_actionable_diagnostics(self):
        error = MediaError("需要有效会话。", code="session_required")
        self.website.analyze.side_effect = error
        with patch("framekeep.douyin.open_url", return_value=PageResponse("<html>Login</html>")), self.assertRaises(MediaError) as caught:
            self.adapter.analyze(self.request)
        self.assertIs(caught.exception, error)
        self.assertEqual(error.normalized_url, CANONICAL)
        self.assertEqual(error.code, "session_required")
        self.assertEqual([item["status"] for item in error.diagnostics], ["success", "failed", "failed", "failed", "waiting"])
        self.probe.assert_not_called()

    def test_protected_target_aborts_without_probing_or_trying_other_strategies(self):
        with patch("framekeep.douyin.open_url", return_value=page(drm=True)) as opened, self.assertRaises(MediaError) as caught:
            self.adapter.analyze(self.request)
        self.assertEqual(caught.exception.code, "drm_protected")
        self.assertEqual(caught.exception.normalized_url, CANONICAL)
        opened.assert_called_once()
        self.probe.assert_not_called()
        self.website.analyze.assert_not_called()

    def test_cookie_snapshot_reaches_pages_and_private_download_context_only(self):
        self.sessions.import_text("douyin", "# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tTRUE\t0\tsession\tsynthetic-secret\n")
        with patch("framekeep.douyin.open_url", return_value=page()) as opened:
            media = self.adapter.analyze(self.request)
        self.assertEqual(opened.call_args.args[3][0].value, "synthetic-secret")
        self.assertEqual(media["_cookies"][0].value, "synthetic-secret")
        self.assertNotIn("synthetic-secret", json.dumps(ExtractorService.public(media)))
        self.sessions.clear("douyin")
        self.assertEqual(media["_cookies"][0].value, "synthetic-secret")

    def test_stream_failures_try_backup_and_report_measured_quality(self):
        detail = {"title": "素材", "streams": [
            {"url": "https://cdn.example.org/high.mp4", "width": 2160, "height": 3840},
            {"url": SOURCE, "width": 720, "height": 1280}]}
        self.probe.side_effect = [MediaError("expired"), dict(METADATA)]
        media = self.adapter.from_detail(detail, self.request)
        self.assertEqual([call.args[0] for call in self.probe.call_args_list], [detail["streams"][0]["url"], SOURCE])
        self.assertEqual((media["width"], media["height"]), (1080, 1920))
        self.assertEqual(media["_sources"]["0"]["url"], SOURCE)

    def test_audio_only_probe_cannot_be_mistaken_for_target_video(self):
        self.probe.return_value = {**METADATA, "has_video": False}
        with self.assertRaises(MediaError) as caught:
            self.adapter.from_detail({"streams": [{"url": SOURCE}]}, self.request)
        self.assertEqual(caught.exception.code, "stream_not_found")

    def test_verified_manifest_keeps_canonical_identity_and_common_downloader(self):
        manifest = "https://cdn.example.org/target.m3u8?signature=synthetic"
        media = self.adapter.from_detail({"streams": [{"url": manifest, "ext": "m3u8"}]}, self.request)
        self.assertEqual(media["url"], CANONICAL)
        self.assertEqual(media["_provider"], "website")
        self.assertEqual(media["_download_url"], manifest)
        self.assertEqual(media["_download_headers"]["Referer"], CANONICAL)
        self.assertNotIn(manifest, json.dumps(ExtractorService.public(media)))

    def test_service_dispatches_douyin_without_generic_html_probe(self):
        service = ExtractorService(self.settings)
        service.douyin = Mock()
        service.douyin.analyze.return_value = {"id": "dispatch", "_created": time.time(), "_cookies": "private"}
        with patch("framekeep.extractors.validate_url", side_effect=lambda value, *_: value), patch.object(service.providers["direct"], "analyze") as generic:
            public = service.analyze("视频 " + ORIGINAL + " 复制打开抖音")
        request = service.douyin.analyze.call_args.args[0]
        self.assertEqual(request.original_url, ORIGINAL)
        self.assertEqual(request.video_id, VIDEO_ID)
        self.assertEqual(public["url"], CANONICAL)
        self.assertNotIn("_cookies", public)
        generic.assert_not_called()

    def test_short_redirect_retains_original_url_for_browser(self):
        service = ExtractorService(self.settings)
        short = "https://v.douyin.com/synthetic/"
        with patch("framekeep.extractors.validate_url", side_effect=lambda value, *_: value), patch("framekeep.extractors.open_url", return_value=PageResponse("", url=ORIGINAL)):
            original, canonical = service.prepare_urls(short)
        self.assertEqual((original, canonical), (short, CANONICAL))

    def test_site_cookie_application_does_not_expand_host_only_scope(self):
        self.sessions.import_text("douyin", "# Netscape HTTP Cookie File\nwww.douyin.com\tFALSE\t/video\tTRUE\t0\tsession\tsynthetic-secret\n")
        downloader = SimpleNamespace(cookiejar=CookieJar())
        cookies = self.sessions.snapshot("douyin")
        WebsiteExtractor._apply_session(downloader, cookies)
        for url, expected in ((CANONICAL, "session=synthetic-secret"),
                              ("https://child.www.douyin.com/video/1", None),
                              ("https://www.douyin.com/user/self", None),
                              ("https://cdn.example.org/video/1", None)):
            with self.subTest(url=url):
                request = Request(url)
                downloader.cookiejar.add_cookie_header(request)
                self.assertEqual(request.get_header("Cookie"), expected)
        next(iter(downloader.cookiejar)).value = "mutated"
        self.assertEqual(cookies[0].value, "synthetic-secret")


if __name__ == "__main__":
    unittest.main()

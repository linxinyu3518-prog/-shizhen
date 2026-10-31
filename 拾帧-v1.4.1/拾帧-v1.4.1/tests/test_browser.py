# SPDX-License-Identifier: GPL-3.0-or-later
"""Browser job lifecycle tests; injected runners never start a personal browser."""
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from framekeep.browser import BrowserManager, playwright_cookies, store_browser_session
from framekeep.config import Settings
from framekeep.extractors import ExtractorService
from framekeep.network import Cancelled, MediaError
from framekeep.sessions import SessionStore


VIDEO_ID = "7670847854776159515"
CANONICAL = f"https://www.douyin.com/video/{VIDEO_ID}"
ORIGINAL = f"https://www.douyin.com/user/self?modal_id={VIDEO_ID}&showTab=like"


class BrowserManagerTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(browser="synthetic-chrome", playwright=True)
        self.service = ExtractorService(self.settings)
        self.validation = patch("framekeep.extractors.validate_url", side_effect=lambda value, *_: value)
        self.validation.start()
        self.addCleanup(self.validation.stop)

    def manager(self, runner):
        manager = BrowserManager(self.settings, self.service, runner)
        self.addCleanup(manager.close)
        return manager

    def media(self, request, **extra):
        return {"id": uuid4().hex, "url": request.canonical_url, "source_id": request.video_id,
                "title": "合成浏览器结果", "_created": time.time(), "_provider": "direct", **extra}

    def finished(self, manager, identifier):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with manager.lock:
                if identifier not in manager.events:
                    return manager.get(identifier)
            threading.Event().wait(0.005)
        self.fail("synthetic browser worker did not finish")

    def test_success_retains_target_and_stores_private_context_outside_json(self):
        seen = []
        self.service.sessions.import_text("douyin", "# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tTRUE\t0\tsession\tsynthetic-secret\n")
        def runner(settings, extractors, request, cancel, update):
            seen.append(request)
            update("waiting", "checking synthetic data")
            return self.media(request, _cookies=extractors.sessions.snapshot("douyin"), _original_url=request.original_url)
        manager = self.manager(runner)
        created = manager.create(ORIGINAL)
        final = self.finished(manager, created["id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(seen[0].original_url, ORIGINAL)
        self.assertEqual(seen[0].video_id, VIDEO_ID)
        self.assertEqual(final["media"]["url"], CANONICAL)
        self.assertNotIn("synthetic-secret", json.dumps(final))
        self.assertNotIn("_cookies", final["media"])
        private = self.service.get(final["media"]["id"])
        self.assertEqual(private["_cookies"][0].value, "synthetic-secret")
        final["media"]["title"] = "external mutation"
        self.assertEqual(manager.get(created["id"])["media"]["title"], "合成浏览器结果")

    def test_missing_dependency_fails_before_url_or_runner_actions(self):
        runner = Mock()
        manager = self.manager(runner)
        for playwright, browser in ((False, "synthetic"), (True, None)):
            self.settings.playwright, self.settings.browser = playwright, browser
            with self.subTest(playwright=playwright, browser=browser), patch.object(self.service, "prepare_urls") as prepare, self.assertRaises(MediaError) as caught:
                manager.create(ORIGINAL)
            self.assertEqual(caught.exception.code, "browser_unavailable")
            prepare.assert_not_called()
        runner.assert_not_called()

    def test_non_douyin_page_never_starts_browser_runner(self):
        runner = Mock()
        manager = self.manager(runner)
        with self.assertRaises(MediaError) as caught:
            manager.create("https://example.org/video/123")
        self.assertEqual(caught.exception.code, "unsupported_url")
        runner.assert_not_called()

    def test_only_one_active_browser_and_cancellation_prevents_caching(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def runner(settings, extractors, request, cancel, update):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test runner release timeout")
            update("waiting", "late update must not override cancellation")
            return self.media(request)
        manager = self.manager(runner)
        created = manager.create(ORIGINAL)
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(MediaError, "已有一个"):
            manager.create(ORIGINAL)
        self.assertEqual(manager.cancel(created["id"])["status"], "cancelled")
        release.set()
        final = self.finished(manager, created["id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertIsNone(final["media"])
        self.assertEqual(self.service.records, {})

    def test_cancellation_wins_over_a_late_browser_exception(self):
        entered = threading.Event()
        def runner(settings, extractors, request, cancel, update):
            entered.set()
            if not cancel.wait(2):
                raise RuntimeError("test cancel timeout")
            raise RuntimeError("Target page, context or browser has been closed")
        manager = self.manager(runner)
        created = manager.create(ORIGINAL)
        self.assertTrue(entered.wait(1))
        manager.cancel(created["id"])
        final = self.finished(manager, created["id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertIsNone(final["error"])

    def test_runner_cancellation_and_completed_cancel_are_stable(self):
        def closed(*args):
            raise Cancelled("浏览器窗口已关闭。")
        manager = self.manager(closed)
        first = manager.create(ORIGINAL)
        self.assertEqual(self.finished(manager, first["id"])["status"], "cancelled")
        manager.runner = lambda settings, extractors, request, cancel, update: self.media(request)
        second = manager.create(ORIGINAL)
        finished = self.finished(manager, second["id"])
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(manager.cancel(second["id"]), finished)

    def test_unknown_exception_is_sanitized_but_actionable_error_keeps_diagnostics(self):
        manager = self.manager(Mock(side_effect=RuntimeError("Cookie: synthetic-secret https://cdn.example.org/?signed=private")))
        created = manager.create(ORIGINAL)
        final = self.finished(manager, created["id"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["error"]["code"], "browser_error")
        self.assertNotIn("synthetic-secret", json.dumps(final))
        self.assertNotIn("signed=private", json.dumps(final))
        diagnostics = [{"strategy": "browser", "status": "failed", "http_status": 403}]
        manager.runner = Mock(side_effect=MediaError("等待登录结束。", code="browser_wait_timeout", diagnostics=diagnostics))
        created = manager.create(ORIGINAL)
        final = self.finished(manager, created["id"])
        self.assertEqual(final["error"]["code"], "browser_wait_timeout")
        self.assertEqual(final["error"]["normalized_url"], CANONICAL)
        self.assertEqual(final["error"]["diagnostics"], diagnostics)

    def test_old_browser_history_is_bounded_and_unknown_ids_are_rejected(self):
        manager = self.manager(lambda settings, extractors, request, cancel, update: self.media(request))
        identifiers = []
        for _ in range(21):
            created = manager.create(ORIGINAL)
            identifiers.append(created["id"])
            self.assertEqual(self.finished(manager, created["id"])["status"], "completed")
        self.assertEqual(len(manager.jobs), 20)
        self.assertEqual(manager.get(identifiers[-1])["status"], "completed")
        for method in (manager.get, manager.cancel):
            with self.assertRaises(MediaError):
                method(identifiers[0])

    def test_clearing_session_invalidates_browser_media_records(self):
        manager = self.manager(lambda settings, extractors, request, cancel, update: self.media(request))
        created = manager.create(ORIGINAL)
        final = self.finished(manager, created["id"])
        self.service.clear_session("douyin")
        with self.assertRaisesRegex(MediaError, "已过期"):
            self.service.get(final["media"]["id"])


class BrowserCookieBoundaryTests(unittest.TestCase):
    def test_browser_import_filters_other_sites_and_preserves_flags(self):
        store = SessionStore()
        cookies = [
            {"domain": ".douyin.com", "name": "domain", "value": "synthetic", "secure": True, "httpOnly": True, "path": "/", "expires": -1},
            {"domain": "www.iesdouyin.com", "name": "host", "value": "test", "secure": True, "path": "/share", "expires": 0},
            {"domain": ".example.org", "name": "unrelated", "value": "excluded"},
            {"domain": ".douyin.com.example.org", "name": "spoof", "value": "excluded"}]
        stored = store_browser_session(store, cookies)
        self.assertEqual({cookie.name for cookie in stored}, {"domain", "host"})
        converted = {item["name"]: item for item in playwright_cookies(stored)}
        self.assertEqual(converted["domain"]["domain"], ".douyin.com")
        self.assertTrue(converted["domain"]["httpOnly"])
        self.assertEqual(converted["host"]["url"], "https://www.iesdouyin.com/share")
        self.assertNotIn("domain", converted["host"])
        self.assertNotIn("expires", converted["domain"])
        self.assertNotIn("synthetic", json.dumps(store.status("douyin")))

    def test_empty_browser_cookie_capture_does_not_erase_an_imported_session(self):
        store = SessionStore()
        store.import_text("douyin", "# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tTRUE\t0\tsession\tprior-synthetic\n")
        stored = store_browser_session(store, [])
        self.assertEqual(stored[0].value, "prior-synthetic")


if __name__ == "__main__":
    unittest.main()

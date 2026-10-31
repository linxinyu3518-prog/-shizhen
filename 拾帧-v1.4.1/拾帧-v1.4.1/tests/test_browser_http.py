# SPDX-License-Identifier: GPL-3.0-or-later
"""Real loopback HTTP checks for sessions and browser discovery orchestration.

Browser work is replaced at its injected runner boundary. These tests need no
browser, login account, codec executable, DNS lookup or external media request.
"""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from framekeep.config import Settings
from framekeep.network import MediaError
from framekeep.server import create_server


class BrowserHttpTests(unittest.TestCase):
    target = "7670847854776159515"
    canonical = f"https://www.douyin.com/video/{target}"
    modal = f"https://www.douyin.com/user/self?modal_id={target}&showTab=like"
    secret = "synthetic-private-cookie-never-return-this"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(data_dir=Path(self.temporary.name), browser="fixture-browser", playwright=True)
        self.server = create_server(self.settings, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.port = self.server.server_port
        self.extractors = self.server.app.extractors
        self.manager = self.server.app.browser
        self.manager.runner = Mock(side_effect=AssertionError("A test must inject its own browser runner"))
        self.urls = patch.object(self.extractors, "prepare_urls", return_value=(self.modal, self.canonical)).start()
        self.addCleanup(patch.stopall)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, payload=None, *, headers=None, authorize=True):
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        if method == "POST" and authorize:
            request_headers.setdefault("X-App-Token", self.server.app.token)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            self.assertNotIn(self.secret.encode(), raw, "A private Cookie value appeared in an HTTP response")
            return response.status, json.loads(raw)
        finally:
            connection.close()

    def cookie_text(self):
        return ("# Netscape HTTP Cookie File\n"
                f".douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\t{self.secret}\n"
                f"www.iesdouyin.com\tFALSE\t/\tTRUE\t0\twebid\t{self.secret}\n"
                f".unrelated.example\tTRUE\t/\tTRUE\t0\tother\t{self.secret}\n"
                f".douyin.com.evil.example\tTRUE\t/\tTRUE\t0\tlookalike\t{self.secret}\n")

    def media(self, identifier=None, url=None):
        return {"id": identifier or uuid4().hex, "title": "HTTP browser fixture", "url": url or self.canonical,
                "duration": 1, "width": 640, "height": 360, "has_video": True, "has_audio": True,
                "formats": [{"id": "0", "width": 640, "height": 360, "ext": "mp4"}],
                "_cookies": self.extractors.sessions.snapshot("douyin"), "_created": time.time(), "_provider": "direct",
                "_sources": {"0": {"url": "https://media.example/video.mp4", "private": self.secret}},
                "_download_headers": {"Cookie": "sessionid=" + self.secret}}

    def wait_for(self, identifier, statuses=("completed", "failed", "cancelled")):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            status, result = self.request("GET", f"/api/browser/analyze/{identifier}")
            self.assertEqual(status, 200)
            if result["status"] in statuses:
                return result
            time.sleep(0.01)
        self.fail(f"Browser fixture did not reach {statuses}")

    def wait_for_worker_exit(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with self.manager.lock:
                if not self.manager.events:
                    return
            time.sleep(0.01)
        self.fail("Browser fixture worker did not finish")

    def test_session_import_get_clear_filters_domains_and_never_exposes_values(self):
        status, result = self.request("GET", "/api/session/douyin")
        self.assertEqual((status, result["configured"], result["count"]), (200, False, 0))
        status, imported = self.request("POST", "/api/session/douyin", {"cookies": self.cookie_text()})
        self.assertEqual(status, 200)
        self.assertEqual(imported, {"configured": True, "count": 2, "platform": "douyin", "storage": "memory"})
        cookies = self.extractors.sessions.snapshot("douyin")
        self.assertEqual({cookie.domain for cookie in cookies}, {".douyin.com", "www.iesdouyin.com"})
        status, result = self.request("GET", "/api/session/douyin")
        self.assertEqual((status, result), (200, imported))
        status, result = self.request("POST", "/api/session/douyin/clear", {})
        self.assertEqual((status, result["configured"], result["count"]), (200, False, 0))
        self.assertEqual(self.extractors.sessions.snapshot("douyin"), ())
        status, result = self.request("GET", "/api/session/douyin")
        self.assertEqual((status, result["configured"]), (200, False))
        self.assertEqual(sorted(path.name for path in self.settings.data_dir.iterdir()), ["jobs.sqlite3"])

    def test_session_clear_invalidates_platform_results_and_keeps_other_sources(self):
        self.request("POST", "/api/session/douyin", {"cookies": self.cookie_text()})
        douyin, other = self.media(), self.media(url="https://media.example/other.mp4")
        self.extractors.remember(douyin)
        self.extractors.remember(other)
        status, result = self.request("POST", "/api/session/douyin/clear", {})
        self.assertEqual(status, 200)
        self.assertFalse(result["configured"])
        with self.assertRaises(MediaError):
            self.extractors.get(douyin["id"])
        self.assertIs(self.extractors.get(other["id"]), other)

    def test_session_errors_do_not_echo_cookie_values_or_overwrite_existing_session(self):
        self.request("POST", "/api/session/douyin", {"cookies": self.cookie_text()})
        for text in (self.secret, "# Netscape HTTP Cookie File\n.douyin.com\t" + self.secret,
                     f"# Netscape HTTP Cookie File\n.example.org\tTRUE\t/\tTRUE\t0\tname\t{self.secret}\n"):
            with self.subTest(text=text[:30]):
                status, result = self.request("POST", "/api/session/douyin", {"cookies": text})
                self.assertEqual(status, 400)
                self.assertIn("error", result)
        status, result = self.request("GET", "/api/session/douyin")
        self.assertEqual((status, result["count"]), (200, 2))

    def test_new_mutation_routes_require_token_and_same_origin_before_dispatch(self):
        routes = ("/api/session/douyin", "/api/session/douyin/clear", "/api/browser/analyze",
                  "/api/browser/analyze/" + "a" * 32 + "/cancel")
        for route in routes:
            for headers, authorize in (({}, False), ({"X-App-Token": "wrong"}, False),
                                       ({"Origin": "https://unrelated.example"}, True), ({"Origin": "null"}, True)):
                with self.subTest(route=route, headers=headers):
                    status, result = self.request("POST", route,
                        {"url": self.modal, "cookies": self.cookie_text()}, headers=headers, authorize=authorize)
                    self.assertEqual(status, 403)
                    self.assertIn("error", result)
        self.urls.assert_not_called()
        self.manager.runner.assert_not_called()
        self.assertFalse(self.extractors.sessions.status("douyin")["configured"])

    def test_session_and_browser_reads_reject_foreign_origins_and_host_names(self):
        for route in ("/api/session/douyin", "/api/browser/analyze/" + "a" * 32):
            for headers in ({"Origin": "https://unrelated.example"}, {"Host": f"unrelated.example:{self.port}"}):
                with self.subTest(route=route, headers=headers):
                    status, result = self.request("GET", route, headers=headers)
                    self.assertEqual(status, 403)
                    self.assertIn("error", result)

    def test_browser_create_wait_complete_registers_reusable_private_media(self):
        self.request("POST", "/api/session/douyin", {"cookies": self.cookie_text()})
        captured = self.media()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def runner(settings, extractors, request, cancel, update):
            self.assertIs(extractors, self.extractors)
            self.assertEqual((request.original_url, request.canonical_url, request.video_id),
                             (self.modal, self.canonical, self.target))
            update("waiting", "Fixture is waiting for playback")
            entered.set()
            if not release.wait(4):
                raise RuntimeError("Fixture was not released")
            return captured

        self.manager.runner = runner
        status, created = self.request("POST", "/api/browser/analyze", {"url": self.modal},
                                       headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual((status, created["status"]), (202, "starting"))
        self.assertTrue(entered.wait(2))
        waiting = self.wait_for(created["id"], ("waiting",))
        self.assertIsNone(waiting["media"])
        self.assertIsNone(waiting["error"])
        release.set()
        completed = self.wait_for(created["id"])
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual(completed["media"]["id"], captured["id"])
        self.assertFalse(any(key.startswith("_") for key in completed["media"]))
        stored = self.extractors.get(completed["media"]["id"])
        self.assertIs(stored, captured)
        self.assertEqual(stored["_cookies"][0].value, self.secret)
        self.assertIn("_sources", stored)
        self.assertIn("_download_headers", stored)
        self.wait_for_worker_exit()
        status, result = self.request("POST", f"/api/browser/analyze/{created['id']}/cancel", {})
        self.assertEqual((status, result["status"]), (200, "completed"))

    def test_browser_cancel_prevents_late_result_from_becoming_downloadable(self):
        entered = threading.Event()
        captured = self.media()

        def runner(settings, extractors, request, cancel, update):
            update("waiting", "Fixture is waiting")
            entered.set()
            if not cancel.wait(4):
                raise RuntimeError("Cancellation was not delivered")
            # Simulate a late result racing with cancellation; it must be discarded.
            return captured

        self.manager.runner = runner
        status, created = self.request("POST", "/api/browser/analyze", {"url": self.modal})
        self.assertEqual(status, 202)
        self.assertTrue(entered.wait(2))
        status, duplicate = self.request("POST", "/api/browser/analyze", {"url": self.modal})
        self.assertEqual(status, 400)
        self.assertIn("error", duplicate)
        status, result = self.request("POST", f"/api/browser/analyze/{created['id']}/cancel", {})
        self.assertEqual((status, result["status"]), (200, "cancelled"))
        self.wait_for_worker_exit()
        result = self.wait_for(created["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["media"])
        with self.assertRaises(MediaError):
            self.extractors.get(captured["id"])

    def test_browser_failure_preserves_actionable_status_and_sanitizes_unexpected_error(self):
        for error, expected_code in ((MediaError("Fixture browser timed out", code="browser_wait_timeout",
            diagnostics=[{"stage": "浏览器解析", "status": "failed", "message": "No target media"}]), "browser_wait_timeout"),
            (RuntimeError("private implementation error " + self.secret), "browser_error")):
            with self.subTest(expected_code=expected_code):
                self.manager.runner = Mock(side_effect=error)
                status, created = self.request("POST", "/api/browser/analyze", {"url": self.modal})
                self.assertEqual(status, 202)
                result = self.wait_for(created["id"])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["code"], expected_code)
                self.assertEqual(result["error"]["normalized_url"], self.canonical)
                self.assertIsNone(result["media"])
                self.wait_for_worker_exit()

    def test_unknown_browser_state_and_cancel_are_actionable_http_errors(self):
        path = "/api/browser/analyze/" + "0" * 32
        status, result = self.request("GET", path)
        self.assertEqual(status, 404)
        self.assertIn("error", result)
        status, result = self.request("POST", path + "/cancel", {})
        self.assertEqual(status, 400)
        self.assertIn("error", result)

    def test_unavailable_browser_and_unsupported_platform_do_not_start_runner(self):
        self.settings.playwright = False
        status, result = self.request("POST", "/api/browser/analyze", {"url": self.modal})
        self.assertEqual((status, result["code"]), (400, "browser_unavailable"))
        self.settings.playwright = True
        self.urls.return_value = ("https://example.org/watch/1", "https://example.org/watch/1")
        status, result = self.request("POST", "/api/browser/analyze", {"url": "https://example.org/watch/1"})
        self.assertEqual((status, result["code"]), (400, "unsupported_url"))
        self.manager.runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()

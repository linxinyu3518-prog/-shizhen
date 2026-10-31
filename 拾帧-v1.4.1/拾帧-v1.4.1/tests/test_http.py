# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP trust boundaries and an end-to-end generated-video download workflow."""
import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from framekeep.config import Settings
from framekeep.jobs import JobManager
from framekeep.network import Cancelled
from framekeep.server import create_server


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        with patch.dict("os.environ", {"MEDIA_DATA_DIR": str(cls.root)}):
            cls.settings = Settings.discover(data_dir=cls.root)
        cls.server = create_server(cls.settings, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port
        cls.fixture = cls.root / "range-fixture.mp4"
        cls.fixture.write_bytes(b"0123456789")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temporary.cleanup()

    def request(self, method, path, payload=None, headers=None, authorize=True):
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        if method == "POST" and authorize:
            request_headers.setdefault("X-App-Token", self.server.app.token)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            result = (response.status, dict(response.getheaders()), response.read())
        finally:
            connection.close()
        return result

    def request_json(self, method, path, **kwargs):
        status, headers, body = self.request(method, path, **kwargs)
        return status, headers, json.loads(body)

    def test_server_binds_loopback_and_reports_runtime_capabilities(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, result = self.request_json("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(result["token"], self.server.app.token)
        self.assertIn("ffmpeg", result)
        self.assertIn("ffprobe", result)
        self.assertIn("h264", result)
        self.assertIn("aac", result)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_foreign_host_or_origin_cannot_obtain_session_token(self):
        cases = ({"Host": f"attacker.example:{self.port}"},
                 {"Host": "127.0.0.1:1"}, {"Origin": "https://attacker.example"},
                 {"Origin": "null"})
        for headers in cases:
            with self.subTest(headers=headers):
                status, _, result = self.request_json("GET", "/api/health", headers=headers)
                self.assertEqual(status, 403)
                self.assertNotIn("token", result)

    def test_post_requires_valid_token_even_for_same_origin(self):
        for token in (None, "wrong-token", "\xe9"):
            with self.subTest(token=token):
                headers = {} if token is None else {"X-App-Token": token}
                status, _, result = self.request_json("POST", "/api/analyze", payload={"url": "unused"},
                                                      headers=headers, authorize=False)
                self.assertEqual(status, 403)
                self.assertIn("error", result)

    def test_cross_origin_post_is_rejected_even_with_token(self):
        status, _, _ = self.request_json("POST", "/api/analyze", payload={"url": "unused"},
                                        headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)

    def test_json_schema_content_type_and_body_size_are_checked(self):
        cases = ((b"{}", {"Content-Type": "text/plain"}, 415),
                 (b"{broken", {}, 400), (b"[]", {}, 400), (b"null", {}, 400),
                 (b"\xff", {}, 400), (b"{}", {"Content-Length": "20000"}, 413),
                 (b"{}", {"Content-Length": "-1"}, 413))
        for payload, headers, expected in cases:
            with self.subTest(payload=payload, headers=headers):
                status, _, result = self.request_json("POST", "/api/analyze", payload=payload, headers=headers)
                self.assertEqual(status, expected)
                self.assertIn("error", result)

    def test_unknown_routes_and_filesystem_paths_are_not_served(self):
        for route in ("/missing", "/../runtime.local.json", "/%2e%2e/runtime.local.json",
                      "/runtime.local.json", "/data/jobs.sqlite3", "/api/jobs/" + "0" * 32):
            with self.subTest(route=route):
                status, _, _ = self.request_json("GET", route)
                self.assertEqual(status, 404)

    def test_file_range_and_head_responses(self):
        with patch.object(self.server.app, "demo_path", self.fixture):
            status, headers, body = self.request("GET", "/sample.mp4")
            self.assertEqual((status, body), (200, b"0123456789"))
            self.assertEqual(headers["Content-Length"], "10")
            for requested, expected, interval in (("bytes=2-5", b"2345", "bytes 2-5/10"),
                                                  ("bytes=-4", b"6789", "bytes 6-9/10"),
                                                  ("bytes=7-", b"789", "bytes 7-9/10"),
                                                  ("bytes=7-99", b"789", "bytes 7-9/10")):
                with self.subTest(requested=requested):
                    status, headers, body = self.request("GET", "/sample.mp4", headers={"Range": requested})
                    self.assertEqual((status, body), (206, expected))
                    self.assertEqual(headers["Content-Range"], interval)
            status, headers, body = self.request("HEAD", "/sample.mp4")
            self.assertEqual((status, body), (200, b""))
            self.assertEqual(headers["Content-Length"], "10")

    def test_invalid_or_unsatisfiable_ranges_are_rejected(self):
        with patch.object(self.server.app, "demo_path", self.fixture):
            for requested in ("bytes=10-", "bytes=8-2", "bytes=-0", "bytes=", "bytes=0-1,3-4"):
                with self.subTest(requested=requested):
                    status, headers, body = self.request("GET", "/sample.mp4", headers={"Range": requested})
                    self.assertEqual(status, 416)
                    self.assertEqual(headers["Content-Range"], "bytes */10")
                    self.assertEqual(body, b"")

    def test_invalid_analysis_and_expired_media_return_actionable_errors(self):
        status, _, result = self.request_json("POST", "/api/analyze", payload={"url": "file:///private.mp4"})
        self.assertEqual(status, 400)
        self.assertIn("error", result)
        status, _, result = self.request_json("POST", "/api/jobs", payload={"media_id": "expired"})
        self.assertEqual(status, 400)
        self.assertIn("error", result)

    def wait_for_job(self, identifier):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status, _, result = self.request_json("GET", f"/api/jobs/{identifier}")
            self.assertEqual(status, 200)
            if result["status"] in ("completed", "failed", "cancelled"):
                return result
            time.sleep(0.05)
        self.fail("The local generated-fixture job did not finish in 30 seconds")

    def test_generated_video_analyze_original_download_and_h264_export(self):
        if not self.server.app.demo_path:
            self.skipTest("The end-to-end media pipeline needs FFmpeg / FFprobe")
        status, _, demo = self.request_json("GET", "/api/demo")
        self.assertEqual(status, 200)
        status, _, media = self.request_json("POST", "/api/analyze", payload={"url": demo["url"]})
        self.assertEqual(status, 200, media)
        self.assertEqual((media["width"], media["height"]), (640, 360))
        self.assertAlmostEqual(media["duration"], 8, delta=0.1)
        self.assertFalse(any(key.startswith("_") for key in media))
        completed = []
        for kind in ("original", "video"):
            with self.subTest(kind=kind):
                status, _, job = self.request_json("POST", "/api/jobs", payload={
                    "media_id": media["id"], "kind": kind, "container": "mp4", "format_id": "best", "height": 720})
                self.assertEqual(status, 202, job)
                result = self.wait_for_job(job["id"])
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual(result["progress"], 100)
                self.assertNotIn("_path", result)
                status, headers, content = self.request("GET", result["download_url"])
                self.assertEqual(status, 200)
                self.assertIn("attachment", headers["Content-Disposition"])
                self.assertEqual(len(content), result["result"]["size"])
                if kind == "original":
                    self.assertEqual(hashlib.sha256(content).digest(),
                                     hashlib.sha256(self.server.app.demo_path.read_bytes()).digest())
                else:
                    self.assertEqual(result["result"]["video_codec"], "h264")
                    self.assertEqual(result["result"]["audio_codec"], "aac")
                    self.assertEqual(result["result"]["sample_rate"], 48000)
                    self.assertEqual(result["result"]["target_audio_bit_rate"], 256000)
                    self.assertEqual(result["result"]["height"], 360)
                completed.append(result)
        # A fresh manager loads the persisted records and resolves their downloads.
        reopened = JobManager(self.settings, self.server.app.extractors)
        try:
            for result in completed:
                self.assertEqual(reopened.get(result["id"])["status"], "completed")
                self.assertTrue(reopened.download_path(result["id"]).is_file())
        finally:
            reopened.close()

    def test_cancellation_reaches_worker_and_removes_partial_files(self):
        identifier = "c" * 32
        self.server.app.extractors.records[identifier] = {
            "id": identifier, "title": "Cancellation fixture", "has_video": True, "has_audio": True,
            "formats": [], "_created": time.time(), "_provider": "direct"}
        started, stopped = threading.Event(), threading.Event()
        def delayed_download(media, options, folder, progress, cancel):
            (folder / "source.part").write_bytes(b"temporary")
            started.set()
            try:
                if cancel.wait(timeout=5):
                    raise Cancelled("cancelled by test")
                raise RuntimeError("Cancellation did not reach the worker")
            finally:
                stopped.set()
        with patch.object(self.server.app.extractors, "download", side_effect=delayed_download):
            status, _, job = self.request_json("POST", "/api/jobs", payload={"media_id": identifier, "kind": "original"})
            self.assertEqual(status, 202)
            self.assertTrue(started.wait(timeout=3))
            status, _, result = self.request_json("POST", f"/api/jobs/{job['id']}/cancel", payload={})
            self.assertEqual(status, 200)
            self.assertEqual(result["status"], "cancelled")
            self.assertTrue(stopped.wait(timeout=3))
            deadline = time.monotonic() + 3
            folder = self.root / "tmp" / job["id"]
            while folder.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(folder.exists())
            status, _, _ = self.request_json("GET", f"/api/jobs/{job['id']}/download")
            self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()

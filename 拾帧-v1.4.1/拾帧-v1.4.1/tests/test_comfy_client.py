# SPDX-License-Identifier: GPL-3.0-or-later
"""Real local HTTP contract tests using original synthetic PNG fixtures."""
import binascii
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
import zlib

from framekeep import comfy_client
from framekeep.comfy_client import ComfyClient, REQUIRED_NODES, validate_comfy_url
from framekeep.network import Cancelled, MediaError


def png(width=4, height=4):
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", binascii.crc32(name + data) & 0xffffffff)
    raw = (b"\x00" + b"\x80\x90\xa0" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class TestComfyClient(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.png"
        self.source.write_bytes(png(2, 2))
        self.output = self.root / "output.png"
        self.cancel = threading.Event()
        self.calls = []
        self.prompt_id = str(uuid4())
        self.mode = "success"
        self.image_bytes = png()
        self.missing_node = None
        self.models = ["4x-local-test.pth"]
        self.combo = False
        self.native_multi = False
        self.upload_path = {"name": "renamed.png", "subfolder": "framekeep", "type": "input"}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, data, status=200, headers=None):
                if not isinstance(data, bytes):
                    data = json.dumps(data).encode()
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.calls.append(("GET", self.path, None))
                if outer.mode == "redirect":
                    self.send({}, 302, {"Location": "http://example.invalid/forbidden"})
                elif self.path.startswith("/object_info/"):
                    node = self.path.rsplit("/", 1)[1]
                    if node == outer.missing_node:
                        self.send({})
                    else:
                        self.send({node: {"input": {"required": {"model_name": ["COMBO", {"options": outer.models}] if outer.combo else [outer.models]}}}})
                elif self.path.startswith("/history/"):
                    if outer.mode in ("pending", "cancel"):
                        if outer.mode == "cancel":
                            outer.cancel.set()
                        self.send({})
                    elif outer.mode == "execution_error":
                        self.send({outer.prompt_id: {"status": {"status_str": "error"}}})
                    elif outer.mode == "no_result":
                        self.send({outer.prompt_id: {"status": {"completed": True}, "outputs": {}}})
                    else:
                        self.send({outer.prompt_id: {"status": {"completed": True}, "outputs": {
                            "5": {"images": [{"filename": "upscaled.png", "subfolder": "", "type": "temp"}]},
                            "6": {"images": [{"filename": "native.png", "subfolder": "", "type": "temp"}]}}}})
                elif self.path.startswith("/view?"):
                    submitted = [row[2]["prompt"] for row in outer.calls if row[1] == "/prompt"]
                    size = 12 if outer.native_multi and submitted and "13" in submitted[-1] else 8
                    self.send(png(size, size) if "native.png" in self.path else outer.image_bytes)
                else:
                    self.send({}, 404)

            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/upload/image":
                    outer.calls.append(("POST", self.path, data))
                    self.send(outer.upload_path)
                else:
                    payload = json.loads(data)
                    outer.calls.append(("POST", self.path, payload))
                    if self.path == "/prompt":
                        if outer.mode == "reject":
                            self.send({"error": {}}, 400)
                        else:
                            self.send({"prompt_id": outer.prompt_id, "node_errors": {}})
                    elif self.path == "/queue":
                        self.send(b"")
                    else:
                        self.send({}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = ComfyClient(f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def run_upscale(self):
        self.client.upscale(self.source, self.output, "4x-local-test.pth", 4, 4, self.cancel, "my-own-job")

    def test_url_loopback_only(self):
        for url in ("https://127.0.0.1:8188", "http://10.0.0.1", "http://127.0.0.2", "http://127.1", "http://[::ffff:127.0.0.1]",
                    "http://u:p@localhost:8188", "http://localhost:8188/path", "http://localhost:8188/?", "http://localhost/#",
                    "http://localhost:0", "http://localhost:99999", "http://localhost\\x", " http://localhost"):
            with self.subTest(url=url), self.assertRaises(MediaError):
                validate_comfy_url(url)
        self.assertEqual(validate_comfy_url("http://localhost:8188/"), "http://127.0.0.1:8188")
        self.assertEqual(validate_comfy_url("http://[::1]:8188"), "http://[::1]:8188")

    def test_inspection_lists_model_and_requires_every_node(self):
        self.assertEqual(self.client.inspect()["models"], self.models)
        self.assertTrue(self.client.inspect()["available"])
        self.missing_node = "PreviewImage"
        result = self.client.inspect()
        self.assertFalse(result["available"])
        self.assertIn("PreviewImage", result["reason"])

    def test_no_model_is_not_ready(self):
        self.models = []
        self.assertFalse(self.client.inspect()["available"])

    def test_new_combo_schema(self):
        self.combo = True
        self.assertEqual(self.client.inspect()["models"], self.models)
        self.assertTrue(self.client.inspect()["available"])

    def test_redirect_rejected(self):
        self.mode = "redirect"
        self.assertIn("重定向", self.client.inspect()["reason"])
        self.assertEqual(len(self.calls), 1)

    def test_png_result_and_fixed_workflow(self):
        self.run_upscale()
        self.assertEqual(self.output.read_bytes(), png())
        workflow = next(value[2]["prompt"] for value in self.calls if value[1] == "/prompt")
        self.assertEqual({node["class_type"] for node in workflow.values()}, set(REQUIRED_NODES))
        self.assertEqual(workflow["1"]["inputs"]["image"], "framekeep/renamed.png")
        self.assertEqual(workflow["4"]["inputs"]["width"], 4)
        upload = next(value[2] for value in self.calls if value[1] == "/upload/image")
        self.assertIn(b'name="overwrite"\r\n\r\ntrue', upload)
        self.assertNotIn("/interrupt", [row[1] for row in self.calls])
        self.assertFalse(list(self.root.glob("*.part")))

    def test_invalid_png_and_wrong_size_are_rejected(self):
        for data in (b"not png", png()[:-4], png(5, 4)):
            with self.subTest(data=data[:12]):
                self.image_bytes = data
                with self.assertRaises(MediaError):
                    self.run_upscale()
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob("*.part")))

    def test_invalid_input_prevents_upload(self):
        self.source.write_bytes(b"fake input")
        with self.assertRaises(MediaError):
            self.run_upscale()
        self.assertEqual(self.calls, [])

    def test_multiple_ai_passes_and_only_downscale_between_them(self):
        self.native_multi = True
        self.image_bytes = png(12, 12)
        result = self.client.upscale(self.source, self.output, "4x-local-test.pth", 12, 12, self.cancel, "multi")
        self.assertEqual(result["passes"], 2)
        prompts = [row[2]["prompt"] for row in self.calls if row[1] == "/prompt"]
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[-1]["12"]["inputs"]["width"], 3)
        self.assertEqual(prompts[-1]["13"]["class_type"], "ImageUpscaleWithModel")
        self.assertEqual(self.output.read_bytes(), png(12, 12))

    def test_insufficient_native_ai_output_never_claims_resize_as_ai(self):
        self.image_bytes = png(12, 12)
        with self.assertRaises(MediaError):
            self.client.upscale(self.source, self.output, "4x-local-test.pth", 12, 12, self.cancel, "small")
        self.assertFalse(self.output.exists())

    def test_cancel_deletes_only_own_queued_prompt(self):
        self.mode = "cancel"
        with self.assertRaises(Cancelled):
            self.run_upscale()
        queues = [row[2] for row in self.calls if row[1] == "/queue"]
        self.assertEqual(queues, [{"delete": [self.prompt_id]}])
        self.assertNotIn("/interrupt", [row[1] for row in self.calls])
        self.assertFalse(self.output.exists())

    def test_precancel_sends_no_requests(self):
        self.cancel.set()
        with self.assertRaises(Cancelled):
            self.run_upscale()
        self.assertEqual(self.calls, [])

    def test_history_poll_is_bounded(self):
        self.mode = "pending"
        started = time.monotonic()
        with patch.object(comfy_client, "FRAME_TIMEOUT", 0.08), self.assertRaises(MediaError):
            self.run_upscale()
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(any(row[1] == "/queue" for row in self.calls))

    def test_execution_failure_and_empty_result(self):
        for mode in ("execution_error", "no_result", "reject"):
            with self.subTest(mode=mode):
                self.mode = mode
                with self.assertRaises(MediaError):
                    self.run_upscale()
                self.assertFalse(self.output.exists())

    def test_unsafe_upload_reference_rejected(self):
        self.upload_path["subfolder"] = "../private"
        with self.assertRaises(MediaError):
            self.run_upscale()
        self.assertFalse(any(row[1] == "/prompt" for row in self.calls))

    def test_payload_bound(self):
        with patch.object(comfy_client, "MAX_IMAGE_BYTES", 60), self.assertRaises(MediaError):
            self.run_upscale()
        self.assertEqual(self.calls, [])

    def test_bad_target_dimensions_prevent_network(self):
        for width, height in ((True, 4), (0, 4), (20000, 4), (10000, 10000)):
            with self.subTest(width=width, height=height), self.assertRaises(MediaError):
                self.client.upscale(self.source, self.output, "model", width, height, self.cancel, "job")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

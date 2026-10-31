# SPDX-License-Identifier: GPL-3.0-or-later
"""AI orchestration tests; mock AI pixels, real FFmpeg timing and audio."""
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from framekeep.jobs import JobManager, validate_options
from framekeep.media import encode, probe
from framekeep.network import Cancelled, MediaError
from framekeep.upscale import UpscaleService, target_size
from test_frame_rates import MediaHarness


class UpscaleOptionsTests(unittest.TestCase):
    def test_optional_and_video_only(self):
        media = {"has_video": True, "has_audio": True, "formats": []}
        self.assertEqual(validate_options({}, media)["upscale"], "none")
        for mode in ("auto", "rtx", "comfyui"):
            self.assertEqual(validate_options({"upscale": mode, "height": 2160}, media)["upscale"], mode)
            self.assertEqual(validate_options({"kind": "audio", "upscale": mode}, media)["upscale"], "none")
        for value in (None, [], {}, True, "cuda", "rtx;cmd"):
            with self.assertRaises(MediaError):
                validate_options({"upscale": value, "height": 1440}, media)
        with self.assertRaises(MediaError):
            validate_options({"upscale": "auto"}, media)

    def test_dimensions_sar_and_limits(self):
        self.assertEqual(target_size({"width": 1280, "height": 720}, 360), (640, 360))
        self.assertEqual(target_size({"width": 640, "height": 360}, 1440), (2560, 1440))
        self.assertEqual(target_size({"width": 720, "height": 576, "sample_aspect_ratio": "16:15"}, 1080), (1440, 1080))
        with self.assertRaises(MediaError):
            target_size({"width": 9000, "height": 100}, 2160)

    def test_rtx_priority_and_explicit_provider_never_silent_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            from framekeep.config import Settings
            service = UpscaleService(Settings(data_dir=Path(root)))
            ready = {"providers": [{"id": "rtx", "available": True}, {"id": "comfyui", "available": True}],
                     "models": ["test.pth"], "comfyui_model": "test.pth", "comfyui_url": "http://127.0.0.1:8188"}
            with patch.object(service, "status", return_value=ready):
                options = {"upscale": "auto", "height": 1440}
                self.assertEqual(service.select(options, {"height": 360})["provider"], "rtx")
                ready["providers"][0].update(available=False, reason="missing")
                self.assertEqual(service.select(options, {"height": 360})["provider"], "comfyui")
                with self.assertRaises(MediaError):
                    service.select({**options, "upscale": "rtx"}, {"height": 360})


class UpscaleTimingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.harness = MediaHarness(self.root)
        self.service = UpscaleService(self.harness.settings)
        harness = self.harness
        class PixelFixture:
            def __init__(self, *_):
                pass
            def close(self):
                pass
            def upscale(self, source, output, width, height, cancel):
                if cancel.is_set():
                    raise Cancelled("fixture cancelled")
                harness.ffmpeg(["-v", "error", "-i", str(source), "-vf", f"scale={width}:{height}", str(output)])
        self.mock = patch("framekeep.upscale.RTXProcess", PixelFixture)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def convert(self, *, vfr=False, offset=False, container="mp4", fps=None, mode="fast"):
        source = self.harness.fixture("source", size="160x96", duration=.4, fps=10, vfr=vfr, offset=offset)
        metadata = probe(source, self.harness.settings)
        folder = self.root / "job"
        folder.mkdir()
        options = {"kind": "video", "container": container, "height": 480, "upscale": "rtx", "fps": fps,
                   "video_quality": "high", "frame_rate_mode": mode}
        frames, result = self.service.prepare(source, options, metadata, folder, threading.Event(), lambda *_: None,
                                               {"provider": "rtx", "model": "fixture-only", "rtx_python": "mock"})
        output = self.root / ("output." + container)
        encode(source, output, options, self.harness.settings, metadata, lambda *_: None, threading.Event(), prepared=frames)
        measured = probe(output, self.harness.settings)
        self.harness.decode(output)
        self.assertEqual((measured["width"], measured["height"]), (800, 480))
        self.assertEqual(measured["audio_codec"], "aac")
        self.assertEqual(result["upscale_frames"], metadata["frame_count"])
        return source, output, measured

    def test_preserve_frame_count_and_timing(self):
        source, output, measured = self.convert()
        self.assertEqual(measured["frame_count"], 4)
        self.assertAlmostEqual(measured["video_duration"], .4, delta=.002)

    def test_vfr_preserves_each_pts(self):
        source, output, _ = self.convert(vfr=True)
        before, after = self.harness.frames(source), self.harness.frames(output)
        self.assertEqual(len(before), len(after))
        for a, b in zip(before, after):
            self.assertAlmostEqual(float(a["best_effort_timestamp_time"]), float(b["best_effort_timestamp_time"]), delta=.001)

    def test_audio_offset_and_120fps(self):
        source, output, measured = self.convert(offset=True, fps=120)
        self.assertEqual(measured["fps"], 120)
        self.assertEqual(measured["frame_count"], 48)
        before = probe(source, self.harness.settings)
        self.assertAlmostEqual(measured["audio_start_time"] - measured["video_start_time"],
                               before["audio_start_time"] - before["video_start_time"], delta=1024/48000 + .002)

    def test_avi_120fps(self):
        _, _, measured = self.convert(container="avi", fps=120)
        self.assertEqual(measured["fps"], 120)
        self.assertEqual(measured["frame_count"], 48)

    def test_cancelled_preparation_stops(self):
        source = self.harness.fixture("cancel", duration=.1)
        folder = self.root / "cancel-job"
        folder.mkdir()
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            self.service.prepare(source, {"height": 480}, probe(source, self.harness.settings), folder, event,
                                 lambda *_: None, {"provider": "rtx", "rtx_python": "mock", "model": "fixture"})

    def test_hdr_and_rotation_are_not_silently_misconverted(self):
        for change in ({"color_transfer": "smpte2084"}, {"color_transfer": "arib-std-b67"}, {"rotation": 90}):
            metadata = {"width": 160, "height": 96, **change}
            with self.subTest(change=change), self.assertRaises(MediaError):
                self.service.prepare(self.root / "unread.mp4", {"height": 480}, metadata, self.root,
                                     threading.Event(), lambda *_: None, {"provider": "rtx"})


class UpscaleQueueTests(unittest.TestCase):
    def test_waiting_and_running_cancellation_release_only_owned_slot(self):
        from framekeep.config import Settings
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(data_dir=Path(root), ffmpeg="fixture", ffprobe="fixture", h264=True, aac=True)
            info = {"title": "fixture", "height": 360, "width": 640, "fps": 30, "duration": 1,
                    "has_video": True, "has_audio": True, "formats": []}
            extractors = Mock()
            extractors.get.return_value = info
            def download(media, options, folder, *_):
                path = folder / "source.mp4"
                path.write_bytes(b"fixture")
                return path
            extractors.download.side_effect = download
            manager = JobManager(settings, extractors)
            entered = threading.Event()
            calls = []
            def prepare(source, options, metadata, folder, cancel, progress, selection):
                calls.append(folder.name)
                if len(calls) == 1:
                    entered.set()
                    if not cancel.wait(5):
                        raise MediaError("fixture wait exceeded")
                    raise Cancelled("cancelled")
                return None, {"upscale_provider": "none", "upscale_note": "fixture"}
            def finish(source, output, *args, **kwargs):
                output.write_bytes(b"fixture encoded")
            def wait_for(identifier, predicate):
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    job = manager.get(identifier)
                    if predicate(job):
                        return job
                    time.sleep(.01)
                self.fail(str(manager.get(identifier)))
            try:
                with patch.object(manager.upscale, "select", return_value={"provider": "rtx"}), \
                     patch.object(manager.upscale, "prepare", side_effect=prepare), \
                     patch("framekeep.jobs.probe", return_value=info), patch("framekeep.jobs.encode", side_effect=finish):
                    payload = {"media_id": "fixture", "upscale": "rtx", "height": 1440}
                    first = manager.create(payload)
                    self.assertTrue(entered.wait(2))
                    second = manager.create(payload)
                    wait_for(second["id"], lambda job: job["stage"] == "等待 AI 超分")
                    manager.cancel(second["id"])
                    self.assertFalse(manager.upscale.slot.acquire(blocking=False), "A cancelled waiter must not release the running slot")
                    manager.cancel(first["id"])
                    third = manager.create(payload)
                    self.assertEqual(wait_for(third["id"], lambda job: job["status"] in ("completed", "failed"))["status"], "completed")
                    self.assertEqual(len(calls), 2)
            finally:
                manager.close()
            self.assertFalse((settings.data_dir / "tmp" / first["id"]).exists())
            self.assertFalse((settings.data_dir / "tmp" / second["id"]).exists())

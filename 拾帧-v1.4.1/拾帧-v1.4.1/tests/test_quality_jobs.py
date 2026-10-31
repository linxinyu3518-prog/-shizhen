# SPDX-License-Identifier: GPL-3.0-or-later
"""Quality option boundaries, interpolation scheduling and persisted outcomes.

Encoder and probe seams use small local fixtures. Actual FFmpeg output checks
belong to the media integration suite; these tests verify job orchestration.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from framekeep.config import Settings
from framekeep.jobs import JobManager, validate_options
from framekeep.network import Cancelled, MediaError


class QualityOptionsTests(unittest.TestCase):
    media = {"has_video": True, "has_audio": True, "formats": []}

    def test_video_defaults_and_supported_quality_frame_rate_combinations(self):
        defaults = validate_options({}, self.media)
        self.assertEqual((defaults["video_quality"], defaults["fps"], defaults["frame_rate_mode"]),
                         ("high", None, "interpolate"))
        for quality in ("balanced", "high", "maximum"):
            for fps in (None, 1, 24, 60, 120):
                for mode in ("interpolate", "fast"):
                    with self.subTest(quality=quality, fps=fps, mode=mode):
                        result = validate_options({"video_quality": quality, "fps": fps, "frame_rate_mode": mode}, self.media)
                        self.assertEqual((result["video_quality"], result["fps"], result["frame_rate_mode"]), (quality, fps, mode))

    def test_invalid_or_injected_options_are_rejected(self):
        for field, values in (("video_quality", (None, True, 1, [], {}, "ultra", "high;-crf 0")),
                              ("fps", (True, False, "120", 120.0, 0, -1, 121, float("nan"), [], {})),
                              ("frame_rate_mode", (None, True, 1, [], {}, "source", "minterpolate=fps=1000"))):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(MediaError):
                    validate_options({field: value}, self.media)

    def test_audio_and_original_clear_video_only_options(self):
        for kind in ("audio", "original"):
            with self.subTest(kind=kind):
                result = validate_options({"kind": kind, "video_quality": "maximum", "fps": 120,
                                           "frame_rate_mode": "interpolate"}, self.media)
                for field in ("video_quality", "fps", "frame_rate_mode"):
                    self.assertIsNone(result[field])


class QualityJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(data_dir=Path(self.temporary.name), ffmpeg="fixture-ffmpeg", ffprobe="fixture-ffprobe",
                                 h264=True, aac=True)
        self.settings.interpolation = True
        self.source_metadata = {"duration": 2, "width": 640, "height": 360, "fps": 30,
                                "has_video": True, "has_audio": True, "video_codec": "h264", "audio_codec": "aac"}
        self.extracted = {"id": "fixture-media", "title": "Fixture video", **self.source_metadata, "fps": 120, "formats": []}
        self.extractors = Mock()
        self.extractors.get.return_value = self.extracted
        self.extractors.download.side_effect = self.download
        self.manager = JobManager(self.settings, self.extractors)
        self.addCleanup(self.manager.close)
        self.plan = {"effective_frame_rate_mode": "interpolate", "source_fps": 30, "target_fps": 120,
                     "frame_rate_note": "Fixture motion interpolation", "video_quality": "maximum"}
        self.plan_patch = patch("framekeep.jobs.encoding_plan", return_value=self.plan)
        self.plan_call = self.plan_patch.start()
        self.addCleanup(self.plan_patch.stop)
        self.probe_patch = patch("framekeep.jobs.probe", side_effect=self.probe)
        self.probe_call = self.probe_patch.start()
        self.addCleanup(self.probe_patch.stop)
        self.encode_patch = patch("framekeep.jobs.encode", side_effect=self.encode)
        self.encode_call = self.encode_patch.start()
        self.addCleanup(self.encode_patch.stop)

    def download(self, media, options, folder, progress, cancel):
        path = folder / "source.mp4"
        path.write_bytes(b"original-fixture-bytes")
        return path

    def probe(self, source, settings):
        if Path(source).name == "source.mp4":
            return dict(self.source_metadata)
        return {**self.source_metadata, "fps": 119.88, "sample_rate": 48000}

    def encode(self, source, output, options, settings, metadata, progress, cancel, plan=None):
        Path(output).write_bytes(b"encoded-fixture-bytes")
        return output

    def create(self, **options):
        return self.manager.create({"media_id": "fixture-media", "kind": "video", "fps": 120,
                                    "video_quality": "maximum", "frame_rate_mode": "interpolate", **options})

    def wait_for(self, identifier, *, stage=None, terminal=True):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            result = self.manager.get(identifier)
            if stage is not None and result["stage"] == stage:
                return result
            if terminal and result["status"] in ("completed", "failed", "cancelled"):
                return result
            time.sleep(0.01)
        self.fail(f"Fixture job failed to reach expected state: {self.manager.get(identifier)}")

    def wait_for_worker(self, identifier):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with self.manager.lock:
                if identifier not in self.manager.events:
                    return
            time.sleep(0.01)
        self.fail("Fixture job worker did not stop")

    def test_plan_uses_downloaded_metadata_and_actual_result_survives_history_reload(self):
        created = self.create()
        result = self.wait_for(created["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["fps"], 120)
        self.assertEqual(result["video_quality"], "maximum")
        self.assertEqual(result["frame_rate_mode"], "interpolate")
        self.assertEqual(result["result"]["fps"], 119.88)
        self.assertEqual(result["result"]["target_fps"], 120)
        self.assertEqual(result["result"]["source_fps"], 30)
        self.assertEqual(result["result"]["effective_frame_rate_mode"], "interpolate")
        self.assertEqual(result["result"]["frame_rate_note"], self.plan["frame_rate_note"])
        self.assertEqual(result["result"]["target_audio_bit_rate"], 256000)
        self.assertTrue(result["filename"].endswith("-H264-120fps.mp4"))
        self.assertEqual(self.plan_call.call_args.args[1]["fps"], 30)
        self.assertIs(self.encode_call.call_args.kwargs["plan"], self.plan)
        self.assertEqual(self.manager.download_path(created["id"]).read_bytes(), b"encoded-fixture-bytes")
        self.wait_for_worker(created["id"])
        reopened = JobManager(self.settings, self.extractors)
        try:
            restored = reopened.get(created["id"])
            self.assertEqual(restored["result"], result["result"])
            self.assertEqual(restored["video_quality"], "maximum")
            self.assertEqual(restored["fps"], 120)
            self.assertEqual(reopened.download_path(created["id"]).read_bytes(), b"encoded-fixture-bytes")
        finally:
            reopened.close()

    def test_only_one_interpolation_runs_and_waiting_cancel_preserves_the_slot(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = 0

        def blocking_encode(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                if not release.wait(4):
                    raise MediaError("Fixture encoding was not released")
            return self.encode(*args, **kwargs)

        self.encode_call.side_effect = blocking_encode
        first = self.create()
        self.assertTrue(entered.wait(2))
        second = self.create()
        waiting = self.wait_for(second["id"], stage="等待插帧", terminal=False)
        self.assertEqual(waiting["status"], "queued")
        self.assertEqual(calls, 1)
        began = time.monotonic()
        self.manager.cancel(second["id"])
        self.wait_for_worker(second["id"])
        self.assertLess(time.monotonic() - began, 1)
        self.assertFalse((self.settings.data_dir / "tmp" / second["id"]).exists())
        third = self.create()
        self.wait_for(third["id"], stage="等待插帧", terminal=False)
        self.assertEqual(calls, 1, "Cancelling a waiter must not release a slot owned by another job")
        release.set()
        self.assertEqual(self.wait_for(first["id"])["status"], "completed")
        self.assertEqual(self.wait_for(third["id"])["status"], "completed")
        self.assertEqual(calls, 2)
        self.assertEqual(self.manager.get(second["id"])["status"], "cancelled")

    def test_encoder_failure_releases_interpolation_slot(self):
        self.encode_call.side_effect = MediaError("Fixture encoder failure")
        first = self.create()
        self.assertEqual(self.wait_for(first["id"])["status"], "failed")
        self.wait_for_worker(first["id"])
        self.encode_call.side_effect = self.encode
        second = self.create()
        self.assertEqual(self.wait_for(second["id"])["status"], "completed")

    def test_cancelling_active_interpolation_releases_slot_and_removes_partial_output(self):
        entered = threading.Event()

        def cancellable_encode(source, output, options, settings, metadata, progress, cancel, plan=None):
            Path(output).write_bytes(b"partial-output")
            entered.set()
            if not cancel.wait(4):
                raise MediaError("Fixture cancellation was not delivered")
            raise Cancelled("Fixture encoding cancelled")

        self.encode_call.side_effect = cancellable_encode
        first = self.create()
        self.assertTrue(entered.wait(2))
        self.manager.cancel(first["id"])
        self.wait_for_worker(first["id"])
        self.assertEqual(self.manager.get(first["id"])["status"], "cancelled")
        self.assertFalse((self.settings.data_dir / "tmp" / first["id"]).exists())
        with self.assertRaises(MediaError):
            self.manager.download_path(first["id"])
        self.encode_call.side_effect = self.encode
        second = self.create()
        self.assertEqual(self.wait_for(second["id"])["status"], "completed")

    def test_unsupported_interpolation_fails_with_clear_error_after_source_probe(self):
        self.settings.interpolation = False
        job = self.create()
        result = self.wait_for(job["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("minterpolate", result["message"])
        self.assertIn("快速转换", result["message"])
        self.plan_call.assert_called_once()
        self.encode_call.assert_not_called()

    def test_effective_fast_and_source_modes_ignore_interpolation_capability_and_queue(self):
        self.settings.interpolation = False
        self.assertTrue(self.manager.interpolation_slot.acquire(blocking=False))
        self.addCleanup(self.manager.interpolation_slot.release)
        for effective in ("fast", "source"):
            with self.subTest(effective=effective):
                self.plan_call.return_value = {**self.plan, "effective_frame_rate_mode": effective,
                                              "frame_rate_note": "Fixture fallback"}
                job = self.create()
                result = self.wait_for(job["id"])
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual(result["result"]["effective_frame_rate_mode"], effective)
                self.wait_for_worker(job["id"])

    def test_audio_and_original_do_not_compute_video_plans_or_reserve_interpolation(self):
        self.settings.interpolation = False
        self.assertTrue(self.manager.interpolation_slot.acquire(blocking=False))
        self.addCleanup(self.manager.interpolation_slot.release)
        for kind in ("audio", "original"):
            with self.subTest(kind=kind):
                job = self.create(kind=kind)
                result = self.wait_for(job["id"])
                self.assertEqual(result["status"], "completed", result)
                for field in ("fps", "video_quality", "frame_rate_mode"):
                    self.assertIsNone(result[field])
                self.assertNotIn("effective_frame_rate_mode", result["result"])
                if kind == "original":
                    self.assertEqual(self.manager.download_path(job["id"]).read_bytes(), b"original-fixture-bytes")
                self.wait_for_worker(job["id"])
        self.plan_call.assert_not_called()

    def test_waiting_cancelled_state_persists_after_restart(self):
        self.assertTrue(self.manager.interpolation_slot.acquire(blocking=False))
        self.addCleanup(self.manager.interpolation_slot.release)
        job = self.create()
        self.wait_for(job["id"], stage="等待插帧", terminal=False)
        self.manager.cancel(job["id"])
        self.wait_for_worker(job["id"])
        reopened = JobManager(self.settings, self.extractors)
        try:
            restored = reopened.get(job["id"])
            self.assertEqual(restored["status"], "cancelled")
            self.assertEqual(restored["fps"], 120)
            self.assertEqual(restored["video_quality"], "maximum")
            self.assertIsNone(restored["result"])
            with self.assertRaises(MediaError):
                reopened.download_path(job["id"])
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()

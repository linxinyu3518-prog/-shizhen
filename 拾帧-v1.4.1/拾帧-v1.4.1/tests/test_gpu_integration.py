# SPDX-License-Identifier: GPL-3.0-or-later
"""GPU routing, owned queue slots and raw-frame A/V muxing regressions."""
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from framekeep.config import NO_WINDOW, Settings
from framekeep.jobs import JobManager, validate_options
from framekeep.media import build_encode_command, encoding_plan, probe
from framekeep.network import Cancelled, MediaError


class GPUOptionsTests(unittest.TestCase):
    media = {"has_video": True, "has_audio": True, "formats": []}

    def test_legacy_request_defaults_to_gpu_preferred_and_other_modes_ignore_it(self):
        self.assertEqual(validate_options({}, self.media)["processing_mode"], "auto")
        for mode in ("auto", "gpu", "cpu"):
            self.assertEqual(validate_options({"processing_mode": mode}, self.media)["processing_mode"], mode)
        for kind in ("audio", "original"):
            self.assertIsNone(validate_options({"kind": kind, "processing_mode": "invalid"}, self.media)["processing_mode"])

    def test_invalid_hardware_modes_are_rejected(self):
        for value in (True, 1, None, [], {}, "cuda", "gpu;-i"):
            with self.subTest(value=value), self.assertRaises(MediaError):
                validate_options({"processing_mode": value}, self.media)

    def test_raw_command_does_not_interpolate_again_or_cut_audio_at_video_end(self):
        settings = Settings(ffmpeg="ffmpeg", nvenc=True, interpolation=False)
        metadata = {"fps": 30, "video_duration": .4, "frame_count": 12, "video_start_time": 2,
                    "format_start_time": 2, "has_video": True, "has_audio": True}
        options = {"kind": "video", "container": "mp4", "fps": 120, "height": None,
                   "video_quality": "high", "frame_rate_mode": "interpolate"}
        command = build_encode_command("source.mp4", "output.mp4", options, settings, metadata,
                                       prepared={"raw": True, "width": 160, "height": 96, "fps": 120, "interpolated": True},
                                       compute={"video_encoder": "h264_nvenc"})
        self.assertEqual(command[command.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(command[command.index("-cq") + 1], "16")
        self.assertEqual(command[command.index("-preset") + 1], "p5")
        self.assertIn("pipe:0", command)
        self.assertIn("1:a:0?", command)
        self.assertNotIn("-frames:v", command)
        self.assertNotIn("-shortest", command)
        self.assertNotIn("-t", command)
        self.assertNotIn("-crf", command)
        self.assertNotIn("minterpolate", " ".join(command))


class GPUJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(data_dir=Path(self.temporary.name), ffmpeg="fixture", ffprobe="fixture",
                                 h264=True, aac=True, nvenc=True, interpolation=True)
        self.metadata = {"duration": .4, "video_duration": .4, "width": 640, "height": 360,
                         "fps": 30, "frame_count": 12, "has_video": True, "has_audio": True,
                         "title": "GPU fixture", "formats": []}
        self.extractors = Mock()
        self.extractors.get.return_value = self.metadata
        self.extractors.download.side_effect = self.download
        self.manager = JobManager(self.settings, self.extractors)
        self.addCleanup(self.manager.close)
        self.compute = {"video_encoder": "h264_nvenc", "interpolation_backend": "gpu",
                        "compute_device": "gpu", "compute_note": "fixture CUDA RIFE + NVENC"}
        self.selection_patch = patch.object(self.manager.compute, "select", return_value=self.compute.copy())
        self.selection = self.selection_patch.start()
        self.addCleanup(self.selection_patch.stop)
        self.encode_patch = patch.object(self.manager.compute, "encode", side_effect=self.encode)
        self.gpu_encode = self.encode_patch.start()
        self.addCleanup(self.encode_patch.stop)
        self.probe_patch = patch("framekeep.jobs.probe", return_value=self.metadata)
        self.probe_patch.start()
        self.addCleanup(self.probe_patch.stop)

    def download(self, media, options, folder, *_):
        path = folder / "source.mp4"
        path.write_bytes(b"source fixture")
        return path

    def encode(self, source, output, options, metadata, folder, cancel, progress, plan, compute, prepared=None):
        self.assertFalse(self.manager.upscale.slot.acquire(blocking=False), "GPU encode must own the shared GPU slot")
        output.write_bytes(b"CUDA frame output")
        return {"interpolation_frames": 48, "pipeline": "streaming"}

    def create(self, **options):
        return self.manager.create({"media_id": "fixture", "fps": 120, **options})

    def wait_for(self, identifier, predicate=None):
        predicate = predicate or (lambda job: job["status"] in ("completed", "failed", "cancelled"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.manager.get(identifier)
            if predicate(job):
                return job
            time.sleep(.01)
        self.fail(str(self.manager.get(identifier)))

    def wait_worker(self, identifier):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.manager.lock:
                if identifier not in self.manager.events:
                    return
            time.sleep(.01)
        self.fail("Worker did not release its resources")

    def test_gpu_pipeline_records_actual_backend_and_timing(self):
        with patch("framekeep.jobs.encode") as software_encode:
            job = self.wait_for(self.create()["id"])
        self.assertEqual(job["status"], "completed", job)
        software_encode.assert_not_called()
        self.assertEqual(job["result"]["interpolation_backend"], "gpu")
        self.assertEqual(job["result"]["pipeline"], "streaming")
        self.assertGreaterEqual(job["result"]["total_seconds"], job["result"]["processing_seconds"])
        self.assertEqual(self.selection.call_args.args[1]["fps"], 30)

    def test_auto_gpu_failure_is_visible_and_does_not_silently_retry_on_cpu(self):
        self.gpu_encode.side_effect = MediaError("GPU memory fixture failure")
        with patch("framekeep.jobs.encode") as software_encode:
            job = self.wait_for(self.create()["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("GPU memory", job["message"])
        software_encode.assert_not_called()
        self.wait_worker(job["id"])
        self.assertTrue(self.manager.upscale.slot.acquire(blocking=False))
        self.manager.upscale.slot.release()

    def test_waiting_cancellation_does_not_release_another_jobs_gpu(self):
        entered = threading.Event()

        def blocking(source, output, options, metadata, folder, cancel, progress, plan, compute, prepared=None):
            entered.set()
            if not cancel.wait(5):
                raise MediaError("Fixture cancellation timeout")
            raise Cancelled("Cancelled GPU fixture")

        self.gpu_encode.side_effect = blocking
        first = self.create()
        self.assertTrue(entered.wait(2))
        second = self.create()
        self.wait_for(second["id"], lambda job: job["stage"] == "等待 GPU")
        self.manager.cancel(second["id"])
        self.wait_worker(second["id"])
        self.assertFalse(self.manager.upscale.slot.acquire(blocking=False))
        self.manager.cancel(first["id"])
        self.wait_worker(first["id"])
        self.gpu_encode.side_effect = self.encode
        third = self.wait_for(self.create()["id"])
        self.assertEqual(third["status"], "completed", third)

    def test_gpu_slot_spans_super_resolution_and_final_encode(self):
        def prepare(source, options, metadata, folder, *_):
            self.assertFalse(self.manager.upscale.slot.acquire(blocking=False))
            return {"manifest": str(folder / "fixture.txt")}, {"upscale_provider": "rtx"}

        with patch.object(self.manager.upscale, "select", return_value={"provider": "rtx"}), \
             patch.object(self.manager.upscale, "prepare", side_effect=prepare):
            job = self.wait_for(self.create(upscale="rtx", height=1440)["id"])
        self.assertEqual(job["status"], "completed", job)
        self.assertIsNotNone(self.gpu_encode.call_args.kwargs["prepared"])


class RawFrameMuxTests(unittest.TestCase):
    def test_raw_gpu_frame_contract_keeps_late_audio_and_exact_frame_count(self):
        from test_frame_rates import MediaHarness
        with tempfile.TemporaryDirectory() as root:
            harness = MediaHarness(root)
            source = harness.fixture("raw-source", fps=30, duration=.4, offset=True)
            metadata = probe(source, harness.settings)
            raw = harness.ffmpeg(["-v", "error", "-i", str(source), "-vf", "fps=60", "-an",
                                  "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"], binary=True).stdout
            for container in ("mp4", "avi"):
                with self.subTest(container=container):
                    output = Path(root) / ("raw-output." + container)
                    options = {"kind": "video", "container": container, "fps": 60,
                               "frame_rate_mode": "interpolate", "video_quality": "balanced"}
                    command = build_encode_command(source, output, options, harness.settings, metadata,
                        prepared={"raw": True, "width": 160, "height": 96, "fps": 60, "interpolated": True})
                    result = subprocess.run(command, input=raw, capture_output=True, timeout=30, creationflags=NO_WINDOW)
                    self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
                    actual = probe(output, harness.settings)
                    self.assertEqual(actual["fps"], 60)
                    self.assertEqual(actual["frame_count"], 24)
                    self.assertEqual(actual["sample_rate"], 48000)
                    self.assertGreater(actual["duration"], .48, "Late audio must survive after video EOF")
                    harness.ffmpeg(["-v", "error", "-i", str(output), "-f", "null", "-"])


if __name__ == "__main__":
    unittest.main()

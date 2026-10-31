# SPDX-License-Identifier: GPL-3.0-or-later
"""Three-hour timestamp limits, bounded pipes, and opt-in real CUDA acceptance.

The default suite exercises all 1,296,000 timestamps without allocating images.
Set FRAMEKEEP_REAL_GPU_SMOKE=1 for 8-second RIFE/NVENC MP4 + AVI exports.
Set FRAMEKEEP_REAL_GPU_LONG=1 for a real three-hour 160x128, 1-to-2 fps export.
The long fixture checks media duration, not full-resolution GPU throughput.
"""
from array import array
from contextlib import nullcontext
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from framekeep import interpolation_worker as worker
from framekeep.compute import ComputeService
from framekeep.config import NO_WINDOW, ROOT, Settings
from framekeep.media import encoding_plan, probe
from framekeep.storage import configure_storage


MAX_SECONDS = 3 * 3600
MAX_FRAMES = MAX_SECONDS * 120
ARTIFACTS = ROOT / "deployment" / "long-gpu-20261007"


def write_timestamps(path, count, fps):
    """Generate the maximum-size file in small batches rather than a Python list."""
    with path.open("wb") as handle:
        for start in range(0, count, 4096):
            values = array("d", (index / fps for index in range(start, min(start + 4096, count))))
            if sys.byteorder != "little":
                values.byteswap()
            values.tofile(handle)


class StaticProcessor:
    model_name = "bounded pipeline fixture"
    device = "fixture"
    torch = SimpleNamespace(inference_mode=nullcontext,
                            cuda=SimpleNamespace(max_memory_allocated=lambda: 0))

    def prepare(self, pixels, width, height):
        return pixels

    def pair_kind(self, *arguments):
        return "static"

    def infer(self, *arguments):
        raise AssertionError("Static fixture must hold the source frame")


class CountingPipes:
    """Run every scheduling iteration without keeping decoded/output frame lists."""
    def __init__(self, spec, emit):
        self.spec, self.emit = spec, emit
        self.read_count = self.write_count = 0
        self.closed = self.finished = False
        self.pixels = bytes(spec.width * spec.height * 3)

    def start(self):
        return self

    def check(self):
        pass

    def read(self):
        if self.read_count == len(self.spec.times):
            return None
        self.read_count += 1
        return self.pixels

    def write(self, pixels):
        if len(pixels) != len(self.pixels):
            raise AssertionError("Unexpected frame size")
        self.write_count += 1

    def finish(self):
        self.finished = True

    def close(self):
        self.closed = True


class LongTimelineWorkerTests(unittest.TestCase):
    def setUp(self):
        # configure_storage also keeps native worker caches and temporary files on E:.
        configure_storage(ARTIFACTS / "test-runtime")
        self.temporary = tempfile.TemporaryDirectory(prefix="long-worker-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def specification(self, source_count, source_fps, output_fps=120,
                      duration=MAX_SECONDS, **overrides):
        timing = self.root / "times.bin"
        write_timestamps(timing, source_count, source_fps)
        values = {
            "decoder_command": [sys.executable, "-B", "-c", "pass"],
            "encoder_command": [sys.executable, "-B", "-c", "pass"],
            "width": 8, "height": 8, "fps": output_fps,
            "times_file": str(timing.resolve()), "source_count": source_count,
            "duration": duration, "output_count": round(duration * output_fps),
            "log_dir": str(self.root),
        }
        values.update(overrides)
        return values

    def parse_spec(self, values):
        spec = worker.StreamSpec.parse(values)
        # The worker owns this handle during its stream; directly parsed specs
        # also release it so Windows can remove their temporary files.
        if hasattr(spec.times, "close"):
            self.addCleanup(spec.times.close)
        return spec

    def test_full_120fps_three_hour_timeline_fits_and_stays_mapped(self):
        values = self.specification(MAX_FRAMES, 120)
        self.assertEqual(Path(values["times_file"]).stat().st_size, MAX_FRAMES * 8)
        spec = self.parse_spec(values)
        self.assertNotIsInstance(spec.times, list)
        self.assertEqual(len(spec.times), MAX_FRAMES)
        self.assertEqual(spec.times[0], 0)
        for index in (1, 323999, 324000, MAX_FRAMES - 1):
            self.assertAlmostEqual(spec.times[index], index / 120, places=9)

    def test_every_120fps_source_and_output_frame_over_three_hours_is_consumed(self):
        spec = self.parse_spec(self.specification(MAX_FRAMES, 120))
        instances = []

        def capture(*arguments):
            pipes = CountingPipes(*arguments)
            instances.append(pipes)
            return pipes

        last = {}
        with patch.object(worker, "FramePipes", side_effect=capture):
            result = worker.stream_frames(spec, StaticProcessor(), lambda value: last.update(value))
        self.assertEqual(result["source_frames"], MAX_FRAMES)
        self.assertEqual(result["output_frames"], MAX_FRAMES)
        self.assertEqual(result["source_output_frames"], MAX_FRAMES)
        self.assertEqual(result["generated_frames"], 0)
        self.assertEqual(instances[0].read_count, MAX_FRAMES)
        self.assertEqual(instances[0].write_count, MAX_FRAMES)
        self.assertTrue(instances[0].closed)
        self.assertTrue(instances[0].finished)
        self.assertEqual(last["progress"], 1)

    def test_30_to_120_preserves_final_source_and_all_three_hour_outputs(self):
        source_count = MAX_SECONDS * 30
        spec = self.parse_spec(self.specification(source_count, 30))
        with patch.object(worker, "FramePipes", CountingPipes):
            result = worker.stream_frames(spec, StaticProcessor(), lambda event: None)
        self.assertEqual(result["source_frames"], source_count)
        self.assertEqual(result["output_frames"], MAX_FRAMES)
        self.assertEqual(result["source_output_frames"], source_count)
        self.assertEqual(result["held_frames"], MAX_FRAMES - source_count)
        # Three final output ticks have no future reference and hold the last frame.
        self.assertEqual(result["static_frames"], MAX_FRAMES - source_count - 3)

    def test_duration_and_source_output_frame_caps_are_independent(self):
        values = self.specification(4, 1, output_fps=2, duration=4)
        for override in ({"duration": MAX_SECONDS + .01},
                         {"source_count": MAX_FRAMES + 1},
                         {"output_count": MAX_FRAMES + 1}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                worker.StreamSpec.parse({**values, **override})

    def test_corrupted_mapped_timestamp_data_is_rejected(self):
        values = self.specification(4, 1, output_fps=2, duration=4)
        timing = Path(values["times_file"])
        for times in ((0, 1, float("nan"), 3), (0, 1, float("inf"), 3),
                      (0, 1, 1, 3), (0, 1, .5, 3), (.1, 1, 2, 3),
                      (0, 1, 2, 4.01)):
            timing.write_bytes(struct.pack("<4d", *times))
            with self.subTest(times=times), self.assertRaises(ValueError):
                worker.StreamSpec.parse(values)
        timing.write_bytes(struct.pack("<3d", 0, 1, 2))
        with self.assertRaises(ValueError):
            worker.StreamSpec.parse(values)

    def test_close_unblocks_a_full_native_encoder_pipe_and_both_queues(self):
        width, height = 512, 512
        pixels = bytes(width * height * 3)
        spec = self.parse_spec(self.specification(
            16, 30, duration=16 / 30, width=width, height=height,
            decoder_command=[sys.executable, "-B", "-c",
                             f"import sys; sys.stdout.buffer.write(bytes({len(pixels) * 16}))"],
            encoder_command=[sys.executable, "-B", "-c", "import time; time.sleep(60)"]))
        pipes = worker.FramePipes(spec, lambda event: None)
        self.addCleanup(pipes.close)
        pipes.start()
        # The first large frame blocks in the OS pipe while three more fill
        # the outgoing queue. Decode independently fills the incoming queue.
        for _ in range(worker.QUEUE_CAPACITY + 1):
            pipes.outgoing.put(pixels, timeout=2)
        deadline = time.monotonic() + 3
        while not pipes.incoming.full() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(pipes.incoming.full())
        self.assertTrue(pipes.outgoing.full())
        self.assertEqual(pipes.incoming.maxsize, 3)
        self.assertEqual(pipes.outgoing.maxsize, 3)
        started = time.monotonic()
        pipes.close()
        self.assertLess(time.monotonic() - started, 15)
        for process in (pipes.decoder, pipes.encoder):
            self.assertIsNotNone(process.poll())
        for thread in (pipes.reader, pipes.writer):
            self.assertFalse(thread.is_alive())


class RealGPUMixin:
    def setUp(self):
        configure_storage(ARTIFACTS / "real-gpu-runtime")
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self.settings = Settings.discover(data_dir=ARTIFACTS / "real-gpu-runtime")
        self.assertTrue(self.settings.nvenc, "Configured NVENC runtime must pass its actual encoding probe")
        self.compute = ComputeService(self.settings)
        status = self.compute.status(refresh=True)
        self.assertTrue(status["gpu_interpolation"], status)
        self.status = status

    def ffmpeg(self, arguments, timeout=120):
        result = subprocess.run([self.settings.ffmpeg, "-hide_banner", "-nostdin", "-y", *arguments],
                                capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace")[-4000:])
        return result

    def convert(self, source, output, fps):
        before = probe(source, self.settings)
        options = {"kind": "video", "container": output.suffix[1:], "fps": fps,
                   "processing_mode": "gpu", "frame_rate_mode": "interpolate", "video_quality": "high"}
        plan = encoding_plan(options, before)
        self.assertEqual(plan["effective_frame_rate_mode"], "interpolate")
        selection = self.compute.select(options, before, plan)
        self.assertEqual(selection["interpolation_backend"], "gpu")
        self.assertEqual(selection["video_encoder"], "h264_nvenc")
        folder = ARTIFACTS / output.stem
        folder.mkdir(exist_ok=True)
        report = self.compute.encode(source, output, options, before, folder,
                                     threading.Event(), lambda *arguments: None, plan, selection)
        actual = probe(output, self.settings)
        self.assertGreater(report["interpolation_generated_frames"], 0)
        self.assertEqual(report["pipeline_queue_capacity"], 3)
        self.assertEqual(report["interpolation_temp_frame_files"], 0)
        self.assertEqual(actual["video_codec"], "h264")
        self.assertEqual(actual["frame_count"], round(before["video_duration"] * fps))
        self.assertEqual(actual["fps"], fps)
        self.assertAlmostEqual(actual["video_duration"], before["video_duration"], delta=1 / fps + .001)
        self.ffmpeg(["-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"], timeout=600)
        evidence = {"source": str(source), "output": str(output), "before": before,
                    "actual": actual, "selection": selection, "report": report, "status": self.status}
        (ARTIFACTS / (output.stem + "-acceptance.json")).write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        return before, actual, report


@unittest.skipUnless(os.environ.get("FRAMEKEEP_REAL_GPU_SMOKE") == "1", "Explicit short CUDA/NVENC acceptance")
class RealGPUShortTests(RealGPUMixin, unittest.TestCase):
    def test_eight_second_rife_nvenc_preserves_audio_offset_and_tail(self):
        source = ARTIFACTS / "real-short-source.mp4"
        self.ffmpeg(["-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=8",
                     "-itsoffset", "0.16", "-f", "lavfi", "-i", "sine=frequency=523:sample_rate=48000:duration=8",
                     "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
                     "-crf", "0", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k",
                     "-output_ts_offset", "2", str(source)])
        for fps, container in ((60, "mp4"), (120, "mp4"), (60, "avi")):
            with self.subTest(fps=fps, container=container):
                output = ARTIFACTS / f"real-short-{fps}.{container}"
                before, actual, report = self.convert(source, output, fps)
                self.assertEqual(actual["audio_codec"], "aac")
                self.assertEqual(actual["sample_rate"], 48000)
                if container == "mp4":
                    old_offset = before["audio_start_time"] - before["video_start_time"]
                    new_offset = actual["audio_start_time"] - actual["video_start_time"]
                    self.assertAlmostEqual(new_offset, old_offset, delta=.045)
                self.assertGreater(actual["duration"], 8.10, "Late audio must survive video EOF")
                self.assertEqual(report["interpolation_output_frames"], fps * 8)


@unittest.skipUnless(os.environ.get("FRAMEKEEP_REAL_GPU_LONG") == "1", "Explicit three-hour low-resolution CUDA acceptance")
class RealGPUThreeHourTests(RealGPUMixin, unittest.TestCase):
    def test_three_hour_real_rife_nvenc_at_small_resolution_and_low_fps(self):
        source = ARTIFACTS / "real-three-hour-source.mp4"
        # A one-pixel shift per second yields valid interpolation pairs without
        # the scene-cut detector treating successive low-rate pictures as cuts.
        self.ffmpeg(["-v", "error", "-f", "lavfi", "-i",
                     "nullsrc=size=160x128:rate=1:duration=10800,geq=r='mod(X-N+160000,160)*255/160':g='Y*255/128':b='mod(X-N+160000,160)*128/160'",
                     "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0",
                     "-pix_fmt", "yuv420p", str(source)], timeout=600)
        before, actual, report = self.convert(source, ARTIFACTS / "real-three-hour-2fps.mp4", 2)
        self.assertEqual(before["video_duration"], MAX_SECONDS)
        self.assertEqual(before["frame_count"], 10800)
        self.assertEqual(actual["frame_count"], 21600)
        self.assertEqual(actual["video_duration"], MAX_SECONDS)
        self.assertEqual(report["interpolation_source_frames"], 10800)
        self.assertGreater(report["interpolation_generated_frames"], 10000)


if __name__ == "__main__":
    unittest.main()

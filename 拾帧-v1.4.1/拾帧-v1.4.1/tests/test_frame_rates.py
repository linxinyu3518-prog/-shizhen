# SPDX-License-Identifier: GPL-3.0-or-later
"""Frame-rate regressions against decoded original synthetic media fixtures.

All integration inputs are generated locally. No platform or account is used.
The small fixture harness is also reused by work/qa/frame_rate_quality_check.py.
"""
import hashlib
import json
import math
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from framekeep import media
from framekeep.config import NO_WINDOW, Settings
from framekeep.jobs import validate_options
from framekeep.network import MediaError


class MediaHarness:
    """Create source files, use the application encoder, and decode its outputs."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        with patch.dict("os.environ", {"MEDIA_DATA_DIR": str(self.root / "runtime")}):
            self.settings = Settings.discover(data_dir=self.root / "runtime")
        if not all((self.settings.ffmpeg, self.settings.ffprobe, self.settings.h264, self.settings.aac)):
            raise unittest.SkipTest("FFmpeg libx264 / AAC and FFprobe are required")
        self.sequence = 0

    def ffmpeg(self, arguments, *, timeout=40, binary=False):
        result = subprocess.run([self.settings.ffmpeg, "-hide_banner", "-nostdin", "-y", *arguments],
                                capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace")[-5000:])
        return result if binary else result.stderr.decode("utf-8", errors="replace")

    def fixture(self, name, *, fps=30, duration=0.6, size="160x96", audio=True, vfr=False, offset=False, frames=None):
        path = self.root / (name + ".mp4")
        command = ["-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={duration}"]
        if audio:
            if offset:
                command += ["-itsoffset", "0.16"]
            command += ["-f", "lavfi", "-i", f"sine=frequency=523:sample_rate=48000:duration={duration}", "-map", "0:v", "-map", "1:a"]
        if vfr:
            command += ["-vf", "select=not(mod(n\\,3))+not(mod(n\\,5))", "-fps_mode", "vfr"]
        if frames is not None:
            command += ["-frames:v", str(frames)]
        command += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p"]
        if audio:
            command += ["-c:a", "aac", "-b:a", "256k"]
        else:
            command += ["-an"]
        if offset:
            command += ["-output_ts_offset", "2"]
        self.ffmpeg([*command, str(path)])
        return path

    def transcode(self, source, *, fps=None, mode="interpolate", container="mp4", quality="high", height=None, name=None):
        self.sequence += 1
        output = self.root / ((name or f"output-{self.sequence}") + "." + container)
        options = {"kind": "video", "container": container, "fps": fps,
                   "frame_rate_mode": mode, "video_quality": quality, "height": height}
        metadata = media.probe(source, self.settings)
        started = time.perf_counter()
        media.encode(source, output, options, self.settings, metadata, lambda *_: None, threading.Event())
        elapsed = time.perf_counter() - started
        result = media.probe(output, self.settings)
        self.decode(output)
        return output, result, elapsed

    def decode(self, path):
        self.ffmpeg(["-v", "error", "-xerror", "-i", str(path), "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"])

    def frames(self, path):
        command = [self.settings.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                   "-show_entries", "frame=best_effort_timestamp_time,pkt_duration_time", "-of", "json", str(path)]
        result = subprocess.run(command, capture_output=True, check=True, timeout=30, creationflags=NO_WINDOW)
        return json.loads(result.stdout)["frames"]

    def filtered_hashes(self, source, filters):
        result = self.ffmpeg(["-v", "error", "-i", str(source), "-an", "-vf", filters,
                              "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", "-f", "framemd5", "pipe:1"], binary=True)
        return [line.rsplit(",", 1)[-1].strip() for line in result.stdout.decode().splitlines() if line and not line.startswith("#")]


class FrameRateOptionsTests(unittest.TestCase):
    def setUp(self):
        self.media = {"has_video": True, "has_audio": True, "formats": [{"id": "0"}]}
        self.base = {"kind": "video", "container": "mp4", "format_id": "best"}
        self.settings = Settings(ffmpeg="ffmpeg")
        self.metadata = {"fps": 30, "avg_frame_rate": "30/1", "r_frame_rate": "30/1",
                         "duration": 3, "video_duration": 3, "frame_count": 90,
                         "video_start_time": 0, "format_start_time": 0}

    def test_defaults_are_high_quality_and_original_frame_rate(self):
        options = validate_options(self.base, self.media)
        self.assertEqual(options["video_quality"], "high")
        self.assertIsNone(options["fps"])
        self.assertEqual(options["frame_rate_mode"], "interpolate")

    def test_integer_frame_rates_are_bounded_without_coercion(self):
        for fps in (1, 24, 30, 60, 119, 120):
            with self.subTest(fps=fps):
                self.assertEqual(validate_options({**self.base, "fps": fps}, self.media)["fps"], fps)
        for fps in (0, -1, 121, 1000, 29.97, "60", True, False, [], {}):
            with self.subTest(fps=fps), self.assertRaises(MediaError):
                validate_options({**self.base, "fps": fps}, self.media)

    def test_unknown_quality_or_mode_and_shell_text_are_rejected(self):
        for change in ({"video_quality": "ultra"}, {"video_quality": 16},
                       {"frame_rate_mode": "flow"}, {"frame_rate_mode": True},
                       {"fps": "120,movie=private"}, {"video_quality": "16 -i secret"}):
            with self.subTest(change=change), self.assertRaises(MediaError):
                validate_options({**self.base, **change}, self.media)

    def test_quality_choices_map_to_reviewable_encoder_profiles(self):
        for quality, expected in (("balanced", ("18", "medium")), ("high", ("16", "slow")), ("maximum", ("14", "slow"))):
            command = media.build_encode_command(Path("in.mp4"), Path("out.mp4"),
                {**self.base, "video_quality": quality}, self.settings, self.metadata)
            with self.subTest(quality=quality):
                self.assertEqual((command[command.index("-crf") + 1], command[command.index("-preset") + 1]), expected)
                self.assertNotIn("-level", command)
                self.assertNotIn("-level:v", command)

    def test_plan_distinguishes_keep_reduce_interpolate_and_fast(self):
        cases = [(None, "interpolate", "mp4", "source"),
                 (None, "interpolate", "avi", "fast"),
                 (24, "interpolate", "mp4", "fast"),
                 (30, "interpolate", "mp4", "fast"),
                 (120, "interpolate", "mp4", "interpolate"),
                 (120, "fast", "mp4", "fast")]
        for fps, mode, container, expected in cases:
            with self.subTest(fps=fps, mode=mode, container=container):
                plan = media.encoding_plan({**self.base, "container": container, "fps": fps, "frame_rate_mode": mode}, self.metadata)
                self.assertEqual(plan["effective_frame_rate_mode"], expected)

    def test_insufficient_source_metadata_or_frames_falls_back_with_an_explanation(self):
        for metadata in ({**self.metadata, "frame_count": 1, "video_duration": 1 / 30, "duration": 1 / 30},
                         {**self.metadata, "frame_count": 2, "video_duration": 2 / 30, "duration": 2 / 30},
                         {"fps": None, "duration": 3}, {"fps": 30, "duration": None}):
            with self.subTest(metadata=metadata):
                plan = media.encoding_plan({**self.base, "fps": 120, "frame_rate_mode": "interpolate"}, metadata)
                self.assertEqual(plan["effective_frame_rate_mode"], "fast")
                self.assertTrue(plan["frame_rate_note"])


class FrameRateMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.harness = MediaHarness(cls.temporary.name)
        cls.sources = {fps: cls.harness.fixture(f"source-{fps}", fps=fps) for fps in (30, 60, 120)}

    def assert_timing(self, source, output, result, fps):
        before = media.probe(source, self.harness.settings)
        frames = self.harness.frames(output)
        timestamps = [float(frame["best_effort_timestamp_time"]) for frame in frames]
        self.assertTrue(timestamps)
        self.assertTrue(all(second > first for first, second in zip(timestamps, timestamps[1:])))
        self.assertAlmostEqual(result["fps"], fps, delta=0.01)
        expected = before.get("video_duration") or before["duration"]
        self.assertAlmostEqual(len(frames) / fps, expected, delta=1 / fps + .001)
        self.assertAlmostEqual(result["duration"], before["duration"], delta=max(1 / fps, 1024 / 48000) + .001)
        self.assertEqual(result["video_codec"], "h264")
        self.assertEqual(result["pixel_format"], "yuv420p")
        self.assertEqual(result["audio_codec"], "aac")
        self.assertEqual(result["sample_rate"], 48000)
        if len(timestamps) > 1:
            self.assertTrue(all(abs((second - first) - 1 / fps) < 0.001 for first, second in zip(timestamps, timestamps[1:])))

    def test_conversion_matrix_decodes_and_preserves_duration(self):
        for source_fps, target in ((30, 60), (30, 120), (60, 120), (120, 60)):
            for mode in ("interpolate", "fast"):
                for container in ("mp4", "avi"):
                    with self.subTest(source_fps=source_fps, target=target, mode=mode, container=container):
                        source = self.sources[source_fps]
                        output, result, _ = self.harness.transcode(source, fps=target, mode=mode, container=container)
                        self.assert_timing(source, output, result, target)

    def test_nonstandard_integer_rates_reach_actual_output(self):
        for target in (1, 47, 119):
            with self.subTest(target=target):
                output, result, _ = self.harness.transcode(self.sources[30], fps=target, mode="fast")
                self.assertAlmostEqual(result["fps"], target, delta=0.01)
                self.assertGreater(len(self.harness.frames(output)), 0)

    def test_motion_compensation_creates_intermediate_images_not_duplicate_frames(self):
        source = self.sources[30]
        metadata = media.probe(source, self.harness.settings)
        values = {}
        for mode in ("interpolate", "fast"):
            command = media.build_encode_command(source, Path("unused.mp4"),
                {"kind": "video", "container": "mp4", "fps": 120, "frame_rate_mode": mode}, self.harness.settings, metadata)
            filters = command[command.index("-vf") + 1]
            hashes = self.harness.filtered_hashes(source, filters)
            self.assertGreaterEqual(len(hashes), 68)
            values[mode] = set(hashes)
        self.assertGreater(len(values["interpolate"]), len(values["fast"]) * 1.5)
        self.assertGreater(len(values["interpolate"] - values["fast"]), 20)

    def test_mp4_keep_source_preserves_variable_frame_timestamps(self):
        source = self.harness.fixture("vfr", fps=30, audio=False, vfr=True)
        before = self.harness.frames(source)
        output, result, _ = self.harness.transcode(source, fps=None)
        after = self.harness.frames(output)
        self.assertEqual(len(after), len(before))
        first_before = float(before[0]["best_effort_timestamp_time"])
        first_after = float(after[0]["best_effort_timestamp_time"])
        for old, new in zip(before, after):
            self.assertAlmostEqual(float(old["best_effort_timestamp_time"]) - first_before,
                                   float(new["best_effort_timestamp_time"]) - first_after, delta=0.001)
        self.assertFalse(result["has_audio"])

    def test_variable_rate_source_converts_without_truncated_tail(self):
        source = self.harness.fixture("vfr-convert", fps=30, audio=True, vfr=True)
        before = media.probe(source, self.harness.settings)
        for container in ("mp4", "avi"):
            with self.subTest(container=container):
                output, result, _ = self.harness.transcode(source, fps=120, container=container)
                self.assertAlmostEqual(result["fps"], 120, delta=0.01)
                self.assertAlmostEqual(result["duration"], before["duration"], delta=0.075)
                self.assertGreaterEqual(len(self.harness.frames(output)), 60)

    def test_audio_video_relative_offset_survives_frame_conversion(self):
        source = self.harness.fixture("offset", offset=True)
        before = media.probe(source, self.harness.settings)
        old_offset = before["audio_start_time"] - before["video_start_time"]
        self.assertGreater(old_offset, 0.1)
        for mode in ("interpolate", "fast"):
            with self.subTest(mode=mode):
                _, result, _ = self.harness.transcode(source, fps=120, mode=mode)
                new_offset = result["audio_start_time"] - result["video_start_time"]
                self.assertAlmostEqual(new_offset, old_offset, delta=0.045)
                self.assertAlmostEqual(result["video_duration"], before["video_duration"], delta=0.025)

    def test_single_frame_and_silent_video_remain_decodable(self):
        sources = [self.harness.fixture("single", fps=30, audio=False, frames=1),
                   self.harness.fixture("silent", fps=30, audio=False)]
        for source in sources:
            for container in ("mp4", "avi"):
                with self.subTest(source=source.name, container=container):
                    output, result, _ = self.harness.transcode(source, fps=120, container=container)
                    self.assertTrue(result["has_video"])
                    self.assertFalse(result["has_audio"])
                    self.assertGreater(len(self.harness.frames(output)), 0)

    def test_1440_and_2160_options_never_upscale_lower_resolution(self):
        source = self.sources[30]
        for height in (1440, 2160):
            with self.subTest(height=height):
                _, result, _ = self.harness.transcode(source, height=height)
                self.assertEqual((result["width"], result["height"]), (160, 96))


if __name__ == "__main__":
    unittest.main()

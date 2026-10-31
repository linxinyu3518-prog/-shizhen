# SPDX-License-Identifier: GPL-3.0-or-later
"""Long interpolation limits, bounded PTS collection and process cleanup."""
import io
import json
import queue
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from framekeep.compute import ComputeService
from framekeep.config import NO_WINDOW, Settings
from framekeep.network import Cancelled, MediaError
from framekeep.stream_timing import (BoundedErrorLog, DoubleTimeline, MAX_DURATION_SECONDS,
                                     MAX_ERROR_LOG_BYTES, MAX_OUTPUT_FRAMES, MAX_PIPE_LINE_BYTES,
                                     MAX_SOURCE_FRAMES, read_pipe_lines, scan_timestamps, validate_limits)
from framekeep.upscale import MAX_FRAMES as SR_MAX_FRAMES


class ProbeProcess:
    def __init__(self, output=b"", errors=b"", returncode=0):
        self.stdout, self.stderr = io.BytesIO(output), io.BytesIO(errors)
        self.returncode = returncode
        self.killed = False
        self.waited = False

    def poll(self):
        return self.returncode

    def wait(self):
        self.waited = True
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -1


class BlockingProcess(ProbeProcess):
    def __init__(self):
        super().__init__(returncode=None)
        self.released = threading.Event()
        process = self
        class BlockingPipe:
            def readline(self, _):
                process.released.wait(2)
                return b""

            def close(self):
                pass
        self.stdout = BlockingPipe()

    def kill(self):
        super().kill()
        self.released.set()


class LongTimingTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / ".runtime-temp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.folder = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def scan(self, output, duration=10, process=None, **kwargs):
        process = process or ProbeProcess(output)
        with patch("framekeep.stream_timing.subprocess.Popen", return_value=process):
            result = scan_timestamps("ffprobe", "source.mp4", self.folder, duration,
                                     kwargs.pop("cancel", threading.Event()), **kwargs)
        self.assertTrue(process.waited)
        return result

    def test_three_hours_and_120_fps_are_allowed_without_changing_super_resolution(self):
        self.assertEqual(MAX_DURATION_SECONDS, 10800)
        self.assertEqual(MAX_SOURCE_FRAMES, 1296000)
        self.assertEqual(MAX_OUTPUT_FRAMES, 1296000)
        self.assertEqual(validate_limits(10800, 120, 1296000), 1296000)
        self.assertEqual(validate_limits(10800, 60, 324000), 648000)
        self.assertEqual(SR_MAX_FRAMES, 18000)
        for duration, fps, source_count in ((10800.001, 60, 0), (10800, 121, 0),
                                             (10800, 120, 1296001)):
            with self.subTest(duration=duration, fps=fps, source_count=source_count):
                with self.assertRaises(MediaError):
                    validate_limits(duration, fps, source_count)

    def test_compact_frame_records_ignore_sei_and_normalize_start_pts(self):
        data = (b"frame|best_effort_timestamp_time=2.000000|side_data|side_data_type=SEI\n"
                b"\nside_data|side_data_type=ignored\n"
                b"frame|best_effort_timestamp_time=2.033333\n")
        result = self.scan(data)
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(Path(result["times_file"]).stat().st_size, 16)
        with DoubleTimeline(result["times_file"], 2) as times:
            self.assertEqual(times[0], 0)
            self.assertAlmostEqual(times[-1], .033333)
            self.assertEqual(times[:1], [0])
            with self.assertRaises(IndexError):
                times[2]
        times.close()  # worker cleanup may close a timeline twice

    def test_more_than_the_old_source_limit_is_fully_written_and_indexable(self):
        count = 18031
        data = b"".join(f"frame|best_effort_timestamp_time={7 + index / 30:.6f}\n".encode()
                        for index in range(count))
        result = self.scan(data, duration=602)
        self.assertEqual(result["source_count"], count)
        self.assertEqual(Path(result["times_file"]).stat().st_size, count * 8)
        with DoubleTimeline(result["times_file"], count) as times:
            self.assertEqual(len(times), count)
            self.assertEqual(times[18000], 600)
            self.assertEqual(times[-1], 601)

    def test_full_120_fps_three_hour_timeline_can_be_mapped_without_a_list(self):
        path = self.folder / "full-times.f64"
        with path.open("wb") as output:
            output.write(struct.pack("<d", 0))
            output.seek((MAX_SOURCE_FRAMES - 1) * 8)
            output.write(struct.pack("<d", (MAX_SOURCE_FRAMES - 1) / 120))
        with DoubleTimeline(path, MAX_SOURCE_FRAMES) as times:
            self.assertEqual(len(times), MAX_SOURCE_FRAMES)
            self.assertAlmostEqual(times[-1], 10800 - 1 / 120)
        with self.assertRaises(ValueError):
            DoubleTimeline(path, MAX_SOURCE_FRAMES - 1)

    def test_invalid_or_missing_timestamps_never_leave_a_partial_file(self):
        for data in (b"", b"frame\n", b"frame|best_effort_timestamp_time=N/A\n",
                     b"frame|best_effort_timestamp_time=nan\n",
                     b"frame|best_effort_timestamp_time=inf\n",
                     b"frame|best_effort_timestamp_time=0\nframe|best_effort_timestamp_time=0\n",
                     b"frame|best_effort_timestamp_time=0\nframe|best_effort_timestamp_time=10.001\n"):
            with self.subTest(data=data), self.assertRaises(MediaError):
                self.scan(data)
            self.assertFalse((self.folder / "times.partial").exists())

    def test_actual_scan_enforces_source_limit_independently_of_metadata(self):
        data = b"".join(f"frame|best_effort_timestamp_time={index}\n".encode() for index in range(4))
        with patch("framekeep.stream_timing.MAX_SOURCE_FRAMES", 3):
            with self.assertRaisesRegex(MediaError, "来源画面"):
                self.scan(data)

    def test_timeout_and_cancellation_kill_and_reap_the_probe(self):
        for cancelled in (False, True):
            process = BlockingProcess()
            cancel = threading.Event()
            if cancelled:
                timer = threading.Timer(.02, cancel.set)
                timer.start()
                self.addCleanup(timer.cancel)
            with self.subTest(cancelled=cancelled), self.assertRaises(Cancelled if cancelled else MediaError):
                self.scan(b"", process=process, cancel=cancel, timeout=.03)
            self.assertTrue(process.killed)
            self.assertTrue(process.waited)
            self.assertFalse((self.folder / "times.partial").exists())

    def test_bad_pipe_record_is_bounded_and_fails_without_a_stuck_reader(self):
        with self.assertRaisesRegex(MediaError, "单行"):
            self.scan(b"x" * (MAX_PIPE_LINE_BYTES + 1) + b"\n")
        stop = threading.Event()
        messages = queue.Queue(maxsize=1)
        reader = threading.Thread(target=read_pipe_lines,
                                  args=(io.BytesIO(b"{}\n" * 500), messages, stop))
        reader.start()
        stop.set()
        reader.join(timeout=1)
        self.assertFalse(reader.is_alive())
        self.assertLessEqual(messages.qsize(), 1)

    def test_stderr_is_drained_but_its_local_log_and_tail_are_bounded(self):
        content = b"x" * (MAX_ERROR_LOG_BYTES * 3) + b"last useful failure"
        log = BoundedErrorLog(io.BytesIO(content), self.folder / "probe.log")
        log.close()
        self.assertEqual(log.path.stat().st_size, MAX_ERROR_LOG_BYTES)
        self.assertTrue(log.details().endswith("last useful failure"))

    def test_gpu_spec_uses_a_file_and_accepts_source_and_output_above_old_caps(self):
        settings = SimpleNamespace(ffprobe="ffprobe", ffmpeg="ffmpeg", interpolation_python="python",
                                   interpolation_root="rife", interpolation_model="model", max_download_bytes=10)
        destination = self.folder / "long.mp4"
        destination.write_bytes(b"output larger than the input download cap")
        timing = {"times_file": str(self.folder / "times.f64"), "source_count": 18031}
        process = ProbeProcess(json.dumps({"event": "result", "ok": True,
                                          "output_frames": 648000}).encode() + b"\n")
        compute = ComputeService(settings)
        metadata = {"video_duration": 10800, "frame_count": 324000, "width": 1920, "height": 1080}
        with patch("framekeep.compute.scan_timestamps", return_value=timing), \
                patch("framekeep.compute.build_encode_command", return_value=["ffmpeg"]), \
                patch("framekeep.compute.subprocess.Popen", return_value=process), \
                patch("framekeep.compute.shutil.disk_usage", return_value=SimpleNamespace(free=2 ** 30)):
            result = compute.encode("source.mp4", destination, {"fps": 60}, metadata, self.folder,
                                    threading.Event(), Mock(), {}, {})
        spec = json.loads((self.folder / "gpu-stream" / "stream.json").read_text(encoding="utf-8"))
        self.assertNotIn("times", spec)
        self.assertEqual(spec["times_file"], timing["times_file"])
        self.assertEqual(spec["source_count"], 18031)
        self.assertEqual(result["interpolation_output_frames"], 648000)
        self.assertTrue(process.waited)

    def test_gpu_supervisor_cancellation_timeouts_and_pipe_failure_reap_worker(self):
        settings = SimpleNamespace(ffprobe="ffprobe", ffmpeg="ffmpeg", interpolation_python="python",
                                   interpolation_root="rife", interpolation_model="model")
        metadata = {"video_duration": 1, "frame_count": 2, "width": 1920, "height": 1080}
        timing = {"times_file": str(self.folder / "times.f64"), "source_count": 2}
        for mode in ("cancel", "timeout", "stall", "pipe"):
            process = (ProbeProcess(b"x" * (MAX_PIPE_LINE_BYTES + 1)) if mode == "pipe"
                       else BlockingProcess())
            cancel = threading.Event()
            if mode == "cancel":
                timer = threading.Timer(.02, cancel.set)
                timer.start()
                self.addCleanup(timer.cancel)
            with self.subTest(mode=mode), \
                    patch("framekeep.compute.scan_timestamps", return_value=timing), \
                    patch("framekeep.compute.build_encode_command", return_value=["ffmpeg"]), \
                    patch("framekeep.compute.subprocess.Popen", return_value=process), \
                    patch("framekeep.compute.stop_worker_tree", side_effect=lambda worker: worker.kill()), \
                    patch("framekeep.compute.GPU_TIMEOUT_SECONDS", -1 if mode == "timeout" else 86400), \
                    patch("framekeep.compute.STALL_TIMEOUT_SECONDS", -1 if mode == "stall" else 600), \
                    patch("framekeep.compute.shutil.disk_usage", return_value=SimpleNamespace(free=2 ** 30)), \
                    self.assertRaises(Cancelled if mode == "cancel" else MediaError):
                ComputeService(settings).encode("source.mp4", self.folder / "output.mp4", {"fps": 60},
                                                 metadata, self.folder, cancel, Mock(), {}, {})
            self.assertTrue(process.killed)
            self.assertTrue(process.waited)

    def test_low_disk_space_is_reported_before_timestamp_or_gpu_process_starts(self):
        metadata = {"video_duration": 1, "frame_count": 2, "width": 1920, "height": 1080}
        with patch("framekeep.compute.shutil.disk_usage", return_value=SimpleNamespace(free=0)), \
                patch("framekeep.compute.scan_timestamps") as scan, \
                patch("framekeep.compute.subprocess.Popen") as popen, \
                self.assertRaisesRegex(MediaError, "512 MiB"):
            ComputeService(SimpleNamespace()).encode("source.mp4", self.folder / "output.mp4", {"fps": 60},
                                                     metadata, self.folder, threading.Event(), Mock(), {}, {})
        scan.assert_not_called()
        popen.assert_not_called()


class RealLongTimestampTests(unittest.TestCase):
    def test_local_h264_video_above_18000_frames_retains_every_display_pts(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / ".runtime-temp") as temporary:
            folder = Path(temporary)
            settings = Settings.discover(data_dir=folder / "runtime")
            if not all((settings.ffmpeg, settings.ffprobe, settings.h264)):
                self.skipTest("Local FFmpeg / H.264 / FFprobe are required")
            source = folder / "long-source.mp4"
            completed = subprocess.run([settings.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi",
                                        "-i", "testsrc2=size=64x48:rate=30:duration=601", "-an",
                                        "-c:v", "libx264", "-preset", "ultrafast", "-bf", "2", "-pix_fmt", "yuv420p",
                                        str(source)], capture_output=True, timeout=40, creationflags=NO_WINDOW)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
            result = scan_timestamps(settings.ffprobe, source, folder, 601, threading.Event())
            self.assertEqual(result["source_count"], 18030)
            with DoubleTimeline(result["times_file"], 18030) as times:
                self.assertEqual(times[0], 0)
                self.assertAlmostEqual(times[-1], 601 - 1 / 30, places=5)
                self.assertTrue(all(times[index] > times[index - 1] for index in range(1, len(times))))


if __name__ == "__main__":
    unittest.main()

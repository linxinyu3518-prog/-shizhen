# SPDX-License-Identifier: GPL-3.0-or-later
"""Pipe/timestamp acceptance without requiring Torch in the application venv."""
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from framekeep import interpolation_worker as worker


class FakeProcessor:
    model_name = "test interpolation"
    device = "test device"
    torch = SimpleNamespace(inference_mode=nullcontext,
                            cuda=SimpleNamespace(max_memory_allocated=lambda: 0))

    def __init__(self, kind="interpolate"):
        self.kind = kind
        self.prepared = []
        self.pairs = []

    def prepare(self, pixels, width, height):
        self.prepared.append(pixels[0])
        return pixels

    def pair_kind(self, raw0, raw1, first, second, width, height):
        self.pairs.append((raw0[0], raw1[0]))
        return "static" if raw0 == raw1 else self.kind

    def infer(self, first, second, timestep, width, height):
        value = round(first[0] * (1 - timestep) + second[0] * timestep)
        return bytes([value]) * (width * height * 3)


class StreamWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def spec(self, values=(0, 100, 200), times=(0, .1, .2), fps=20, count=6, decoder=None, encoder=None):
        output = self.root / "frames.rgb"
        decoder = decoder or f"import sys; sys.stdout.buffer.write(b''.join(bytes([v])*192 for v in {list(values)!r}))"
        encoder = encoder or "import sys,pathlib; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
        return worker.StreamSpec.parse({
            "decoder_command": [sys.executable, "-B", "-c", decoder],
            "encoder_command": [sys.executable, "-B", "-c", encoder, str(output)],
            "width": 8, "height": 8, "fps": fps, "times": list(times),
            "output_count": count, "log_dir": str(self.root),
        })

    def pixels(self):
        return list((self.root / "frames.rgb").read_bytes()[::192])

    def run_stream(self, spec, processor=None):
        events = []
        processor = processor or FakeProcessor()
        result = worker.stream_frames(spec, processor, events.append)
        self.assertEqual(result["output_frames"], result["generated_frames"] + result["held_frames"] + result["source_output_frames"])
        self.assertEqual(result["source_frames"], len(spec.times))
        self.assertEqual(result["queue_capacity"], 3)
        self.assertTrue(any(event.get("encoder_pid") for event in events))
        self.assertEqual(events[-1]["progress"], 1)
        return result

    def test_exact_pts_generated_middle_and_full_tail(self):
        processor = FakeProcessor()
        result = self.run_stream(self.spec(), processor)
        self.assertEqual(self.pixels(), [0, 50, 100, 150, 200, 200])
        self.assertEqual(result["generated_frames"], 2)
        self.assertEqual(result["held_frames"], 1)
        self.assertEqual(processor.prepared, [0, 100, 200])
        self.assertEqual(processor.pairs, [(0, 100), (100, 200)])

    def test_30_to_120_checks_pair_once_and_caches_upload(self):
        processor = FakeProcessor()
        self.run_stream(self.spec(values=(0, 100, 200), times=(0, 1/30, 2/30), fps=120, count=12), processor)
        self.assertEqual(self.pixels(), [0, 25, 50, 75, 100, 125, 150, 175, 200, 200, 200, 200])
        self.assertEqual(len(processor.pairs), 2)
        self.assertEqual(len(processor.prepared), 3)

    def test_vfr_uses_original_timestamps(self):
        self.run_stream(self.spec(times=(0, .075, .2)))
        self.assertEqual(self.pixels(), [0, 67, 120, 160, 200, 200])

    def test_cut_holds_original_until_next_source(self):
        result = self.run_stream(self.spec(), FakeProcessor("cut"))
        self.assertEqual(self.pixels(), [0, 0, 100, 100, 200, 200])
        self.assertEqual(result["cut_frames"], 2)
        self.assertEqual(result["generated_frames"], 0)

    def test_static_and_single_frame(self):
        result = self.run_stream(self.spec(values=(100, 100, 100)))
        self.assertEqual(self.pixels(), [100] * 6)
        self.assertEqual(result["static_frames"], 2)
        result = self.run_stream(self.spec(values=(90,), times=(0,), count=4))
        self.assertEqual(self.pixels(), [90] * 4)
        self.assertEqual(result["held_frames"], 3)

    def test_downsample_consumes_all_sources(self):
        self.run_stream(self.spec(values=range(10), times=[i / 100 for i in range(10)], fps=20, count=2))
        self.assertEqual(self.pixels(), [0, 5])

    def test_short_output_still_verifies_decoder(self):
        spec = self.spec(values=range(10), times=[i / 100 for i in range(10)], count=1)
        self.run_stream(spec)
        self.assertEqual(self.pixels(), [0])

    def assert_pipeline_fails_cleanly(self, spec, expected):
        instances = []
        original = worker.FramePipes

        def capture(*args):
            instance = original(*args)
            instances.append(instance)
            return instance

        with patch.object(worker, "FramePipes", side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, expected):
                worker.stream_frames(spec, FakeProcessor(), lambda event: None)
        for process in (instances[0].decoder, instances[0].encoder):
            self.assertIsNotNone(process.poll())
        for thread in (instances[0].reader, instances[0].writer):
            self.assertFalse(thread.is_alive())

    def test_extra_decoded_frames_fail_without_deadlock(self):
        self.assert_pipeline_fails_cleanly(self.spec(values=range(10)), "超过来源时间戳")

    def test_missing_decoded_frames_fail_without_deadlock(self):
        self.assert_pipeline_fails_cleanly(self.spec(values=(0, 100)), "时间戳数量.*不符")

    def test_partial_frame_fails_without_deadlock(self):
        self.assert_pipeline_fails_cleanly(self.spec(decoder="import sys; sys.stdout.buffer.write(b'x'*193)"), "不完整")

    def test_decoder_nonzero_exit_propagates(self):
        self.assert_pipeline_fails_cleanly(self.spec(decoder="import sys; sys.exit(7)"), "解码失败")

    def test_encoder_exit_unblocks_full_output_queue(self):
        self.assert_pipeline_fails_cleanly(self.spec(count=3000, encoder="import sys; sys.exit(8)"), "编码")

    def test_rejects_invalid_timestamp_and_numeric_values(self):
        valid = self.spec()
        value = vars(valid).copy()
        value["log_dir"] = str(valid.log_dir)
        for name, bad in (("fps", float("nan")), ("fps", True), ("fps", 121), ("width", True),
                          ("output_count", 0), ("times", [0, 0]), ("times", [0, float("inf")]),
                          ("times", [0.1]), ("times", []), ("decoder_command", "ffmpeg")):
            with self.subTest(name=name, value=bad):
                with self.assertRaises(ValueError):
                    worker.StreamSpec.parse({**value, name: bad})


if __name__ == "__main__":
    unittest.main()

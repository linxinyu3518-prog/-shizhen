"""Known over-limit GPU interpolation should fail before downloading media."""
import unittest
from framekeep.jobs import check_long_gpu_request
from framekeep.network import MediaError


class LongGPUPreflightTests(unittest.TestCase):
    def setUp(self):
        self.options = {"kind": "video", "fps": 120, "frame_rate_mode": "interpolate", "processing_mode": "auto"}
        self.media = {"fps": 30, "duration": 10800, "frame_count": 324000}

    def test_three_hours_allowed_at_60_and_120(self):
        for fps in (60, 120):
            check_long_gpu_request({**self.options, "fps": fps}, self.media)

    def test_above_three_hours_rejected_before_download(self):
        with self.assertRaisesRegex(MediaError, "3 小时"):
            check_long_gpu_request(self.options, {**self.media, "duration": 10800.001})

    def test_large_source_count_is_checked_independently(self):
        with self.assertRaisesRegex(MediaError, "1,296,000"):
            check_long_gpu_request(self.options, {**self.media, "frame_count": 1296001})

    def test_fast_source_audio_original_cpu_are_unaffected(self):
        long_media = {**self.media, "duration": 16000}
        for override in ({"frame_rate_mode": "fast"}, {"kind": "audio"}, {"kind": "original"},
                         {"processing_mode": "cpu"}, {"fps": 30}, {"fps": None}):
            check_long_gpu_request({**self.options, **override}, long_media)

    def test_unknown_source_rate_waits_for_actual_probe(self):
        check_long_gpu_request(self.options, {"duration": 16000, "fps": None})


if __name__ == "__main__":
    unittest.main()

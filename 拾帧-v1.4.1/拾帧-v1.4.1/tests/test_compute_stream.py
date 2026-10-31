# SPDX-License-Identifier: GPL-3.0-or-later
"""Fallback decisions cannot label a CPU interpolation as GPU work."""
import unittest
from unittest.mock import Mock

from framekeep.compute import ComputeService, output_size
from framekeep.config import Settings
from framekeep.network import MediaError


class StreamSelectionTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.settings.nvenc = True
        self.compute = ComputeService(self.settings)
        self.compute.status = Mock(return_value={"gpu_interpolation": True, "model": "RIFE 4.25 lite"})
        self.source = {"width": 1920, "height": 1080, "sample_aspect_ratio": "1:1"}
        self.plan = {"effective_frame_rate_mode": "interpolate"}

    def test_default_runs_gpu_interpolation_and_encoder(self):
        result = self.compute.select({}, self.source, self.plan)
        self.assertEqual((result["video_encoder"], result["interpolation_backend"], result["pipeline"]),
                         ("h264_nvenc", "gpu", "streaming"))

    def test_cpu_does_not_probe_external_runtime(self):
        result = self.compute.select({"processing_mode": "cpu"}, self.source, self.plan)
        self.assertEqual((result["video_encoder"], result["interpolation_backend"]), ("libx264", "cpu"))
        self.compute.status.assert_not_called()

    def test_missing_rife_discloses_partial_gpu_use(self):
        self.compute.status.return_value = {"gpu_interpolation": False, "reason": "missing model"}
        result = self.compute.select({}, self.source, self.plan)
        self.assertEqual(result["compute_device"], "mixed")
        self.assertEqual(result["interpolation_backend"], "cpu")
        self.assertIn("missing model", result["compute_note"])
        with self.assertRaises(MediaError):
            self.compute.select({"processing_mode": "gpu"}, self.source, self.plan)

    def test_missing_encoder_never_silently_accepts_gpu_only(self):
        self.settings.nvenc = False
        with self.assertRaises(MediaError):
            self.compute.select({"processing_mode": "gpu"}, self.source, self.plan)
        result = self.compute.select({}, self.source, self.plan)
        self.assertEqual(result["compute_device"], "cpu")
        self.compute.status.assert_not_called()

    def test_no_interpolation_only_needs_nvenc(self):
        result = self.compute.select({}, self.source, {"effective_frame_rate_mode": "fast"})
        self.assertEqual(result["interpolation_backend"], "none")
        self.assertEqual(result["video_encoder"], "h264_nvenc")
        self.compute.status.assert_not_called()

    def test_rotated_hdr_sources_are_not_fed_into_rgb_pipeline(self):
        for fields in ({"rotation": 90}, {"color_transfer": "smpte2084"}):
            with self.subTest(fields=fields), self.assertRaises(MediaError):
                self.compute.select({"processing_mode": "gpu"}, {**self.source, **fields}, self.plan)

    def test_display_aspect_even_dimensions_and_no_upscale(self):
        self.assertEqual(output_size(self.source, {"height": 2160}), (1920, 1080))
        self.assertEqual(output_size(self.source, {"height": 720}), (1280, 720))
        self.assertEqual(output_size({"width": 720, "height": 576, "sample_aspect_ratio": "16:15"}, {}), (768, 576))
        self.assertEqual(output_size(self.source, {}, {"width": 2560, "height": 1440}), (2560, 1440))

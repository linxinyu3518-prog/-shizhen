# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for runtime adapters whose metadata can be incomplete."""
import json
import unittest
from unittest.mock import MagicMock, patch

from framekeep.config import Settings
from framekeep.extractors import WebsiteExtractor
from framekeep.media import probe


class AdapterTests(unittest.TestCase):
    def test_generic_video_without_dimensions_is_probed_before_classifying_streams(self):
        module = MagicMock()
        module.YoutubeDL.return_value.__enter__.return_value.extract_info.return_value = {
            "title": "Example", "url": "https://example.org/media.mp4", "ext": "mp4", "format_id": "mp4"}
        metadata = {"has_video": True, "has_audio": True, "width": 1920, "height": 1080, "fps": 25,
                    "video_codec": "h264", "audio_codec": "aac", "duration": 12.5, "size": 4096}
        settings = Settings(yt_dlp=True, ffmpeg="ffmpeg", ffprobe="ffprobe")
        with patch("framekeep.extractors.validate_url", side_effect=lambda url, *_: url), \
             patch("framekeep.extractors.probe", return_value=metadata), \
             patch("framekeep.extractors.importlib.import_module", return_value=module):
            info = WebsiteExtractor(settings).analyze("https://example.org/video")
        self.assertEqual((info["has_video"], info["has_audio"]), (True, True))
        self.assertEqual((info["width"], info["height"], info["duration"]), (1920, 1080, 12.5))
        self.assertEqual(info["formats"][0]["id"], "mp4")

    def test_remote_probe_allows_http_proxy_but_not_local_file_protocol(self):
        result = MagicMock(returncode=0, stdout=json.dumps({"streams": [{"codec_type": "audio", "codec_name": "aac"}], "format": {}}))
        with patch("framekeep.media.subprocess.run", return_value=result) as run:
            probe("https://example.org/audio.m4a", Settings(ffprobe="ffprobe"), remote=True)
        args = run.call_args.args[0]
        protocols = args[args.index("-protocol_whitelist") + 1].split(",")
        self.assertIn("httpproxy", protocols)
        self.assertIn("https", protocols)
        self.assertNotIn("file", protocols)


if __name__ == "__main__":
    unittest.main()

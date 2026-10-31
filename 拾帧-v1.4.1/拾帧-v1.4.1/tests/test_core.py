# SPDX-License-Identifier: GPL-3.0-or-later
"""Public-input boundaries and real output media, using only generated fixtures.

Run with ``python -m unittest discover -s tests -v``. Encoder tests skip when
FFmpeg / FFprobe are unavailable; no test contacts an external website.
"""
import io
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from framekeep.config import NO_WINDOW, Settings
from framekeep.extractors import DirectExtractor, ExtractorService, VideoHTMLParser
from framekeep.media import build_encode_command, create_demo, encode, number, probe, rate
from framekeep.network import Cancelled, MediaError, ValidatedRedirect, download_file, validate_url


def dns_answer(address="93.184.216.34"):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]


class UrlValidationTests(unittest.TestCase):
    @patch("framekeep.network.socket.getaddrinfo", return_value=dns_answer())
    def test_public_http_and_https_links_keep_query_parameters(self, resolver):
        for value in ("https://media.example/video.mp4?token=a%2Bb&quality=1080",
                      "http://media.example:8080/watch?id=123"):
            with self.subTest(value=value):
                self.assertEqual(validate_url(value), value)
        self.assertEqual(resolver.call_count, 2)

    def test_malformed_unsafe_and_credential_urls_are_rejected(self):
        values = (None, "", "   ", "https://", "file:///C:/private.mp4", "ftp://media.example/a",
                  "https://alice:secret@media.example/a", "https://@media.example/a",
                  "https://:secret@media.example/a", "https://media.example:99999/a",
                  "https://[broken/a", "https://media.example/a\r\nHost: localhost",
                  "https://media.example/a\x00b", "https://media.example/a\x7fb",
                  "https://media.example\\@localhost/a", "https://media.example/" + "a" * 8192)
        with patch("framekeep.network.socket.getaddrinfo", return_value=dns_answer()):
            for value in values:
                with self.subTest(value=value):
                    with self.assertRaises(MediaError):
                        validate_url(value)

    def test_all_resolved_addresses_must_be_public(self):
        for address in ("127.0.0.1", "10.0.0.9", "172.16.0.1", "192.168.1.1",
                        "169.254.169.254", "0.0.0.0", "::1", "fe80::1", "fc00::1",
                        "::ffff:127.0.0.1"):
            with self.subTest(address=address), patch(
                "framekeep.network.socket.getaddrinfo", return_value=dns_answer(address)
            ):
                with self.assertRaises(MediaError):
                    validate_url("https://media.example/video.mp4")
        with patch("framekeep.network.socket.getaddrinfo", return_value=dns_answer() + dns_answer("10.0.0.1")):
            with self.assertRaises(MediaError):
                validate_url("https://media.example/video.mp4")

    def test_failed_or_empty_dns_is_a_user_facing_error(self):
        with patch("framekeep.network.socket.getaddrinfo", side_effect=socket.gaierror("not found")):
            with self.assertRaises(MediaError):
                validate_url("https://unknown.example/a")
        with patch("framekeep.network.socket.getaddrinfo", return_value=[]):
            with self.assertRaises(MediaError):
                validate_url("https://unknown.example/a")

    def test_demo_exception_is_for_one_exact_url(self):
        demo = "http://127.0.0.1:9876/demo/sample.mp4"
        with patch("framekeep.network.socket.getaddrinfo", return_value=dns_answer("127.0.0.1")) as resolver:
            self.assertEqual(validate_url(demo, demo), demo)
            resolver.assert_not_called()
            for value in (demo + "?extra=1", demo.replace("sample.mp4", "private.mp4"),
                          demo.replace("9876", "9877")):
                with self.subTest(value=value), self.assertRaises(MediaError):
                    validate_url(value, demo)

    def test_redirect_to_private_network_is_rejected(self):
        with patch("framekeep.network.socket.getaddrinfo", return_value=dns_answer("127.0.0.1")):
            with self.assertRaises(MediaError):
                ValidatedRedirect().redirect_request(None, None, 302, "Found", {}, "http://localhost/secret")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.destination = Path(self.temporary.name) / "video.mp4"
        self.settings = Settings(max_download_bytes=8)

    def response(self, payload, declared=None):
        response = io.BytesIO(payload)
        response.headers = {} if declared is None else {"Content-Length": str(declared)}
        return response

    def fetch(self, response, cancel=None):
        with patch("framekeep.network.open_url", return_value=response):
            return download_file("https://media.example/a", self.destination, self.settings,
                                 lambda *_: None, cancel or threading.Event())

    def test_download_preserves_bytes(self):
        self.assertEqual(self.fetch(self.response(b"fixture", 7)), self.destination)
        self.assertEqual(self.destination.read_bytes(), b"fixture")

    def test_size_limit_applies_with_and_without_content_length(self):
        for declared in (None, 9):
            with self.subTest(declared=declared), self.assertRaises(MediaError):
                self.fetch(self.response(b"123456789", declared))

    def test_empty_and_truncated_downloads_fail(self):
        for payload, declared in ((b"", None), (b"short", 8)):
            with self.subTest(payload=payload), self.assertRaises(MediaError):
                self.fetch(self.response(payload, declared))

    def test_cancelled_download_reports_cancellation(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            self.fetch(self.response(b"fixture", 7), cancel)


class EncoderArgumentsTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(ffmpeg="ffmpeg")

    def test_paths_with_shell_characters_stay_single_arguments(self):
        source = Path("input ; & $(echo bad) 视频.mp4")
        target = Path("output ; & $(echo bad) 视频.mp4")
        command = build_encode_command(source, target, {"kind": "video", "container": "mp4"}, self.settings)
        self.assertIsInstance(command, list)
        self.assertEqual(command[command.index("-i") + 1], str(source))
        self.assertEqual(command[-1], str(target))
        self.assertEqual(command[command.index("-protocol_whitelist") + 1], "file,pipe")

    def test_unsupported_containers_and_filter_inputs_are_rejected(self):
        bad_options = ({"kind": "audio", "container": "mp4"},
                       {"kind": "video", "container": "mkv"},
                       {"kind": "video", "container": "mp4", "height": "720;movie=secret"},
                       {"kind": "video", "container": "mp4", "height": 999},
                       {"kind": "video", "container": "mp4", "height": -1})
        for options in bad_options:
            with self.subTest(options=options), self.assertRaises(MediaError):
                build_encode_command(Path("in.mp4"), Path("out.mp4"), options, self.settings)

    def test_metadata_numbers_tolerate_missing_and_non_finite_values(self):
        for value in (None, "N/A", "NaN", "inf", "-inf"):
            with self.subTest(value=value):
                self.assertIsNone(number(value))
        self.assertEqual(number("123.5"), 123.5)
        self.assertEqual(rate("30000/1001"), 29.97)
        self.assertEqual(rate("25"), 25)
        self.assertIsNone(rate("0/0"))


class NativeExtractionTests(unittest.TestCase):
    def test_declared_html_media_and_structured_data_are_read_without_executing_scripts(self):
        parser = VideoHTMLParser()
        parser.feed('''<title>Original video</title>
          <video src="/film.mp4"><source src="/film-720.mp4?x=1&amp;y=2"></video>
          <meta property="og:video:secure_url" content="https://cdn.example/share.mp4">
          <script>window.fetch("https://should-not-be-read.example/private.mp4")</script>
          <script type="application/ld+json">{"@graph":[{"@type":"VideoObject",
          "name":"Film", "contentUrl":"https://cdn.example/structured.mp4"}]}</script>''')
        self.assertEqual(parser.title, "Original video")
        self.assertEqual(parser.urls, ["/film.mp4", "/film-720.mp4?x=1&y=2",
                                      "https://cdn.example/share.mp4", "https://cdn.example/structured.mp4"])

    def test_malformed_structured_data_does_not_discard_valid_video_tag(self):
        parser = VideoHTMLParser()
        parser.feed('<video src="/valid.mp4"></video><script type="application/ld+json">not json</script>')
        self.assertEqual(parser.urls, ["/valid.mp4"])

    def test_best_source_is_highest_resolution_and_private_fields_are_hidden(self):
        common = {"has_video": True, "has_audio": True, "fps": 30, "format_name": "mp4", "size": 100}
        record = DirectExtractor(Settings())._record(
            "https://media.example/watch", "Example", [
                ("https://cdn.example/small.mp4", {**common, "width": 640, "height": 360}),
                ("https://cdn.example/big.mp4", {**common, "width": 1920, "height": 1080}),
            ], "https://media.example/watch")
        self.assertEqual(record["formats"][0]["height"], 1080)
        self.assertEqual(record["_sources"]["0"]["url"], "https://cdn.example/big.mp4")
        visible = ExtractorService.public(record)
        self.assertEqual(visible["title"], "Example")
        self.assertFalse(any(key.startswith("_") for key in visible))


class JobOptionsTests(unittest.TestCase):
    def setUp(self):
        from framekeep.jobs import validate_options
        self.validate = validate_options
        self.media = {"id": "fixture-media", "has_video": True, "has_audio": True,
                      "formats": [{"id": "0", "height": 360}, {"id": "137", "height": 1080}]}
        self.options = {"media_id": "fixture-media", "kind": "video", "container": "mp4",
                        "height": None, "format_id": "best"}

    def test_supported_video_profiles_and_analyzed_source_ids(self):
        for container in ("mp4", "avi"):
            for height in (None, 360, 480, 720, 1080, 1440, 2160):
                with self.subTest(container=container, height=height):
                    value = self.validate({**self.options, "container": container,
                                           "height": height, "format_id": "137"}, self.media)
                    self.assertEqual(value["container"], container)
                    self.assertEqual(value["height"], height)
                    self.assertEqual(value["format_id"], "137")

    def test_audio_profiles(self):
        for container in ("m4a", "aac"):
            with self.subTest(container=container):
                value = self.validate({**self.options, "kind": "audio", "container": container}, self.media)
                self.assertEqual(value["kind"], "audio")
                self.assertEqual(value["container"], container)

    def test_unknown_modes_containers_heights_and_source_ids_are_rejected(self):
        changes = ({"kind": "shell"}, {"container": "m4a"}, {"container": "mkv"},
                   {"height": "720"}, {"height": 999}, {"height": -1},
                   {"height": True}, {"format_id": "missing"},
                   {"format_id": "137+best[height>0]"}, {"format_id": "../../private"},
                   {"kind": "audio", "container": "avi"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(MediaError):
                self.validate({**self.options, **change}, self.media)

    def test_missing_media_tracks_are_reported_before_starting_a_job(self):
        with self.assertRaises(MediaError):
            self.validate(self.options, {**self.media, "has_video": False})
        with self.assertRaises(MediaError):
            self.validate({**self.options, "kind": "audio", "container": "m4a"},
                          {**self.media, "has_audio": False})

    def test_non_object_options_are_rejected(self):
        for value in (None, [], "video", 123):
            with self.subTest(value=value), self.assertRaises(MediaError):
                self.validate(value, self.media)


class MediaIntegrationTests(unittest.TestCase):
    """Inspect actual encoded streams, not merely the requested CLI options."""
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        with patch.dict("os.environ", {"MEDIA_DATA_DIR": str(cls.root)}):
            cls.settings = Settings.discover(data_dir=cls.root)
        if not all((cls.settings.ffmpeg, cls.settings.ffprobe, cls.settings.h264, cls.settings.aac)):
            raise unittest.SkipTest("FFmpeg with libx264 / AAC and FFprobe are needed for media integration tests")
        cls.source = create_demo(cls.settings)
        if cls.source is None:
            raise AssertionError("The configured encoder could not generate the original test fixture")
        cls.metadata = probe(cls.source, cls.settings)

    def convert(self, container, kind="video", height=None):
        destination = self.root / ("converted_" + kind + "." + container)
        options = {"kind": kind, "container": container, "height": height}
        progress = []
        encode(self.source, destination, options, self.settings, self.metadata,
               lambda amount, message: progress.append(amount), threading.Event())
        self.assertTrue(destination.is_file())
        self.assertGreater(destination.stat().st_size, 0)
        return probe(destination, self.settings)

    def test_mp4_and_avi_are_h264_with_compatible_pixels_and_aac(self):
        for container in ("mp4", "avi"):
            with self.subTest(container=container):
                result = self.convert(container, height=720)
                self.assertEqual(result["video_codec"], "h264")
                self.assertEqual(result["pixel_format"], "yuv420p")
                self.assertEqual(result["audio_codec"], "aac")
                self.assertEqual(result["sample_rate"], 48000)
                self.assertEqual((result["width"], result["height"]), (640, 360), "Output must not upscale the source")
                self.assertAlmostEqual(result["duration"], self.metadata["duration"], delta=0.2)

    def test_360p_export_downscales_hd_source_in_both_containers(self):
        source = self.root / "hd-for-360p.mp4"
        subprocess.run([self.settings.ffmpeg, "-v", "error", "-y", "-i", str(self.source),
                        "-t", "0.5", "-vf", "scale=1280:720", "-c:v", "libx264",
                        "-preset", "ultrafast", "-c:a", "copy", str(source)],
                       capture_output=True, check=True, timeout=30, creationflags=NO_WINDOW)
        metadata = probe(source, self.settings)
        for container in ("mp4", "avi"):
            with self.subTest(container=container):
                output = self.root / f"hd-to-360p.{container}"
                encode(source, output, {"kind": "video", "container": container, "height": 360},
                       self.settings, metadata, lambda *_: None, threading.Event())
                measured = probe(output, self.settings)
                self.assertEqual((measured["width"], measured["height"]), (640, 360))
                self.assertEqual((measured["video_codec"], measured["audio_codec"]), ("h264", "aac"))
                subprocess.run([self.settings.ffmpeg, "-v", "error", "-i", str(output),
                                "-f", "null", "-"], capture_output=True, check=True,
                               timeout=30, creationflags=NO_WINDOW)

    def test_m4a_and_aac_contain_only_aac_audio_at_48000_hz(self):
        for container in ("m4a", "aac"):
            with self.subTest(container=container):
                result = self.convert(container, "audio")
                self.assertFalse(result["has_video"])
                self.assertTrue(result["has_audio"])
                self.assertEqual(result["audio_codec"], "aac")
                self.assertEqual(result["sample_rate"], 48000)
                # AAC uses a target rate: observed average is not exactly 256000.
                self.assertGreater(result["bit_rate"], 210000)
                self.assertLess(result["bit_rate"], 285000)
                # Raw ADTS has no duration index; FFprobe estimates from bitrate.
                # Count the decoded stereo PCM samples to check its real duration.
                destination = self.root / ("converted_audio." + container)
                decoded = subprocess.run(
                    [self.settings.ffmpeg, "-v", "error", "-i", str(destination),
                     "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"],
                    capture_output=True, check=True, timeout=20, creationflags=NO_WINDOW)
                actual_duration = len(decoded.stdout) / (48000 * 2 * 2)
                self.assertAlmostEqual(actual_duration, self.metadata["duration"], delta=0.1)

    def test_extracting_audio_from_silent_video_fails_cleanly(self):
        source = self.root / "without_audio.mp4"
        subprocess.run([self.settings.ffmpeg, "-v", "error", "-y", "-i", str(self.source),
                        "-map", "0:v:0", "-c", "copy", "-an", str(source)],
                       capture_output=True, check=True, timeout=20, creationflags=NO_WINDOW)
        metadata = probe(source, self.settings)
        self.assertFalse(metadata["has_audio"])
        with self.assertRaises(MediaError):
            encode(source, self.root / "absent.m4a", {"kind": "audio", "container": "m4a"},
                   self.settings, metadata, lambda *_: None, threading.Event())

    def test_cancellation_stops_encoding(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            encode(self.source, self.root / "cancelled.mp4", {"kind": "video", "container": "mp4"},
                   self.settings, self.metadata, lambda *_: None, cancel)

    def test_non_square_pixel_video_preserves_display_shape(self):
        source = self.root / "anamorphic.mp4"
        subprocess.run([self.settings.ffmpeg, "-v", "error", "-y", "-i", str(self.source),
                        "-t", "1", "-vf", "setsar=4/3", "-c:v", "libx264", "-an", str(source)],
                       capture_output=True, check=True, timeout=20, creationflags=NO_WINDOW)
        before = probe(source, self.settings)
        destination = self.root / "preserved-shape.mp4"
        encode(source, destination, {"kind": "video", "container": "mp4", "height": 720},
               self.settings, before, lambda *_: None, threading.Event())
        after = probe(destination, self.settings)
        self.assertEqual(after["display_aspect_ratio"], before["display_aspect_ratio"])
        self.assertEqual((after["width"], after["height"]), (before["width"], before["height"]))


if __name__ == "__main__":
    unittest.main()

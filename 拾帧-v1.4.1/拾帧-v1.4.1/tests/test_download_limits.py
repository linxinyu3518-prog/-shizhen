"""Large-file limits tested with metadata and tiny files, without large downloads."""
import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from framekeep.config import Settings
from framekeep.extractors import WebsiteExtractor
from framekeep.network import MediaError, download_file


GIB = 1024 ** 3


class DirectLargeFileLimitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.destination = Path(self.temporary.name) / "source.mp4"
        self.settings = Settings()

    def fetch_declared_size(self, declared_size):
        response = io.BytesIO(b"short fixture")
        response.headers = {"Content-Length": str(declared_size)}
        with patch("framekeep.network.open_url", return_value=response):
            return download_file("https://media.example/source.mp4", self.destination,
                                 self.settings, lambda *_: None, threading.Event())

    def test_default_limit_is_ten_gib(self):
        self.assertEqual(self.settings.max_download_bytes, 10 * GIB)

    def test_five_gib_and_exact_limit_pass_size_check(self):
        # The deliberately short body then fails completeness, proving that the
        # large Content-Length was accepted without transferring GiB of data.
        for size in (5 * GIB, 10 * GIB):
            with self.subTest(size=size), self.assertRaisesRegex(MediaError, "下载不完整"):
                self.fetch_declared_size(size)

    def test_one_byte_above_ten_gib_is_rejected_before_body_read(self):
        with self.assertRaisesRegex(MediaError, "超过"):
            self.fetch_declared_size(10 * GIB + 1)
        self.assertEqual(self.destination.stat().st_size, 0)


class WebsiteMergeLimitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.settings = Settings(max_download_bytes=8, yt_dlp=True)
        self.extractor = WebsiteExtractor(self.settings)
        self.media = {"url": "https://media.example/watch", "title": "fixture"}
        self.options = {"kind": "video", "format_id": "best"}

    def download_with(self, operation):
        testcase = self

        class FakeDownloader:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def extract_info(self, url, download):
                testcase.assertEqual(url, testcase.media["url"])
                testcase.assertTrue(download)
                testcase.assertEqual(self.options["max_filesize"], 8)
                operation(self.options)

        package = SimpleNamespace(YoutubeDL=FakeDownloader)
        with patch("framekeep.extractors.importlib.import_module", return_value=package):
            return self.extractor.download(self.media, self.options, self.folder,
                                           lambda *_: None, threading.Event())

    @staticmethod
    def fire_postprocessor(options, event):
        for hook in options["postprocessor_hooks"]:
            hook(event)

    def test_merge_completion_does_not_count_final_and_tracks_twice(self):
        video = self.folder / "source.fvideo.mp4"
        audio = self.folder / "source.faudio.m4a"
        final = self.folder / "source.mkv"

        def merge(options):
            video.write_bytes(b"vvv")
            audio.write_bytes(b"aaa")
            info = {"filepath": str(final), "__files_to_merge": [str(video), str(audio)]}
            self.fire_postprocessor(options, {"status": "started", "info_dict": info})
            final.write_bytes(b"vvvaaa")
            self.fire_postprocessor(options, {"status": "finished", "info_dict": info})
            # yt-dlp removes input tracks only after the finished hook returns.
            video.unlink()
            audio.unlink()

        self.assertEqual(self.download_with(merge), final)
        self.assertEqual(final.read_bytes(), b"vvvaaa")

    def test_real_downloaded_components_above_limit_are_rejected(self):
        def merge(options):
            video = self.folder / "source.fvideo.mp4"
            audio = self.folder / "source.faudio.m4a"
            video.write_bytes(b"vvvvvv")
            audio.write_bytes(b"aaa")
            info = {"filepath": str(self.folder / "source.mkv"),
                    "__files_to_merge": [str(video), str(audio)]}
            self.fire_postprocessor(options, {"status": "started", "info_dict": info})

        with self.assertRaises(MediaError):
            self.download_with(merge)

    def test_final_source_above_limit_is_rejected_even_without_hooks(self):
        def download(options):
            (self.folder / "source.mkv").write_bytes(b"123456789")

        with self.assertRaises(MediaError):
            self.download_with(download)

    def test_outside_merge_filepath_cannot_hide_real_download_size(self):
        with tempfile.TemporaryDirectory() as outside:
            outside_final = Path(outside) / "source.mkv"
            outside_final.write_bytes(b"external")

            def merge(options):
                video = self.folder / "source.fvideo.mp4"
                audio = self.folder / "source.faudio.m4a"
                video.write_bytes(b"vvvvvv")
                audio.write_bytes(b"aaa")
                self.fire_postprocessor(options, {
                    "status": "finished", "info_dict": {
                        "filepath": str(outside_final),
                        "__files_to_merge": [str(video), str(audio)]}})

            with self.assertRaises(MediaError):
                self.download_with(merge)


if __name__ == "__main__":
    unittest.main()

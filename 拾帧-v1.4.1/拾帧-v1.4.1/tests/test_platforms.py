# SPDX-License-Identifier: GPL-3.0-or-later
"""URL normalization regressions; these tests never contact a website."""
import unittest
from unittest.mock import patch

from framekeep.network import MediaError, validate_url
from framekeep.platforms import extract_share_url, normalize_media_url, platform_for_url


class PlatformUrlTests(unittest.TestCase):
    video_id = "7670847854776159515"
    canonical = f"https://www.douyin.com/video/{video_id}"

    def test_user_reported_liked_video_modal_link(self):
        link = f"https://www.douyin.com/user/self?from_tab_name=main&modal_id={self.video_id}&showTab=like"
        self.assertEqual(normalize_media_url(link), self.canonical)

    def test_presentation_escaped_query_parameters(self):
        for separator in ("&amp;", "\\&"):
            link = f"https://www.douyin.com/user/self?from_tab_name=main{separator}modal_id={self.video_id}{separator}showTab=like"
            with self.subTest(separator=separator):
                self.assertEqual(normalize_media_url(link), self.canonical)

    def test_standard_and_legacy_video_pages(self):
        for link in (self.canonical, self.canonical + "/?from=share",
                     f"http://douyin.com/video/{self.video_id}",
                     f"https://www.iesdouyin.com/share/video/{self.video_id}/?region=CN",
                     f"https://iesdouyin.com/share/video/{self.video_id}",
                     f"https://www.douyin.com/?modal_id={self.video_id}"):
            with self.subTest(link=link):
                self.assertEqual(normalize_media_url(link), self.canonical)

    def test_share_text_with_chinese_punctuation_and_line_break(self):
        short = "https://v.douyin.com/AbCdEF/"
        for text in (f"3.14 复制打开抖音，看看这个作品 {short} 复制此链接，打开抖音搜索！",
                     f"看看视频：{short}，复制打开抖音", f"分享视频\n「{short}」\n打开抖音",
                     f"[视频]({short})"):
            with self.subTest(text=text):
                self.assertEqual(normalize_media_url(text), short)

    def test_short_links_remain_for_redirect_resolution(self):
        link = "https://v.douyin.com/AbCdEF/?from=share"
        self.assertEqual(normalize_media_url(link), link)
        self.assertEqual(platform_for_url(link), "douyin")

    def test_unrelated_signed_queries_are_unchanged(self):
        for link in ("https://cdn.example/video.mp4?token=a%2Bb&part=2&token=second#clip",
                     "https://media.example/audio.m4a?sig=abc!.,;()&expires=123",
                     "https://media.example/video.mp4?title=你好，世界&sig=abc",
                     "https://media.example/video?redirect=https://another.example/asset&sig=x%26y",
                     "https://v26.douyinvod.com/path/video.mp4?token=x&modal_id=12"):
            with self.subTest(link=link):
                self.assertEqual(normalize_media_url(link), link)
                self.assertIsNone(platform_for_url(link))

    def test_only_exact_platform_hosts_are_recognized(self):
        for host in ("douyin.com.evil.example", "evildouyin.com", "www.douyin.com.evil.example"):
            link = f"https://{host}/user/self?modal_id={self.video_id}"
            with self.subTest(host=host):
                self.assertEqual(normalize_media_url(link), link)
                self.assertIsNone(platform_for_url(link))

    def test_profiles_and_live_pages_require_a_single_video(self):
        for link in ("https://www.douyin.com/user/self?showTab=like",
                     "https://www.douyin.com/user/abc", "https://www.douyin.com/user",
                     "https://live.douyin.com/12345", "https://www.douyin.com/live/123"):
            with self.subTest(link=link), self.assertRaisesRegex(MediaError, "具体视频"):
                normalize_media_url(link)

    def test_invalid_or_ambiguous_video_ids_are_rejected(self):
        for identifier in ("", "abc", "123abc", "-123", "%2F123", "１２３", "1" * 33):
            link = "https://www.douyin.com/user/self?modal_id=" + identifier
            with self.subTest(identifier=identifier), self.assertRaisesRegex(MediaError, "视频编号"):
                normalize_media_url(link)
        with self.assertRaisesRegex(MediaError, "多个视频编号"):
            normalize_media_url("https://www.douyin.com/user/self?modal_id=123&modal_id=456")
        with self.assertRaisesRegex(MediaError, "视频编号"):
            normalize_media_url("https://www.douyin.com/video/abc")

    def test_multiple_share_urls_require_user_to_select_one(self):
        for text in ("https://v.douyin.com/one/ https://v.douyin.com/two/",
                     "视频 https://v.douyin.com/one/\n视频 https://example.org/two"):
            with self.subTest(text=text), self.assertRaisesRegex(MediaError, "多个网址"):
                extract_share_url(text)

    def test_invalid_inputs_are_actionable_errors(self):
        for value in (None, "", "  ", "只有文字", "ftp://example.org/video", "a" * 8193,
                      "https://example.org/\x00video", "https://example.org/\x7fvideo",
                      "https://example.org/a\r\nHost: localhost", "https://[broken/video"):
            with self.subTest(value=value), self.assertRaises(MediaError):
                normalize_media_url(value)

    def test_normalization_does_not_strip_unsafe_authority_or_backslashes(self):
        for link in (f"https://user:secret@www.douyin.com/video/{self.video_id}",
                     f"https://@www.douyin.com/video/{self.video_id}",
                     f"https://www.douyin.com/video/{self.video_id}?evil=\\file",
                     "https://www.douyin.com\\@localhost/video/123"):
            with self.subTest(link=link), self.assertRaises(MediaError):
                normalize_media_url(link)

    def test_private_network_validation_is_still_required(self):
        link = "http://127.0.0.1/private.mp4"
        self.assertEqual(normalize_media_url(link), link)
        with patch("framekeep.network.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("127.0.0.1", 80))
        ]), self.assertRaises(MediaError):
            validate_url(normalize_media_url(link))

    def test_nonstandard_port_is_not_rewritten(self):
        link = f"https://www.douyin.com:8443/video/{self.video_id}?token=sig"
        self.assertEqual(normalize_media_url(link), link)


if __name__ == "__main__":
    unittest.main()

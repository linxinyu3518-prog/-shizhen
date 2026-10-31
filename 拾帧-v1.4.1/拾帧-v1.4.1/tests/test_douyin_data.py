# SPDX-License-Identifier: GPL-3.0-or-later
"""Original synthetic parser fixtures: no account data or remote requests."""
import json
import unittest
from urllib.parse import quote

from framekeep.douyin_data import MAX_HTML_BYTES, parse_embedded_page, parse_video_detail


class DouyinDataTests(unittest.TestCase):
    target = "7670847854776159515"
    source = "https://v.example.net/video.mp4?token=a%2Bb"

    def detail(self, **video):
        return {"aweme_id": self.target, "desc": "测试视频 + 原画", "video": {
            "duration": 12450, "width": 1080, "height": 1920,
            "play_addr": {"url_list": [self.source]}, **video}}

    def test_snake_case_api_metadata_and_url_preservation(self):
        result = parse_video_detail({"aweme_detail": self.detail(fps=30)}, self.target)
        self.assertEqual(result["title"], "测试视频 + 原画")
        self.assertEqual(result["duration"], 12.45)
        self.assertEqual(result["width"], 1080)
        self.assertEqual(result["height"], 1920)
        self.assertEqual(result["streams"][0]["url"], self.source)
        self.assertIsNone(result["streams"][0]["video_codec"])
        self.assertEqual(result["streams"][0]["fps"], 30)

    def test_bitrate_variants_and_backup_url_deduplication(self):
        data = self.detail(bit_rate=[{"bit_rate": 3500000, "is_h265": 1,
            "play_addr": {"url_list": [self.source, "https://v.example.net/high.mp4"], "width": 2160, "height": 3840}}])
        result = parse_video_detail(data, self.target)
        self.assertEqual(len(result["streams"]), 2)
        high = result["streams"][1]
        self.assertEqual((high["width"], high["height"], high["bit_rate"], high["video_codec"]),
                         (2160, 3840, 3500000, "hevc"))

    def test_camel_case_video_detail_with_playaddr_objects(self):
        data = {"videoDetail": {"awemeId": self.target, "desc": "Camel", "video": {
            "duration": 3000, "playAddr": [{"src": self.source, "width": 720, "height": 1280}],
            "bitRateList": [{"bitRate": 1500000, "codecType": "H.264", "playAddr": {
                "urlList": ["https://v.example.net/high.mp4"], "width": 1080, "height": 1920}}]}}}
        result = parse_video_detail(data, self.target)
        self.assertEqual(result["duration"], 3)
        self.assertEqual(result["streams"][0]["width"], 720)
        self.assertEqual(result["streams"][1]["video_codec"], "h264")
        self.assertEqual(result["streams"][1]["bit_rate"], 1500000)

    def test_camel_case_playaddr_url_strings(self):
        data = {"awemeId": self.target, "video": {"playAddr": [self.source]}}
        self.assertEqual(parse_video_detail(data, self.target)["streams"][0]["url"], self.source)

    def test_exact_target_not_recommendations_or_background_video(self):
        wrong = self.detail()
        wrong["aweme_id"] = "12345"
        wrong["video"]["play_addr"]["url_list"] = ["https://v.example.net/wrong.mp4"]
        data = {"recommendations": [wrong], "static_config": {"video": {"codec_type": "h265"}},
                "aweme_detail": self.detail()}
        result = parse_video_detail(data, self.target)
        self.assertEqual([stream["url"] for stream in result["streams"]], [self.source])
        self.assertIsNone(result["streams"][0]["video_codec"])

    def test_wrong_id_and_unbound_video_objects_are_ignored(self):
        detail = self.detail()
        detail["aweme_id"] = "12345"
        for data in (detail, {"aweme_id": self.target, "nested": {"video": self.detail()["video"]}},
                     {"video": self.detail()["video"]}, {"id": self.target, "src": self.source}):
            with self.subTest(data=data):
                self.assertIsNone(parse_video_detail(data, self.target))

    def test_conflicting_explicit_ids_are_ignored(self):
        data = {**self.detail(), "awemeId": "999"}
        self.assertIsNone(parse_video_detail(data, self.target))

    def test_no_streams_in_login_or_unavailable_page(self):
        data = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": []}, "needLogin": True}},
                "config": {"video": {"play_addr": {"url_list": [self.source]}}}}
        html = "<script>window._ROUTER_DATA = " + json.dumps(data) + ";</script>"
        self.assertIsNone(parse_embedded_page(html, self.target))

    def test_percent_encoded_render_data_preserves_plus_and_unicode(self):
        payload = quote(json.dumps({"aweme": self.detail()}, ensure_ascii=False))
        result = parse_embedded_page(f'<script id="RENDER_DATA" type="application/json">{payload}</script>', self.target)
        self.assertEqual(result["title"], "测试视频 + 原画")
        self.assertEqual(result["streams"][0]["url"], self.source)

    def test_raw_router_and_initial_state_assignments(self):
        for name in ("window._ROUTER_DATA", "window.__INITIAL_STATE__", "__NEXT_DATA__"):
            html = f"<script>{name} = " + json.dumps({"data": self.detail()}) + "; window.after = true;</script>"
            with self.subTest(name=name):
                self.assertEqual(parse_embedded_page(html, self.target)["streams"][0]["url"], self.source)

    def test_pace_push_with_nested_serialized_json(self):
        packed = json.dumps([1, json.dumps({"detail": self.detail()})])
        html = "<script>self.__pace_f.push(" + packed + ");</script>"
        self.assertEqual(parse_embedded_page(html, self.target)["streams"][0]["url"], self.source)

    def test_pace_push_javascript_expressions_are_not_accepted(self):
        for expression in ("getVideo()", json.dumps([self.detail()]) + ".map(run)"):
            with self.subTest(expression=expression):
                self.assertIsNone(parse_embedded_page(f"<script>self.__pace_f.push({expression});</script>", self.target))

    def test_application_json_and_malformed_script_recovery(self):
        html = '<script type="application/json">{broken</script><script type="application/json">'
        html += json.dumps(self.detail()) + "</script>"
        self.assertEqual(parse_embedded_page(html, self.target)["duration"], 12.45)

    def test_unrelated_scripts_and_html_video_elements_are_ignored(self):
        html = '<script>const background = ' + json.dumps(self.detail()) + ';</script>'
        html += f'<video src="{self.source}"></video><meta property="og:video" content="{self.source}">'
        self.assertIsNone(parse_embedded_page(html, self.target))

    def test_json_ld_requires_exact_target_identity(self):
        base = {"@type": "VideoObject", "name": "Structured video", "contentUrl": self.source, "duration": "PT1M2.5S"}
        self.assertIsNone(parse_video_detail(base, self.target))
        for identity in ({"identifier": self.target}, {"identifier": {"value": self.target}},
                         {"url": f"https://www.douyin.com/video/{self.target}"},
                         {"@id": f"https://www.douyin.com/user/self?modal_id={self.target}"}):
            with self.subTest(identity=identity):
                result = parse_video_detail({**base, **identity}, self.target)
                self.assertEqual(result["duration"], 62.5)
                self.assertEqual(result["streams"][0]["url"], self.source)

    def test_json_ld_lookalike_hosts_and_substring_ids_do_not_match(self):
        for url in (f"https://douyin.com.evil.example/video/{self.target}",
                    f"https://www.douyin.com/video/{self.target}1", f"https://evil.example/?modal_id={self.target}"):
            data = {"@type": "VideoObject", "url": url, "contentUrl": self.source}
            with self.subTest(url=url):
                self.assertIsNone(parse_video_detail(data, self.target))

    def test_drm_target_never_exposes_streams(self):
        for marker in ({"is_drm": True}, {"drmType": 1}, {"drm_info": {"license": "https://example.org"}}):
            with self.subTest(marker=marker):
                result = parse_video_detail(self.detail(**marker), self.target)
                self.assertTrue(result["has_drm"])
                self.assertEqual(result["streams"], [])

    def test_false_drm_flags_do_not_block_playable_video(self):
        for value in (False, 0, "0", "false", "none", None, {}):
            with self.subTest(value=value):
                self.assertFalse(parse_video_detail(self.detail(is_drm=value), self.target)["has_drm"])

    def test_unrelated_drm_recommendation_does_not_block_target(self):
        other = self.detail(is_drm=True)
        other["aweme_id"] = "999"
        result = parse_video_detail([self.detail(), other], self.target)
        self.assertFalse(result["has_drm"])

    def test_unsafe_url_schemes_credentials_and_controls_are_rejected(self):
        for url in ("file:///C:/secret", "blob:https://www.douyin.com/id", "data:video/mp4;base64,abc", "javascript:alert(1)",
                    "https://user:password@example.org/a.mp4", "https://example.org/a\r\nCookie:x", "https://[broken/a.mp4", "//example.org/a.mp4"):
            with self.subTest(url=url):
                data = self.detail(play_addr={"url_list": [url]})
                self.assertIsNone(parse_video_detail(data, self.target))

    def test_unknown_schema_not_download_or_cover_addresses(self):
        for field in ("cover", "origin_cover", "download_addr", "unrecognized"):
            data = self.detail(play_addr={}, **{field: {"url_list": [self.source]}})
            with self.subTest(field=field):
                self.assertIsNone(parse_video_detail(data, self.target))

    def test_limits_cyclic_data_deep_nesting_and_oversize_html(self):
        cyclic = {}
        cyclic["self"] = cyclic
        self.assertIsNone(parse_video_detail(cyclic, self.target))
        deep = self.detail()
        for _ in range(80):
            deep = {"nested": deep}
        self.assertIsNone(parse_video_detail(deep, self.target))
        self.assertIsNone(parse_embedded_page("x" * (MAX_HTML_BYTES + 1), self.target))
        self.assertIsNone(parse_embedded_page("视" * (MAX_HTML_BYTES // 2), self.target))

    def test_bad_numeric_metadata_does_not_escape_as_nan_or_raise(self):
        data = self.detail(duration="nan", width="Infinity", height=[], fps=-1,
                           bit_rate=[{"bit_rate": {}, "play_addr": {"url_list": [self.source]}}])
        result = parse_video_detail(data, self.target)
        for field in ("duration", "width", "height", "fps"):
            self.assertIsNone(result[field])


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: GPL-3.0-or-later
"""Read declared Douyin video data without evaluating website JavaScript.

Both public functions return None when the exact requested video has no usable
data. A result has title, duration (seconds), width, height, fps, streams and
has_drm. Stream bit_rate is bits/second; unknown codec metadata stays None.
DRM-marked target data returns an empty streams list and has_drm=True.
URL validation here is syntactic; the network layer must still validate DNS and
redirect destinations before making requests.
"""
import json
import math
import re
from html.parser import HTMLParser
from itertools import islice
from urllib.parse import parse_qs, unquote, urlsplit

MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_NODES = 20000
MAX_DEPTH = 48
MAX_STREAMS = 128
_ASSIGNMENT = re.compile(r"(?<![\w$])(?:window\.)?(?:_ROUTER_DATA|__INITIAL_STATE__|__NEXT_DATA__)\s*=\s*")
_PACE_PUSH = re.compile(r"(?<![\w$])self\.__pace_f\.push\s*\(\s*")
_DRM_KEYS = {"is_drm", "isDrm", "has_drm", "hasDrm", "drm", "drm_type", "drmType", "drm_info", "drmInfo"}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _first_number(*values):
    return next((number for value in values if (number := _number(value)) is not None), None)


def _dimension(*values):
    number = _first_number(*values)
    return int(number) if number is not None and number <= 65536 else None


def _safe_url(value):
    if not isinstance(value, str) or len(value) > 16384:
        return None
    value = value.strip()
    if not value or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() in ("http", "https") and parts.hostname and not parts.username and not parts.password:
            return value
    except ValueError:
        pass
    return None


def _json(value):
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return None


def _walk(data):
    """Iterative traversal bounded even for cyclic or unexpectedly large input."""
    stack = [(data, 0)]
    seen = {}
    string_bytes = 0
    for _ in range(MAX_NODES):
        if not stack:
            return
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        if isinstance(node, (dict, list)):
            if id(node) in seen:
                continue
            # Keep parsed containers alive so Python cannot recycle their IDs.
            seen[id(node)] = node
            yield node
            children = node.values() if isinstance(node, dict) else node
            capacity = MAX_NODES - len(stack)
            stack.extend((child, depth + 1) for child in islice(children, capacity))
        elif isinstance(node, str) and node.lstrip().startswith(("{", "[")):
            string_bytes += len(node)
            if string_bytes <= MAX_HTML_BYTES:
                parsed = _json(node)
                if isinstance(parsed, (dict, list)):
                    stack.append((parsed, depth + 1))


def _identifier(item, target):
    values = [item[key] for key in ("aweme_id", "awemeId") if key in item]
    if not values and "id" in item:
        values = [item["id"]]
    return bool(values) and all(not isinstance(value, bool) and str(value) == target for value in values)


def _has_drm(item):
    for node in _walk(item):
        if not isinstance(node, dict):
            continue
        for key in _DRM_KEYS.intersection(node):
            value = node[key]
            if isinstance(value, str):
                value = value.strip().lower() not in ("", "0", "false", "none", "null")
            if value:
                return True
    return False


def _codec(*values):
    aliases = {"h264": "h264", "h.264": "h264", "avc": "h264", "avc1": "h264",
               "h265": "hevc", "h.265": "hevc", "hevc": "hevc", "hvc1": "hevc",
               "av1": "av1", "av01": "av1", "vp9": "vp9", "vp09": "vp9", "aac": "aac"}
    for value in values:
        if isinstance(value, str) and value.lower() in aliases:
            return aliases[value.lower()]
    return None


def _addresses(value, inherited=None, depth=0):
    """Accept only play-address schemas, never arbitrary URLs in a video object."""
    if depth > 8:
        return
    inherited = inherited or {}
    if isinstance(value, str):
        if url := _safe_url(value):
            yield url, inherited
    elif isinstance(value, list):
        for child in value[:MAX_STREAMS]:
            yield from _addresses(child, inherited, depth + 1)
    elif isinstance(value, dict):
        metadata = {**inherited, **value}
        for key in ("url_list", "urlList", "src", "url"):
            if key in value:
                yield from _addresses(value[key], metadata, depth + 1)


def _native(item, target):
    video = item.get("video")
    if not isinstance(video, dict) or not _identifier(item, target):
        return None
    result = {"title": str(item.get("desc") or item.get("description") or item.get("title") or f"抖音视频 {target}")[:300],
              "duration": None, "width": _dimension(video.get("width")), "height": _dimension(video.get("height")),
              "fps": _first_number(video.get("fps")), "streams": [], "has_drm": False}
    duration = _first_number(video.get("duration"), item.get("duration"))
    if duration is not None:
        result["duration"] = duration / 1000
    # Restrict DRM inspection to this video's fields, not unrelated recommendations.
    drm_fields = {key: value for key, value in item.items() if key in _DRM_KEYS}
    if _has_drm({"video": video, **drm_fields}):
        result["has_drm"] = True
        return result
    variants = [video]
    for key in ("bit_rate", "bitRateList", "bit_rate_list"):
        if isinstance(video.get(key), list):
            variants.extend(value for value in video[key][:MAX_STREAMS] if isinstance(value, dict))
    seen = set()
    for variant in variants[:MAX_STREAMS]:
        for key, forced_codec in (("play_addr", None), ("playAddr", None), ("play_addr_h264", "h264"),
                                  ("playAddrH264", "h264"), ("play_addr_265", "hevc"), ("playAddrH265", "hevc")):
            for url, address in _addresses(variant.get(key)):
                if url in seen or len(result["streams"]) >= MAX_STREAMS:
                    continue
                seen.add(url)
                suffix = urlsplit(url).path.rsplit(".", 1)[-1].lower()
                codec = forced_codec or _codec(address.get("codec_type"), address.get("codecType"),
                    variant.get("codec_type"), variant.get("codecType"), video.get("codec_type"), video.get("codecType"))
                if codec is None and variant.get("is_h265") in (1, True, "1"):
                    codec = "hevc"
                result["streams"].append({"url": url,
                    "width": _dimension(address.get("width"), variant.get("width"), video.get("width")),
                    "height": _dimension(address.get("height"), variant.get("height"), video.get("height")),
                    "fps": _first_number(address.get("fps"), variant.get("fps"), video.get("fps")),
                    "video_codec": codec, "audio_codec": _codec(address.get("audio_codec"), video.get("audio_codec")),
                    "ext": suffix if suffix in ("mp4", "m4v", "webm", "m3u8", "mpd") else "mp4",
                    "bit_rate": _first_number(address.get("bit_rate"), address.get("bitRate"),
                        variant.get("bit_rate"), variant.get("bitRate"))})
    if result["streams"]:
        best = max(result["streams"], key=lambda stream: (stream["width"] or 0) * (stream["height"] or 0))
        for field in ("width", "height", "fps"):
            if result[field] is None:
                result[field] = best[field]
        return result
    return None


def _url_matches(value, target):
    if not (url := _safe_url(value)):
        return False
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in ("douyin.com", "www.douyin.com", "iesdouyin.com", "www.iesdouyin.com"):
        return False
    path_match = re.fullmatch(r"/(?:share/)?video/([0-9]+)/?", parts.path)
    if path_match:
        return path_match[1] == target
    return parse_qs(parts.query).get("modal_id") == [target]


def _json_ld(item, target):
    if item.get("@type") != "VideoObject":
        return None
    identifier = item.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value")
    matches = str(identifier) == target or any(_url_matches(item.get(key), target) for key in ("url", "@id", "embedUrl"))
    if not matches or not (url := _safe_url(item.get("contentUrl"))):
        return None
    duration = None
    if isinstance(item.get("duration"), str):
        match = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", item["duration"])
        if match and any(match.groups()):
            duration = sum(float(value or 0) * factor for value, factor in zip(match.groups(), (3600, 60, 1)))
    width, height = _dimension(item.get("width")), _dimension(item.get("height"))
    has_drm = _has_drm(item)
    return {"title": str(item.get("name") or f"抖音视频 {target}")[:300], "duration": duration,
            "width": width, "height": height, "fps": None, "has_drm": has_drm,
            "streams": [] if has_drm else [{"url": url, "width": width, "height": height, "fps": None,
                "video_codec": None, "audio_codec": None, "ext": "mp4", "bit_rate": None}]}


def parse_video_detail(data, video_id):
    """Parse API/router data for one exact video ID; unknown schemas are ignored."""
    target = str(video_id)
    if not re.fullmatch(r"[0-9]{1,32}", target):
        return None
    found = None
    seen_urls = set()
    for item in _walk(data):
        if not isinstance(item, dict):
            continue
        result = _native(item, target) or _json_ld(item, target)
        if result is None:
            continue
        if result["has_drm"]:
            return result
        if found is None:
            found = {**result, "streams": []}
        for stream in result["streams"]:
            if stream["url"] not in seen_urls and len(found["streams"]) < MAX_STREAMS:
                seen_urls.add(stream["url"])
                found["streams"].append(stream)
    return found


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.documents = []
        self.attrs = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.attrs, self.parts = dict(attrs), []

    def handle_data(self, data):
        if self.attrs is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag != "script" or self.attrs is None:
            return
        body = "".join(self.parts).strip()
        attrs, self.attrs, self.parts = self.attrs, None, []
        if attrs.get("id") == "RENDER_DATA":
            self.documents.append(_json(unquote(body)))
        elif attrs.get("type", "").lower() in ("application/json", "application/ld+json"):
            self.documents.append(_json(body))
        else:
            decoder = json.JSONDecoder()
            for pattern in (_ASSIGNMENT, _PACE_PUSH):
                for match in islice(pattern.finditer(body), 64):
                    try:
                        value, end = decoder.raw_decode(body, match.end())
                        if pattern is _PACE_PUSH and not body[end:].lstrip().startswith(")"):
                            continue
                        self.documents.append(value)
                    except (ValueError, RecursionError):
                        continue


def parse_embedded_page(html, video_id):
    """Read known JSON script forms, with a 5 MiB input ceiling and no JS eval."""
    if not isinstance(html, str) or len(html) > MAX_HTML_BYTES or len(html.encode("utf-8", errors="replace")) > MAX_HTML_BYTES:
        return None
    parser = _Scripts()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, RecursionError):
        return None
    return parse_video_detail(parser.documents, video_id)

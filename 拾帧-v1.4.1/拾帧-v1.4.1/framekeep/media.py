# SPDX-License-Identifier: GPL-3.0-or-later
"""FFprobe metadata and fixed, reviewable H.264 / AAC encoding profiles."""
import json
import math
import queue
import subprocess
import threading
import time
from pathlib import Path

from .config import NO_WINDOW
from .network import Cancelled, MediaError, USER_AGENT

VIDEO_QUALITY = {"balanced": (18, "medium"), "high": (16, "slow"), "maximum": (14, "slow")}
NVENC_QUALITY = {"balanced": (18, "p4"), "high": (16, "p5"), "maximum": (14, "p6")}


def number(value, default=None):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError):
        return default


def rate(value):
    try:
        numerator, denominator = str(value).split("/")
        return round(float(numerator) / float(denominator), 3)
    except (ValueError, ZeroDivisionError):
        return number(value)


def _packet_timing(source, settings):
    """Read timestamps, without decoding frames, when a container omits duration."""
    command = [settings.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "v:0",
               "-show_packets", "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", str(source)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45, creationflags=NO_WINDOW)
        if result.returncode:
            return {}
        start, last, tail, count = None, None, 0, 0
        for line in result.stdout.splitlines():
            fields = line.split(",")
            pts = number(fields[0])
            if pts is None:
                continue
            count += 1
            start = pts if start is None else min(start, pts)
            duration = number(fields[1], 0) if len(fields) > 1 else 0
            if last is None or pts > last:
                last, tail = pts, duration
        if start is not None and last is not None:
            return {"start": start, "last": last, "tail": tail, "count": count}
    except (OSError, subprocess.SubprocessError):
        pass
    return {}


def probe(source, settings, remote=False, referer=None, user_agent=USER_AGENT):
    if not settings.ffprobe:
        raise MediaError("未找到 FFprobe，请先完成运行环境配置。")
    command = [settings.ffprobe, "-v", "error"]
    if remote:
        command += ["-rw_timeout", "15000000", "-protocol_whitelist", "http,https,httpproxy,tcp,tls",
                    "-user_agent", user_agent]
        if referer:
            command += ["-headers", f"Referer: {referer}\r\n"]
    else:
        command += ["-protocol_whitelist", "file,pipe"]
    command += ["-show_streams", "-show_format", "-of", "json", str(source)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=45, creationflags=NO_WINDOW)
        if result.returncode:
            raise MediaError("无法读取媒体信息：链接可能已失效、需要登录，或不是可读取的媒体文件。")
        info = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        raise MediaError("读取媒体信息超时或失败，请检查链接及 FFprobe 环境。") from exc
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    if not video and not audio:
        raise MediaError("这个链接没有可读取的视频或音频轨道。")
    fmt = info.get("format", {})
    frame_rate = rate(video.get("avg_frame_rate")) or rate(video.get("r_frame_rate"))
    video_duration = number(video.get("duration"))
    video_start = number(video.get("start_time"))
    frame_count = number(video.get("nb_frames"))
    if video and not remote and video_duration is None:
        timing = _packet_timing(source, settings)
        if timing:
            tail = timing["tail"] or (1 / frame_rate if frame_rate and frame_rate > 0 else 0)
            video_duration = timing["last"] + tail - timing["start"]
            video_start = timing["start"]
            frame_count = timing["count"]
            if video_duration > 0 and not frame_rate:
                frame_rate = frame_count / video_duration
    return {"duration": number(fmt.get("duration")) or number(video.get("duration")) or number(audio.get("duration")),
            "width": video.get("width"), "height": video.get("height"),
            "fps": frame_rate, "video_codec": video.get("codec_name"),
            "video_duration": video_duration, "video_start_time": video_start,
            "audio_duration": number(audio.get("duration")), "audio_start_time": number(audio.get("start_time")),
            "format_start_time": number(fmt.get("start_time"), 0), "frame_count": frame_count,
            "avg_frame_rate": video.get("avg_frame_rate"), "r_frame_rate": video.get("r_frame_rate"),
            "audio_codec": audio.get("codec_name"), "has_video": bool(video), "has_audio": bool(audio),
            "sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
            "bit_rate": number(audio.get("bit_rate")), "size": number(fmt.get("size")),
            "format_name": fmt.get("format_name"), "pixel_format": video.get("pix_fmt"),
            "sample_aspect_ratio": video.get("sample_aspect_ratio"),
            "color_transfer": video.get("color_transfer"), "color_primaries": video.get("color_primaries"),
            "rotation": next((number(item.get("rotation"), 0) for item in video.get("side_data_list", []) if "rotation" in item), 0),
            "display_aspect_ratio": video.get("display_aspect_ratio")}


def encoding_plan(options, metadata):
    """Describe the operation actually needed, separately from requested settings."""
    quality = options.get("video_quality", "high")
    target = options.get("fps")
    mode = options.get("frame_rate_mode", "interpolate")
    if not isinstance(quality, str) or quality not in VIDEO_QUALITY:
        raise MediaError("请选择有效的编码质量档位。")
    if target is not None and (type(target) is not int or not 1 <= target <= 120):
        raise MediaError("输出帧率必须为 1–120 的整数，或选择跟随源视频。")
    if not isinstance(mode, str) or mode not in ("interpolate", "fast"):
        raise MediaError("请选择流畅插帧或快速转换。")
    source = number(metadata.get("fps"))
    if source is not None and source <= 0:
        source = None
    duration = number(metadata.get("video_duration"))
    count = number(metadata.get("frame_count"))
    effective, note = "source", "保留源视频的画面时间信息。"
    if target is None and options.get("container") == "avi":
        if source:
            target, effective = source, "fast"
            note = "AVI 使用来源平均帧率规范化输出，实际帧率以完成文件为准。"
        else:
            note = "未能确定来源帧率，AVI 使用编码器检测到的帧率输出。"
    elif target is not None:
        effective = "fast"
        note = "通过重复或丢弃帧转换帧率，保持原播放速度。"
        if mode == "interpolate":
            if source is not None and target <= source + .001:
                note = "目标帧率不高于来源，直接转换，无需运动补偿插帧。"
            elif source is None or not duration or duration <= 0:
                note = "来源帧率或视频时长不完整，使用快速转换，未执行运动补偿。"
            elif (count is not None and count < 3) or (count is None and duration * source < 2.5):
                note = "片段不足三个画面，使用重复帧补足，未执行运动补偿。"
            else:
                effective = "interpolate"
                note = "已使用运动补偿生成中间画面；复杂运动可能产生局部伪影。"
    return {"source_fps": source, "target_fps": target, "effective_frame_rate_mode": effective,
            "frame_rate_note": note, "video_quality": quality}


def build_encode_command(source, destination, options, settings, metadata=None, plan=None, prepared=None, compute=None):
    """Never interpolate a user value into shell commands or filter expressions."""
    kind, container = options["kind"], options["container"]
    metadata = metadata or {}
    command = [settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-copyts", "-start_at_zero",
               "-protocol_whitelist", "file,pipe", "-i", str(source), "-map_metadata", "-1"]
    if prepared and prepared.get("raw"):
        width, height, frame_rate = prepared.get("width"), prepared.get("height"), number(prepared.get("fps"))
        if type(width) is not int or type(height) is not int or width < 2 or height < 2 or not frame_rate or not 0 < frame_rate <= 120:
            raise MediaError("GPU 视频帧描述无效。")
        command = [settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-copyts", "-start_at_zero",
                   "-protocol_whitelist", "file,pipe", "-f", "rawvideo", "-pixel_format", "rgb24",
                   "-video_size", f"{width}x{height}", "-framerate", str(frame_rate), "-i", "pipe:0",
                   "-protocol_whitelist", "file,pipe", "-i", str(source), "-map_metadata", "-1"]
    elif prepared:
        command = [settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-copyts", "-start_at_zero",
                   "-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "0", "-i", str(prepared["manifest"]),
                   "-protocol_whitelist", "file,pipe", "-i", str(source), "-map_metadata", "-1"]
    if kind == "audio":
        if container not in ("m4a", "aac"):
            raise MediaError("音频输出格式必须为 M4A 或 AAC。")
        command += ["-map", "0:a:0", "-vn"]
    else:
        if container not in ("mp4", "avi"):
            raise MediaError("视频输出格式必须为 MP4 或 AVI。")
        height = options.get("height")
        if height is not None and (type(height) is not int or height not in (360, 480, 720, 1080, 1440, 2160)):
            raise MediaError("不支持这个输出分辨率。")
        # Preserve aspect ratio and never enlarge a source. H.264 yuv420p needs even dimensions.
        # The scale filter adjusts sample aspect ratio to preserve display geometry.
        scale = f"scale=-2:trunc(min(ih\\,{height})/2)*2:flags=lanczos" if height else "scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=lanczos"
        resolved = encoding_plan(options, metadata)
        if plan is not None and plan != resolved:
            raise MediaError("编码设置与任务计划不一致，请重新创建任务。")
        plan = resolved
        crf, preset = VIDEO_QUALITY[plan["video_quality"]]
        filters = [scale]
        target = plan["target_fps"]
        duration = number(metadata.get("video_duration"))
        start = number(metadata.get("video_start_time"), 0) - number(metadata.get("format_start_time"), 0)
        end = start + duration if duration is not None else None
        if prepared:
            filters.insert(0, f"setpts=PTS+{start:.9f}/TB")
        already_interpolated = bool(prepared and prepared.get("interpolated"))
        if already_interpolated:
            if end is not None and duration > 0:
                # Worker emits the nearest whole frame count, including a
                # cloned final picture when no future reference is available.
                # Filter only video; a global duration/frame limit clips audio.
                actual_rate = number(prepared.get("fps")) or target
                output_end = start + max(1, math.floor(duration * actual_rate + .5)) / actual_rate
                filters.append(f"trim=end={output_end:.9f}")
        elif plan["effective_frame_rate_mode"] == "interpolate":
            if not getattr(settings, "interpolation", False):
                raise MediaError("当前 FFmpeg 不支持流畅插帧，请选择快速转换或更换运行库。", code="interpolation_unavailable")
            pad = max(3 / plan["source_fps"], .1)
            filters += [f"tpad=stop_mode=clone:stop_duration={pad:.9f}",
                        f"minterpolate=fps={target}:mi_mode=mci:mc_mode=aobmc:me_mode=bilat:me=epzs:vsbmc=1:scd=fdiff:scd_threshold=10",
                        f"trim=end={end:.9f}"]
        elif plan["effective_frame_rate_mode"] == "fast":
            # Keep exact rational rates for AVI following a 30000/1001 source.
            expression = str(target)
            if options.get("fps") is None:
                raw_rate = metadata.get("avg_frame_rate")
                if raw_rate and rate(raw_rate) and str(raw_rate).replace("/", "").isdigit():
                    expression = str(raw_rate)
            if end is not None and duration > 0:
                filters.append(f"tpad=stop_mode=clone:stop_duration={max(2 / target, .1):.9f}")
            filters.append(f"fps=fps={expression}:round=near")
            if end is not None and duration > 0:
                # Frame count rounds to nearest while retaining a partial final frame.
                output_end = start + max(1, math.floor(duration * target + .5)) / target
                filters.append(f"trim=end={output_end:.9f}")
        if prepared and plan["effective_frame_rate_mode"] == "source" and end is not None:
            filters.append(f"trim=end={end:.9f}")
        encoder = (compute or {}).get("video_encoder", "libx264")
        if encoder not in ("libx264", "h264_nvenc"):
            raise MediaError("不支持这个视频编码器。")
        encoding_args = ["-preset", preset, "-crf", str(crf)]
        if encoder == "h264_nvenc":
            if not getattr(settings, "nvenc", False):
                raise MediaError("NVIDIA 硬件编码不可用，请检查驱动或明确选择 CPU 模式。")
            cq, gpu_preset = NVENC_QUALITY[plan["video_quality"]]
            encoding_args = ["-preset", gpu_preset, "-tune", "hq", "-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
        command += ["-map", "0:v:0", "-map", "1:a:0?" if prepared else "0:a:0?", "-c:v", encoder, *encoding_args,
                    "-pix_fmt", "yuv420p", "-vf", ",".join(filters), "-threads", "4",
                    "-fps_mode:v", "passthrough"]
        if prepared and prepared.get("raw"):
            command += ["-r", str(prepared["fps"]), "-enc_time_base:v", "0"]
        elif prepared:
            # Concat PNGs have microsecond timestamps; set a sensible nominal
            # rate while preserving every actual source/filtered timestamp.
            command += ["-r", str(target or metadata.get("fps") or 30), "-enc_time_base:v", "0" if container == "avi" else "1:1000000"]
        if container == "avi":
            # AVI cannot signal AAC priming through an MP4 edit list. Prevent
            # the muxer shifting the video timeline to accommodate negative AAC
            # packets; fill intentional leading audio silence and discard only
            # the new encoder's negative-time priming packet (no noise added).
            command += ["-vtag", "H264", "-bf", "0", "-avoid_negative_ts", "disabled",
                        "-af", "aresample=first_pts=0", "-bsf:a", r"noise=amount=0:drop=lt(pts\,0)"]
    command += ["-c:a", "aac", "-profile:a", "aac_low", "-ar", "48000", "-b:a", "256k", "-ac", "2"]
    if container in ("mp4", "m4a"):
        command += ["-movflags", "+faststart"]
    if container == "aac":
        command += ["-f", "adts"]
    command += ["-progress", "pipe:1", "-nostats", str(destination)]
    return command


def encode(source, destination, options, settings, metadata, progress, cancel, plan=None, prepared=None, compute=None):
    command = build_encode_command(source, destination, options, settings, metadata, plan, prepared, compute)
    messages = queue.Queue()
    log = destination.parent / "encoder.log"
    with log.open("wb") as error_log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_log, text=True,
                                   encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        def read_progress():
            for line in process.stdout:
                messages.put(line.strip())
        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            while process.poll() is None or not messages.empty():
                if cancel.is_set():
                    raise Cancelled("任务已取消。")
                if time.monotonic() - started > 4 * 3600:
                    raise MediaError("转码超过 4 小时限制，任务已停止。")
                try:
                    key, _, value = messages.get(timeout=0.2).partition("=")
                    duration = metadata.get("duration")
                    if key == "out_time_us" and duration:
                        progress(55 + min(43, max(0, float(value) / 1_000_000 / duration * 43)), "正在编码并保存文件")
                except queue.Empty:
                    pass
            if process.returncode != 0:
                detail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
                raise MediaError("转码失败，请检查源文件是否损坏及编码器是否可用。详情：" + detail)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join(timeout=2)
            process.stdout.close()
    return destination


def create_demo(settings):
    """Generate an original eight-second test pattern with stereo tones."""
    path = settings.data_dir / "demo" / "sample.mp4"
    if path.is_file():
        return path
    if not (settings.ffmpeg and settings.h264 and settings.aac):
        return None
    temporary = path.with_name("sample.part.mp4")
    command = [settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
               "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i",
               "aevalsrc=0.12*sin(2*PI*440*t)|0.12*sin(2*PI*660*t):s=48000", "-t", "8",
               "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-movflags", "+faststart", str(temporary)]
    try:
        subprocess.run(command, capture_output=True, check=True, timeout=45, creationflags=NO_WINDOW)
        temporary.replace(path)
        return path
    except (OSError, subprocess.SubprocessError):
        temporary.unlink(missing_ok=True)
        return None

# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded background jobs with SQLite persistence and isolated working folders."""
import json
import math
import re
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .media import encode, encoding_plan, probe, number
from .network import Cancelled, MediaError
from .upscale import UpscaleService
from .compute import ComputeService
from .stream_timing import MAX_DURATION_SECONDS, MAX_SOURCE_FRAMES, MAX_OUTPUT_FRAMES

TERMINAL = {"completed", "failed", "cancelled"}


def check_long_gpu_request(options, media):
    """Reject known over-limit GPU interpolation before an expensive download."""
    fps, source = options.get("fps"), number(media.get("fps"))
    if (options.get("kind") != "video" or options.get("frame_rate_mode") != "interpolate" or
            options.get("processing_mode") == "cpu" or not fps or not source or fps <= source + .001):
        return
    duration = number(media.get("video_duration")) or number(media.get("duration"))
    if duration and (duration > MAX_DURATION_SECONDS or math.floor(duration * fps + .5) > MAX_OUTPUT_FRAMES):
        raise MediaError("GPU 插帧单个视频最长支持 3 小时；请拆分视频，或选择快速转换。", code="gpu_interpolation_duration_limit")
    count = number(media.get("frame_count"))
    if count and count > MAX_SOURCE_FRAMES:
        raise MediaError("GPU 插帧来源超过 1,296,000 帧，请拆分视频。", code="gpu_interpolation_frame_limit")


def validate_options(options, media):
    if not isinstance(options, dict):
        raise MediaError("输出设置必须是 JSON 对象。")
    kind = options.get("kind", "video")
    container = options.get("container", "m4a" if kind == "audio" else "mp4")
    height = options.get("height")
    format_id = str(options.get("format_id", "best"))
    if kind not in ("video", "audio", "original"):
        raise MediaError("不支持这个提取类型。")
    if kind == "video" and container not in ("mp4", "avi"):
        raise MediaError("视频输出支持 MP4 或 AVI。")
    if kind == "audio" and container not in ("m4a", "aac"):
        raise MediaError("音频输出支持 M4A 或 AAC。")
    if kind == "video" and not media.get("has_video"):
        raise MediaError("源文件没有视频轨道，请选择音频提取。")
    if kind == "audio" and not media.get("has_audio"):
        raise MediaError("源文件没有音频轨道，无法提取音频。")
    if height is not None and (type(height) is not int or height not in (360, 480, 720, 1080, 1440, 2160)):
        raise MediaError("不支持这个分辨率，请从列表中选择。")
    known = {str(fmt["id"]) for fmt in media.get("formats", [])}
    if format_id != "best" and (format_id not in known or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", format_id)):
        raise MediaError("画质选项无效，请重新解析。")
    video_quality, fps, frame_rate_mode = None, None, None
    upscale = "none"
    processing_mode = None
    if kind == "video":
        video_quality = options.get("video_quality", "high")
        fps = options.get("fps")
        frame_rate_mode = options.get("frame_rate_mode", "interpolate")
        if video_quality not in ("balanced", "high", "maximum"):
            raise MediaError("视频质量请选择均衡、高质量或最高质量。")
        if fps is not None and (type(fps) is not int or not 1 <= fps <= 120):
            raise MediaError("输出帧率应为 1 到 120 的整数，或保留原帧率。")
        if frame_rate_mode not in ("interpolate", "fast"):
            raise MediaError("帧率处理请选择运动插帧或快速转换。")
        upscale = options.get("upscale", "none")
        if not isinstance(upscale, str) or upscale not in ("none", "auto", "rtx", "comfyui"):
            raise MediaError("请选择有效的 AI 超分方式。")
        if upscale != "none" and height is None:
            raise MediaError("AI 超分需要选择明确的输出分辨率，例如 1440p 或 2160p。")
        processing_mode = options.get("processing_mode", "auto")
        if not isinstance(processing_mode, str) or processing_mode not in ("auto", "gpu", "cpu"):
            raise MediaError("处理硬件请选择 GPU 优先、仅 GPU 或 CPU。")
    return {"kind": kind, "container": container if kind != "original" else "original",
            "height": height if kind == "video" else None, "format_id": format_id,
            "video_quality": video_quality, "fps": fps, "frame_rate_mode": frame_rate_mode, "upscale": upscale,
            "processing_mode": processing_mode}


def safe_filename(title):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title).strip(" .")[:80]
    if not value:
        value = "媒体文件"
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return value


class JobManager:
    def __init__(self, settings, extractors):
        self.settings, self.extractors = settings, extractors
        self.lock = threading.RLock()
        self.db = sqlite3.connect(settings.data_dir / "jobs.sqlite3", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self.db.commit()
        self.jobs = {row[0]: json.loads(row[1]) for row in self.db.execute("SELECT id, record FROM jobs")}
        self.events = {}
        self.last_update = {}
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="framekeep")
        self.interpolation_slot = threading.BoundedSemaphore(1)
        self.upscale = UpscaleService(settings)
        self.compute = ComputeService(settings, slot=self.upscale.slot)
        for job in self.jobs.values():
            # A relocated installation may still have a completed record whose
            # private path points at the old project directory. Rebuild only
            # the safe, canonical download path and keep every public field
            # unchanged. This keeps rollback compatible with E: drive data
            # without trusting arbitrary paths stored in the database.
            if job.get("status") == "completed" and re.fullmatch(r"[a-f0-9]{32}", str(job.get("id", ""))):
                name = job.get("filename")
                if isinstance(name, str) and name and Path(name).name == name and not any(c in name for c in "/\\"):
                    directory = (settings.data_dir / "downloads" / job["id"]).resolve()
                    candidate = (directory / name).resolve()
                    downloads = (settings.data_dir / "downloads").resolve()
                    if directory.parent == downloads and candidate.parent == directory and candidate.is_file() and job.get("_path") != str(candidate):
                        job["_path"] = str(candidate)
                        self._save(job)
            if job["status"] not in TERMINAL:
                job.update(status="failed", message="上次运行被中断，请重新解析并创建任务。", stage="已中断")
                self._save(job)
            self._clean_temp(job["id"])

    @staticmethod
    def public(job):
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def _save(self, job):
        self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?)", (job["id"], json.dumps(job, ensure_ascii=False)))
        self.db.commit()

    def _clean_temp(self, job_id):
        path = (self.settings.data_dir / "tmp" / job_id).resolve()
        expected = (self.settings.data_dir / "tmp").resolve()
        if path.parent == expected and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)

    def list(self):
        with self.lock:
            return [self.public(j) for j in sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True)[:100]]

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise MediaError("找不到这个任务。")
            return self.public(self.jobs[job_id])

    def create(self, payload):
        if not isinstance(payload.get("media_id"), str):
            raise MediaError("请先解析媒体链接。")
        media = self.extractors.get(payload.get("media_id"))
        options = validate_options(payload, media)
        check_long_gpu_request(options, media)
        if options["kind"] != "original" and not (self.settings.ffmpeg and self.settings.h264 and self.settings.aac):
            raise MediaError("请安装同时支持 libx264 和原生 AAC 的 FFmpeg 后再导出。")
        selection = self.upscale.select(options, media) if options["upscale"] != "none" else None
        with self.lock:
            if sum(j["status"] not in TERMINAL for j in self.jobs.values()) >= 12:
                raise MediaError("任务队列已满，请等待部分任务完成。")
            identifier = uuid4().hex
            job = {"id": identifier, "title": media["title"], **options, "status": "queued", "progress": 0,
                   "stage": "等待处理", "message": "已加入本地任务队列", "filename": None, "download_url": None,
                   "created_at": datetime.now(timezone.utc).isoformat(), "result": None, "_path": None}
            self.jobs[identifier] = job
            self.events[identifier] = threading.Event()
            self._save(job)
            self.executor.submit(self._run, identifier, media, options, selection, time.monotonic())
            return self.public(job)

    def _update(self, job_id, **values):
        with self.lock:
            job = self.jobs[job_id]
            if job["status"] == "cancelled":
                return
            job.update(values)
            self._save(job)

    def _progress(self, job_id, progress, message):
        now = time.monotonic()
        if now - self.last_update.get(job_id, 0) < 0.4:
            return
        self.last_update[job_id] = now
        self._update(job_id, progress=round(progress, 1), message=message)

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise MediaError("找不到这个任务。")
            if job["status"] not in TERMINAL:
                self.events[job_id].set()
                job.update(status="cancelled", stage="已取消", message="任务已取消，正在清理临时文件。")
                self._save(job)
            return self.public(job)

    def _run(self, job_id, media, options, selection=None, queued_at=None):
        started = queued_at if queued_at is not None else time.monotonic()
        processing_seconds = 0
        folder = self.settings.data_dir / "tmp" / job_id
        cancel = self.events[job_id]
        final_path = None
        interpolation_acquired = False
        upscale_acquired = False
        try:
            if cancel.is_set():
                raise Cancelled("任务已取消。")
            folder.mkdir(parents=True, exist_ok=True)
            self._update(job_id, status="downloading", stage="下载源文件", message="正在连接媒体来源")
            progress = lambda value, message: self._progress(job_id, value, message)
            source = self.extractors.download(media, options, folder, progress, cancel)
            if cancel.is_set():
                raise Cancelled("任务已取消。")
            metadata = probe(source, self.settings)
            validate_options(options if options["kind"] != "original" else {**options, "container": "mp4"}, {**media, **metadata})
            plan = encoding_plan(options, metadata) if options["kind"] == "video" else {}
            prepared, upscale_report, compute = None, {}, {}
            if options["kind"] == "original":
                output = source
                filename = safe_filename(media["title"]) + "-源文件" + source.suffix
            else:
                if options["kind"] == "video":
                    compute = self.compute.select(options, metadata, plan, cancel=cancel)
                gpu_work = compute.get("video_encoder") == "h264_nvenc" or compute.get("interpolation_backend") == "gpu"
                if options.get("upscale", "none") != "none":
                    self._update(job_id, status="queued", progress=55, stage="等待 AI 超分", message="正在等待本机 AI 处理槽位，可以取消。")
                    while not upscale_acquired:
                        if cancel.is_set():
                            raise Cancelled("任务已取消。")
                        upscale_acquired = self.upscale.slot.acquire(timeout=.2)
                    if cancel.is_set():
                        raise Cancelled("任务已取消。")
                    self._update(job_id, status="converting", stage="AI 超分", message="正在处理来源画面")
                    # The queued job keeps its own provider/configuration snapshot.
                    upscale_started = time.monotonic()
                    prepared, upscale_report = self.upscale.prepare(source, options, metadata, folder, cancel, progress, selection)
                    processing_seconds += time.monotonic() - upscale_started
                    if not gpu_work:
                        self.upscale.slot.release()
                        upscale_acquired = False
                if gpu_work and not upscale_acquired:
                    self._update(job_id, status="queued", progress=55, stage="等待 GPU", message="正在等待 GPU 处理槽位，可以取消。")
                    while not upscale_acquired:
                        if cancel.is_set():
                            raise Cancelled("任务已取消。")
                        upscale_acquired = self.upscale.slot.acquire(timeout=.2)
                if compute.get("interpolation_backend") == "cpu":
                    if not getattr(self.settings, "interpolation", False):
                        raise MediaError("当前 FFmpeg 不支持运动插帧，请安装包含 minterpolate 的版本，或选择快速转换。")
                    interpolation_acquired = self.interpolation_slot.acquire(blocking=False)
                    if not interpolation_acquired:
                        self._update(job_id, status="queued", progress=55, stage="等待插帧",
                                     message="正在等待上一项运动插帧任务完成，可以随时取消。")
                    while not interpolation_acquired:
                        if cancel.is_set():
                            raise Cancelled("任务已取消。")
                        interpolation_acquired = self.interpolation_slot.acquire(timeout=0.2)
                if cancel.is_set():
                    raise Cancelled("任务已取消。")
                self._update(job_id, status="converting", progress=83 if prepared else 55, stage="编码处理", message="正在应用输出参数")
                output = folder / ("encoded." + options["container"])
                processing_started = time.monotonic()
                encoding_progress = (lambda value, message: progress(83 + max(0, value - 55) / 43 * 15, message)) if prepared else progress
                if compute.get("interpolation_backend") == "gpu":
                    # The worker streams decoded frames through CUDA RIFE and
                    # NVENC; no intermediate PNG sequence is written here.
                    report = self.compute.encode(source, output, options, metadata, folder, cancel,
                                                 progress, plan, compute, prepared=prepared)
                    compute.update(report)
                else:
                    encode_args = {"plan": plan}
                    if prepared:
                        encode_args["prepared"] = prepared
                    if compute.get("video_encoder") == "h264_nvenc":
                        encode_args["compute"] = compute
                    encode(source, output, options, self.settings, metadata, encoding_progress, cancel, **encode_args)
                processing_seconds += time.monotonic() - processing_started
                if upscale_acquired:
                    self.upscale.slot.release()
                    upscale_acquired = False
                suffix = ("-H264" if options["kind"] == "video" else "-AAC")
                if prepared:
                    suffix += f"-AI{options['height']}p"
                if options["kind"] == "video" and options["fps"] is not None:
                    suffix += f"-{options['fps']}fps"
                filename = safe_filename(media["title"]) + suffix + "." + options["container"]
            if cancel.is_set():
                raise Cancelled("任务已取消。")
            result = {**plan, **upscale_report, **compute, **probe(output, self.settings)}
            result["size"] = output.stat().st_size
            result["processing_seconds"] = round(processing_seconds, 3)
            result["total_seconds"] = round(time.monotonic() - started, 3)
            if options["kind"] != "original":
                result["target_audio_bit_rate"] = 256000
            final_path = self.settings.data_dir / "downloads" / job_id / filename
            final_path.parent.mkdir(parents=True, exist_ok=True)
            output.replace(final_path)
            with self.lock:
                if cancel.is_set():
                    raise Cancelled("任务已取消。")
                self._update(job_id, status="completed", progress=100, stage="已完成", message="文件已保存，可以下载",
                             filename=filename, download_url=f"/api/jobs/{job_id}/download", result=result, _path=str(final_path))
        except Cancelled:
            self._update(job_id, status="cancelled", stage="已取消", message="任务已取消。")
        except Exception as exc:
            message = str(exc) if isinstance(exc, MediaError) else "任务未能完成，请检查磁盘空间、源文件和运行环境。"
            self._update(job_id, status="failed", stage="处理失败", message=message)
        finally:
            if upscale_acquired:
                self.upscale.slot.release()
            if interpolation_acquired:
                self.interpolation_slot.release()
            if final_path and cancel.is_set():
                final_path.unlink(missing_ok=True)
            self._clean_temp(job_id)
            with self.lock:
                self.events.pop(job_id, None)
                self.last_update.pop(job_id, None)

    def download_path(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["status"] != "completed" or not job.get("_path"):
                raise MediaError("文件尚未生成。")
            path = Path(job["_path"]).resolve()
            root = (self.settings.data_dir / "downloads").resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise MediaError("文件已被移动或删除，请重新创建任务。")
            return path

    def close(self):
        with self.lock:
            for event in self.events.values():
                event.set()
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.db.close()

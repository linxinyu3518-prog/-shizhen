# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded timestamp collection and on-disk access for long GPU video tasks."""
import math
import mmap
import operator
import queue
import struct
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path

MAX_DURATION_SECONDS = 3 * 3600
MAX_SOURCE_FRAMES = MAX_DURATION_SECONDS * 120
MAX_OUTPUT_FRAMES = MAX_DURATION_SECONDS * 120
GPU_TIMEOUT_SECONDS = 24 * 3600
STALL_TIMEOUT_SECONDS = 600
MAX_PIPE_LINE_BYTES = 16 * 1024
MAX_ERROR_LOG_BYTES = 256 * 1024


def validate_limits(duration, fps, source_count=0):
    from .network import MediaError
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration <= 0 or type(fps) is not int
            or not 1 <= fps <= 120):
        raise MediaError("GPU 插帧需要有效的目标帧率和视频时长。")
    if (isinstance(source_count, bool) or not isinstance(source_count, (int, float))
            or not math.isfinite(source_count) or source_count < 0):
        raise MediaError("GPU 插帧需要有效的来源画面数量。")
    output_count = max(1, math.floor(duration * fps + .5))
    if (duration > MAX_DURATION_SECONDS or output_count > MAX_OUTPUT_FRAMES
            or source_count > MAX_SOURCE_FRAMES):
        raise MediaError("GPU 插帧最多支持 3 小时、1,296,000 个来源画面和 1,296,000 个输出画面。")
    return output_count


class DoubleTimeline(Sequence):
    """Read float64 timestamps by index without making a Python list of frames."""
    def __init__(self, path, count):
        if type(count) is not int or not 1 <= count <= MAX_SOURCE_FRAMES:
            raise ValueError("Invalid timestamp count")
        self.count = count
        self.file = Path(path).open("rb")
        self.mapping = None
        try:
            if self.file.seek(0, 2) != count * 8:
                raise ValueError("Timestamp file size does not match source count")
            self.file.seek(0)
            self.mapping = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        except BaseException:
            self.file.close()
            raise

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(self.count))]
        index = operator.index(index)
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError("Timestamp index out of range")
        return struct.unpack_from("<d", self.mapping, index * 8)[0]

    def close(self):
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def read_pipe_lines(pipe, messages, stop):
    """One bounded reader used by both the timestamp and GPU control pipes."""
    def send(kind, value=None):
        while not stop.is_set():
            try:
                messages.put((kind, value), timeout=.1)
                return
            except queue.Full:
                pass
    try:
        while not stop.is_set():
            line = pipe.readline(MAX_PIPE_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_PIPE_LINE_BYTES:
                raise ValueError("进程消息超过单行大小限制。")
            send("line", line)
    except (OSError, ValueError) as exc:
        send("error", str(exc))
    finally:
        send("eof")


class BoundedErrorLog:
    """Drain stderr continuously and retain only its bounded, most recent tail."""
    def __init__(self, pipe, path):
        self.pipe, self.path = pipe, Path(path)
        self.tail = bytearray()
        self.path.write_bytes(b"")
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            with self.path.open("wb") as output:
                written = 0
                while True:
                    chunk = self.pipe.read(4096)
                    if not chunk:
                        break
                    self.tail.extend(chunk)
                    if len(self.tail) > MAX_ERROR_LOG_BYTES:
                        del self.tail[:-MAX_ERROR_LOG_BYTES]
                    keep = min(len(chunk), MAX_ERROR_LOG_BYTES - written)
                    if keep:
                        output.write(chunk[:keep])
                        output.flush()
                        written += keep
        except (OSError, ValueError):
            pass

    def close(self):
        self.thread.join(timeout=3)
        if not self.thread.is_alive():
            self.pipe.close()
            self.path.write_bytes(self.tail)

    def details(self):
        return bytes(self.tail[-1200:]).decode("utf-8", errors="replace")


def scan_timestamps(ffprobe, source, folder, duration, cancel, progress=None, monitor=None,
                    timeout=None):
    """Validate display-order PTS incrementally, storing eight bytes per frame."""
    from .config import NO_WINDOW
    from .network import Cancelled, MediaError
    validate_limits(duration, 1)
    if cancel.is_set():
        raise Cancelled("任务已取消。")
    folder = Path(folder)
    output = folder / "times.f64"
    temporary = folder / "times.partial"
    command = [str(ffprobe), "-v", "error", "-threads", "4", "-protocol_whitelist", "file,pipe",
               "-select_streams", "v:0", "-show_frames", "-show_entries",
               "frame=best_effort_timestamp_time", "-of", "compact=p=1:nk=0", str(source)]
    timeout = min(MAX_DURATION_SECONDS * 2, max(600, duration * 2)) if timeout is None else timeout
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=NO_WINDOW)
    except OSError as exc:
        raise MediaError("无法启动视频时间戳读取进程。") from exc
    messages, stop = queue.Queue(maxsize=32), threading.Event()
    reader = threading.Thread(target=read_pipe_lines, args=(process.stdout, messages, stop), daemon=True)
    errors = BoundedErrorLog(process.stderr, folder / "timing.log")
    reader.start()
    began = last_event = last_progress = time.monotonic()
    count, first, previous, ended = 0, None, None, False
    try:
        with temporary.open("wb") as timestamps:
            while process.poll() is None or not ended or not messages.empty():
                if cancel.is_set():
                    raise Cancelled("任务已取消。")
                now = time.monotonic()
                if now - began > timeout:
                    raise MediaError("来源时间戳读取超过时间限制，请检查视频或存储速度。")
                if now - last_event > STALL_TIMEOUT_SECONDS:
                    raise MediaError("来源时间戳读取超过 600 秒没有进展。")
                if monitor:
                    monitor()
                try:
                    kind, payload = messages.get(timeout=.1)
                except queue.Empty:
                    continue
                if kind == "error":
                    raise MediaError("无法读取来源时间戳：" + payload)
                if kind == "eof":
                    ended = True
                    continue
                line = payload.decode("utf-8", errors="replace").strip()
                # Compact output marks frame records explicitly; SEI side-data and
                # empty lines are not additional pictures or timestamp values.
                if not line or line.split("|", 1)[0] != "frame":
                    continue
                fields = [part.partition("=") for part in line.split("|")[1:]]
                values = [value for key, separator, value in fields
                          if key == "best_effort_timestamp_time" and separator]
                try:
                    current = float(values[0]) if len(values) == 1 else float("nan")
                except ValueError:
                    current = float("nan")
                if not math.isfinite(current) or (previous is not None and current <= previous):
                    raise MediaError("来源画面时间戳缺失或不递增，无法保证插帧音画同步。")
                if count >= MAX_SOURCE_FRAMES:
                    raise MediaError("GPU 插帧来源画面超过 1,296,000 帧限制。")
                if first is None:
                    first = current
                normalized = current - first
                if normalized >= duration + .001:
                    raise MediaError("来源最后一帧超出视频时长，停止插帧以避免截短。")
                timestamps.write(struct.pack("<d", normalized))
                count, previous, last_event = count + 1, current, now
                if progress and now - last_progress >= .5:
                    progress(count, normalized)
                    last_progress = now
        process.wait()
        if process.returncode:
            errors.thread.join(timeout=3)
            raise MediaError("来源时间戳读取失败：" + errors.details())
        if not count:
            raise MediaError("GPU 插帧无法读取来源画面时间戳。")
        temporary.replace(output)
        if progress:
            progress(count, previous - first)
        return {"times_file": str(output.resolve()), "source_count": count}
    finally:
        stop.set()
        if process.poll() is None:
            process.kill()
        process.wait()
        reader.join(timeout=3)
        if not reader.is_alive():
            process.stdout.close()
        errors.close()
        temporary.unlink(missing_ok=True)

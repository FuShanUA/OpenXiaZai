"""Live-stream recording tasks for the WeChat Channels UI.

The capture addon only discovers the live URL. Recording is intentionally kept
separate from finite video downloads: a live source has no total size, cannot be
resumed with byte ranges, and must remain useful after the user stops it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .downloads import human_size

PROGRESS_INTERVAL = 0.25
STOP_GRACE_SECONDS = 15
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36 MicroMessenger/3.8.9"
)


def ffmpeg_path() -> str | None:
    configured = os.environ.get("WX_FFMPEG_PATH")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        os.path.expanduser("~/.local/bin/ffmpeg"),
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def safe_stem(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", value.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return (cleaned or fallback)[:80]


def duration_text(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "未知"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, sec = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


@dataclass
class LiveTask:
    id: str
    live_id: str
    title: str
    author: str
    url: str
    state: str = "queued"  # queued | recording | stopping | done | failed
    output: str | None = None
    received: int = 0
    duration_seconds: float | None = None
    speed: float = 0
    error: str | None = None
    started: float | None = None
    finished: float | None = None
    stop_requested: bool = False

    def as_dict(self) -> dict[str, Any]:
        now = self.finished or time.time()
        elapsed = max(0.0, now - (self.started or now))
        speed = self.received / elapsed if self.state == "recording" and elapsed else 0
        output = Path(self.output) if self.output else None
        return {
            "id": self.id,
            "live_id": self.live_id,
            "title": self.title,
            "author": self.author,
            "state": self.state,
            "output": self.output,
            "received": self.received,
            "received_human": human_size(self.received),
            "duration_seconds": self.duration_seconds,
            "duration_human": duration_text(self.duration_seconds),
            "speed": round(speed, 1),
            "speed_human": f"{human_size(speed)}/s" if speed else "",
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
            "output_exists": bool(output and output.is_file()),
        }


class LiveRecordingManager:
    """Owns FFmpeg recording tasks and their durable sidecar records."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.tasks: dict[str, LiveTask] = {}
        self.order: list[str] = []
        self.lock = threading.RLock()
        self._seq = 0
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._last_emit: dict[str, float] = {}
        self._load_records()

    def _load_records(self) -> None:
        directory: Path = self.owner.video_dir
        if not directory.exists():
            return
        for sidecar in sorted(directory.glob("*.live.json")):
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            task_id = str(payload.get("task_id") or "")
            live_id = str(payload.get("live_id") or "")
            output = str(payload.get("output") or "")
            if not task_id or not live_id or not output:
                continue
            output_path = Path(output)
            size = output_path.stat().st_size if output_path.is_file() else 0
            task = LiveTask(
                id=task_id,
                live_id=live_id,
                title=str(payload.get("title") or ""),
                author=str(payload.get("author") or ""),
                url=str(payload.get("url") or ""),
                state="done" if output_path.is_file() else "failed",
                output=output,
                received=int(payload.get("received") or size),
                duration_seconds=payload.get("duration_seconds"),
                started=payload.get("started"),
                finished=payload.get("finished"),
                error=None if output_path.is_file() else "录制文件不存在",
            )
            self.tasks[task_id] = task
            self.order.append(task_id)
            self._seq = max(self._seq, int(re.sub(r"\D", "", task_id) or 0))

    def all(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self.tasks[task_id].as_dict() for task_id in self.order]

    def get(self, task_id: str) -> LiveTask | None:
        with self.lock:
            return self.tasks.get(task_id)

    def active_for_live(self, live_id: str) -> LiveTask | None:
        with self.lock:
            for task_id in reversed(self.order):
                task = self.tasks[task_id]
                if task.live_id == live_id and task.state in {"queued", "recording", "stopping"}:
                    return task
        return None

    def start(self, live_id: str) -> LiveTask:
        with self.owner.state_lock:
            live = dict(self.owner.live_by_id.get(live_id) or {})
        url = str(live.get("url") or "")
        if not live or not url:
            raise KeyError(f"unknown live id {live_id!r}")

        active = self.active_for_live(live_id)
        if active is not None:
            return active

        binary = ffmpeg_path()
        if not binary:
            raise RuntimeError("未找到 ffmpeg，请先执行 brew install ffmpeg")

        self.owner.video_dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self._seq += 1
            task = LiveTask(
                id=f"live{self._seq}",
                live_id=live_id,
                title=str(live.get("title") or ""),
                author=str(live.get("author") or ""),
                url=url,
                started=time.time(),
            )
            self.tasks[task.id] = task
            self.order.append(task.id)

        threading.Thread(target=self._run, args=(task, binary), daemon=True).start()
        return task

    def stop(self, live_id: str) -> LiveTask | None:
        task = self.active_for_live(live_id)
        if task is None:
            return None
        with self.lock:
            task.state = "stopping"
            task.stop_requested = True
            process = self._processes.get(task.id)
        if process is not None and process.poll() is None:
            try:
                assert process.stdin is not None
                process.stdin.write("q\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            threading.Thread(
                target=self._force_stop, args=(task.id,), daemon=True
            ).start()
        self._emit(task, force=True)
        return task

    def _force_stop(self, task_id: str) -> None:
        time.sleep(STOP_GRACE_SECONDS)
        with self.lock:
            process = self._processes.get(task_id)
        if process is not None and process.poll() is None:
            process.terminate()

    def _run(self, task: LiveTask, binary: str) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(task.started or time.time()))
        stem = safe_stem(task.title or task.author or task.live_id, task.live_id)
        final_path = self.owner.video_dir / f"直播_{stem}_{stamp}.mp4"
        sequence = 1
        while final_path.exists():
            final_path = self.owner.video_dir / f"直播_{stem}_{stamp}-{sequence}.mp4"
            sequence += 1
        part_path = final_path.with_name(f"{final_path.stem}.part.mp4")
        sidecar_path = final_path.with_name(f"{final_path.stem}.live.json")
        args = [
            binary,
            "-hide_banner",
            "-loglevel", "error",
            "-progress", "pipe:1",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-user_agent", USER_AGENT,
            "-i", task.url,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c", "copy",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4",
            str(part_path),
        ]

        stderr_tail: list[str] = []
        process: subprocess.Popen[str] | None = None
        try:
            with self.lock:
                task.state = "recording"
                task.output = str(final_path)
            self._emit(task, force=True)
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            with self.lock:
                self._processes[task.id] = process

            stderr_thread = threading.Thread(
                target=self._read_stderr, args=(process, stderr_tail), daemon=True
            )
            stderr_thread.start()
            assert process.stdout is not None
            for line in process.stdout:
                key, separator, value = line.strip().partition("=")
                if not separator:
                    continue
                if key == "total_size":
                    with self.lock:
                        task.received = max(0, int(value or 0))
                elif key in {"out_time_us", "out_time_ms"}:
                    try:
                        microseconds = int(value or 0)
                    except ValueError:
                        microseconds = 0
                    with self.lock:
                        task.duration_seconds = max(0.0, microseconds / 1_000_000)
                elif key == "progress":
                    self._emit(task)

            returncode = process.wait()
            stderr_thread.join(timeout=2)
            size = part_path.stat().st_size if part_path.exists() else 0
            if size > 0:
                part_path.replace(final_path)
                with self.lock:
                    task.output = str(final_path)
                    task.received = size
                    task.state = "done"
                    task.finished = time.time()
                    if task.duration_seconds is None:
                        task.duration_seconds = max(
                            0.0, task.finished - (task.started or task.finished)
                        )
                self._write_sidecar(sidecar_path, task)
            else:
                detail = " ".join(stderr_tail)[-500:] if stderr_tail else "ffmpeg did not write any data"
                with self.lock:
                    task.state = "failed"
                    task.error = detail
                    task.finished = time.time()
                self._write_sidecar(sidecar_path, task)
            if returncode != 0 and size == 0:
                self.owner._count("live_recording_failed")
            else:
                self.owner._count("live_recording_done")
        except Exception as exc:
            with self.lock:
                task.state = "failed"
                task.error = str(exc)[:500]
                task.finished = time.time()
            self._write_sidecar(sidecar_path, task)
            self.owner._count("live_recording_failed")
        finally:
            if process is not None:
                with self.lock:
                    self._processes.pop(task.id, None)
            self._emit(task, force=True)

    @staticmethod
    def _read_stderr(process: subprocess.Popen[str], tail: list[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            line = line.strip()
            if line:
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)

    def _write_sidecar(self, path: Path, task: LiveTask) -> None:
        payload = task.as_dict()
        payload["task_id"] = task.id
        payload["url"] = task.url
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def _emit(self, task: LiveTask, force: bool = False) -> None:
        now = time.time()
        with self.lock:
            last = self._last_emit.get(task.id, 0.0)
            if not force and now - last < PROGRESS_INTERVAL:
                return
            self._last_emit[task.id] = now
            payload = task.as_dict()
        self.owner._write({"event": "live_recording", **payload})


class LivePreviewManager:
    """One FFmpeg process per live room, exposed to the browser as HLS."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._urls: dict[str, str] = {}
        self._dirs: dict[str, Path] = {}

    def playlist(self, live_id: str, url: str) -> Path:
        with self.lock:
            process = self._processes.get(live_id)
            directory = self._dirs.get(live_id)
            same_url = self._urls.get(live_id) == url
            running = process is not None and process.poll() is None
            playlist = directory / "index.m3u8" if directory else None
            if running and same_url and playlist is not None and playlist.is_file():
                return playlist

            if process is not None:
                self._stop_locked(live_id)
            for other_id in list(self._processes):
                if other_id != live_id:
                    self._stop_locked(other_id)

            binary = ffmpeg_path()
            if not binary:
                raise RuntimeError("未找到 ffmpeg，请先执行 brew install ffmpeg")
            directory = self.owner.video_dir / ".live-preview" / safe_stem(live_id, "live")
            if directory.exists():
                shutil.rmtree(directory)
            directory.mkdir(parents=True, exist_ok=True)
            playlist = directory / "index.m3u8"
            args = [
                binary,
                "-hide_banner",
                "-loglevel", "error",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-user_agent", USER_AGENT,
                "-i", url,
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-tune", "zerolatency",
                "-vf", "scale=-2:360",
                "-force_key_frames", "expr:gte(t,n_forced*1)",
                "-c:a", "aac",
                "-b:a", "96k",
                "-f", "hls",
                "-hls_time", "1",
                "-hls_list_size", "3",
                "-hls_flags", "delete_segments+omit_endlist+independent_segments",
                "-hls_segment_filename", str(directory / "segment%05d.ts"),
                str(playlist),
            ]
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            self._processes[live_id] = process
            self._urls[live_id] = url
            self._dirs[live_id] = directory

        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                ready = "#EXTINF" in playlist.read_text(encoding="utf-8", errors="replace")
            except OSError:
                ready = False
            if ready:
                return playlist
            with self.lock:
                process = self._processes.get(live_id)
            if process is None or process.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError("直播预览启动超时")

    def file(self, live_id: str, relative: str) -> tuple[Path, str] | None:
        with self.lock:
            directory = self._dirs.get(live_id)
        if directory is None:
            return None
        if relative != "index.m3u8" and not re.fullmatch(r"segment\d+\.ts", relative):
            return None
        path = directory / relative
        resolved = path.resolve()
        if not resolved.is_relative_to(directory.resolve()) or not resolved.is_file():
            return None
        content_type = (
            "application/vnd.apple.mpegurl"
            if relative == "index.m3u8"
            else "video/mp2t"
        )
        return resolved, content_type

    def stop_all(self) -> None:
        with self.lock:
            for live_id in list(self._processes):
                self._stop_locked(live_id)

    def _stop_locked(self, live_id: str) -> None:
        process = self._processes.pop(live_id, None)
        self._urls.pop(live_id, None)
        directory = self._dirs.get(live_id)
        if directory is not None and directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
            self._dirs.pop(live_id, None)
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


__all__ = ["LivePreviewManager", "LiveRecordingManager", "LiveTask", "ffmpeg_path"]

"""Download tasks, quality probing and range-capable file serving.

Split out of the mitmproxy addon so the capture path stays independent: the addon
works fine with `work/ui/` deleted, and this module never imports the addon.

Everything here is user-initiated. The only automatic outbound traffic is the
size probe (one 1-byte range request per rendition), which exists so the UI can
show real byte counts instead of bitrates.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

CHUNK = 1 << 20
PROBE_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 300
PROGRESS_INTERVAL = 0.2

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36 MicroMessenger/3.8.9"
)


def quality_suffix(quality: str, client_format: str = "") -> str:
    """Map a UI quality choice to the on-disk suffix and the URL selector.

    ``best`` means "omit X-snsvideoflag" (the untranscoded master). ``client``
    resolves to whatever rendition the WeChat client itself picked, which we
    learn passively from its own stodownload request.
    """
    if quality == "best":
        return ".best"
    if quality in {"client", ""}:
        return f".{client_format}" if client_format else ".best"
    return f".{quality}"


def describe_spec(spec: dict[str, Any]) -> str:
    """Human label for one entry of ``media.spec``."""
    parts: list[str] = []
    height = spec.get("height")
    width = spec.get("width")
    if height:
        parts.append(f"{height}p")
    elif width:
        parts.append(f"{width}w")
    rate = spec.get("video_bitrate")
    if rate:
        parts.append(f"{rate} kbps")
    return " · ".join(parts) or str(spec.get("file_format") or "?")


def human_size(value: int | None) -> str:
    if not value:
        return "未知"
    units = ("B", "KB", "MB", "GB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


# --------------------------------------------------------------------- probing
def _probe_one(url: str, timeout: int = PROBE_TIMEOUT) -> int | None:
    """Read a rendition's real size from a 1-byte range request."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Range": "bytes=0-0",
        "Accept": "*/*",
    })
    # The system proxy points at our own mitmproxy; bypass it or we would loop.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        content_range = response.headers.get("Content-Range")
        if content_range:
            match = re.search(r"/(\d+)\s*$", content_range)
            if match:
                return int(match.group(1))
        length = response.headers.get("Content-Length")
        return int(length) if length and length.isdigit() else None


def probe_formats(owner: Any, media: dict[str, Any], slots: threading.Semaphore) -> list[dict[str, Any]]:
    """Probe the master plus every rendition listed in ``media.spec``."""
    raw = str(media.get("url") or "") + str(media.get("urlToken") or "")
    if len(raw) < 32:
        return []
    with owner.state_lock:
        client_format = owner.client_formats.get(owner._file_key(raw), "")
    entries: list[tuple[str, str]] = [("best", "原片")]
    for spec in media.get("spec") or []:
        fmt = spec.get("file_format")
        if fmt:
            entries.append((str(fmt), describe_spec(spec)))
    # De-duplicate while keeping the master first.
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for quality, label in entries:
        if quality in seen:
            continue
        seen.add(quality)
        if quality == "client" or quality == client_format:
            label = f"{label} · 客户端正在播这档"
        entry: dict[str, Any] = {"quality": quality, "label": label, "bytes": None, "ok": False}
        try:
            url = owner._variant_url(raw, quality_suffix(quality, client_format))
            with slots:
                entry["bytes"] = _probe_one(url)
            entry["ok"] = entry["bytes"] is not None
        except Exception as exc:  # expired link, network hiccup, …
            entry["error"] = str(exc)[:160]
        results.append(entry)
    return results


# ----------------------------------------------------------------- range HTTP
class RangeResult:
    """A resolved file response: either the whole file or one byte range."""

    def __init__(self, status: int, headers: dict[str, str], path: Path | None = None,
                 start: int = 0, length: int = 0, body: bytes | None = None) -> None:
        self.status = status
        self.headers = headers
        self.path = path
        self.start = start
        self.length = length
        self.body = body

    def chunks(self, size: int = CHUNK) -> Iterator[bytes]:
        if self.body is not None:
            if self.body:
                yield self.body
            return
        assert self.path is not None
        with self.path.open("rb") as handle:
            handle.seek(self.start)
            remaining = self.length
            while remaining > 0:
                chunk = handle.read(min(size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk


def parse_range(range_header: str | None, size: int) -> tuple[int, int] | None:
    """Resolve a Range header to ``(start, end_inclusive)``.

    Returns ``None`` for "serve the whole file". Raises ``ValueError`` when the
    range is malformed or unsatisfiable, which the caller turns into a 416.
    """
    if not range_header or not range_header.startswith("bytes="):
        return None
    spec = range_header[len("bytes="):].split(",")[0].strip()
    first, _, last = spec.partition("-")
    if not first:  # bytes=-N -> the final N bytes
        if not last.isdigit() or int(last) <= 0:
            raise ValueError("malformed suffix range")
        length = min(int(last), size)
        return size - length, size - 1
    if not first.isdigit():
        raise ValueError("malformed range")
    start = int(first)
    if start >= size:
        raise ValueError("range starts past end of file")
    end = int(last) if last.isdigit() else size - 1
    return start, min(end, size - 1)


def serve_range(path: Path, range_header: str | None) -> RangeResult:
    """Serve a local file, honouring Range. 206/200/416 per RFC 7233."""
    size = path.stat().st_size
    etag = f'"{int(path.stat().st_mtime)}-{size}"'
    last_modified = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(path.stat().st_mtime))
    content_type = "video/mp4"
    try:
        resolved = parse_range(range_header, size)
    except ValueError:
        return RangeResult(416, {
            "Content-Range": f"bytes */{size}",
            "Content-Type": content_type,
            "Content-Length": "0",
            "Accept-Ranges": "bytes",
        })
    if resolved is None:
        return RangeResult(200, {
            "Content-Type": content_type,
            "Content-Length": str(size),
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Last-Modified": last_modified,
            "Cache-Control": "private, max-age=3600",
        }, path=path, start=0, length=size)
    start, end = resolved
    length = end - start + 1
    return RangeResult(206, {
        "Content-Type": content_type,
        "Content-Length": str(length),
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Last-Modified": last_modified,
        "Cache-Control": "private, max-age=3600",
    }, path=path, start=start, length=length)


# --------------------------------------------------------------------- tasks
@dataclass
class DownloadTask:
    id: str
    file_key: str
    quality: str
    label: str = ""
    state: str = "queued"          # queued | downloading | decrypting | done | failed
    total: int | None = None
    received: int = 0
    error: str | None = None
    output: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        now = self.finished or time.time()
        elapsed = max(0.0, now - self.started)
        speed = self.received / elapsed if self.state == "downloading" and elapsed else 0
        data["percent"] = (
            round(100.0 * self.received / self.total, 1) if self.total else None
        )
        data["speed"] = round(speed, 1) if speed else 0
        data["eta_seconds"] = (
            round((self.total - self.received) / speed)
            if self.total and speed and self.state == "downloading"
            else None
        )
        data["received_human"] = human_size(self.received)
        data["total_human"] = human_size(self.total)
        return data


class DownloadManager:
    """Owns the download tasks. One manager per capture session."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.tasks: dict[str, DownloadTask] = {}
        self.order: list[str] = []
        self.lock = threading.Lock()
        self.probes: dict[str, list[dict[str, Any]]] = {}
        self._seq = 0
        self._last_emit: dict[str, float] = {}

    # ------------------------------------------------------------- queries
    def all(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self.tasks[tid].as_dict() for tid in self.order]

    def get(self, task_id: str) -> DownloadTask | None:
        with self.lock:
            return self.tasks.get(task_id)

    def for_file(self, file_key: str) -> list[dict[str, Any]]:
        with self.lock:
            return [t.as_dict() for t in self.tasks.values() if t.file_key == file_key]

    def cached_probe(self, file_key: str) -> list[dict[str, Any]] | None:
        with self.lock:
            return self.probes.get(file_key)

    # ------------------------------------------------------------- probing
    def probe(self, file_key: str, refresh: bool = False) -> list[dict[str, Any]]:
        if not refresh:
            cached = self.cached_probe(file_key)
            if cached is not None:
                return cached
        medias = self.owner.media_by_file_key.get(file_key) or []
        if not medias:
            return []
        results = probe_formats(self.owner, self.owner.freshest_media(file_key), self.owner.variant_slots)
        with self.lock:
            self.probes[file_key] = results
        self.owner._count("probe")
        self.owner._write({"event": "probe", "file_key": file_key, "formats": results})
        return results

    def probe_async(self, file_key: str) -> None:
        threading.Thread(target=self.probe, args=(file_key,), daemon=True).start()

    # ------------------------------------------------------------- download
    def start(self, file_key: str, quality: str) -> DownloadTask:
        # Always download against the freshest signed URL: an earlier prefetch
        # of the same video carries an older, possibly expired one.
        media = self.owner.freshest_media(file_key)
        if not media:
            raise KeyError(f"unknown file key {file_key!r}")
        with self.owner.state_lock:
            client_format = self.owner.client_formats.get(file_key, "")
        suffix = quality_suffix(quality, client_format)
        stem = f"{file_key}{suffix}"
        # Resolve the probed size before taking the lock: _known_total() reads
        # self.probes through cached_probe(), which acquires the same lock.
        total = self._known_total(file_key, quality)

        with self.lock:
            existing = next(
                (t for t in self.tasks.values() if f"{t.file_key}{quality_suffix(t.quality, client_format)}" == stem),
                None,
            )
            if existing is not None and existing.state in {"queued", "downloading", "decrypting"}:
                return existing
            if existing is not None and existing.state == "done":
                return existing
            self._seq += 1
            task = DownloadTask(
                id=f"dl{self._seq}",
                file_key=file_key,
                quality=quality,
                label=quality if quality != "best" else "原片",
                total=total,
            )
            self.tasks[task.id] = task
            self.order.append(task.id)

        url = self.owner._variant_url(
            str(media.get("url") or "") + str(media.get("urlToken") or ""), suffix
        )
        threading.Thread(target=self._run, args=(task, url), daemon=True).start()
        return task

    def _known_total(self, file_key: str, quality: str) -> int | None:
        probed = self.cached_probe(file_key) or []
        for entry in probed:
            if entry.get("quality") == quality and entry.get("bytes"):
                return int(entry["bytes"])
        return None

    def _run(self, task: DownloadTask, url: str) -> None:
        try:
            with self.lock:
                task.state = "downloading"
            self._emit(task, force=True)
            payload = self._fetch(task, url)
            with self.lock:
                task.state = "decrypting"
            self._emit(task, force=True)
            sidecar = self.owner.persist_download(task.file_key, task.quality, url, payload)
            with self.lock:
                task.output = sidecar.get("decrypted_path") or sidecar.get("encrypted_path")
                task.state = "done" if sidecar.get("decoded") else "failed"
                if task.state == "failed":
                    task.error = "已下载但解密失败,候选 key 都不匹配"
            self.owner._count("download_ok" if task.state == "done" else "download_undecoded")
        except Exception as exc:
            with self.lock:
                task.state = "failed"
                task.error = str(exc)[:300]
            self.owner._count("download_failed")
        finally:
            with self.lock:
                task.finished = time.time()
            self._emit(task, force=True)

    def _fetch(self, task: DownloadTask, url: str) -> bytes:
        # No Range here on purpose: one full GET. Chunked reads exist only to
        # feed the progress bar, not for resumption. See docs/WEBUI.md §5.3.
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        })
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        buffer = bytearray()
        with opener.open(request, timeout=DOWNLOAD_TIMEOUT) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit():
                with self.lock:
                    task.total = int(length)
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                buffer.extend(chunk)
                with self.lock:
                    task.received = len(buffer)
                self._emit(task)
        return bytes(buffer)

    def _emit(self, task: DownloadTask, force: bool = False) -> None:
        now = time.time()
        with self.lock:
            last = self._last_emit.get(task.id, 0.0)
            if not force and now - last < PROGRESS_INTERVAL:
                return
            self._last_emit[task.id] = now
            payload = task.as_dict()
        self.owner._write({"event": "download", **payload})


def read_tail(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    """Last ``limit`` records of the capture log, oldest first."""
    import json

    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


__all__ = [
    "DownloadManager", "DownloadTask", "RangeResult",
    "human_size", "parse_range", "probe_formats", "quality_suffix",
    "read_tail", "serve_range",
]

"""HTTP router for the Web UI.

Mounted onto the HTTP server the addon already runs on 127.0.0.1:18765, so the UI
needs no extra process. The addon imports this lazily: delete `work/ui/` and
capture still works.

Loopback only, and the whole workflow assumes the machine already runs the WeChat
client, so there is deliberately no auth (see docs/WEBUI.md §10).

Routes
------
GET  /                                   the UI
GET  /static/<path>                      assets
GET  /api/state                          capture/hook summary
GET  /api/videos                         captured videos
GET  /api/lives                          captured live streams
GET  /api/videos/<key>                   one video in detail
GET  /api/videos/<key>/stream?variant=   local file, Range-capable
GET  /api/videos/<key>/probe             (re)probe rendition sizes
GET  /api/tasks                          download tasks
GET  /api/events                         SSE stream
GET  /api/log?tail=200                   tail of the capture log
POST /api/videos/<key>/download          {"quality": "best"}
POST /api/lives/<id>/record              start live recording
POST /api/lives/<id>/stop                stop the active live recording
GET  /api/live-recordings/<id>/stream    recorded live MP4
GET  /api/live-recordings/<id>/reveal    show the recording in Finder
GET  /api/live-recordings/<id>/open      open with the default player
GET  /api/lives/<id>/preview/index.m3u8  live preview playlist
GET  /api/lives/<id>/preview/segment*.ts live preview segment
POST /api/service                        {"enabled": true|false}
POST /api/selftest                       run the decryptor's self-test
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import downloads as dl
from . import live as live_recording

# Bumped whenever a UI behaviour changes. Exposed over the API and the SSE
# hello frame so a stale browser tab or an un-restarted mitmdump can be
# detected instead of silently showing wrong results.
UI_VERSION = 17

STATIC_DIR = Path(__file__).resolve().parent / "static"
JSON_TYPE = "application/json; charset=utf-8"
SSE_TYPE = "text/event-stream; charset=utf-8"
SSE_HEARTBEAT = 15.0
MAX_LOG_TAIL = 2000


class Response:
    def __init__(self, status: int = 200, body: bytes = b"", content_type: str = JSON_TYPE,
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.extra = headers or {}

    @classmethod
    def json(cls, payload: Any, status: int = 200) -> "Response":
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        return cls(status, body, JSON_TYPE)

    @classmethod
    def error(cls, status: int, message: str) -> "Response":
        return cls.json({"ok": False, "error": message}, status)

    def header_items(self) -> dict[str, str]:
        items = {
            "Content-Type": self.content_type,
            "Content-Length": str(len(self.body)),
            "Cache-Control": "no-store",
        }
        items.update(self.extra)
        return items

    def stream_to(self, wfile: Any) -> None:
        if self.body:
            wfile.write(self.body)


class StreamResponse(Response):
    """A response whose body is produced lazily: SSE, or a file Range.

    Headers are whatever the caller supplies. SSE deliberately omits
    Content-Length (it is open-ended); a ranged file response must include it,
    otherwise the browser cannot tell where the slice ends.
    """

    def __init__(self, generator: Any, content_type: str,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(200, b"", content_type, headers)
        self.generator = generator

    def header_items(self) -> dict[str, str]:
        items = {"Content-Type": self.content_type, "Cache-Control": "no-store"}
        items.update(self.extra)
        return items

    def stream_to(self, wfile: Any) -> None:
        try:
            for chunk in self.generator:
                try:
                    wfile.write(chunk)
                    wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
        finally:
            close = getattr(self.generator, "close", None)
            if callable(close):
                close()


class Broadcaster:
    """Fan-out of capture events to every connected SSE client."""

    def __init__(self) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.add(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(channel)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for channel in targets:
            try:
                channel.put_nowait(event)
            except queue.Full:
                pass  # a stalled client must never block capture


# Where a captured media record came from. The Finder feed APIs hand back whole
# lists, so most entries are prefetched rather than opened by the user; the UI
# uses this to keep the list honest.
SOURCE_KINDS = {
    "/cgi-bin/micromsg-bin/pc_findergetcommentdetail": "opened",
    "FinderGetCommentDetail": "opened",
    "/cgi-bin/micromsg-bin/pc_finderuserpage": "profile",
    "/cgi-bin/micromsg-bin/pc_finderpcflow": "flow",
}


def source_kind(endpoint: str) -> str:
    return SOURCE_KINDS.get(endpoint, "other")


class Router:
    version = UI_VERSION

    def __init__(self) -> None:
        self.owner: Any = None
        self.broadcaster = Broadcaster()
        self.downloads: dl.DownloadManager | None = None
        self.live: live_recording.LiveRecordingManager | None = None
        self.live_preview: live_recording.LivePreviewManager | None = None

    # ------------------------------------------------------------- lifecycle
    def attach(self, owner: Any) -> None:
        self.owner = owner
        self.downloads = dl.DownloadManager(owner)
        self.live = live_recording.LiveRecordingManager(owner)
        self.live_preview = live_recording.LivePreviewManager(owner)
        owner.event_sinks.append(self._on_capture_event)

    def _on_capture_event(self, record: dict[str, Any]) -> None:
        """Translate a capture log record into a small SSE signal."""
        event = record.get("event")
        if event in {"media", "video_saved", "probe", "live"}:
            # The UI refetches the list; that keeps serialization in one place.
            payload = {"type": "videos_changed"}
            if event == "live":
                payload = {"type": "lives_changed"}
            elif event in {"media", "video_saved"}:
                payload["file_key"] = record.get("file_key")
        elif event == "hook_status":
            payload = {"type": "hook", "hook": {
                "bridgePresent": record.get("bridgePresent"),
                "wrapped": record.get("wrapped"),
                "hookCalls": record.get("hookCalls"),
                "media": record.get("media"),
                "reports": record.get("reports"),
                "queued": record.get("queued"),
                "errors": record.get("errors"),
            }}
        elif event == "download":
            payload = {"type": "download", "task": {k: v for k, v in record.items() if k != "event"}}
        elif event == "live_recording":
            payload = {"type": "live_recording", "task": {k: v for k, v in record.items() if k != "event"}}
        elif event == "session_start":
            payload = {"type": "session", "session": {k: v for k, v in record.items() if k != "event"}}
        elif event == "capture_status":
            payload = {"type": "service", "service": {
                "capture_enabled": record.get("capture_enabled"),
                "proxy_configured": record.get("proxy_configured"),
            }}
        else:
            return
        self.broadcaster.publish(payload)

    # ---------------------------------------------------------------- routing
    def handle(self, owner: Any, method: str, path: str, headers: dict[str, str],
               body: bytes = b"") -> Response | None:
        split = urlsplit(path)
        route = split.path
        query = parse_qs(split.query)

        if method in {"GET", "HEAD"}:
            if route == "/":
                return self._static("index.html")
            if route.startswith("/static/"):
                return self._static(route[len("/static/"):])
            if route == "/api/state":
                return Response.json(self.state())
            if route == "/api/videos":
                return Response.json({"videos": self.videos(), "ok": True})
            if route == "/api/lives":
                return Response.json({"lives": self.lives(), "ok": True})
            if route == "/api/tasks":
                return Response.json({"tasks": self.tasks(), "ok": True})
            if route == "/api/log":
                tail = min(int((query.get("tail") or ["200"])[0] or 200), MAX_LOG_TAIL)
                return Response.json({"records": dl.read_tail(owner.output_path, tail), "ok": True})
            if route == "/api/events":
                return StreamResponse(self._sse(), SSE_TYPE, {
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "close",
                    "X-Accel-Buffering": "no",
                })

            match = self._video_route(route)
            if match:
                file_key, action = match
                if action == "":
                    return self.video_detail(file_key)
                if action == "probe":
                    return Response.json({
                        "formats": self.downloads.probe(file_key, refresh=True), "ok": True,
                    })
                if action == "stream":
                    return self.stream(file_key, (query.get("variant") or [""])[0], headers)
                if action == "reveal":
                    return self.reveal(file_key, (query.get("variant") or [""])[0])
                if action == "open":
                    return self.open_file(file_key, (query.get("variant") or [""])[0])

            live_match = self._live_recording_route(route)
            if live_match:
                task_id, action = live_match
                if action == "stream":
                    return self.live_stream(task_id, headers)
                if action == "reveal":
                    return self.live_reveal(task_id)
                if action == "open":
                    return self.live_open(task_id)

            live_match = self._live_route(route)
            if live_match:
                live_id, action = live_match
                if action == "preview/index.m3u8":
                    return self.live_preview_playlist(live_id)
                if action.startswith("preview/"):
                    return self.live_preview_segment(live_id, action[len("preview/"):])

        if method == "POST":
            match = self._video_route(route)
            if match:
                file_key, action = match
                if action == "download":
                    return self.download(file_key, body)
                if action in {"reveal", "open"}:
                    return Response.error(405, "use GET for this action")
            live_match = self._live_route(route)
            if live_match:
                live_id, action = live_match
                if action == "record":
                    return self.record_live(live_id)
                if action == "stop":
                    return self.stop_live(live_id)
                if action in {"reveal", "open"}:
                    return Response.error(405, "use GET for this action")
            if route == "/api/selftest":
                return self.selftest()
            if route == "/api/service":
                return self.set_service(body)
        return None

    @staticmethod
    def _video_route(route: str) -> tuple[str, str] | None:
        prefix = "/api/videos/"
        if not route.startswith(prefix):
            return None
        rest = route[len(prefix):]
        parts = rest.split("/", 1)
        file_key = unquote(parts[0])
        action = parts[1] if len(parts) > 1 else ""
        return (file_key, action) if file_key else None

    @staticmethod
    def _live_route(route: str) -> tuple[str, str] | None:
        prefix = "/api/lives/"
        if not route.startswith(prefix):
            return None
        rest = route[len(prefix):]
        parts = rest.split("/", 1)
        live_id = unquote(parts[0])
        action = parts[1] if len(parts) > 1 else ""
        return (live_id, action) if live_id else None

    @staticmethod
    def _live_recording_route(route: str) -> tuple[str, str] | None:
        prefix = "/api/live-recordings/"
        if not route.startswith(prefix):
            return None
        rest = route[len(prefix):]
        parts = rest.split("/", 1)
        task_id = unquote(parts[0])
        action = parts[1] if len(parts) > 1 else ""
        return (task_id, action) if task_id else None

    # ------------------------------------------------------------- static UI
    def _static(self, relative: str) -> Response:
        target = (STATIC_DIR / relative).resolve()
        if not str(target).startswith(str(STATIC_DIR)) or not target.is_file():
            return Response.error(404, "not found")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript"}:
            content_type += "; charset=utf-8"
        return Response(200, target.read_bytes(), content_type)

    # ------------------------------------------------------------ API: read
    def state(self) -> dict[str, Any]:
        owner = self.owner
        proxy_manager = owner.proxy_manager
        with owner.state_lock:
            counters = dict(owner.counters)
        return {
            "ok": True,
            "session": {
                "capture_file": str(owner.output_path),
                "video_dir": str(owner.video_dir),
                "save_video": owner.save_video,
                "auto_fetch": owner.auto_fetch,
                "auto_decrypt": owner.auto_decrypt,
                "link_ttl_minutes": owner.link_ttl_minutes,
                "proxy_port": owner.proxy_port,
                "proxy_configured": bool(proxy_manager and proxy_manager.changed),
                "proxy_service": proxy_manager.service if proxy_manager else "",
                "capture_enabled": owner.capture_enabled,
                "ffmpeg_available": live_recording.ffmpeg_path() is not None,
            },
            "counters": counters,
            "ui_version": UI_VERSION,
            "js_seen": owner.js_seen,
            "html_injections": owner.html_injections,
            "ui_clients": self.broadcaster.count,
        }

    def _variant_files(self, file_key: str) -> dict[str, dict[str, Any]]:
        """Scan the output directory for decoded variants of one video."""
        found: dict[str, dict[str, Any]] = {}
        directory: Path = self.owner.video_dir
        if not directory.exists():
            return found
        for path in directory.glob(f"{file_key}*.json"):
            stem = path.name[: -len(".json")]
            if not stem.startswith(file_key):
                continue
            variant = stem[len(file_key):].lstrip(".") or "client"
            try:
                sidecar = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            decoded = sidecar.get("decrypted_path")
            if decoded and not Path(decoded).exists():
                decoded = None
            decoded_path = Path(decoded) if decoded else None
            found[variant] = {
                "variant": variant,
                "decoded": bool(sidecar.get("decoded")) and bool(decoded),
                "bytes": sidecar.get("bytes"),
                "size": decoded_path.stat().st_size if decoded_path else None,
                "modified": decoded_path.stat().st_mtime if decoded_path else None,
                "filename": decoded_path.name if decoded_path else "",
                "directory": str(decoded_path.parent) if decoded_path else "",
                "decodeKey": sidecar.get("decodeKey"),
                "path": decoded,
                "encrypted_path": sidecar.get("encrypted_path"),
                "candidate_keys": sidecar.get("candidate_keys") or [],
            }
        return found

    def videos(self) -> list[dict[str, Any]]:
        owner = self.owner
        now = time.time()
        ttl = max(1, owner.link_ttl_minutes) * 60
        out: list[dict[str, Any]] = []
        with owner.state_lock:
            media_snapshot = {
                file_key: list(medias)
                for file_key, medias in owner.media_by_file_key.items()
            }
            signed_at_snapshot = dict(owner.signed_at)
            client_formats = dict(owner.client_formats)
            observed_downloads = set(owner.observed_downloads)
            viewing_objects = dict(owner.viewing_objects)
        for file_key, medias in media_snapshot.items():
            # URL / size / spec come from the newest capture (that is the link
            # that still works); titles come from whichever record has them.
            fresh = max(medias, key=owner._signed_at) if medias else {}
            context = self._richest(medias)
            media = {**context, **{k: v for k, v in fresh.items() if v}}
            signed_at = max(
                signed_at_snapshot.get(file_key) or 0.0,
                owner._signed_at(fresh) if fresh else 0.0,
            ) or now
            age = max(0.0, now - signed_at)
            variants = self._variant_files(file_key)
            downloaded = {v for v, info in variants.items() if info["decoded"]}
            client_format = client_formats.get(file_key, "")
            endpoint = media.get("endpoint") or media.get("source") or ""
            # "Played" means the client pulled bytes for this file key. Source
            # alone is not enough: a video opened from the recommendation flow
            # can have its only media record arrive in a prefetch response.
            client_downloaded = file_key in observed_downloads or bool(client_format)
            # Watching is judged by comment activity on the object, not by bytes:
            # the client fully downloads the *next* video to autoplay it, so a
            # download alone cannot tell "watched" from "prefetched ahead".
            evidence = next(
                (viewing_objects[str(m["objectId"])]
                 for m in medias if str(m.get("objectId") or "") in viewing_objects),
                "",
            )
            watched = bool(evidence)
            out.append({
                "file_key": file_key,
                "title": media.get("title") or "",
                "author": media.get("author") or "",
                "objectId": media.get("objectId") or "",
                "decodeKeys": [str(m.get("decodeKey")) for m in medias],
                "urlToken": bool(media.get("urlToken")),
                "source": endpoint,
                "source_kind": source_kind(endpoint),
                "fileSize": media.get("fileSize"),
                "width": media.get("width"),
                "height": media.get("height"),
                "spec": media.get("spec") or [],
                "client_format": client_format,
                "watched": watched,
                # Which Finder call made us consider it watched. Shown in the
                # UI so a wrong judgement can be traced instead of guessed at.
                "watch_evidence": evidence,
                "client_downloaded": client_downloaded,
                # kept for the UI badge: did the client pull bytes at all
                "played": client_downloaded or bool(downloaded),
                "signed_at": signed_at,
                "link_age_seconds": round(age),
                "link_expired": age > ttl,
                "link_ttl_seconds": ttl,
                "formats": self.downloads.cached_probe(file_key) or [],
                "variants": list(variants.values()),
                "downloaded": sorted(downloaded),
                "tasks": self.downloads.for_file(file_key),
            })
        # Played/downloaded first, then the freshest links.
        out.sort(key=lambda item: (item["played"], item["signed_at"]), reverse=True)
        return out

    @staticmethod
    def _richest(medias: list[dict[str, Any]]) -> dict[str, Any]:
        """The record carrying the most context — titles can arrive on a later one."""
        def score(media: dict[str, Any]) -> tuple[int, float]:
            context = sum(1 for key in ("title", "author", "spec", "fileSize") if media.get(key))
            return (context, float(media.get("publishTime") or 0))

        return max(medias, key=score) if medias else {}

    def video_detail(self, file_key: str) -> Response:
        for item in self.videos():
            if item["file_key"] == file_key:
                return Response.json({"video": item, "ok": True})
        return Response.error(404, "unknown file key")

    def tasks(self) -> list[dict[str, Any]]:
        return self.downloads.all()

    def lives(self) -> list[dict[str, Any]]:
        owner = self.owner
        with owner.state_lock:
            lives = [dict(item) for item in owner.live_by_id.values()]
            current_live_id = owner.current_live_id
        tasks_by_live: dict[str, list[dict[str, Any]]] = {}
        for task in self.live.all():
            tasks_by_live.setdefault(task["live_id"], []).append(task)

        visible: dict[str, dict[str, Any]] = {}
        for item in lives:
            item["is_current"] = item["live_id"] == current_live_id
            if item["is_current"]:
                visible[item["live_id"]] = item

        for live_id, tasks in tasks_by_live.items():
            latest = tasks[-1]
            item = visible.get(live_id)
            if item is None:
                item = {
                    "live_id": live_id,
                    "title": latest.get("title") or "",
                    "author": latest.get("author") or "",
                    "url": "",
                    "source": "",
                    "endpoint": "",
                    "start_time": 0,
                    "captured_at": latest.get("started"),
                    "is_current": live_id == current_live_id,
                }
                visible[live_id] = item
            item["task"] = latest

        result = list(visible.values())
        result.sort(
            key=lambda item: (
                bool(item.get("is_current")),
                item.get("captured_at")
                or (item.get("task") or {}).get("started")
                or 0,
            ),
            reverse=True,
        )
        return result

    # ------------------------------------------------------------ API: files
    def stream(self, file_key: str, variant: str, headers: dict[str, str]) -> Response:
        name = f"{file_key}.{variant}.mp4" if variant else f"{file_key}.mp4"
        path = self.owner.video_dir / name
        if not path.is_file():
            return Response.error(404, "not downloaded yet")
        range_header = headers.get("Range") or headers.get("range")
        result = dl.serve_range(path, range_header)
        content_type = result.headers.pop("Content-Type", "video/mp4")

        def body() -> Any:
            for chunk in result.chunks():
                yield chunk

        response = StreamResponse(body(), content_type, result.headers)
        response.status = result.status
        return response

    def _decoded_variant_path(self, file_key: str, variant: str) -> Path | None:
        variants = self._variant_files(file_key)
        info = variants.get(variant or "client")
        if not info or not info.get("decoded"):
            decoded = [item for item in variants.values() if item.get("decoded")]
            info = decoded[0] if decoded else None
        if not info or not info.get("decoded"):
            return None
        path = Path(str(info.get("path") or ""))
        if not path.is_file():
            return None
        root = self.owner.video_dir.resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            return None
        return resolved

    def reveal(self, file_key: str, variant: str) -> Response:
        path = self._decoded_variant_path(file_key, variant)
        if path is None:
            return Response.error(404, "not downloaded yet")
        try:
            result = subprocess.run(
                ["open", "-R", str(path)],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Response.error(500, f"could not open Finder: {exc}")
        if result.returncode != 0:
            return Response.error(500, result.stderr.strip() or "could not open Finder")
        return Response.json({"ok": True, "path": str(path)})

    def open_file(self, file_key: str, variant: str) -> Response:
        path = self._decoded_variant_path(file_key, variant)
        if path is None:
            return Response.error(404, "not downloaded yet")
        try:
            result = subprocess.run(
                ["open", str(path)],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Response.error(500, f"could not open video: {exc}")
        if result.returncode != 0:
            return Response.error(500, result.stderr.strip() or "could not open video")
        return Response.json({"ok": True, "path": str(path)})

    def live_stream(self, task_id: str, headers: dict[str, str]) -> Response:
        path = self._live_output_path(task_id)
        if path is None:
            return Response.error(404, "live recording not found")
        range_header = headers.get("Range") or headers.get("range")
        result = dl.serve_range(path, range_header)
        content_type = result.headers.pop("Content-Type", "video/mp4")

        def body() -> Any:
            for chunk in result.chunks():
                yield chunk

        response = StreamResponse(body(), content_type, result.headers)
        response.status = result.status
        return response

    def live_preview_playlist(self, live_id: str) -> Response:
        with self.owner.state_lock:
            live = dict(self.owner.live_by_id.get(live_id) or {})
        url = str(live.get("url") or "")
        if not live or not url:
            return Response.error(404, "live stream not found")
        try:
            playlist = self.live_preview.playlist(live_id, url)
        except RuntimeError as exc:
            return Response.error(500, str(exc))
        return Response(
            200,
            playlist.read_bytes(),
            "application/vnd.apple.mpegurl",
            {"Cache-Control": "no-store, no-transform"},
        )

    def live_preview_segment(self, live_id: str, relative: str) -> Response:
        found = self.live_preview.file(live_id, relative)
        if found is None:
            return Response.error(404, "live preview segment not found")
        path, content_type = found
        return Response(200, path.read_bytes(), content_type)

    def _live_output_path(self, task_id: str) -> Path | None:
        task = self.live.get(task_id)
        if task is None or not task.output:
            return None
        path = Path(task.output)
        root = self.owner.video_dir.resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        return resolved

    def live_reveal(self, task_id: str) -> Response:
        path = self._live_output_path(task_id)
        if path is None:
            return Response.error(404, "live recording not found")
        try:
            result = subprocess.run(
                ["open", "-R", str(path)],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Response.error(500, f"could not open Finder: {exc}")
        if result.returncode != 0:
            return Response.error(500, result.stderr.strip() or "could not open Finder")
        return Response.json({"ok": True, "path": str(path)})

    def live_open(self, task_id: str) -> Response:
        path = self._live_output_path(task_id)
        if path is None:
            return Response.error(404, "live recording not found")
        try:
            result = subprocess.run(
                ["open", str(path)],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Response.error(500, f"could not open video: {exc}")
        if result.returncode != 0:
            return Response.error(500, result.stderr.strip() or "could not open video")
        return Response.json({"ok": True, "path": str(path)})

    # ----------------------------------------------------------- API: write
    def download(self, file_key: str, body: bytes) -> Response:
        payload: dict[str, Any] = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return Response.error(400, "invalid json body")
        quality = str(payload.get("quality") or "best")
        try:
            task = self.downloads.start(file_key, quality)
        except KeyError as exc:
            return Response.error(404, str(exc))
        return Response.json({"ok": True, "task": task.as_dict()})

    def record_live(self, live_id: str) -> Response:
        try:
            task = self.live.start(live_id)
        except KeyError as exc:
            return Response.error(404, str(exc))
        except RuntimeError as exc:
            return Response.error(500, str(exc))
        return Response.json({"ok": True, "task": task.as_dict()})

    def stop_live(self, live_id: str) -> Response:
        task = self.live.stop(live_id)
        if task is None:
            return Response.error(404, "no active recording for this live stream")
        return Response.json({"ok": True, "task": task.as_dict()})

    def set_service(self, body: bytes) -> Response:
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response.error(400, "invalid json body")
        if "enabled" not in payload:
            return Response.error(400, "enabled is required")

        enabled = bool(payload["enabled"])
        try:
            self.owner.set_capture_enabled(enabled)
        except Exception as exc:
            return Response.error(500, f"could not change capture service: {exc}")

        manager = self.owner.proxy_manager
        return Response.json({
            "ok": True,
            "service": {
                "capture_enabled": self.owner.capture_enabled,
                "proxy_configured": bool(manager and manager.changed),
            },
        })

    def selftest(self) -> Response:
        decryptor = self.owner.decryptor
        if not decryptor.exists():
            return Response.error(500, "decryptor not found")
        node = os.environ.get("WX_NODE_PATH") or shutil.which("node")
        if not node:
            for candidate in (
                "/opt/homebrew/bin/node",
                "/usr/local/bin/node",
                os.path.expanduser("~/.local/bin/node"),
            ):
                if os.path.isfile(candidate):
                    node = candidate
                    break
        if not node:
            return Response.error(500, "node is unavailable")
        try:
            result = subprocess.run(
                [node, str(decryptor), "selftest", "--json"],
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Response.error(500, f"could not run selftest: {exc}")
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            payload = {"ok": False, "raw": result.stdout[-500:], "stderr": result.stderr[-500:]}
        payload["returncode"] = result.returncode
        return Response.json(payload, 200 if result.returncode == 0 else 500)

    # ------------------------------------------------------------------ SSE
    def _sse(self) -> Any:
        channel = self.broadcaster.subscribe()

        def generate() -> Any:
            try:
                yield _sse_frame({"type": "hello", "now": time.time(), "version": UI_VERSION})
                while True:
                    try:
                        event = channel.get(timeout=SSE_HEARTBEAT)
                    except queue.Empty:
                        yield b": heartbeat\n\n"
                        continue
                    yield _sse_frame(event)
            finally:
                self.broadcaster.unsubscribe(channel)

        return generate()


def _sse_frame(payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"data: {data}\n\n".encode("utf-8")

#!/usr/bin/env python3
"""Standalone launcher for the WeChat Channels downloader."""

from __future__ import annotations

import atexit
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import webview


CAPTURE_PORT = 18765
PROXY_PORT = 9090
APP_NAME = "WeChatDownloader"


def allow_autoplay_in_wkwebview() -> None:
    """Let the live preview start without a user gesture.

    pywebview leaves WKWebView's default media policy in place, which can pause
    muted autoplay. The live preview is local and user-initiated by opening a
    live room, so programmatic playback is safe here.
    """
    try:
        from webview.platforms import cocoa
        original_webkit = cocoa.WebKit
        original_configuration = original_webkit.WKWebViewConfiguration

        class ConfigurationBuilder:
            def __init__(self, configuration):
                self.configuration = configuration

            def init(self):
                return self.configuration

        class AutoplayConfiguration:
            @classmethod
            def alloc(cls):
                configuration = original_configuration.alloc().init()
                configuration.setMediaTypesRequiringUserActionForPlayback_(0)
                return ConfigurationBuilder(configuration)

        class WebKitProxy:
            def __init__(self, original):
                self.original = original
                self.WKWebViewConfiguration = AutoplayConfiguration

            def __getattr__(self, name):
                return getattr(self.original, name)

        original_init = cocoa.BrowserView.__init__

        def autoplay_init(self, window):
            cocoa.WebKit = WebKitProxy(original_webkit)
            try:
                original_init(self, window)
            finally:
                cocoa.WebKit = original_webkit

        cocoa.BrowserView.__init__ = autoplay_init
    except Exception:
        pass


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


HOME = Path.home()
APP_SUPPORT = HOME / "Library" / "Application Support" / "OpenXiaZai"
CAPTURE_FILE = APP_SUPPORT / "wechat-captures.jsonl"
CAPTURE_TOKEN_PATH = APP_SUPPORT / "wechat_capture_token"
WASM_DIR = APP_SUPPORT / "vendor" / "wechat_files" / "raw"
VIDEO_DIR = HOME / "Downloads" / "OpenXiaZai" / "WeChatChannels"
LOG_PATH = APP_SUPPORT / "wechat_downloader.log"


def find_binary(name: str) -> str | None:
    candidates = []
    local = resource_path(name)
    candidates.append(local)
    candidates.append(Path("/opt/homebrew/bin") / name)
    candidates.append(Path("/usr/local/bin") / name)
    candidates.append(HOME / ".local" / "bin" / name)
    which = shutil.which(name)
    if which:
        candidates.insert(0, Path(which))

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def wasm_ready() -> bool:
    required = ("wasm_video_decode.js", "wasm_video_decode.wasm")
    return all((WASM_DIR / name).is_file() for name in required)


def load_capture_token() -> str:
    """Keep the page-report token stable across downloader restarts."""
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    try:
        token = CAPTURE_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""

    if len(token) >= 32:
        CAPTURE_TOKEN_PATH.chmod(0o600)
        return token

    token = secrets.token_urlsafe(24)
    descriptor = os.open(
        str(CAPTURE_TOKEN_PATH),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token)
    CAPTURE_TOKEN_PATH.chmod(0o600)
    return token


def ensure_wasm(node: str) -> tuple[bool, str]:
    if wasm_ready():
        return True, ""
    fetcher = resource_path("wechat_capture/decrypt/fetch_wasm_assets.mjs")
    if not fetcher.is_file():
        return False, "缺少微信解密组件下载脚本"
    env = os.environ.copy()
    env["WX_WASM_ASSETS"] = str(WASM_DIR)
    try:
        result = subprocess.run(
            [node, str(fetcher)],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "下载微信解密组件超时"
    except Exception as exc:
        return False, f"下载微信解密组件失败：{exc}"
    if result.returncode != 0 or not wasm_ready():
        detail = (result.stderr or result.stdout or "").strip()[-800:]
        return False, f"下载微信解密组件失败：{detail or '未知错误'}"
    return True, ""


def service_ready() -> bool:
    """Only accept our own capture API, not any process listening on the port."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{CAPTURE_PORT}/api/state",
            timeout=0.5,
        ) as response:
            payload = json.load(response)
        return bool(payload.get("ok") and payload.get("session"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def start_capture() -> tuple[subprocess.Popen | None, str]:
    if service_ready():
        return None, ""

    mitmdump = find_binary("mitmdump")
    node = find_binary("node")
    ffmpeg = find_binary("ffmpeg")
    missing = []
    if not mitmdump:
        missing.append("mitmproxy")
    if not node:
        missing.append("Node.js")
    if missing:
        return None, "缺少：" + "、".join(missing) + "。请先执行 brew install mitmproxy node"

    ok, error = ensure_wasm(node)
    if not ok:
        return None, error
    if not port_available(CAPTURE_PORT):
        return None, f"本地端口 {CAPTURE_PORT} 被占用"
    if not port_available(PROXY_PORT):
        return None, f"本地代理端口 {PROXY_PORT} 被占用"

    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    capture_script = resource_path("wechat_capture/mitmproxy_wechat_capture.py")
    if not capture_script.is_file():
        return None, "应用包内缺少微信捕获脚本"

    env = os.environ.copy()
    env.update(
        {
            "WX_CAPTURE_FILE": str(CAPTURE_FILE),
            "WX_CAPTURE_TOKEN": load_capture_token(),
            "WX_VIDEO_DIR": str(VIDEO_DIR),
            "WX_SAVE_VIDEO": "off",
            "WX_AUTO_FETCH": "0",
            "WX_AUTO_DECRYPT": "1",
            "WX_NODE_PATH": node,
            "WX_FFMPEG_PATH": ffmpeg or "",
            "WX_CAPTURE_HOST": "127.0.0.1",
            "WX_CAPTURE_PORT": str(CAPTURE_PORT),
            "WX_PROXY_PORT": str(PROXY_PORT),
            "WX_WASM_ASSETS": str(WASM_DIR),
        }
    )
    env["PATH"] = os.pathsep.join(
        [os.path.dirname(node), os.path.dirname(mitmdump), env.get("PATH", "")]
    )

    with LOG_PATH.open("ab", buffering=0) as log_handle:
        proc = subprocess.Popen(
            [
                mitmdump,
                "-s",
                str(capture_script),
                "--listen-host",
                "127.0.0.1",
                "--listen-port",
                str(PROXY_PORT),
            ],
            cwd=str(resource_path(".")),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            return None, "捕获进程启动失败，请查看日志：" + str(LOG_PATH)
        if service_ready():
            return proc, ""
        time.sleep(0.25)
    stop_process(proc)
    return None, "捕获服务启动超时，请查看日志：" + str(LOG_PATH)


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def error_window(message: str) -> None:
    html = f"""
    <!doctype html>
    <html><head><meta charset="utf-8"><title>{APP_NAME}</title>
    <style>
      body {{ margin: 0; font: 15px -apple-system, BlinkMacSystemFont, sans-serif;
             color: #1f2937; display: grid; place-items: center; height: 100vh;
             background: #f6f8f7; }}
      .box {{ max-width: 520px; padding: 28px; background: white; border-radius: 10px;
              box-shadow: 0 8px 28px rgba(15, 42, 28, .12); }}
      h1 {{ margin: 0 0 12px; font-size: 20px; }} p {{ margin: 0; line-height: 1.6; }}
      code {{ display: block; margin-top: 14px; padding: 10px; border-radius: 6px;
              background: #eef2ef; font-size: 12px; word-break: break-all; }}
    </style></head><body><div class="box">
      <h1>无法启动微信视频号下载</h1><p>{message}</p>
      <code>{LOG_PATH}</code>
    </div></body></html>
    """
    webview.create_window(APP_NAME, html=html, width=620, height=380)
    webview.start()


def main() -> int:
    if "--check" in sys.argv:
        print(f"service_ready={service_ready()}")
        print(f"mitmdump={find_binary('mitmdump') or ''}")
        print(f"node={find_binary('node') or ''}")
        print(f"ffmpeg={find_binary('ffmpeg') or ''}")
        print(f"wasm_ready={wasm_ready()}")
        return 0 if service_ready() else 1

    proc, error = start_capture()
    if error:
        error_window(error)
        return 1

    atexit.register(lambda: stop_process(proc))

    def handle_signal(_signum, _frame):
        stop_process(proc)
        os._exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    allow_autoplay_in_wkwebview()
    webview.create_window(
        APP_NAME,
        f"http://127.0.0.1:{CAPTURE_PORT}/",
        width=1120,
        height=780,
        min_size=(900, 600),
    )

    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

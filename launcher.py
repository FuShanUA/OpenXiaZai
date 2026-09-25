"""桌面应用启动器:用原生窗口(WKWebView)承载 Flask + aria2c 后端。

双击运行即弹出一个独立的桌面窗口,无需浏览器。
"""
import os
import sys
import time
import tempfile
import threading
import atexit
import signal

# 确保能 import 同目录的 app 模块(打包后同样生效)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows + console=False: stdout/stderr are None, redirect to log file to prevent crashes
if sys.platform == "win32" and sys.stdout is None:
    _log_dir = tempfile.gettempdir()
    sys.stdout = open(os.path.join(_log_dir, "openxiazai_stdout.log"), "w", encoding="utf-8")
    sys.stderr = open(os.path.join(_log_dir, "openxiazai_stderr.log"), "w", encoding="utf-8")

import webview
from app import app, engine, DEFAULT_SAVE

# Capture any uncaught exceptions so the window doesn't silently vanish
import faulthandler, traceback as _tb
faulthandler.enable()
_orig_excepthook = sys.excepthook
_CRASH_LOG = os.path.join(tempfile.gettempdir(), "openxiazai_crash.log")
def _excepthook(t, v, tb):
    msg = "".join(_tb.format_exception(t, v, tb))
    try:
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    _orig_excepthook(t, v, tb)
sys.excepthook = _excepthook

PORT = 5566


def _app_icon():
    """Use the project icon on platforms without a bundled app icon."""
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        return None

    root = os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "win32":
        path = os.path.join(root, "assets", "icon_256.png")
    else:
        return None
    return path if os.path.isfile(path) else None


def _serve():
    """在 daemon 线程里跑 Flask 服务。"""
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", PORT, app, threaded=True)
    server.serve_forever()


def _cleanup():
    try:
        engine.shutdown()
    except Exception:
        pass


def _signal_cleanup(signum, _frame):
    _cleanup()
    os._exit(0)


def _wait_ready(timeout=20):
    import requests
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(f"http://127.0.0.1:{PORT}/", timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def main():
    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, _signal_cleanup)
    signal.signal(signal.SIGINT, _signal_cleanup)
    os.makedirs(DEFAULT_SAVE, exist_ok=True)
    threading.Thread(target=_serve, daemon=True).start()
    if not _wait_ready():
        print("后端启动失败,请检查 aria2c 是否已安装。")
        return
    webview.create_window(
        "OpenXiaZai",
        f"http://127.0.0.1:{PORT}/",
        width=1120,
        height=780,
        min_size=(900, 600),
    )
    if sys.platform == "darwin":
        # macOS already uses CFBundleIconFile; setting it again replaces the
        # correctly sized Dock icon with a raw runtime image.
        webview.start()
    else:
        webview.start(icon=_app_icon())


if __name__ == "__main__":
    main()

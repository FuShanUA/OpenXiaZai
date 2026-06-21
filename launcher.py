"""桌面应用启动器:用原生窗口(WKWebView)承载 Flask + aria2c 后端。

双击运行即弹出一个独立的桌面窗口,无需浏览器。
"""
import os
import sys
import time
import threading

# 确保能 import 同目录的 app 模块(打包后同样生效)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import webview
from app import app, engine, DEFAULT_SAVE

# Capture any uncaught exceptions so the window doesn't silently vanish
import faulthandler, traceback as _tb
faulthandler.enable()
_orig_excepthook = sys.excepthook
def _excepthook(t, v, tb):
    msg = "".join(_tb.format_exception(t, v, tb))
    with open("/tmp/launcher_crash.log", "a") as f:
        f.write(msg + "\n")
    _orig_excepthook(t, v, tb)
sys.excepthook = _excepthook

PORT = 5566


def _serve():
    """在 daemon 线程里跑 Flask 服务。"""
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", PORT, app, threaded=True)
    server.serve_forever()


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
    webview.start()
    # 用户关闭窗口后,清理 aria2c 子进程
    try:
        engine.proc.terminate()
        engine.proc.wait(timeout=5)
    except Exception:
        pass


if __name__ == "__main__":
    main()

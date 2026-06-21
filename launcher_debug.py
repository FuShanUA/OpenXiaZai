"""带调试信息的启动器"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import webview
from app import app, engine, DEFAULT_SAVE

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
    print("="*60)
    print("DEBUG: Starting application...")
    print("="*60)
    
    os.makedirs(DEFAULT_SAVE, exist_ok=True)
    print(f"DEBUG: Default save path: {DEFAULT_SAVE}")
    
    threading.Thread(target=_serve, daemon=True).start()
    print("DEBUG: Flask server thread started")
    
    if not _wait_ready():
        print("ERROR: Backend failed to start. Check if aria2c is installed.")
        return
    
    print("DEBUG: Backend ready!")
    
    try:
        print(f"DEBUG: Creating window with URL http://127.0.0.1:{PORT}/")
        window = webview.create_window(
            "磁力 / P2P 下载器",
            f"http://127.0.0.1:{PORT}/",
            width=1120,
            height=780,
            min_size=(900, 600),
        )
        print("DEBUG: Window created, starting webview...")
        webview.start()
        print("DEBUG: webview started successfully")
    except Exception as e:
        print(f"ERROR: Failed to create/start window: {e}")
        import traceback
        traceback.print_exc()
        with open("/tmp/launcher_crash.log", "a") as f:
            f.write(f"Window error: {e}\n")
            f.write("".join(_tb.format_exception(*sys.exc_info())) + "\n")
    
    try:
        engine.proc.terminate()
        engine.proc.wait(timeout=5)
    except Exception:
        pass
    print("DEBUG: Application closed")

if __name__ == "__main__":
    main()

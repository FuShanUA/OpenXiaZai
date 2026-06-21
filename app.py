import os
import json
import time
import shutil
import base64
import subprocess
import re
import threading
import requests
from flask import Flask, request, jsonify, render_template

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SAVE = os.path.expanduser("~/Downloads/OpenXiaZai")
RECORDS_FILE = os.path.join(BASE_DIR, "records.json")

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"),
            static_folder=os.path.join(BASE_DIR, "static"))


# --------------------------------------------------------------------------- #
#  Link classification
# --------------------------------------------------------------------------- #
def classify(url):
    u = url.strip().lower()
    if u.startswith("magnet:"):
        return "torrent"
    if u.startswith(("http://", "https://")):
        if "pan.quark.cn" in u or "quark.cn" in u:
            return "quark"
        if any(h in u for h in ("pan.baidu.com", "115.com", "aliyundrive", "alipan.com")):
            return "cloud"
        return "http"
    if u.startswith(("ftp://", "ftps://", "sftp://")):
        return "ftp"
    if u.startswith("ed2k://"):
        return "ed2k"
    if u.startswith("thunder://"):
        return "thunder"
    return "http"  # default: treat as direct link


def _extract_title(html):
    """从 HTML 中提取页面标题。"""
    # og:title
    match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html, re.IGNORECASE)
    if match:
        title = match.group(1)
        for suffix in [' - 磁力熊', ' - YouTube', ' - Bilibili', ' - 哔哩哔哩', ' | Facebook', ' - 腾讯视频']:
            title = title.replace(suffix, '')
        return title.strip()
    # <title> tag
    match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
    if match:
        title = match.group(1)
        for suffix in [' - 磁力熊', ' - YouTube', ' - Bilibili', ' - 哔哩哔哩', ' - 腾讯视频']:
            title = title.replace(suffix, '')
        return title.strip()
    # <h1> tag
    match = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.IGNORECASE)
    if match:
        return re.sub(r'<[^>]+>', '', match.group(1)).strip()
    return ""


def _check_direct_video_url(url):
    """检查 URL 本身是否直接指向视频文件。"""
    lower = url.lower()
    # m3u8/HLS 播放列表
    if lower.endswith('.m3u8') or '.m3u8?' in lower:
        name = url.split('/')[-1].split('?')[0]
        return {"ok": True, "title": name, "m3u8_url": url, "poster": "", "magnet": "", "type": "m3u8"}
    # 直接视频文件
    for ext in ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm', '.ts', '.m4v', '.wmv', '.mpg', '.mpeg'):
        if ext in lower and (lower.endswith(ext) or ext + '?' in lower):
            name = url.split('/')[-1].split('?')[0]
            return {"ok": True, "title": name, "m3u8_url": url, "poster": "", "magnet": "", "type": "direct"}
    return None


def _extract_from_video_tag(html, base_url):
    """从 HTML 的 <video> / <source> 标签中提取视频地址。"""
    # <video src="...">
    for m in re.finditer(r'<video[^>]+src="([^"]+)"', html, re.IGNORECASE):
        return {"video_url": m.group(1), "title": _extract_title(html)}
    for m in re.finditer(r"<video[^>]+src='([^']+)'", html, re.IGNORECASE):
        return {"video_url": m.group(1), "title": _extract_title(html)}
    # <source src="...">
    for m in re.finditer(r'<source[^>]+src="([^"]+)"', html, re.IGNORECASE):
        return {"video_url": m.group(1), "title": _extract_title(html)}
    for m in re.finditer(r"<source[^>]+src='([^']+)'", html, re.IGNORECASE):
        return {"video_url": m.group(1), "title": _extract_title(html)}
    return None


def _extract_from_js(html):
    """从 JavaScript 代码中提取视频地址（m3u8/mp4 等）。"""
    patterns = [
        # 常见变量名 → m3u8
        (r"(?:var|let|const)\s+vurl\s*=\s*'([^']+)'", 'm3u8'),
        (r'(?:var|let|const)\s+vurl\s*=\s*"([^"]+)"', 'm3u8'),
        (r"(?:var|let|const)\s+video_url\s*=\s*'([^']+)'", 'm3u8'),
        (r'(?:var|let|const)\s+video_url\s*=\s*"([^"]+)"', 'm3u8'),
        (r"(?:var|let|const)\s+player_url\s*=\s*'([^']+)'", 'm3u8'),
        (r'(?:var|let|const)\s+player_url\s*=\s*"([^"]+)"', 'm3u8'),
        (r"(?:var|let|const)\s+src\s*=\s*'([^']*\.(?:mp4|m3u8|mkv|flv|webm|ts)[^']*)'", 'direct'),
        (r'(?:var|let|const)\s+src\s*=\s*"([^"]*\.(?:mp4|m3u8|mkv|flv|webm|ts)[^"]*)"', 'direct'),
        (r"(?:var|let|const)\s+url\s*=\s*'([^']*\.(?:mp4|m3u8|mkv|flv|webm|ts)[^']*)'", 'direct'),
        (r'(?:var|let|const)\s+url\s*=\s*"([^"]*\.(?:mp4|m3u8|mkv|flv|webm|ts)[^"]*)"', 'direct'),
        # JSON 对象中的 url/src
        (r"['\"]url['\"]\s*:\s*'([^']*\.m3u8[^']*)'", 'm3u8'),
        (r'[\'"]url[\'"]\s*:\s*"([^"]*\.m3u8[^"]*)"', 'm3u8'),
        (r"['\"]src['\"]\s*:\s*'([^']*\.(?:mp4|m3u8|mkv|flv|webm|ts)[^']*)'", 'direct'),
        (r'[\'"]src[\'"]\s*:\s*"([^"]*\.(?:mp4|m3u8|mkv|flv|webm|ts)[^"]*)"', 'direct'),
        # 任意 https://...m3u8 字符串
        (r'"(https?://[^"]*\.m3u8[^"]*)"', 'm3u8'),
        (r"'(https?://[^']*\.m3u8[^']*)'", 'm3u8'),
    ]
    for pattern, vtype in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return {"video_url": match.group(1), "title": _extract_title(html), "type": vtype}
    return None


def _extract_from_iframe(html, base_url, headers):
    """从 iframe 中提取视频地址（跟踪一级 iframe）。"""
    iframe_srcs = re.findall(r'<iframe[^>]+src="([^"]+)"', html, re.IGNORECASE)
    iframe_srcs += re.findall(r"<iframe[^>]+src='([^']+)'", html, re.IGNORECASE)

    skip_domains = ('doubleclick', 'googlesyndication', 'googleads', 'facebook.com/plugins',
                    'platform.twitter', 'accounts.google', 'googletagmanager')

    for iframe_src in iframe_srcs:
        if any(s in iframe_src.lower() for s in skip_domains):
            continue
        iframe_url = iframe_src
        if not iframe_url.startswith('http'):
            from urllib.parse import urljoin
            iframe_url = urljoin(base_url, iframe_url)
        try:
            r = requests.get(iframe_url, headers={**headers, 'Referer': base_url}, timeout=10)
            r.encoding = 'utf-8'
            iframe_html = r.text

            # 在 iframe 页面中尝试所有策略
            result = _extract_from_video_tag(iframe_html, iframe_url)
            if result:
                return result

            result = _extract_from_js(iframe_html)
            if result:
                return result

            # 递归：iframe 中还有 iframe
            result = _extract_from_iframe(iframe_html, iframe_url, headers)
            if result:
                return result
        except Exception:
            continue
    return None


def _extract_from_meta(html):
    """从 meta 标签和结构化数据中提取视频地址。"""
    # og:video
    match = re.search(r'<meta\s+property="og:video(?::\w+)?"\s+content="([^"]+)"', html, re.IGNORECASE)
    if match:
        return {"video_url": match.group(1), "title": _extract_title(html)}

    # twitter:player
    match = re.search(r'<meta\s+name="twitter:player(?::\w+)?"\s+content="([^"]+)"', html, re.IGNORECASE)
    if match:
        return {"video_url": match.group(1), "title": _extract_title(html)}

    # JSON-LD VideoObject
    jsonld_blocks = re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.IGNORECASE | re.DOTALL)
    for block in jsonld_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, list):
                data = data[0]
            if isinstance(data, dict) and data.get('@type') == 'VideoObject':
                video_url = data.get('contentUrl') or data.get('embedUrl')
                if video_url:
                    title = data.get('name', '') or _extract_title(html)
                    return {"video_url": video_url, "title": title}
        except Exception:
            continue
    return None


def extract_video(url):
    """通用视频提取器：从任意 URL 中尝试提取可下载的视频地址。

    策略顺序：
    0. 检查 URL 本身是否是直接视频链接
    1. 从页面 <video> / <source> 标签提取
    2. 从 JavaScript 变量中提取
    3. 从 iframe 嵌入页面中提取（跟踪一级）
    4. 从 meta 标签 / JSON-LD 结构化数据中提取
    """
    from urllib.parse import urljoin

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    }

    try:
        # 策略 0: URL 本身是直接视频链接
        direct = _check_direct_video_url(url)
        if direct:
            return direct

        # 获取页面
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = 'utf-8'
        html = r.text

        # 提取公共元数据
        poster_match = re.search(r'<meta property="og:image" content="([^"]+)"', html)
        poster = poster_match.group(1) if poster_match else ""
        magnet_match = re.search(r'href="(magnet:\?xt=urn:btih:[^"]+)"', html)
        magnet = magnet_match.group(1) if magnet_match else ""

        # 策略 1: <video> / <source> 标签
        result = _extract_from_video_tag(html, url)
        if result:
            video_url = result["video_url"]
            if not video_url.startswith('http'):
                video_url = urljoin(url, video_url)
            vtype = "m3u8" if '.m3u8' in video_url else "direct"
            return {"ok": True, "title": result["title"], "poster": poster,
                    "m3u8_url": video_url, "magnet": magnet, "type": vtype}

        # 策略 2: JavaScript 变量
        result = _extract_from_js(html)
        if result:
            video_url = result["video_url"]
            if not video_url.startswith('http'):
                video_url = urljoin(url, video_url)
            vtype = result.get("type", "m3u8" if '.m3u8' in video_url else "direct")
            title = result.get("title", "") or _extract_title(html)
            return {"ok": True, "title": title, "poster": poster,
                    "m3u8_url": video_url, "magnet": magnet, "type": vtype}

        # 策略 3: iframe 嵌入页面
        result = _extract_from_iframe(html, url, headers)
        if result:
            video_url = result["video_url"]
            if not video_url.startswith('http'):
                video_url = urljoin(url, video_url)
            vtype = result.get("type", "m3u8" if '.m3u8' in video_url else "direct")
            title = _extract_title(html) or result.get("title", "")
            return {"ok": True, "title": title, "poster": poster,
                    "m3u8_url": video_url, "magnet": magnet, "type": vtype}

        # 策略 4: meta 标签 / JSON-LD
        result = _extract_from_meta(html)
        if result:
            video_url = result["video_url"]
            if not video_url.startswith('http'):
                video_url = urljoin(url, video_url)
            vtype = "m3u8" if '.m3u8' in video_url else "direct"
            return {"ok": True, "title": result.get("title", ""), "poster": poster,
                    "m3u8_url": video_url, "magnet": magnet, "type": vtype}

        return {"ok": False, "error": "未解析出可下载内容"}

    except requests.RequestException as e:
        return {"ok": False, "error": "未解析出可下载内容"}
    except Exception as e:
        return {"ok": False, "error": "未解析出可下载内容"}


TYPES = {
    "torrent": "种子/磁力",
    "http": "HTTP 直链",
    "ftp": "FTP",
    "ed2k": "电驴 eD2k",
    "quark": "夸克网盘",
    "cloud": "网盘链接",
    "thunder": "迅雷链接",
    "m3u8": "M3U8 流媒体",
    "direct": "直接视频",
}
UNSUPPORTED = {"ed2k", "quark", "cloud", "thunder"}
UNSUPPORTED_MSG = {
    "ed2k": "电驴(eD2k)链接需要专用的 eMule/aMule 客户端，本工具无法下载。可安装 aMule 后使用。",
    "quark": "夸克网盘链接需要先在浏览器中转存到自己的网盘，再获取直链下载。",
    "cloud": "网盘链接需要先在浏览器中转存，再获取直链下载。",
    "thunder": "迅雷链接请用迅雷客户端下载，或转换为磁力/直链后使用。",
}

# Public BitTorrent trackers to maximize peer discovery for magnet links
BT_TRACKERS = ",".join([
    # UDP trackers (fastest for magnet resolution)
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://tracker.bitsearch.to:1337/announce",
    "udp://tracker.auctor.tv:6969/announce",
    "udp://retracker.lanta-net.ru:2710/announce",
    "udp://retracker.netbynet.ru:2710/announce",
    "udp://opentracker.i2p.rocks:6969/announce",
    "udp://tracker.4.babico.name.tr:3131/announce",
    "udp://tracker.publictracker.xyz:6969/announce",
    "udp://tracker.skyts.net:6969/announce",
    "udp://p2p.publictracker.xyz:6969/announce",
    # HTTP/HTTPS trackers (fallback)
    "https://tracker.gbitt.info:443/announce",
    "https://tracker.lilithraws.org:443/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "wss://tracker.openwebtorrent.com:443/announce",
])

# DHT bootstrap nodes for fast initial peer discovery
DHT_ENTRY_POINTS = [
    "router.bittorrent.com:6881",
    "router.utorrent.com:6881",
    "dht.transmissionbt.com:6881",
    "dht.libtorrent.org:25401",
]

# --------------------------------------------------------------------------- #
#  aria2 RPC client
# --------------------------------------------------------------------------- #
class Aria2:
    def __init__(self, port=6800, secret="codex"):
        self.port = port
        self.secret = secret
        self.url = f"http://127.0.0.1:{port}/jsonrpc"

    def _call(self, method, params=None):
        payload = {"jsonrpc": "2.0", "id": "1", "method": method}
        if params is None:
            params = []
        if self.secret:
            params = ["token:" + self.secret] + list(params)
        payload["params"] = params
        r = requests.post(self.url, json=payload, timeout=10)
        data = r.json()
        if "error" in data and data["error"]:
            raise RuntimeError(str(data["error"]))
        return data.get("result")

    def add(self, url, opts=None):
        gid = self._call("aria2.addUri", [[url], opts or {}])
        return gid

    def add_torrent(self, torrent_data, opts=None):
        encoded = base64.b64encode(torrent_data).decode("ascii")
        return self._call("aria2.addTorrent", [encoded, [], opts or {}])

    def status(self, gid, keys=None):
        return self._call("aria2.tellStatus", [gid, keys or []])

    def files(self, gid):
        return self._call("aria2.getFiles", [gid])

    def pause(self, gid):
        return self._call("aria2.forcePause", [gid])

    def resume(self, gid):
        return self._call("aria2.unpause", [gid])

    def remove(self, gid):
        try:
            return self._call("aria2.forceRemove", [gid])
        except Exception:
            try:
                return self._call("aria2.removeDownloadResult", [gid])
            except Exception:
                return None

    def change_option(self, gid, opts):
        return self._call("aria2.changeOption", [gid, opts])

    def change_global(self, opts):
        return self._call("aria2.changeGlobalOption", [opts])

    def active(self):
        return self._call("aria2.tellActive", [[]])

    def waiting(self):
        return self._call("aria2.tellWaiting", [0, 100, []])

    def stopped(self):
        return self._call("aria2.tellStopped", [0, 100, []])


# --------------------------------------------------------------------------- #
#  Engine: aria2 subprocess + task store + record persistence
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self):
        self.save_path = DEFAULT_SAVE
        self.max_active = 3
        self.connections = 16   # download threads per task (max-connection-per-server)
        self.uploads = 4        # upload slots per task (BT)
        self.aria = Aria2()
        self.proc = None
        self._start_aria2()
        # in-memory extra metadata for tasks not kept by aria2
        self.tasks = {}   # gid -> {type, url, submitted, picked_files}
        self.records = self._load_records()
        self.m3u8_tasks = {}
        self._m3u8_counter = 0
        self._reconcile_stopped()

    def _start_aria2(self):
        os.makedirs(self.save_path, exist_ok=True)
        aria2c = shutil.which("aria2c") or "aria2c"
        args = [
            aria2c, "--enable-rpc", f"--rpc-listen-port={self.aria.port}",
            "--rpc-listen-all=false", "--rpc-allow-origin-all",
            f"--rpc-secret={self.aria.secret}",
            f"--dir={self.save_path}",
            "--max-concurrent-downloads=3", "--max-connection-per-server=16",
            "--split=16", "--min-split-size=1M", "--continue=true",
            "--allow-overwrite=true", "--auto-file-renaming=false",
            "--file-allocation=none", "--bt-metadata-only=false",
            # DHT & peer discovery - critical for fast magnet link resolution
            "--enable-dht=true", "--dht-listen-port=6881-6999",
            "--dht-message-timeout=8",
            "--bt-enable-lpd=true", "--enable-peer-exchange=true",
            "--bt-require-crypto=false",
            "--bt-tracker-connect-timeout=8", "--bt-tracker-timeout=8",
            "--bt-tracker=" + BT_TRACKERS,
            "--seed-time=0",
            "--rpc-max-request-size=20M",
            "--bt-remove-unselected-file=true",
        ]
        # Add DHT entry points as separate args
        for ep in DHT_ENTRY_POINTS:
            args.append("--dht-entry-point=" + ep)
        # Persist DHT routing table between sessions for faster magnet resolution
        args.append("--dht-file-path=" + os.path.join(BASE_DIR, ".aria2_dht.dat"))
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # wait for RPC to come up
        for _ in range(40):
            try:
                requests.post(self.aria.url, json={"jsonrpc": "2.0", "id": "1",
                            "method": "aria2.getVersion", "params": ["token:" + self.aria.secret]}, timeout=1)
                return
            except Exception:
                time.sleep(0.25)

    def _apply_settings(self):
        opts = {
            "max-concurrent-downloads": str(self.max_active),
            "max-connection-per-server": str(self.connections),
            "split": str(self.connections),
            "bt-max-unconfirmed": str(self.uploads),
            "max-upload-limit": "0",
        }
        try:
            self.aria.change_global(opts)
        except Exception:
            pass

    # ---- records persistence ------------------------------------------------
    def _load_records(self):
        if os.path.exists(RECORDS_FILE):
            try:
                return json.load(open(RECORDS_FILE))
            except Exception:
                pass
        return {"history": [], "trash": []}

    def _save_records(self):
        tmp = RECORDS_FILE + ".tmp"
        json.dump(self.records, open(tmp, "w"), ensure_ascii=False, indent=2)
        os.replace(tmp, RECORDS_FILE)

    def _reconcile_stopped(self):
        """Seed task metadata for tasks aria2 already knows about after a restart."""
        try:
            for s in (self.aria.active() + self.aria.waiting() + self.aria.stopped()):
                gid = s.get("gid")
                if gid and gid not in self.tasks:
                    # 检测是否为磁力种子元数据任务
                    files = s.get("files", [])
                    bt = (s.get("bittorrent") or {})
                    bt_info = bt.get("info", {}) or {}
                    bt_name = bt_info.get("name", "") or ""
                    paths = [f.get("path", "") for f in files]
                    is_torrent_meta = any(
                        p.lower().endswith(".torrent") or
                        re.match(r'^[0-9a-f]{40}\.torrent$', os.path.basename(p).lower()) is not None
                        for p in paths
                    ) or bt_name.lower().endswith(".torrent")
                    self.tasks[gid] = {
                        "type": "torrent" if is_torrent_meta else "http",
                        "url": "",
                        "submitted": True,
                        "picked": False,
                        "metadata_only": is_torrent_meta,
                    }
        except Exception:
            pass

    # ---- API actions --------------------------------------------------------
    def add(self, url):
        t = classify(url)
        if t in UNSUPPORTED:
            return {"ok": False, "error": UNSUPPORTED_MSG[t], "type": t}
        opts = {"dir": self.save_path,
                "max-connection-per-server": str(self.connections),
                "split": str(self.connections)}
        if t == "torrent":
            # Don't use bt-metadata-only - it restricts peer discovery to only
            # peers that support BEP 9, which many don't. Instead, let aria2
            # download normally. snapshot() auto-pauses as soon as bt_info.name
            # is resolved, before any significant file data is downloaded.
            pass
        gid = self.aria.add(url, opts)
        self.tasks[gid] = {"type": t, "url": url, "submitted": True, "picked": False,
                           "pending": (t == "torrent"), "added_at": time.time()}
        return {"ok": True, "gid": gid, "type": t}

    def set_settings(self, max_active=None, connections=None, uploads=None):
        if max_active is not None:
            self.max_active = max(1, int(max_active))
        if connections is not None:
            self.connections = max(1, int(connections))
        if uploads is not None:
            self.uploads = max(0, int(uploads))
        self._apply_settings()
        return True

    def set_destination(self, path):
        self.save_path = os.path.expanduser(path)
        os.makedirs(self.save_path, exist_ok=True)
        try:
            self.aria.change_global({"dir": self.save_path})
        except Exception:
            pass
        return True

    def select_files(self, gid, indices):
        info = self.tasks.get(gid)
        if not info:
            return False
        real_gid = info.get("converted_to", gid)
        real_info = self.tasks.get(real_gid, info)
        if real_info["type"] == "torrent" and indices:
            # aria2 select-file uses 1-based indices, comma separated.
            sel = ",".join(str(int(i) + 1) for i in indices)
            self.aria.change_option(real_gid, {"select-file": sel})
        real_info["picked"] = True
        self.aria.resume(real_gid)
        return True

    def confirm(self, gid, indices, action):
        """Confirm file selection from modal: select files, then download or queue."""
        info = self.tasks.get(gid)
        if not info:
            return False
        real_gid = info.get("converted_to", gid)
        real_info = self.tasks.get(real_gid, info)
        if real_info["type"] == "torrent" and indices:
            sel = ",".join(str(int(i) + 1) for i in indices)
            self.aria.change_option(real_gid, {"select-file": sel})
        real_info["picked"] = True
        real_info.pop("pending", None)
        info["picked"] = True
        info.pop("pending", None)
        # Clean up the finished metadata-only task now that user has picked files.
        if real_gid != gid:
            try:
                self.aria.remove(gid)
            except Exception:
                pass
        if action == "download":
            self.aria.resume(real_gid)
        return True

    def pause(self, gid):
        info = self.tasks.get(gid)
        real_gid = info.get("converted_to", gid) if info else gid
        return self.aria.pause(real_gid)

    def resume(self, gid):
        info = self.tasks.get(gid)
        real_gid = info.get("converted_to", gid) if info else gid
        return self.aria.resume(real_gid)

    def stop(self, gid):
        info = self.tasks.get(gid)
        real_gid = info.get("converted_to", gid) if info else gid
        self.aria.remove(real_gid)
        if real_gid != gid:
            try:
                self.aria.remove(gid)
            except Exception:
                pass
        self.tasks.pop(gid, None)
        self.tasks.pop(real_gid, None)
        return True

    # ---- status aggregation -------------------------------------------------
    def _fmt_status(self, s):
        try:
            gid = s.get("gid", "")
            info = self.tasks.get(gid, {})
            total = int(s.get("totalLength", 0) or 0)
            done = int(s.get("completedLength", 0) or 0)
            st = s.get("status", "")
            files = s.get("files", [])
            # bittorrent metadata detection - aria2 exposes info.name only once
            # the .torrent metadata has been fully downloaded and parsed.
            bt = s.get("bittorrent", {}) or {}
            bt_info = bt.get("info", {}) or {}
            # derive a display name
            name = ""
            paths = []
            for f in files:
                p = f.get("path", "")
                if p:
                    paths.append(p)
                    if not name:
                        name = os.path.basename(p)
            if not name:
                name = bt_info.get("name", "")
            if not name:
                name = info.get("url", "").split("/")[-1][:40] or "下载任务"
            # file list (torrent). aria2 returns ALL field values as strings,
            # so coerce to int explicitly and guard against None/empty.
            file_list = []
            for i, f in enumerate(files):
                fp = f.get("path", "")
                flen = int(f.get("length", 0) or 0)
                # skip placeholder file aria2 emits before BT metadata resolves
                if flen == 0 and (fp == "" or fp.startswith("[METADATA]")):
                    continue
                fdone = int(f.get("completedLength", 0) or 0)
                file_list.append({
                    "index": i,
                    "name": os.path.basename(fp) or fp or f"文件 {i+1}",
                    "path": fp,
                    "size": flen,
                    "selected": int(f.get("selected", "false") == "true") if f.get("selected") else 0,
                    "progress": round(100 * fdone / flen, 1) if flen else 0,
                })
            # Metadata is ready ONLY when aria2 reports the info dictionary name.
            # Without bt-metadata-only, placeholder files appear before real metadata.
            has_metadata = bool(bt_info.get("name"))
            state_map = {"active": "downloading", "waiting": "queued",
                         "paused": "paused", "complete": "finished",
                         "removed": "removed", "error": "error"}
            return {
                "gid": gid,
                "type": info.get("type", classify(info.get("url", ""))),
                "name": name,
                "state": state_map.get(st, st),
                "progress": round(100 * done / total, 1) if total else 0,
                "size": total,
                "download_rate": int(s.get("downloadSpeed", 0) or 0),
                "upload_rate": int(s.get("uploadSpeed", 0) or 0),
                "peers": int(s.get("numSeeders", 0) or 0) + int(s.get("connections", 0) or 0),
                "seeds": int(s.get("numSeeders", 0) or 0),
                "dir": s.get("dir", ""),
                "files": file_list,
                "paths": paths,
                "url": info.get("url", ""),
                "has_metadata": has_metadata,
                "metadata_progress": round(100 * done / total, 1) if (total and not has_metadata) else 0,
            }
        except Exception:
            return None
    def snapshot(self):
        items = []
        pending = []   # torrent tasks still resolving metadata, not yet confirmed
        completed = []
        seen_real = set()  # track real GIDs already reported via converted_to
        seen_pending = set()  # prevent duplicate pending entries
        try:
            for s in self.aria.active() + self.aria.waiting() + self.aria.stopped():
                t = self._fmt_status(s)
                if not t:
                    continue
                # Skip removed tasks
                if t.get("state") == "removed":
                    continue
                info = self.tasks.get(t["gid"])
                # Skip tasks whose real GID was already reported via a converted_to proxy
                if t["gid"] in seen_real:
                    continue
                # If this task has been converted to a real torrent task, report
                # the converted task's status under the original gid so the modal
                # keeps polling the same gid.
                if info and info.get("converted_to"):
                    converted_gid = info["converted_to"]
                    try:
                        s2 = self.aria.status(converted_gid)
                        t2 = self._fmt_status(s2)
                        if t2:
                            t2["gid"] = t["gid"]  # keep frontend gid stable
                            seen_real.add(converted_gid)  # mark real GID as reported
                            cinfo = self.tasks.get(converted_gid)
                            is_pending = cinfo and cinfo.get("pending") and not cinfo.get("picked")
                            if is_pending and t2["type"] == "torrent":
                                if t2.get("has_metadata") and t2.get("files") and len(t2["files"]) > 0:
                                    if t2["state"] in ("downloading", "waiting", "queued"):
                                        try:
                                            self.aria.pause(converted_gid)
                                            t2["state"] = "paused"
                                        except Exception:
                                            pass
                                if t2["gid"] not in seen_pending:
                                    seen_pending.add(t2["gid"])
                                    pending.append(t2)
                                continue
                            items.append(t2)
                            if t2["state"] == "finished":
                                completed.append(t2)
                            continue
                    except Exception:
                        pass
                    continue

                is_pending = info and info.get("pending") and not info.get("picked")
                # Auto-pause pending torrent once metadata resolves & files appear.
                if is_pending and t["type"] == "torrent":
                    if t.get("has_metadata") and t.get("files") and len(t["files"]) > 0:
                        if t["state"] in ("downloading", "waiting", "queued"):
                            try:
                                self.aria.pause(t["gid"])
                                t["state"] = "paused"
                            except Exception:
                                pass
                    if t["gid"] not in seen_pending:
                        seen_pending.add(t["gid"])
                        pending.append(t)
                    continue
                # Skip .torrent metadata-only tasks from the main task list.
                # They belong in the file-picker modal (pending), not as download tasks.
                # Finished tasks go to history, not the active task list.
                if t["state"] == "finished":
                    completed.append(t)
                elif not (info and info.get("metadata_only") and not info.get("converted_to")):
                    items.append(t)
        except Exception:
            pass
        # archive newly-finished tasks into history records
        existing = {r["gid"] for r in self.records["history"]} | {r["gid"] for r in self.records["trash"]}
        for t in completed:
            if t["gid"] not in existing:
                # 跳过磁力种子元数据任务（.torrent 文件本身）
                info = self.tasks.get(t["gid"], {})
                if info.get("metadata_only"):
                    continue
                name_lower = (t.get("name") or "").lower()
                if t.get("type") == "torrent" and (
                    name_lower.endswith(".torrent") or
                    re.match(r'^[0-9a-f]{40}$', name_lower)
                ):
                    continue
                self.records["history"].insert(0, {
                    "gid": t["gid"], "type": t["type"], "name": t["name"],
                    "url": t["url"], "dir": t["dir"], "paths": t["paths"],
                    "size": t["size"], "completed_at": int(time.time()),
                })
        if completed:
            self._save_records()
        # 合并 m3u8 流媒体下载任务
        for gid, info in self.m3u8_tasks.items():
            if info.get('state') == 'removed':
                continue
            output = info.get('output', '')
            items.append({
                "gid": gid,
                "type": "m3u8",
                "name": info.get('title', '视频下载'),
                "state": info.get('state', 'downloading'),
                "progress": info.get('progress', 0),
                "size": info.get('size', 0),
                "download_rate": info.get('download_rate', 0),
                "upload_rate": 0,
                "peers": 0,
                "seeds": 0,
                "dir": self.save_path,
                "files": [],
                "paths": [output] if output else [],
                "url": info.get('url', ''),
                "has_metadata": True,
                "metadata_progress": 0,
            })
        return {
            "settings": {
                "max_active": self.max_active,
                "connections": self.connections,
                "uploads": self.uploads,
                "save_path": self.save_path,
            },
            "tasks": items,
            "pending": pending,
            "history": self.records["history"],
            "trash": self.records["trash"],
        }

    # ---- history / trash ----------------------------------------------------
    def to_trash(self, gid):
        for i, r in enumerate(self.records["history"]):
            if r.get("gid") == gid:
                self.records["trash"].insert(0, self.records["history"].pop(i))
                self._save_records()
                return True
        return False

    def restore(self, gid):
        for i, r in enumerate(self.records["trash"]):
            if r.get("gid") == gid:
                self.records["history"].insert(0, self.records["trash"].pop(i))
                self._save_records()
                return True
        return False

    def purge(self, gid, delete_files=True):
        """Permanently delete a record from trash, optionally deleting files on disk."""
        for i, r in enumerate(self.records["trash"]):
            if r.get("gid") == gid:
                rec = self.records["trash"].pop(i)
                if delete_files:
                    for p in rec.get("paths", []):
                        self._safe_remove(p)
                    # also try removing the task-named folder under dir
                    d = rec.get("dir", "")
                    if d:
                        cand = os.path.join(d, rec.get("name", ""))
                        if os.path.isdir(cand) and not os.listdir(cand):
                            try:
                                shutil.rmtree(cand)
                            except Exception:
                                pass
                self._save_records()
                return True
        return False

    def clear_all_tasks(self):
        """Pause all active/waiting tasks (do not remove them)."""
        try:
            for s in self.aria.active() + self.aria.waiting():
                gid = s.get("gid")
                try:
                    self.aria.pause(gid)
                except Exception:
                    pass
        except Exception:
            pass
        # 停止所有 m3u8 下载（ffmpeg 无法暂停，只能终止）
        for gid in list(self.m3u8_tasks.keys()):
            self.stop_m3u8_download(gid)

    def clear_all_history(self):
        """Clear all download history records."""
        self.records["history"] = []
        self._save_records()

    def clear_all_trash(self, delete_files=False):
        """Clear all trash records, optionally deleting files on disk."""
        if delete_files:
            for r in list(self.records["trash"]):
                for p in r.get("paths", []):
                    self._safe_remove(p)
                d = r.get("dir", "")
                if d:
                    cand = os.path.join(d, r.get("name", ""))
                    if os.path.isdir(cand) and not os.listdir(cand):
                        try:
                            shutil.rmtree(cand)
                        except Exception:
                            pass
        self.records["trash"] = []
        self._save_records()

    @staticmethod
    def _safe_remove(path):
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except Exception:
            pass

    # ---- m3u8/HLS 流媒体下载 (ffmpeg) ----
    def _get_m3u8_duration(self, m3u8_url):
        """通过解析 m3u8 播放列表中的 EXTINF 标签来计算总时长（秒）。"""
        try:
            r = requests.get(m3u8_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            total = 0.0
            for line in r.text.split('\n'):
                if line.startswith('#EXTINF:'):
                    dur = line.split(':')[1].split(',')[0]
                    total += float(dur)
            return total if total > 0 else None
        except Exception:
            return None

    def _download_m3u8_thread(self, gid, m3u8_url, title):
        """在后台线程中运行 ffmpeg 下载 m3u8 流，并更新进度。"""
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title).strip() or "视频"
        output_path = os.path.join(self.save_path, f"{safe_title}.mp4")
        # 避免文件名冲突
        counter = 1
        while os.path.exists(output_path):
            output_path = os.path.join(self.save_path, f"{safe_title}_{counter}.mp4")
            counter += 1

        duration = self._get_m3u8_duration(m3u8_url)

        self.m3u8_tasks[gid] = {
            'url': m3u8_url, 'title': title, 'output': output_path,
            'state': 'downloading', 'progress': 0, 'size': 0,
            'download_rate': 0, 'current_time': 0, 'duration': duration,
            'proc': None,
        }

        cmd = [
            'ffmpeg', '-y', '-i', m3u8_url,
            '-c', 'copy', '-bsf:a', 'aac_adtstoasc',
            '-movflags', '+faststart',
            '-progress', 'pipe:1', '-nostats', '-loglevel', 'error',
            output_path
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
            self.m3u8_tasks[gid]['proc'] = proc

            for line in proc.stdout:
                line = line.strip()
                if line.startswith('out_time='):
                    time_str = line.split('=')[1]
                    parts = time_str.split(':')
                    try:
                        seconds = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                        self.m3u8_tasks[gid]['current_time'] = seconds
                        if duration:
                            self.m3u8_tasks[gid]['progress'] = min(round(100 * seconds / duration, 1), 99.9)
                    except (ValueError, IndexError):
                        pass
                elif line.startswith('speed='):
                    self.m3u8_tasks[gid]['speed'] = line.split('=')[1].strip()
                elif line.startswith('total_size='):
                    try:
                        self.m3u8_tasks[gid]['size'] = int(line.split('=')[1])
                    except ValueError:
                        pass

            proc.wait()

            if proc.returncode == 0:
                self.m3u8_tasks[gid]['state'] = 'finished'
                self.m3u8_tasks[gid]['progress'] = 100
                if os.path.exists(output_path):
                    self.m3u8_tasks[gid]['size'] = os.path.getsize(output_path)
            else:
                self.m3u8_tasks[gid]['state'] = 'error'
        except Exception as e:
            self.m3u8_tasks[gid]['state'] = 'error'
            self.m3u8_tasks[gid]['error'] = str(e)

    def start_m3u8_download(self, m3u8_url, title):
        """启动 m3u8 流媒体下载。返回任务 gid。"""
        gid = f"m3u8_{self._m3u8_counter}"
        self._m3u8_counter += 1
        t = threading.Thread(target=self._download_m3u8_thread,
                             args=(gid, m3u8_url, title), daemon=True)
        t.start()
        return gid

    def stop_m3u8_download(self, gid):
        """停止 m3u8 下载并删除部分文件。"""
        if gid in self.m3u8_tasks:
            proc = self.m3u8_tasks[gid].get('proc')
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            output = self.m3u8_tasks[gid].get('output', '')
            if output and os.path.exists(output):
                try:
                    os.remove(output)
                except Exception:
                    pass
            del self.m3u8_tasks[gid]
            return True
        return False


engine = Engine()
engine._apply_settings()


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify(engine.snapshot())


@app.route("/api/add", methods=["POST"])
def api_add():
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    if not url:
        return jsonify(ok=False, error="请输入链接"), 400
    return jsonify(engine.add(url))


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json(force=True)
    engine.set_settings(
        max_active=data.get("max_active"),
        connections=data.get("connections"),
        uploads=data.get("uploads"),
    )
    return jsonify(ok=True)


@app.route("/api/destination", methods=["POST"])
def api_destination():
    data = request.get_json(force=True)
    engine.set_destination(data.get("path", DEFAULT_SAVE))
    return jsonify(ok=True)


@app.route("/api/select", methods=["POST"])
def api_select():
    data = request.get_json(force=True)
    gid = data.get("gid")
    if not gid:
        return jsonify(ok=False, error="缺少 gid"), 400
    engine.select_files(gid, data.get("indices", []))
    return jsonify(ok=True)


@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    data = request.get_json(force=True)
    gid = data.get("gid")
    if not gid:
        return jsonify(ok=False, error="缺少 gid"), 400
    engine.confirm(gid, data.get("indices", []), data.get("action", "download"))
    return jsonify(ok=True)


@app.route("/api/pause", methods=["POST"])
def api_pause():
    data = request.get_json(force=True)
    return jsonify(ok=engine.pause(data.get("gid")))


@app.route("/api/resume", methods=["POST"])
def api_resume():
    data = request.get_json(force=True)
    return jsonify(ok=engine.resume(data.get("gid")))


@app.route("/api/stop", methods=["POST"])
def api_stop():
    data = request.get_json(force=True)
    gid = data.get("gid")
    if gid and gid.startswith("m3u8_"):
        engine.stop_m3u8_download(gid)
    else:
        engine.stop(gid)
    return jsonify(ok=True)


@app.route("/api/trash", methods=["POST"])
def api_trash():
    data = request.get_json(force=True)
    return jsonify(ok=engine.to_trash(data.get("gid")))


@app.route("/api/restore", methods=["POST"])
def api_restore():
    data = request.get_json(force=True)
    return jsonify(ok=engine.restore(data.get("gid")))


@app.route("/api/purge", methods=["POST"])
def api_purge():
    data = request.get_json(force=True)
    return jsonify(ok=engine.purge(data.get("gid"), delete_files=True))


@app.route("/api/purge_record_only", methods=["POST"])
def api_purge_record_only():
    data = request.get_json(force=True)
    return jsonify(ok=engine.purge(data.get("gid"), delete_files=False))


@app.route("/api/clear_tasks", methods=["POST"])
def api_clear_tasks():
    engine.clear_all_tasks()
    return jsonify(ok=True)


@app.route("/api/clear_history", methods=["POST"])
def api_clear_history():
    engine.clear_all_history()
    return jsonify(ok=True)


@app.route("/api/clear_trash", methods=["POST"])
def api_clear_trash():
    data = request.get_json(force=True)
    engine.clear_all_trash(delete_files=data.get("delete_files", False))
    return jsonify(ok=True)


@app.route("/api/parse", methods=["POST"])
def api_parse():
    """通用视频提取：从任意 URL 中尝试提取可下载的视频地址。"""
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    if not url:
        return jsonify(ok=False, error="请输入链接"), 400
    result = extract_video(url)
    return jsonify(result)


@app.route("/api/download_m3u8", methods=["POST"])
def api_download_m3u8():
    """启动 m3u8 流媒体下载。"""
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    title = data.get("title", "").strip()
    if not url:
        return jsonify(ok=False, error="请输入 m3u8 地址"), 400
    gid = engine.start_m3u8_download(url, title or "视频下载")
    return jsonify(ok=True, gid=gid, type="m3u8")


@app.route("/api/choose_dir", methods=["POST"])
def api_choose_dir():
    """打开系统原生文件夹选择对话框，返回选中的路径。"""
    try:
        script = '''
        tell application "System Events"
            activate
            set folderPath to choose folder with prompt "选择下载目录"
            return POSIX path of folderPath
        end tell
        '''
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and result.stdout.strip():
            path = result.stdout.strip()
            engine.set_destination(path)
            return jsonify(ok=True, path=path)
        return jsonify(ok=False, error="未选择目录")
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/clipboard")
def api_clipboard():
    """读取系统剪贴板（macOS pbpaste / Windows powershell）。"""
    import platform
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["powershell", "-Command", "Get-Clipboard"],
                             capture_output=True, text=True, timeout=3)
        else:
            r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3)
        return jsonify(text=r.stdout)
    except Exception as e:
        return jsonify(text="", error=str(e))


if __name__ == "__main__":
    os.makedirs(DEFAULT_SAVE, exist_ok=True)
    print("=" * 60)
    print("  磁力/P2P/HTTP 下载器已启动:  http://127.0.0.1:5566")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5566, debug=False, threaded=True)

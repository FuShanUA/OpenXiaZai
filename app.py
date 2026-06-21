import os
import json
import time
import shutil
import base64
import subprocess
import requests
from flask import Flask, request, jsonify, render_template

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SAVE = os.path.expanduser("~/Downloads/magnet-downloads")
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


TYPES = {
    "torrent": "种子/磁力",
    "http": "HTTP 直链",
    "ftp": "FTP",
    "ed2k": "电驴 eD2k",
    "quark": "夸克网盘",
    "cloud": "网盘链接",
    "thunder": "迅雷链接",
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
            "--bt-enable-lpd=true", "--bt-require-crypto=false",
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
                    self.tasks[gid] = {"type": "http", "url": "", "submitted": True, "picked": False}
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
            # Magnet links: download metadata only first, then convert to real task
            # for file picking. bt-metadata-only=true ensures only the .torrent file
            # is downloaded, not the actual content.
            opts["bt-metadata-only"] = "true"
            opts["bt-save-metadata"] = "true"
        gid = self.aria.add(url, opts)
        self.tasks[gid] = {"type": t, "url": url, "submitted": True, "picked": False,
                           "pending": (t == "torrent"), "metadata_only": (t == "torrent")}
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
                if flen == 0 and fp == "":
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
            # Metadata is ready when aria2 reports the info dictionary name OR
            # when real files have already been parsed from the torrent (not placeholders).
            has_metadata = bool(bt_info.get("name")) or (info.get("type") == "torrent" and len(file_list) > 0)
            # While a magnet link is still in metadata-only mode, don't expose the
            # .torrent file itself in the file list; wait for conversion to real task.
            if info.get("metadata_only"):
                has_metadata = False
                file_list = []
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
                # Magnet metadata-only task finished -> convert to real torrent task.
                if is_pending and t["type"] == "torrent" and info.get("metadata_only"):
                    if t["state"] == "finished" and t["paths"]:
                        # The torrent file is saved to save_path with info-hash filename.
                        # aria2 reports the path as '[METADATA]...', but the real file
                        # uses the hash. Find the actual .torrent file on disk.
                        torrent_path = None
                        url = info.get("url", "")
                        if "btih:" in url:
                            ih = url.split("btih:")[1].split("&")[0].strip().lower()
                            candidate = os.path.join(self.save_path, ih + ".torrent")
                            if os.path.exists(candidate):
                                torrent_path = candidate
                        if not torrent_path:
                            try:
                                for fn in os.listdir(self.save_path):
                                    if fn.endswith(".torrent"):
                                        fp = os.path.join(self.save_path, fn)
                                        if os.path.getsize(fp) > 0:
                                            torrent_path = fp
                                            break
                            except Exception:
                                pass
                        if torrent_path and os.path.exists(torrent_path):
                            try:
                                with open(torrent_path, "rb") as f:
                                    torrent_data = f.read()
                                new_gid = self.aria.add_torrent(torrent_data, {
                                    "dir": self.save_path,
                                    "pause": "true",
                                    "max-connection-per-server": str(self.connections),
                                    "split": str(self.connections),
                                })
                                info["converted_to"] = new_gid
                                info["metadata_only"] = False
                                info["torrent_path"] = torrent_path
                                self.tasks[new_gid] = {"type": "torrent", "url": info["url"],
                                                       "submitted": True, "picked": False,
                                                       "pending": True}
                                # Keep the finished metadata-only task in aria2 for now so
                                # the frontend can keep polling the original gid.
                                continue
                            except Exception:
                                pass
                    # Still fetching metadata from magnet: show live progress.
                    if t["gid"] not in seen_pending:
                        seen_pending.add(t["gid"])
                        pending.append(t)
                    continue
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
                # Skip metadata-only torrent tasks (the .torrent file itself)
                info = self.tasks.get(t["gid"], {})
                if info.get("metadata_only") or (info.get("type") == "torrent" and t.get("name", "").endswith(".torrent")):
                    continue
                self.records["history"].insert(0, {
                    "gid": t["gid"], "type": t["type"], "name": t["name"],
                    "url": t["url"], "dir": t["dir"], "paths": t["paths"],
                    "size": t["size"], "completed_at": int(time.time()),
                })
        if completed:
            self._save_records()
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
        """Remove all active/waiting tasks from aria2."""
        try:
            for s in self.aria.active() + self.aria.waiting():
                self.stop(s.get("gid"))
        except Exception:
            pass

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
    engine.stop(data.get("gid"))
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


if __name__ == "__main__":
    os.makedirs(DEFAULT_SAVE, exist_ok=True)
    print("=" * 60)
    print("  磁力/P2P/HTTP 下载器已启动:  http://127.0.0.1:5566")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5566, debug=False, threaded=True)

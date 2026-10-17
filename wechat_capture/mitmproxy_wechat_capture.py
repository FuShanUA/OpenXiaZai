"""Capture WeChat Channels media metadata and decryption keys with mitmproxy.

Why this exists
---------------
WeChat encrypts only the first 128 KiB of a Channels video with an Isaac64
keystream seeded by a per-response ``decode_key``. The desktop client never
issues a plain HTTP request for the Finder CGI: ``ExtTransferService`` routes
every call through ``window.WeixinJSBridge.invoke("finderH5ExtTransfer", ...)``
into xweb/Helper, so the response carrying ``decode_key`` is invisible to
mitmproxy.

This addon therefore injects a bootstrap script into the page. The script wraps
``WeixinJSBridge.invoke`` (plus XHR/fetch as a fallback), walks every Finder
response for media objects that carry a decode key, and reports them back
through the proxy. Matching video downloads are reassembled and saved next to
the key so the pair can never drift apart.

Injection happens twice, so a stale bundle hash cannot silently break it:

1. HTML documents served from the Finder host (runs before the app bundles).
2. The rewritten ``virtual_svg-icons-register.publish*.js`` bundle, where the
   original ``finderGetCommentDetail`` wrapper is kept as a second choke point.

Run:
    mitmdump -s work/mitmproxy_wechat_capture.py --listen-host 127.0.0.1 --listen-port 9090

The addon temporarily points the active macOS network service at the mitmproxy
port and restores the original settings on exit. Set WX_DISABLE_SYSTEM_PROXY=1
to opt out. Captures append to outputs/wechat-captures.jsonl (override with
WX_CAPTURE_FILE). Matched video/key pairs land in outputs/videos (override with
WX_VIDEO_DIR); WX_SAVE_VIDEO accepts keyed (default), all or off.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from mitmproxy import ctx, http


JS_MARKER = "virtual_svg-icons-register.publish"
VIDEO_MARKER = "/251/20302/stodownload"
CAPTURE_PATH = "/__wechat_decode_capture__"
INJECT_MARKER = "__wxDecodeCaptureInstalled"

# Finder pages that render the web-finder SPA.
# Calling any of these means the client is actively showing that object: it
# fetches the comment panel / comment count / danmaku for the video on screen.
# Merely prebuffering the next video's bytes does not trigger them, which is
# what separates "the user is watching this" from "autoplay downloaded it".
VIEWING_ENDPOINTS = (
    "/cgi-bin/micromsg-bin/pc_findergetcommentdetail",
    "/cgi-bin/micromsg-bin/pc_findergetcommentlist",
    "/cgi-bin/micromsg-bin/pc_finderasyncgetcommentinfo",
    "/cgi-bin/micromsg-bin/pc_findergetbulletcomment",
)

HTML_HOSTS = ("channels.weixin.qq.com",)
HTML_HOST_SUFFIXES = (".channels.weixin.qq.com",)

MAX_VIDEO_BYTES = int(os.environ.get("WX_MAX_VIDEO_BYTES", str(512 * 1024 * 1024)))
MAX_PENDING_VIDEOS = int(os.environ.get("WX_MAX_PENDING_VIDEOS", "6"))

# Long-lived live media must not be routed through the capture proxy. The page
# APIs on channels.weixin.qq.com still use the proxy, so capture is unaffected.
PROXY_BYPASS_DOMAINS = (
    "127.0.0.1",
    "localhost",
    "*.local",
    "*.wxlivecdn.com",
    "pull-m1.wxlivecdn.com",
    "pull-r1.wxlivecdn.com",
    "pull-x1.wxlivecdn.com",
    "voipfinderliveplaythird.wxqcloud.qq.com",
    "hw.flv.wxqcloud.qq.com",
    "*.flv.wxqcloud.qq.com",
)


def _command(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, capture_output=True, text=True)


class SystemProxyManager:
    """Temporarily route macOS HTTP(S) traffic through this mitmproxy."""

    def __init__(self, proxy_port: int) -> None:
        self.proxy_port = proxy_port
        self.service = os.environ.get("WX_PROXY_SERVICE") or self._detect_service()
        self.original: dict[str, Any] = {}
        self.changed = False

    @staticmethod
    def _detect_service() -> str:
        configured = _command(["networksetup", "-listallnetworkservices"]).stdout.splitlines()
        services = [line.strip() for line in configured if line.strip() and not line.startswith("An asterisk") and not line.startswith("*")]
        interface = ""
        route = _command(["route", "-n", "get", "default"], check=False).stdout
        match = re.search(r"^interface:\s*(\S+)", route, re.MULTILINE)
        if match:
            interface = match.group(1)
        hardware = _command(["networksetup", "-listallhardwareports"], check=False).stdout.splitlines()
        for index, line in enumerate(hardware):
            if line.startswith("Hardware Port:") and index + 1 < len(hardware):
                device = re.search(r"Device:\s*(\S+)", hardware[index + 1])
                if device and device.group(1) == interface:
                    return line.split(":", 1)[1].strip()
        if "Wi-Fi" in services:
            return "Wi-Fi"
        if services:
            return services[0]
        raise RuntimeError("No macOS network service was found; set WX_PROXY_SERVICE explicitly")

    @staticmethod
    def _read_proxy(service: str, kind: str) -> dict[str, str | bool]:
        result = _command(["networksetup", f"-get{kind}proxy", service])
        values: dict[str, str | bool] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition(":")
            if not separator:
                continue
            value = value.strip()
            if key.strip() == "Enabled":
                values["enabled"] = value.lower() == "yes"
            elif key.strip() in {"Server", "Port"}:
                values[key.strip().lower()] = value
        values.setdefault("enabled", False)
        values.setdefault("server", "")
        values.setdefault("port", "0")
        return values

    @staticmethod
    def _read_proxy_bypass(service: str) -> list[str]:
        result = _command(["networksetup", "-getproxybypassdomains", service])
        return [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() and not line.startswith("There aren't")
        ]

    def _expected_bypass_domains(self) -> list[str]:
        original = self.original.get("bypass")
        domains = list(original) if isinstance(original, list) else []
        for domain in PROXY_BYPASS_DOMAINS:
            if domain not in domains:
                domains.append(domain)
        return domains

    def configure(self) -> None:
        if os.environ.get("WX_DISABLE_SYSTEM_PROXY") == "1":
            return
        if os.uname().sysname != "Darwin":
            raise RuntimeError("Automatic system proxy setup currently supports macOS only")
        self.original = {
            "web": self._read_proxy(self.service, "web"),
            "secure": self._read_proxy(self.service, "secureweb"),
            "bypass": self._read_proxy_bypass(self.service),
        }
        try:
            self.changed = True
            _command([
                "networksetup", "-setproxybypassdomains", self.service,
                *self._expected_bypass_domains(),
            ])
            _command(["networksetup", "-setwebproxy", self.service, "127.0.0.1", str(self.proxy_port)])
            _command(["networksetup", "-setsecurewebproxy", self.service, "127.0.0.1", str(self.proxy_port)])
            _command(["networksetup", "-setwebproxystate", self.service, "on"])
            _command(["networksetup", "-setsecurewebproxystate", self.service, "on"])
            self.changed = True
        except Exception:
            self.restore()
            raise

    def restore(self) -> None:
        if not self.changed:
            return
        for kind, state in self.original.items():
            if kind == "bypass":
                domains = list(state) if isinstance(state, list) else []
                if not domains:
                    domains = [""]
                _command(
                    ["networksetup", "-setproxybypassdomains", self.service, *domains],
                    check=False,
                )
                continue
            if kind == "web":
                set_proxy = "-setwebproxy"
                set_state = "-setwebproxystate"
            else:
                set_proxy = "-setsecurewebproxy"
                set_state = "-setsecurewebproxystate"
            # Restore the endpoint even when the original proxy was disabled;
            # macOS otherwise leaves the temporary host and port behind.
            _command([
                "networksetup", set_proxy, self.service,
                str(state.get("server") or ""), str(state.get("port") or "0"),
            ], check=False)
            _command([
                "networksetup", set_state, self.service,
                "on" if state.get("enabled") else "off",
            ], check=False)
        self.changed = False

    def ensure(self) -> bool:
        """Reassert the temporary proxy if another process turns it off."""
        if not self.changed:
            return False
        expected = ("127.0.0.1", str(self.proxy_port))
        current = {
            "web": self._read_proxy(self.service, "web"),
            "secure": self._read_proxy(self.service, "secureweb"),
        }
        expected_bypass = set(self._expected_bypass_domains())
        current_bypass = set(self._read_proxy_bypass(self.service))
        healthy = all(
            state.get("enabled")
            and (state.get("server"), state.get("port")) == expected
            for state in current.values()
        ) and expected_bypass.issubset(current_bypass)
        if healthy:
            return False
        _command([
            "networksetup", "-setproxybypassdomains", self.service,
            *self._expected_bypass_domains(),
        ])
        _command(["networksetup", "-setwebproxy", self.service, *expected])
        _command(["networksetup", "-setsecurewebproxy", self.service, *expected])
        _command(["networksetup", "-setwebproxystate", self.service, "on"])
        _command(["networksetup", "-setsecurewebproxystate", self.service, "on"])
        return True


def _jsonable(value: Any) -> Any:
    """Convert values received from JSON into a stable serializable shape."""
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


def _bootstrap_script(token: str, capture_path: str) -> str:
    """Page-side bootstrap: wrap the bridge, scan Finder responses, report back."""
    return BOOTSTRAP_TEMPLATE.replace("__TOKEN__", json.dumps(token)).replace(
        "__CAPTURE_PATH__", json.dumps(capture_path)
    )


BOOTSTRAP_TEMPLATE = r"""
(function () {
  var W = window;
  if (W.__wxDecodeCaptureInstalled) return;
  W.__wxDecodeCaptureInstalled = true;

  var TOKEN = __TOKEN__;
  var PATH = __CAPTURE_PATH__;
  var state = {
    version: 2,
    reports: 0,
    beacons: 0,
    fetches: 0,
    images: 0,
    bridgeSeen: 0,
    wrapped: false,
    xhrScans: 0,
    fetchScans: 0,
    hookCalls: 0,
    media: 0,
    live: 0,
    errors: [],
    started: Date.now()
  };
  W.__wxDecodeCaptureState = state;
  W.__wxDecodeMedia = W.__wxDecodeMedia || {};
  W.__wxDecodeLive = W.__wxDecodeLive || {};

  var rid = 0;
  var outbox = [];
  var MAX_OUTBOX = 200;
  var MAX_FLUSHES = 6;
  function note(kind, detail) {
    if (state.errors.length < 40) state.errors.push(kind + ': ' + String(detail).slice(0, 200));
  }

  // Every channel is attempted for every report; the addon dedupes by rid, so a
  // duplicate costs nothing while a silently dropped beacon would lose a key.
  function channels(url, body) {
    var attempted = 0;
    try {
      if (navigator.sendBeacon) {
        if (navigator.sendBeacon(url, new Blob([body], { type: 'text/plain' }))) state.beacons++;
      }
    } catch (e) { note('beacon', e); }
    try {
      fetch(url, {
        method: 'POST',
        mode: 'cors',
        keepalive: true,
        headers: { 'Content-Type': 'text/plain' },
        body: body
      }).then(function () { state.fetches++; })['catch'](function (e) { note('fetch', e); });
      attempted++;
    } catch (e) { note('fetch-throw', e); }
    if (body.length < 6000) {
      try {
        (new Image()).src = url + '&d=' + encodeURIComponent(body);
        state.images++;
        attempted++;
      } catch (e) { note('img', e); }
    }
    return attempted;
  }

  function post(payload, tag) {
    payload.tag = tag;
    payload.rid = String(++rid) + '-' + Date.now();
    payload.href = String(W.location && W.location.href || '').slice(0, 300);
    payload.readyState = document.readyState;
    var body;
    try { body = JSON.stringify(payload); } catch (e) { note('stringify', e); return; }
    state.reports++;
    var entry = {
      url: PATH + '?token=' + encodeURIComponent(TOKEN) + '&rid=' + encodeURIComponent(payload.rid),
      body: body,
      flushes: 0
    };
    outbox.push(entry);
    while (outbox.length > MAX_OUTBOX) outbox.shift();
    channels(entry.url, entry.body);
    try { console.warn('[wx-decode-capture]', tag, body); } catch (e) {}
  }
  W.__wxDecodeReport = post;

  // Re-send anything still queued. Dedupe on the addon side makes this cheap.
  function flushOutbox() {
    for (var i = outbox.length - 1; i >= 0; i--) {
      var entry = outbox[i];
      if (entry.flushes >= MAX_FLUSHES) { outbox.splice(i, 1); continue; }
      entry.flushes++;
      channels(entry.url, entry.body);
    }
  }
  W.__wxDecodeFlush = flushOutbox;
  try { W.addEventListener('pagehide', flushOutbox); } catch (e) {}
  try { W.addEventListener('beforeunload', flushOutbox); } catch (e) {}

  var MEDIA_URL_RE = /[?&]m=([^&]+)/;
  var ENC_FILE_KEY_RE = /[?&]encfilekey=([^&]+)/;
  // `m=` is empty in the URL the API hands out - the client only fills it in for
  // some builds - so `encfilekey` is the dependable correlation key.
  function correlationKey(url) {
    var enc = ENC_FILE_KEY_RE.exec(url || '');
    if (enc) return enc[1];
    var m = MEDIA_URL_RE.exec(url || '');
    return m ? m[1] : '';
  }
  W.__wxDecodeCorrelationKey = correlationKey;

  // Feed metadata lives on the object that *contains* the media, not on the
  // media itself, so the walk carries the nearest enclosing description down
  // and hands it to captureMedia.
  function feedMeta(node) {
    var desc = node.objectDesc || node.object_desc;
    if (!desc) return null;
    var title = desc.description || desc.title || '';
    var author = desc.nickname || desc.author || '';
    var publish = desc.createtime || desc.createTime || 0;
    if (!title && !author) return null;
    return {
      title: String(title).slice(0, 160),
      author: String(author).slice(0, 80),
      objectId: String(node.id || node.objectId || node.object_id || ''),
      publishTime: Number(publish) || 0
    };
  }

  function captureMedia(source, endpoint, media, context) {
    var decodeKey = media.decodeKey || media.decode_key || media.decodekey;
    var url = media.url || media.fileUrl || media.file_url || '';
    if (!decodeKey || !url) return;
    var fileKey = correlationKey(url);
    var dedupeKey = fileKey + '|' + decodeKey;
    if (W.__wxDecodeMedia[dedupeKey]) return;
    var record = {
      event: 'media',
      source: source,
      endpoint: endpoint || '',
      decodeKey: String(decodeKey),
      url: String(url),
      urlToken: media.urlToken || media.url_token || '',
      fileSize: media.fileSize || media.file_size || 0,
      spec: media.spec || null,
      width: media.width || 0,
      height: media.height || 0,
      fileKey: fileKey,
      encfilekey: fileKey,
      mediaKeys: Object.keys(media).slice(0, 60)
    };
    if (context) {
      record.title = context.title || '';
      record.author = context.author || '';
      record.objectId = context.objectId || '';
      record.publishTime = context.publishTime || 0;
    }
    W.__wxDecodeMedia[dedupeKey] = 1;
    state.media++;
    post(record, 'media');
  }

  function liveCdnUrl(node) {
    var sdk = node.liveSdkInfo || node.live_sdk_info || {};
    var url = sdk.liveCdnUrl || sdk.live_cdn_url || sdk.liveCDNUrl || '';
    return typeof url === 'string' ? url : '';
  }

  function captureLive(source, endpoint, node, context) {
    var url = liveCdnUrl(node);
    var endpointName = String(endpoint || '');
    if (!url || endpointName.indexOf('pc_finderjoinlive') < 0) return;
    var info = node.liveInfo || node.live_info || {};
    var liveId = String(info.liveId || info.live_id || node.liveId || node.live_id || '');
    if (!liveId) liveId = 'url-' + url.slice(0, 120);
    var dedupeKey = liveId + '|' + url;
    if (W.__wxDecodeLive[dedupeKey]) return;
    W.__wxDecodeLive[dedupeKey] = 1;
    var contact = node.anchorContact || node.anchor_contact || node.contact || {};
    var record = {
      event: 'live',
      source: source,
      endpoint: endpoint || '',
      liveId: liveId,
      title: String(node.liveDescription || node.live_description || node.description || context?.title || '').slice(0, 160),
      author: String(node.nickname || contact.nickname || context?.author || '').slice(0, 80),
      startTime: Number(info.startTime || info.start_time || 0) || 0,
      url: url
    };
    state.live++;
    post(record, 'live');
  }

  function scan(source, endpoint, node, budget, context) {
    if (!node || typeof node !== 'object' || budget.n-- <= 0 || budget.d > 12) return;
    if (Object.prototype.toString.call(node) === '[object Array]') {
      for (var i = 0; i < node.length && i < 300; i++) scan(source, endpoint, node[i], budget, context);
      return;
    }
    var next = feedMeta(node) || context;
    if (liveCdnUrl(node)) captureLive(source, endpoint, node, next);
    if (node.decodeKey || node.decode_key || node.decodekey) captureMedia(source, endpoint, node, next);
    var keys = Object.keys(node);
    for (var k = 0; k < keys.length; k++) {
      var value = node[keys[k]];
      if (!value || typeof value !== 'object') continue;
      var child = { n: budget.n, d: budget.d + 1 };
      scan(source, endpoint, value, child, next);
      budget.n = child.n;
    }
  }

  function scanPayload(source, endpoint, payload) {
    var data = payload;
    if (typeof payload === 'string') {
      try { data = JSON.parse(payload); }
      catch (e) {
        note(source + '-json', e);
        if (payload.indexOf('decode_key') >= 0 || payload.indexOf('decodeKey') >= 0) {
          post({ event: 'scan_json_error', source: source, sample: payload.slice(0, 400) }, 'error');
        }
        return;
      }
    }
    scan(source, endpoint, data, { n: 6000, d: 0 }, null);
  }
  W.__wxDecodeScan = scanPayload;

  // ---- WeixinJSBridge: the path the Finder CGI actually uses ----------------
  function wrapBridge(bridge) {
    if (!bridge || bridge.__wxDecodeWrapped) return;
    var names = ['invoke', 'call'];
    var wrappedAny = false;
    for (var i = 0; i < names.length; i++) {
      (function (method) {
        var original = bridge[method];
        if (typeof original !== 'function') return;
        bridge[method] = function (name) {
          var args = arguments;
          if (name === 'finderH5ExtTransfer' || name === 'finderH5Auth') {
            state.hookCalls++;
            try {
              var params = args[1] || {};
              var request = {};
              try { request = params.req_json ? JSON.parse(params.req_json) : {}; } catch (e) { request = {}; }
              post({ event: 'bridge_request', endpoint: params.url || '', cmdid: params.cgi_cmdid || 0, request: request }, 'bridge');
            } catch (e) { note('bridge-req', e); }
            var userCallback = args[2];
            if (typeof userCallback === 'function') {
              args[2] = function (response) {
                try {
                  var payload = response && response.jsapi_resp && response.jsapi_resp.resp_json;
                  if (payload) scanPayload('bridge', args[1] && args[1].url, payload);
                  else post({
                    event: 'bridge_response_without_json',
                    endpoint: (args[1] && args[1].url) || '',
                    responseKeys: response ? Object.keys(response).slice(0, 20) : []
                  }, 'bridge');
                } catch (e) { note('bridge-res', e); }
                return userCallback.apply(this, arguments);
              };
            }
          }
          return original.apply(bridge, args);
        };
        wrappedAny = true;
      })(names[i]);
    }
    if (wrappedAny) {
      bridge.__wxDecodeWrapped = true;
      state.wrapped = true;
      post({ event: 'bridge_wrapped', methods: names }, 'hook');
    }
  }

  // Catch a late assignment of window.WeixinJSBridge as well as the common case
  // where xweb injects it before our script runs.
  try {
    var current = W.WeixinJSBridge;
    if (current) { state.bridgeSeen++; wrapBridge(current); }
    var descriptor = Object.getOwnPropertyDescriptor(W, 'WeixinJSBridge');
    if (!descriptor || descriptor.configurable) {
      Object.defineProperty(W, 'WeixinJSBridge', {
        configurable: true,
        enumerable: true,
        get: function () { return current; },
        set: function (value) {
          current = value;
          state.bridgeSeen++;
          try { wrapBridge(value); } catch (e) { note('bridge-set', e); }
        }
      });
    }
  } catch (e) { note('bridge-define', e); }

  // ---- XHR / fetch fallback (other client builds use plain HTTP) -----------
  try {
    var XHR = W.XMLHttpRequest;
    if (XHR && XHR.prototype && !XHR.prototype.__wxDecodeWrapped) {
      XHR.prototype.__wxDecodeWrapped = true;
      var originalSend = XHR.prototype.send;
      XHR.prototype.send = function () {
        var xhr = this;
        try {
          xhr.addEventListener('load', function () {
            try {
              var text = xhr.responseType === '' || xhr.responseType === 'text' || !xhr.responseType
                ? xhr.responseText : '';
              if (!text || text.indexOf('decode') < 0) return;
              state.xhrScans++;
              scanPayload('xhr', xhr.responseURL || '', text);
            } catch (e) { note('xhr', e); }
          });
        } catch (e) { note('xhr-hook', e); }
        return originalSend.apply(this, arguments);
      };
    }
  } catch (e) { note('xhr-install', e); }

  try {
    var originalFetch = W.fetch;
    if (typeof originalFetch === 'function' && !originalFetch.__wxDecodeWrapped) {
      var wrappedFetch = function () {
        var args = arguments;
        return originalFetch.apply(this, args).then(function (response) {
          try {
            var url = String((args[0] && args[0].url) || args[0] || '');
            var type = response.headers && response.headers.get ? (response.headers.get('content-type') || '') : '';
            if (type.indexOf('json') < 0 && type.indexOf('text') < 0) return response;
            if (url.indexOf('finder') < 0 && url.indexOf('cgi-bin') < 0 && url.indexOf('channels') < 0) return response;
            state.fetchScans++;
            response.clone().text().then(function (text) {
              if (text && text.indexOf('decode') >= 0) scanPayload('fetch', url, text);
            })['catch'](function () {});
          } catch (e) { note('fetch-scan', e); }
          return response;
        });
      };
      wrappedFetch.__wxDecodeWrapped = true;
      W.fetch = wrappedFetch;
    }
  } catch (e) { note('fetch-install', e); }

  // ---- Keep looking for the bridge for a while ----------------------------
  var tries = 0;
  var timer = setInterval(function () {
    tries++;
    var bridge = W.WeixinJSBridge;
    if (bridge && !bridge.__wxDecodeWrapped) { state.bridgeSeen++; wrapBridge(bridge); }
    if (tries % 5 === 0) flushOutbox();
    if (tries === 1 || tries === 5 || tries === 25 || tries === 100 || tries === 300) {
      post({
        event: 'hook_status', tries: tries, bridgePresent: !!bridge,
        wrapped: state.wrapped, hookCalls: state.hookCalls, media: state.media,
        reports: state.reports, beacons: state.beacons, fetches: state.fetches,
        images: state.images, queued: outbox.length,
        errors: state.errors.slice(0, 10)
      }, 'status');
    }
    if (tries >= 300) clearInterval(timer);
  }, 200);

  post({ event: 'bootstrap_installed' }, 'hook');
})();
"""


class VideoAssembler:
    """Reassemble one stodownload file from its (possibly parallel) ranges.

    ``variant`` distinguishes the stream the client fetched (empty) from an
    extra quality variant this addon pulls straight from the CDN, so both can
    be written side by side.
    """

    def __init__(self, file_key: str, url: str, variant: str = "") -> None:
        self.file_key = file_key
        self.variant = variant
        self.url = url
        self.total: int | None = None
        self.chunks: dict[int, bytes] = {}
        self.intervals: list[tuple[int, int]] = []
        self.bytes = 0
        self.created = time.time()

    @property
    def stem(self) -> str:
        return f"{self.file_key}{self.variant}"

    def add(self, offset: int, data: bytes, total: int | None) -> None:
        if total:
            self.total = total
        if offset not in self.chunks:
            self.bytes += len(data)
        self.chunks[offset] = data
        self._merge(offset, offset + len(data))

    def _merge(self, start: int, end: int) -> None:
        self.intervals.append((start, end))
        self.intervals.sort()
        merged: list[tuple[int, int]] = []
        for begin, finish in self.intervals:
            if merged and begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
            else:
                merged.append((begin, finish))
        self.intervals = merged

    @property
    def over_budget(self) -> bool:
        return self.bytes > MAX_VIDEO_BYTES

    def complete(self) -> bool:
        return bool(
            self.total
            and self.intervals
            and self.intervals[0][0] == 0
            and self.intervals[0][1] >= self.total
        )

    def assemble(self) -> bytes:
        assert self.total is not None
        buffer = bytearray(self.total)
        for offset in sorted(self.chunks):
            data = self.chunks[offset]
            if offset >= self.total:
                continue
            buffer[offset:offset + len(data)] = data[: self.total - offset]
        return bytes(buffer)


class _CaptureServer(ThreadingHTTPServer):
    allow_reuse_address = True


class _CaptureHandler(BaseHTTPRequestHandler):
    server: _CaptureServer

    def log_message(self, *_args: Any) -> None:
        return

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-WX-Capture-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    # ------------------------------------------------------------------ UI
    def _try_ui(self, method: str, body: bytes = b"") -> bool:
        """Hand the request to the optional Web UI router; True if it answered.

        The router lives in work/ui/ and is loaded only if present, so the addon
        stays usable on its own.
        """
        router = getattr(self.server.owner, "ui_router", None)  # type: ignore[attr-defined]
        if router is None:
            return False
        try:
            response = router.handle(
                owner=self.server.owner,  # type: ignore[attr-defined]
                method=method,
                path=self.path,
                headers=dict(self.headers.items()),
                body=body,
            )
        except Exception as exc:  # pragma: no cover - defensive
            ctx.log.error(f"UI router failed for {self.path}: {exc}")
            self.send_error(500, "ui router error")
            return True
        if response is None:
            return False

        self.send_response(response.status)
        for key, value in response.header_items().items():
            self.send_header(key, value)
        self.end_headers()
        response.stream_to(self.wfile)
        return True

    def do_OPTIONS(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != CAPTURE_PATH:
            self.send_error(404)
            return
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self._try_ui("HEAD")

    def do_GET(self) -> None:  # noqa: N802
        if self._try_ui("GET"):
            return
        owner: WeChatCapture = self.server.owner  # type: ignore[attr-defined]
        payload = owner.parse_capture_request(
            path=self.path,
            headers=dict(self.headers.items()),
            body=b"",
            method="GET",
        )
        if payload is None:
            self.send_error(404)
            return
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = b""
        if 0 < length <= 8 * 1024 * 1024:
            raw = self.rfile.read(length)
        if self._try_ui("POST", raw):
            return
        if urlsplit(self.path).path != CAPTURE_PATH:
            self.send_error(404)
            return
        if not raw:
            self.send_error(413)
            return
        owner: WeChatCapture = self.server.owner  # type: ignore[attr-defined]
        if owner.parse_capture_request(
            path=self.path,
            headers=dict(self.headers.items()),
            body=raw,
            method="POST",
        ) is None:
            self.send_error(403)
            return
        self.send_response(204)
        self._cors()
        self.end_headers()


class WeChatCapture:
    def __init__(self) -> None:
        default_path = Path.cwd() / "outputs" / "wechat-captures.jsonl"
        self.output_path = Path(os.environ.get("WX_CAPTURE_FILE", str(default_path))).expanduser()
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.video_dir = Path(
            os.environ.get("WX_VIDEO_DIR", str(self.output_path.parent / "videos"))
        ).expanduser()
        # Default off: capture keeps only metadata, every video file comes from a
        # download the user starts in the Web UI. See docs/WEBUI.md.
        self.save_video = os.environ.get("WX_SAVE_VIDEO", "off").lower()
        # Auto-pulling a quality at capture time is opt-in; the UI drives it.
        self.auto_fetch = os.environ.get("WX_AUTO_FETCH", "0") == "1"
        # client = only what the client fetched; best = the untranscoded master
        # served when X-snsvideoflag is omitted; or an explicit format id.
        self.quality = os.environ.get("WX_QUALITY", "client").strip()
        self.link_ttl_minutes = int(os.environ.get("WX_LINK_TTL_MINUTES", "15"))
        self.auto_decrypt = os.environ.get("WX_AUTO_DECRYPT", "1") == "1"
        self.decryptor = Path(__file__).resolve().parent / "decrypt" / "wechat_video_decrypt.mjs"
        self.capture_host = os.environ.get("WX_CAPTURE_HOST", "127.0.0.1")
        self.capture_port = int(os.environ.get("WX_CAPTURE_PORT", "18765"))
        # A fixed token via env keeps the capture endpoint testable from a shell.
        self.capture_token = os.environ.get("WX_CAPTURE_TOKEN") or secrets.token_urlsafe(24)
        self.proxy_port = int(os.environ.get("WX_PROXY_PORT", "9090"))
        self.proxy_manager: SystemProxyManager | None = None
        self.capture_server: _CaptureServer | None = None
        self.capture_thread: threading.Thread | None = None
        self.proxy_watchdog_stop = threading.Event()
        self.proxy_watchdog_thread: threading.Thread | None = None
        self.service_lock = threading.Lock()
        self.capture_enabled = True
        self.write_lock = threading.Lock()
        # mitmproxy callbacks and the capture HTTP server run on different
        # threads. The UI reads these containers, so mutations need one lock.
        self.state_lock = threading.RLock()
        self.latest_feed: dict[str, Any] | None = None
        # One file key can carry several candidate decodeKeys: WeChat reissues a
        # key per API response for the same encfilekey, and only one of them
        # decrypts the bytes the client actually fetched.
        self.media_by_file_key: dict[str, list[dict[str, Any]]] = {}
        self.live_by_id: dict[str, dict[str, Any]] = {}
        self.current_live_id: str | None = None
        self.assemblers: dict[str, VideoAssembler] = {}
        self.pending_completed: list[VideoAssembler] = []
        self.saved_files: set[str] = set()
        self.decoded_files: set[str] = set()
        self.attempted_keys: dict[str, set[str]] = {}
        self.stems_by_file_key: dict[str, list[str]] = {}
        self.fetched_variants: set[str] = set()
        # Bound concurrent CDN pulls: a feed can start many at once, and each one
        # can be tens of megabytes.
        self.variant_slots = threading.Semaphore(
            max(1, int(os.environ.get("WX_MAX_VARIANT_FETCHES", "2")))
        )
        self.seen_report_ids: dict[str, float] = {}
        self.counters: dict[str, int] = {}
        self.js_seen = False
        self.html_injections = 0
        # The format the client itself chose, learned from its own stodownload
        # request (X-snsvideoflag). Free information, used to label the UI.
        self.client_formats: dict[str, str] = {}
        # Every file key the client actually pulled bytes for. A video can be
        # "played" even when its only media record came from the prefetch flow,
        # so this is what decides whether an entry is real or merely prefetched.
        self.observed_downloads: set[str] = set()
        # Object ids the client fetched comments for (videos on screen), mapped
        # to the endpoint that touched them so the UI can show its evidence.
        self.viewing_objects: dict[str, str] = {}
        # When the signed media URL was issued, parsed from svrnonce.
        self.signed_at: dict[str, float] = {}
        # Optional Web UI (work/ui/); None when that directory is absent.
        self.ui_router: Any = None
        self.event_sinks: list[Any] = []
        atexit.register(self._restore_proxy)

    # ------------------------------------------------------------------ setup
    def load(self, loader: Any) -> None:
        listen_port = getattr(getattr(ctx, "options", None), "listen_port", None)
        if listen_port:
            self.proxy_port = int(listen_port)
        self._restore_live_captures()
        if os.environ.get("WX_DISABLE_SYSTEM_PROXY") != "1":
            self.proxy_manager = SystemProxyManager(self.proxy_port)
            try:
                self.proxy_manager.configure()
                self._write({
                    "event": "system_proxy_configured",
                    "service": self.proxy_manager.service,
                    "proxy_host": "127.0.0.1",
                    "proxy_port": self.proxy_port,
                    "original": self.proxy_manager.original,
                })
                ctx.log.info(
                    f"System HTTP(S) proxy set to 127.0.0.1:{self.proxy_port} "
                    f"on {self.proxy_manager.service}"
                )
            except Exception as exc:
                ctx.log.error(f"Could not configure macOS system proxy: {exc}")
                raise
            self.proxy_watchdog_thread = threading.Thread(
                target=self._watch_proxy, name="wx-proxy-watchdog", daemon=True
            )
            self.proxy_watchdog_thread.start()

        # Use a fixed loopback port so the injected page code has a stable URL.
        self._load_ui_router()
        server = _CaptureServer((self.capture_host, self.capture_port), _CaptureHandler)
        server.owner = self  # type: ignore[attr-defined]
        self.capture_server = server
        self.capture_thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.capture_thread.start()
        browser_host = "localhost" if self.capture_host in {"127.0.0.1", "::1"} else self.capture_host
        ctx.log.info(f"WeChat capture endpoint: http://{browser_host}:{self.capture_port}{CAPTURE_PATH}")
        ctx.log.info(f"Capture output: {self.output_path}")
        ctx.log.info(f"Video output:   {self.video_dir} (WX_SAVE_VIDEO={self.save_video})")
        if self.ui_router is not None:
            ctx.log.info(f"Web UI:         http://{browser_host}:{self.capture_port}/")
        self._write({
            "event": "session_start",
            "capture_url": f"http://{browser_host}:{self.capture_port}{CAPTURE_PATH}",
            "capture_token": self.capture_token,
            "proxy_port": self.proxy_port,
            "video_dir": str(self.video_dir),
            "save_video": self.save_video,
            "auto_fetch": self.auto_fetch,
            "auto_decrypt": self.auto_decrypt,
            "link_ttl_minutes": self.link_ttl_minutes,
            "ui": self.ui_router is not None,
            "ui_version": getattr(self.ui_router, "version", 0) if self.ui_router else 0,
        })

    def _restore_live_captures(self) -> None:
        """Restore the latest joined live room after a downloader restart.

        A live page calls joinLive when the room is entered, not on every
        capture-service restart. Without this recovery the UI would show an
        empty live list even while WeChat keeps playing the already-joined room.
        """
        if not self.output_path.exists():
            return
        latest: dict[str, Any] | None = None
        try:
            lines = self.output_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                record.get("event") == "live"
                and "pc_finderjoinlive" in str(record.get("endpoint") or "").lower()
                and str(record.get("source") or "").lower() != "test"
            ):
                latest = record
                break
        if not latest:
            return
        live_id = str(latest.get("live_id") or "")
        url = str(latest.get("url") or "")
        if not live_id or not url.startswith(("http://", "https://")):
            return
        restored = {
            "event": "live",
            "live_id": live_id,
            "title": str(latest.get("title") or ""),
            "author": str(latest.get("author") or ""),
            "url": url,
            "source": str(latest.get("source") or ""),
            "endpoint": str(latest.get("endpoint") or ""),
            "start_time": int(latest.get("start_time") or 0),
            "is_current": True,
            "captured_at": float(latest.get("captured_at") or 0),
        }
        with self.state_lock:
            self.live_by_id[live_id] = restored
            self.current_live_id = live_id
        ctx.log.info(
            f"Restored current live {live_id} from {self.output_path.name}"
        )

    def _load_ui_router(self) -> None:
        """Load work/ui/api.py when present; the addon works fine without it."""
        ui_dir = Path(__file__).resolve().parent
        module_path = ui_dir / "ui" / "api.py"
        if not module_path.exists():
            return
        try:
            if str(ui_dir) not in sys.path:
                sys.path.insert(0, str(ui_dir))
            from ui import api as ui_api  # noqa: PLC0415

            self.ui_router = ui_api.Router()
            self.ui_router.attach(self)
        except Exception as exc:
            ctx.log.error(f"Could not load the Web UI from {module_path}: {exc}")
            self.ui_router = None

    def done(self) -> None:
        preview_manager = getattr(self.ui_router, "live_preview", None)
        if preview_manager is not None:
            try:
                preview_manager.stop_all()
            except Exception:
                pass
        self.proxy_watchdog_stop.set()
        if self.proxy_watchdog_thread is not None:
            self.proxy_watchdog_thread.join(timeout=3)
            self.proxy_watchdog_thread = None
        if self.capture_server is not None:
            self.capture_server.shutdown()
            self.capture_server.server_close()
        self._restore_proxy()
        self._write({"event": "session_summary", "counters": self.counters})

    def _restore_proxy(self) -> None:
        if self.proxy_manager is None or not self.proxy_manager.changed:
            return
        service = self.proxy_manager.service
        self.proxy_manager.restore()
        self._write({"event": "system_proxy_restored", "service": service})
        ctx.log.info(f"Restored macOS system proxy on {service}")

    def set_capture_enabled(self, enabled: bool) -> None:
        """Pause or resume system-proxy capture without stopping the Web UI."""
        enabled = bool(enabled)
        with self.service_lock:
            if enabled == self.capture_enabled:
                return

            if not enabled:
                self.proxy_watchdog_stop.set()
                if self.proxy_watchdog_thread is not None:
                    self.proxy_watchdog_thread.join(timeout=3)
                    self.proxy_watchdog_thread = None
                self._restore_proxy()
                self.capture_enabled = False
                self._write({
                    "event": "capture_status",
                    "capture_enabled": False,
                    "proxy_configured": False,
                })
                return

            if self.proxy_manager is None:
                self.capture_enabled = True
                self._write({
                    "event": "capture_status",
                    "capture_enabled": True,
                    "proxy_configured": False,
                })
                return

            self.proxy_manager.configure()
            self.proxy_watchdog_stop.clear()
            self.proxy_watchdog_thread = threading.Thread(
                target=self._watch_proxy, name="wx-proxy-watchdog", daemon=True
            )
            self.proxy_watchdog_thread.start()
            self.capture_enabled = True
            self._write({
                "event": "capture_status",
                "capture_enabled": True,
                "proxy_configured": self.proxy_manager.changed,
            })

    def _watch_proxy(self) -> None:
        """Keep the temporary system proxy alive while capture is running."""
        while not self.proxy_watchdog_stop.wait(2.0):
            manager = self.proxy_manager
            if manager is None:
                continue
            try:
                if manager.ensure():
                    self._write({
                        "event": "system_proxy_reasserted",
                        "service": manager.service,
                        "proxy_host": "127.0.0.1",
                        "proxy_port": self.proxy_port,
                    })
                    ctx.log.warn(
                        "Reasserted the temporary macOS system proxy after it was disabled"
                    )
            except Exception as exc:
                ctx.log.warn(f"Could not reassert the macOS system proxy: {exc}")

    # --------------------------------------------------------------- requests
    def request(self, flow: http.HTTPFlow) -> None:
        url = flow.request.pretty_url
        split = urlsplit(url)
        if split.path == CAPTURE_PATH:
            self._handle_capture_flow(flow)
            return

        if JS_MARKER in url and url.endswith(".js"):
            # Never let a proxy cache or a conditional request hide the bundle.
            flow.request.headers["Accept-Encoding"] = "identity"
            flow.request.headers.pop("If-None-Match", None)
            flow.request.headers.pop("If-Modified-Since", None)
            flow.request.headers["Cache-Control"] = "no-cache"
            flow.request.headers["Pragma"] = "no-cache"

        if VIDEO_MARKER in split.path:
            self._count("video_request")
            file_key = self._file_key(url)
            client_format = self._query_param(url, "X-snsvideoflag")
            with self.state_lock:
                if file_key:
                    self.observed_downloads.add(file_key)
                if file_key and client_format:
                    # Which transcode the client picked. Passive, free, and it
                    # labels the quality list in the UI.
                    self.client_formats[file_key] = client_format
            self._write({
                "event": "video_request",
                "url": url,
                "file_key": file_key,
                "client_format": client_format,
                "method": flow.request.method,
                "range": flow.request.headers.get("Range"),
                "headers": {
                    "x-snsvideoflag": flow.request.headers.get("X-snsvideoflag"),
                    "referer": flow.request.headers.get("Referer"),
                },
            })

    def _handle_capture_flow(self, flow: http.HTTPFlow) -> None:
        """Receive the injected report through the Finder page's own origin."""
        if flow.request.method == "OPTIONS":
            flow.response = http.Response.make(
                204,
                b"",
                {
                    "Access-Control-Allow-Origin": flow.request.headers.get("Origin", "*"),
                    "Access-Control-Allow-Headers": "Content-Type, X-WX-Capture-Token",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                },
            )
            return

        payload = self.parse_capture_request(
            path=flow.request.path,
            headers={k: v for k, v in flow.request.headers.items()},
            body=flow.request.content or b"",
            method=flow.request.method,
        )
        if payload is None:
            flow.response = http.Response.make(403, b"forbidden")
            return
        flow.response = http.Response.make(
            204,
            b"",
            {"Access-Control-Allow-Origin": flow.request.headers.get("Origin", "*")},
        )

    # -------------------------------------------------------------- responses
    def response(self, flow: http.HTTPFlow) -> None:
        url = flow.request.pretty_url
        split = urlsplit(url)
        if JS_MARKER in url and url.endswith(".js"):
            self._rewrite_bundle(flow)
            return

        if split.path == CAPTURE_PATH:
            return

        if split.hostname and self._is_finder_html(split.hostname):
            self._inject_html(flow)
            return

        if VIDEO_MARKER in split.path and flow.response is not None:
            self._record_video_response(flow)

    @staticmethod
    def _is_finder_html(host: str) -> bool:
        return host in HTML_HOSTS or any(host.endswith(suffix) for suffix in HTML_HOST_SUFFIXES)

    def _inject_html(self, flow: http.HTTPFlow) -> None:
        response = flow.response
        if response is None:
            return
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "html" not in content_type:
            return
        try:
            response.decode()
            source = response.content.decode("utf-8", errors="replace")
        except (AttributeError, ValueError) as exc:
            ctx.log.warn(f"Cannot decode HTML {flow.request.pretty_url}: {exc}")
            return
        if INJECT_MARKER in source:
            return
        script = (
            "<script>"
            + _bootstrap_script(self.capture_token, CAPTURE_PATH)
            + "</script>"
        )
        marker = re.search(r"<head[^>]*>", source, re.I)
        if marker:
            rewritten = source[: marker.end()] + script + source[marker.end():]
        elif re.search(r"<html[^>]*>", source, re.I):
            end = re.search(r"<html[^>]*>", source, re.I).end()
            rewritten = source[:end] + script + source[end:]
        else:
            rewritten = script + source
        response.content = rewritten.encode("utf-8")
        self._no_store(response)
        self.html_injections += 1
        self._count("html_injected")
        self._write({
            "event": "html_injected",
            "url": flow.request.pretty_url,
            "source_length": len(source),
            "rewritten_length": len(rewritten),
        })
        ctx.log.info(f"Injected capture bootstrap into {flow.request.pretty_url}")

    # ------------------------------------------------------------------ video
    def _record_video_response(self, flow: http.HTTPFlow) -> None:
        response = flow.response
        assert response is not None
        url = flow.request.pretty_url
        file_key = self._file_key(url)
        record = {
            "event": "video_response",
            "url": url,
            "file_key": file_key,
            "status": response.status_code,
            "content_type": response.headers.get("Content-Type"),
            "content_length": response.headers.get("Content-Length"),
            "content_range": response.headers.get("Content-Range"),
            "enc_flag": response.headers.get("X-encflag"),
            "enc_len": response.headers.get("X-enclen"),
            "sns_video_flag": response.headers.get("X-snsvideoflag"),
            "sns_video_ticket": response.headers.get("X-snsvideoticket"),
            "media": self.media_by_file_key.get(file_key),
        }
        self._count("video_response")
        self._write(record)
        self._absorb_video_body(flow, file_key, url)

    def _absorb_video_body(self, flow: http.HTTPFlow, file_key: str, url: str) -> None:
        if self.save_video == "off" or not file_key:
            return
        response = flow.response
        assert response is not None
        body = response.content or b""
        if not body:
            return
        offset = 0
        total: int | None = None
        content_range = response.headers.get("Content-Range")
        if content_range:
            match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range)
            if match:
                offset = int(match.group(1))
                if match.group(3) != "*":
                    total = int(match.group(3))
        elif response.status_code == 200:
            offset = 0
            length = response.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else len(body)

        assembler = self.assemblers.get(file_key)
        if assembler is None:
            assembler = VideoAssembler(file_key, url)
            self.assemblers[file_key] = assembler
        assembler.add(offset, body, total)
        if assembler.over_budget:
            self._count("video_over_budget")
            self.assemblers.pop(file_key, None)
            ctx.log.warn(f"Video {file_key} exceeds WX_MAX_VIDEO_BYTES; dropping it")
            return
        if assembler.complete():
            self._count("video_complete")
            self.assemblers.pop(file_key, None)
            self._maybe_persist(assembler)

    # ------------------------------------------------------------------ media
    def _maybe_persist(self, assembler: VideoAssembler) -> None:
        media_list = self.media_by_file_key.get(assembler.file_key)
        if media_list:
            self._persist_video(assembler, media_list)
        elif self.save_video == "all":
            self._persist_video(assembler, [])
        else:
            self.pending_completed.append(assembler)
            while len(self.pending_completed) > MAX_PENDING_VIDEOS:
                dropped = self.pending_completed.pop(0)
                self._count("video_pending_evicted")
                ctx.log.warn(f"No decodeKey for {dropped.file_key}; evicted the buffered video")

    def _preferred_stem(self, file_key: str) -> str:
        """Stem of the quality variant this run prefers, or "" for the client stream.

        Only used by the opt-in auto-fetch path (``WX_AUTO_FETCH=1``). Under the
        Web UI workflow downloads are explicit and every variant is kept, so
        nothing is superseded.
        """
        suffix = self._variant_suffix()
        return f"{file_key}{suffix}" if suffix else ""

    def _persist_video(
        self, assembler: VideoAssembler, media_list: list[dict[str, Any]] | None
    ) -> None:
        if assembler.stem in self.saved_files:
            return
        self.saved_files.add(assembler.stem)
        self.stems_by_file_key.setdefault(assembler.file_key, [])
        if assembler.stem not in self.stems_by_file_key[assembler.file_key]:
            self.stems_by_file_key[assembler.file_key].append(assembler.stem)
        self.video_dir.mkdir(parents=True, exist_ok=True)
        try:
            payload = assembler.assemble()
        except Exception as exc:  # pragma: no cover - defensive
            ctx.log.error(f"Could not assemble {assembler.stem}: {exc}")
            return
        encrypted_path = self.video_dir / f"{assembler.stem}.enc.mp4"
        encrypted_path.write_bytes(payload)
        self._write_sidecar(
            assembler.stem,
            assembler.file_key,
            url=assembler.url,
            variant=assembler.variant.lstrip(".") or "client",
            bytes=len(payload),
            sha256=self._sha256(payload),
            encrypted_path=str(encrypted_path),
            media=media_list or [],
        )
        self._count("video_saved")
        ctx.log.info(f"Saved {len(payload)} bytes to {encrypted_path}")

        # Decode first, then log the event, so it carries the final outcome.
        self._attempt_decode(assembler.stem, assembler.file_key)
        sidecar = self._write_sidecar(assembler.stem, assembler.file_key)
        self._write({"event": "video_saved", **sidecar})
        if sidecar.get("decoded"):
            ctx.log.info(
                f"Decoded {encrypted_path.name} with decodeKey {sidecar.get('decodeKey')} "
                f"-> {sidecar.get('decrypted_path')}"
            )

    def _candidate_keys(self, file_key: str) -> list[str]:
        keys: list[str] = []
        for media in self.media_by_file_key.get(file_key, []):
            key = str(media.get("decodeKey") or "")
            if key and key not in keys:
                keys.append(key)
        return keys

    def persist_download(self, file_key: str, quality: str, url: str, payload: bytes) -> dict[str, Any]:
        """Persist a user-requested download and decode it.

        Called by the Web UI's download manager. Each quality gets its own stem,
        and nothing is ever removed: downloads are explicit user actions, so
        every rendition the user asked for is kept side by side.
        """
        variant = "" if quality in {"client", ""} else f".{quality}"
        assembler = VideoAssembler(file_key, url, variant)
        assembler.add(0, payload, len(payload))
        self._persist_video(assembler, self.media_by_file_key.get(file_key, []))
        return self._write_sidecar(assembler.stem, file_key)

    def _attempt_decode(self, stem: str, file_key: str) -> Path | None:
        """Try every untried candidate key against an already written .enc.mp4.

        WeChat reissues a decodeKey per API response for the same encfilekey, and
        only one of them matches the bytes the client actually fetched. The
        decryptor validates the MP4 signature, so trying each key in turn is
        conclusive; a later response can also supply the right key.
        """
        if stem in self.decoded_files:
            return self.video_dir / f"{stem}.mp4"
        encrypted_path = self.video_dir / f"{stem}.enc.mp4"
        if not encrypted_path.exists():
            return None
        tried = self.attempted_keys.setdefault(stem, set())
        for key in self._candidate_keys(file_key):
            if key in tried:
                continue
            tried.add(key)
            result = self._run_decrypt(encrypted_path, key)
            if result is not None:
                self.decoded_files.add(stem)
                self._write_sidecar(
                    stem, file_key, decoded=True, decodeKey=key, decrypted_path=str(result)
                )
                ctx.log.info(f"Decoded {encrypted_path.name} with decodeKey {key} -> {result}")
                return result
        self._write_sidecar(
            stem, file_key, decoded=False, candidate_keys=self._candidate_keys(file_key)
        )
        self._count("video_saved_undecoded")
        (self.video_dir / f"{stem}.keys.txt").write_text(
            "\n".join(self._candidate_keys(file_key)), encoding="utf-8"
        )
        return None

    def _write_sidecar(self, stem: str, file_key: str, **updates: Any) -> dict[str, Any]:
        """Merge fields into <stem>.json so late keys update the same record."""
        path = self.video_dir / f"{stem}.json"
        sidecar: dict[str, Any] = {}
        if path.exists():
            try:
                sidecar = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                sidecar = {}
        sidecar.setdefault("file_key", file_key)
        sidecar.update({k: v for k, v in updates.items() if v is not None})
        sidecar["candidate_keys"] = self._candidate_keys(file_key) or sidecar.get("candidate_keys", [])
        try:
            path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            ctx.log.warn(f"Could not write {path}: {exc}")
        return sidecar

    # ------------------------------------------------------- quality variants
    def _variant_suffix(self) -> str:
        quality = (self.quality or "client").lower()
        if quality in {"client", "off", ""}:
            return ""
        if quality in {"best", "master", "full", "max"}:
            return ".best"
        return f".{self.quality}"

    def _start_variant_fetch(self, file_key: str, media: dict[str, Any]) -> None:
        suffix = self._variant_suffix()
        if not suffix or not file_key:
            return
        stem = f"{file_key}{suffix}"
        with self.write_lock:
            if stem in self.fetched_variants:
                return
            self.fetched_variants.add(stem)
        threading.Thread(
            target=self._fetch_variant, args=(file_key, media, suffix), daemon=True
        ).start()

    @staticmethod
    def _variant_url(raw_url: str, suffix: str) -> str:
        """Strip the client's format selector and pick the requested variant.

        The API hands out one URL; the client appends ``X-snsvideoflag=<fmt>`` to
        downgrade it. Omitting the flag serves the untranscoded master, which is
        exactly the size the API reports as ``fileSize``.
        """
        target = re.sub(r"[?&]X-snsvideoflag=[^&]*", "", raw_url)
        if suffix != ".best":
            target += f"&X-snsvideoflag={suffix.lstrip('.')}"
        if "taskid=" not in target:
            target += f"&taskid=pc-{int(time.time() * 1000)}"
        if "web=" not in target:
            target += "&web=1"
        return target

    def _fetch_variant(self, file_key: str, media: dict[str, Any], suffix: str) -> None:
        raw = str(media.get("url") or "") + str(media.get("urlToken") or "")
        if len(raw) < 32:
            return
        target = self._variant_url(raw, suffix)
        self._count("variant_fetch_started")
        self._write({
            "event": "variant_fetch_started",
            "file_key": file_key, "variant": suffix.lstrip("."), "url": target,
        })
        try:
            with self.variant_slots:
                payload = self._download_variant(target)
        except Exception as exc:
            self._count("variant_fetch_failed")
            self._write({
                "event": "variant_fetch_failed",
                "file_key": file_key, "variant": suffix.lstrip("."), "error": str(exc),
            })
            ctx.log.warn(f"Could not fetch the {suffix.lstrip('.')} variant of {file_key}: {exc}")
            return
        self._count("variant_fetch_ok")
        ctx.log.info(
            f"Fetched {len(payload)} bytes of the {suffix.lstrip('.')} variant for {file_key}"
        )
        assembler = VideoAssembler(file_key, target, suffix)
        assembler.add(0, payload, len(payload))
        self._persist_video(assembler, self.media_by_file_key.get(file_key, []))

    def _download_variant(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120 Safari/537.36 MicroMessenger/3.8.9"
            ),
            "Range": "bytes=0-",
            "Accept": "*/*",
        })
        # The system proxy currently points at this mitmproxy, so bypass it
        # explicitly or the request would loop back into this addon.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        buffer = bytearray()
        with opener.open(request, timeout=300) as response:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > MAX_VIDEO_BYTES:
                    raise RuntimeError("variant exceeds WX_MAX_VIDEO_BYTES")
        return bytes(buffer)

    def _run_decrypt(self, encrypted_path: Path, decode_key: str) -> Path | None:
        node = self._node_path()
        if not self.auto_decrypt or node is None or not self.decryptor.exists():
            self._count("decrypt_skipped")
            if node is None or not self.decryptor.exists():
                ctx.log.warn("node or the decryptor is unavailable; decrypt by hand with the recorded key")
            return None
        output = encrypted_path.with_name(encrypted_path.name.replace(".enc.mp4", ".mp4"))
        result = subprocess.run(
            [node, str(self.decryptor), "decrypt", "--key", decode_key, "--json",
             "-i", str(encrypted_path), "-o", str(output), "--quiet"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            self._count("decrypt_failed")
            self._write({
                "event": "decrypt_failed",
                "file_key": encrypted_path.name,
                "decode_key": decode_key,
                "returncode": result.returncode,
                "stderr": result.stderr[-800:],
            })
            ctx.log.info(f"decodeKey {decode_key} did not validate {encrypted_path.name}")
            try:
                output.unlink()
            except OSError:
                pass
            return None
        self._count("decrypt_ok")
        try:
            stem = encrypted_path.name.replace(".enc.mp4", "")
            (self.video_dir / f"{stem}.decode.json").write_text(
                result.stdout or "{}", encoding="utf-8"
            )
        except OSError:
            pass
        return output

    @staticmethod
    def _node_path() -> str | None:
        configured = os.environ.get("WX_NODE_PATH")
        if configured and Path(configured).is_file():
            return configured
        node = shutil.which("node")
        if node:
            return node
        for candidate in (
            "/opt/homebrew/bin/node",
            "/usr/local/bin/node",
            os.path.expanduser("~/.local/bin/node"),
        ):
            if Path(candidate).is_file():
                return candidate
        return None

    # ------------------------------------------------------------ JS rewrite
    def _rewrite_bundle(self, flow: http.HTTPFlow) -> None:
        response = flow.response
        try:
            response.decode()
            source = response.content.decode("utf-8")
        except (UnicodeDecodeError, AttributeError, ValueError) as exc:
            ctx.log.warn(f"Cannot decode JS bundle {flow.request.pretty_url}: {exc}")
            return

        bootstrap = _bootstrap_script(self.capture_token, CAPTURE_PATH)
        if INJECT_MARKER in source:
            # Already carries a bootstrap (for example from a cached rewrite).
            self._no_store(response)
            return

        # Current bundles contain: async finderGetCommentDetail(t){return ...}async
        # Keep the original request body intact and only observe its resolved object.
        pattern = re.compile(
            r"async\s+finderGetCommentDetail\((?P<arg>[A-Za-z_$][\w$]*)\)"
            r"\{return(?P<body>.*?)\}async",
            re.S,
        )
        match = pattern.search(source)
        hook_found = match is not None
        if hook_found:
            assert match is not None
            arg = match.group("arg")
            token = json.dumps(self.capture_token)
            capture_path = json.dumps(CAPTURE_PATH)
            injected = (
                f"async finderGetCommentDetail({arg}){{const __wxFeedResult=await{match.group('body')};"
                "try{const __wxDecoded=__wxFeedResult?.data?.object;"
                "if(__wxDecoded&&window.__wxDecodeScan)window.__wxDecodeScan('commentDetail',"
                "'FinderGetCommentDetail',__wxDecoded);"
                "if(__wxDecoded&&window.__wxDecodeMedia){const __wxA=__wxDecoded?.attachmentList"
                "?.attachments?.[0];const __wxM=(__wxA?.type===1?__wxA?.video?.video?.desc?.media?.[0]"
                ":null)||__wxDecoded?.objectDesc?.media?.[0];"
                "if(__wxM&&(__wxM.decodeKey||__wxM.decode_key)&&window.__wxDecodeReport)"
                "window.__wxDecodeReport({event:'media',source:'commentDetail',"
                "endpoint:'FinderGetCommentDetail',decodeKey:String(__wxM.decodeKey||__wxM.decode_key),"
                "url:String(__wxM.url||''),urlToken:__wxM.urlToken||__wxM.url_token||'',"
                "fileSize:__wxM.fileSize||__wxM.file_size||0,spec:__wxM.spec||null,"
                "objectId:__wxDecoded?.id||'',nonceId:__wxDecoded?.objectNonceId||'',"
                "title:__wxDecoded?.objectDesc?.description||''},'media');"
                f"else{{const __wxK=__wxDecoded?.id||__wxDecoded?.objectNonceId;"
                "fetch(location.origin+" + capture_path + ",{method:'POST',mode:'cors',"
                "headers:{'Content-Type':'text/plain','X-WX-Capture-Token':" + token + "},"
                "body:JSON.stringify({event:'comment_detail_without_key',rid:'fallback-'+Date.now(),"
                "objectId:String(__wxK||''),objectKeys:Object.keys(__wxDecoded||{}).slice(0,40),"
                "mediaKeys:Object.keys(__wxM||{}).slice(0,40)})}).catch(()=>{})}}"
                "}catch(__wxCaptureError){}return __wxFeedResult}async"
            )
            # This hook is optional — the page-side bootstrap is what actually
            # captures keys. A malformed splice would break the whole vendor
            # chunk and take the client's Finder pages down with it, so verify
            # the braces balance and otherwise skip it entirely.
            imbalance = self._brace_imbalance(injected)
            if imbalance:
                self._count("js_hook_skipped_unbalanced")
                self._write({
                    "event": "js_hook_skipped",
                    "url": flow.request.pretty_url,
                    "reason": "unbalanced_braces",
                    "imbalance": imbalance,
                })
                ctx.log.error(
                    f"Comment-detail hook is unbalanced ({imbalance}); serving the bundle "
                    "with only the safe bootstrap injection"
                )
                hook_found = False
            else:
                source = source[: match.start()] + injected + source[match.end():]

        # Prepend the bridge hook so it installs before any app code runs.
        rewritten = bootstrap + "\n" + source
        response.content = rewritten.encode("utf-8")
        self._no_store(response)
        self.js_seen = True
        self._count("js_hook_injected")
        self._write({
            "event": "js_hook_injected",
            "url": flow.request.pretty_url,
            "source_length": len(source),
            "rewritten_length": len(rewritten),
            "comment_detail_hook": hook_found,
            "bridge_bootstrap": True,
        })
        ctx.log.info(
            f"Injected capture bootstrap into {flow.request.pretty_url} "
            f"(comment-detail hook: {'yes' if hook_found else 'no'})"
        )

    @staticmethod
    def _no_store(response: http.Response) -> None:
        response.headers.pop("Content-Encoding", None)
        response.headers.pop("Content-Length", None)
        response.headers.pop("ETag", None)
        response.headers.pop("Last-Modified", None)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    # ----------------------------------------------------------------- intake
    def parse_capture_request(
        self,
        *,
        path: str,
        headers: dict[str, str],
        body: bytes,
        method: str,
    ) -> dict[str, Any] | None:
        """Validate and dispatch one bootstrap report. Returns the payload or None."""
        split = urlsplit(path)
        if split.path != CAPTURE_PATH:
            return None
        query = parse_qs(split.query)
        token = (
            headers.get("X-WX-Capture-Token")
            or headers.get("x-wx-capture-token")
            or (query.get("token") or [""])[0]
        )
        report_id = (query.get("rid") or [""])[0]
        if token != self.capture_token:
            self._count("report_token_mismatch")
            self._write({"event": "report_rejected", "reason": "token_mismatch", "rid": report_id})
            return None

        raw = body
        if method == "GET":
            inline = (query.get("d") or [""])[0]
            if not inline:
                self._count("report_get_without_payload")
                return None
            raw = unquote(inline).encode("utf-8")
        if not raw:
            return None

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._count("report_bad_json")
            self._write({
                "event": "report_invalid",
                "reason": "invalid_json",
                "error": str(exc),
                "sample": raw[:400].decode("utf-8", errors="replace"),
            })
            return None
        if not isinstance(payload, dict):
            self._count("report_not_object")
            return None

        dedupe_id = str(payload.get("rid") or report_id or "")
        if dedupe_id and self._is_duplicate(dedupe_id):
            return payload
        return self.accept_report(payload)

    def _is_duplicate(self, report_id: str) -> bool:
        now = time.time()
        with self.write_lock:
            # keep the set small; reports are few and short lived
            if len(self.seen_report_ids) > 4000:
                cutoff = now - 600
                self.seen_report_ids = {
                    key: stamp for key, stamp in self.seen_report_ids.items() if stamp > cutoff
                }
            if report_id in self.seen_report_ids:
                return True
            self.seen_report_ids[report_id] = now
            return False

    def accept_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = str(payload.get("event") or "unknown")
        self._count(f"report:{event}")
        if event == "bridge_request":
            self._track_viewing(payload)
        if event == "media":
            return self.accept_media(payload)
        if event == "live":
            return self.accept_live(payload)
        self._write({k: _jsonable(v) for k, v in payload.items()})
        if event == "bootstrap_installed":
            ctx.log.info("Capture bootstrap installed in the Finder page")
        elif event == "hook_status":
            ctx.log.info(
                "Hook status: bridge=%s wrapped=%s calls=%s media=%s reports=%s"
                % (
                    payload.get("bridgePresent"),
                    payload.get("wrapped"),
                    payload.get("hookCalls"),
                    payload.get("media"),
                    payload.get("reports"),
                )
            )
        return payload

    def _track_viewing(self, payload: dict[str, Any]) -> None:
        """Remember object ids whose comments were fetched (= on screen)."""
        endpoint = str(payload.get("endpoint") or "")
        if endpoint not in VIEWING_ENDPOINTS:
            return
        request = payload.get("request")
        if not isinstance(request, dict):
            return
        for key in ("objectid", "object_id", "objectId"):
            value = request.get(key)
            if value:
                with self.state_lock:
                    self.viewing_objects.setdefault(str(value), endpoint.rsplit("/", 1)[-1])
                break

    def accept_media(self, payload: dict[str, Any]) -> dict[str, Any]:
        decode_key = payload.get("decodeKey")
        url = str(payload.get("url") or "")
        file_key = self._file_key(url)
        if not decode_key:
            self._write({
                "event": "media_invalid",
                "reason": "decodeKey_missing",
                "media_keys": payload.get("mediaKeys"),
            })
            ctx.log.warn("Media report without a decodeKey")
            return payload
        record = {
            "event": "media",
            **{k: _jsonable(v) for k, v in payload.items()},
            "file_key": file_key,
        }
        self.latest_feed = record
        if file_key:
            with self.state_lock:
                bucket = self.media_by_file_key.setdefault(file_key, [])
                if not any(item.get("decodeKey") == record.get("decodeKey") for item in bucket):
                    bucket.append(record)
                # Keep the newest signing time: re-opening a prefetched video
                # hands us a fresh link and the old age would wrongly read as expired.
                self.signed_at[file_key] = max(
                    self.signed_at.get(file_key, 0.0), self._signed_at(record)
                )
        self._write(record)
        ctx.log.info(
            f"Captured decodeKey {decode_key!r} (file_key={file_key or 'unknown'}) from "
            f"{payload.get('source')}:{payload.get('endpoint')}"
        )
        # A key may arrive before or after its video; retry either way so a later
        # response can still decode an already-saved file.
        if file_key:
            for assembler in list(self.pending_completed):
                if assembler.file_key == file_key:
                    self.pending_completed.remove(assembler)
                    self._persist_video(assembler, self.media_by_file_key[file_key])
            for stem in self.stems_by_file_key.get(file_key, []):
                self._attempt_decode(stem, file_key)
            # Downloads are user-driven from the Web UI; auto-pulling is opt-in.
            if self.auto_fetch:
                self._start_variant_fetch(file_key, record)
        if self.save_video == "all":
            for assembler in list(self.pending_completed):
                self.pending_completed.remove(assembler)
                self._persist_video(assembler, self.media_by_file_key.get(assembler.file_key, []))
        return record

    def accept_live(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url") or "")
        endpoint = str(payload.get("endpoint") or "")
        if not url.startswith(("http://", "https://")):
            self._write({
                "event": "live_invalid",
                "reason": "live_cdn_url_missing",
                "live_keys": list(payload.keys()),
            })
            return payload
        if "pc_finderjoinlive" not in endpoint.lower():
            self._count("live_recommendation_ignored")
            return payload

        live_id = str(payload.get("liveId") or "")
        if not live_id:
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
            live_id = f"url-{digest}"
        record = {
            "event": "live",
            "live_id": live_id,
            "title": str(payload.get("title") or ""),
            "author": str(payload.get("author") or ""),
            "url": url,
            "source": str(payload.get("source") or ""),
            "endpoint": endpoint,
            "start_time": int(payload.get("startTime") or 0),
            "is_current": True,
            "captured_at": time.time(),
        }
        with self.state_lock:
            previous = self.live_by_id.get(live_id)
            self.live_by_id[live_id] = record
            self.current_live_id = live_id
        if previous is None or previous.get("url") != url or previous.get("title") != record["title"]:
            self._write(record)
            ctx.log.info(
                f"Captured live stream {live_id} from {payload.get('source')}:"
                f"{payload.get('endpoint')}"
            )
        return record

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _file_key(url: str) -> str:
        """Correlation key shared by an API media URL and a stodownload request.

        The API hands out playable URLs with ``m=`` left empty and the client
        only fills it in for some builds, so ``encfilekey`` is the dependable
        link between a captured decodeKey and the bytes on the wire.
        """
        for pattern in (r"[?&]encfilekey=([^&]+)", r"[?&]m=([^&]+)"):
            match = re.search(pattern, url or "")
            if match:
                return unquote(match.group(1))
        return ""

    @staticmethod
    def _brace_imbalance(source: str) -> int:
        """Net brace depth of a JS snippet; non-zero means it will not parse.

        Braces inside string literals are counted too, which makes this a
        conservative check: it can only refuse a splice, never approve a broken
        one. That is the right direction for a hook we can simply drop.
        """
        depth = 0
        for char in source:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    return depth
        return depth

    @staticmethod
    def _query_param(url: str, name: str) -> str:
        match = re.search(rf"[?&]{re.escape(name)}=([^&]+)", url or "")
        return unquote(match.group(1)) if match else ""

    @classmethod
    def _signed_at(cls, media: dict[str, Any]) -> float:
        """When the CDN signed this URL, from the ``svrnonce`` timestamp.

        ``svrnonce`` lives in ``urlToken``, not in ``url`` — the playable URL is
        the two concatenated — so both halves have to be searched. Falls back to
        now, which is a fine approximation when the media was just signed.
        """
        both = str(media.get("url") or "") + str(media.get("urlToken") or "")
        # Take the largest plausible value rather than the first match, so a
        # junk or stale nonce in one half cannot shadow the real one.
        stamps = [int(m) for m in re.findall(r"[?&]svrnonce=(\d{9,})", both)]
        return float(max(stamps)) if stamps else time.time()

    def freshest_media(self, file_key: str) -> dict[str, Any]:
        """The record with the most recently signed URL.

        The same video turns up in several responses — a prefetch from the feed
        flow, then again when the user actually opens it — each with its own
        decodeKey and its own expiry. Downloads must use the newest one.
        """
        with self.state_lock:
            medias = list(self.media_by_file_key.get(file_key) or [])
        if not medias:
            return {}
        return max(medias, key=self._signed_at)

    def _count(self, name: str) -> None:
        with self.state_lock:
            self.counters[name] = self.counters.get(name, 0) + 1

    @staticmethod
    def _sha256(data: bytes) -> str:
        import hashlib

        return hashlib.sha256(data).hexdigest()

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        with self.write_lock:
            with self.output_path.open("a", encoding="utf-8") as output:
                output.write(line + "\n")
        # Publish outside the lock: sinks may block on slow clients.
        for sink in list(self.event_sinks):
            try:
                sink(record)
            except Exception:  # pragma: no cover - a bad sink must not break capture
                pass


addons = [WeChatCapture()]

import os
import sys
import json
import time
import shutil
import base64
import subprocess
import re
import hashlib
import threading
import requests
from flask import Flask, request, jsonify, render_template, Response

# Ensure yt_dlp can be found: check common locations
_venv_ytdlp = os.path.join(os.path.expanduser("~"), "cc", ".venv", "lib")
if os.path.isdir(_venv_ytdlp):
    # Find the actual site-packages dir (version-dependent)
    for _sub in os.listdir(_venv_ytdlp):
        _sp = os.path.join(_venv_ytdlp, _sub, "site-packages")
        if os.path.isdir(_sp) and _sp not in sys.path:
            sys.path.insert(0, _sp)

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
    # 本地 .torrent 文件路径（绝对路径或 ~ 开头）
    if (u.endswith(".torrent") or u.endswith(".torrent?")) and not u.startswith(("http://", "https://", "ftp://", "ftps://", "sftp://", "magnet:", "ed2k://", "thunder://")):
        return "torrent_file"
    if u.startswith(("http://", "https://")):
        # 远程 .torrent 文件 URL
        path_part = u.split("?")[0].split("#")[0]
        if path_part.endswith(".torrent"):
            return "torrent_url"
        # Bilibili — dedicated API handler (DASH stream extraction + ffmpeg merge)
        if re.match(r'https?://(www\.)?bilibili\.com/(video|bangumi)/', u):
            return "bilibili"
        if re.match(r'https?://b23\.tv/', u):
            return "bilibili"
        # YouTube / X (Twitter) — dedicated yt-dlp handler
        if re.match(r'https?://(www\.)?(youtube\.com|youtu\.be)/', u):
            return "yt_media"
        if re.match(r'https?://(www\.)?(x\.com|twitter\.com|t\.co)/', u):
            return "yt_media"
        # 微博 — yt-dlp handler
        if re.match(r'https?://(www\.)?(weibo\.com|weibo\.cn|video\.weibo\.com|m\.weibo\.cn)/', u):
            return "weibo"
        # 抖音 — yt-dlp handler
        if re.match(r'https?://(www\.)?(douyin\.com|v\.douyin\.com|iesdouyin\.com)/', u):
            return "douyin"
        # TikTok — yt-dlp handler
        if re.match(r'https?://(www\.)?(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)/', u):
            return "tiktok"
        # Facebook — yt-dlp handler
        if re.match(r'https?://(www\.)?facebook\.com/', u) and ('/video' in u or '/watch' in u or '/reel' in u or '/posts' in u or '/permalink' in u):
            return "facebook"
        # Spotify — yt-dlp handler
        if re.match(r'https?://open\.spotify\.com/(track|album|playlist|episode|show)/', u):
            return "spotify"
        # 网易云音乐 — yt-dlp handler
        if re.match(r'https?://music\.163\.com/#/(song|album|playlist|dj|mv)', u):
            return "netease"
        # 快手 — yt-dlp handler
        if re.match(r'https?://(www\.)?(kuaishou\.com|gif\.kuaishou\.com|v\.kuaishou\.com)/', u):
            return "kuaishou"
        # 小红书 — yt-dlp handler
        if re.match(r'https?://(www\.)?(xiaohongshu\.com|xhslink\.com)/', u):
            return "xiaohongshu"
        # 西瓜视频 — yt-dlp handler
        if re.match(r'https?://(www\.)?ixigua\.com/', u):
            return "ixigua"
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


def _detect_login_required(html):
    """检测页面是否需要登录才能查看视频内容。返回提示字符串。"""
    # Common patterns for gated/login-required content
    patterns = [
        (r'class="[^"]*login[^"]*modal[^"]*"', '页面包含登录弹窗，该视频可能需要注册/登录后才能观看'),
        (r'class="[^"]*register[^"]*modal[^"]*"', '该内容需要注册后才能观看'),
        (r'class="[^"]*sign-in[^"]*"', '该内容需要登录后才能观看'),
        (r'isJoined\s*[:=]\s*false', '该内容需要注册后才能观看（Webinar/直播类型）'),
        (r'requires\s+registration', '该内容需要注册后才能观看'),
        (r'loginRequired\s*[:=]\s*true', '该内容需要登录后才能观看'),
        (r'class="[^"]*gated[^"]*content[^"]*"', '该内容为付费/受限内容，需要登录后才能观看'),
        (r'class="[^"]*paywall[^"]*"', '该内容有付费墙限制，需要订阅后才能观看'),
        (r'class="[^"]*restricted[^"]*"', '该内容受访问限制，可能需要登录'),
        (r'class="[^"]*auth[^"]*required[^"]*"', '该内容需要认证后才能观看'),
        # Brightcove player without video ID loaded = gated content
        (r'brightcove[^>]*data-video-id=""', '视频播放器未加载视频（可能需要登录）'),
    ]
    for pattern, hint in patterns:
        if re.search(pattern, html, re.IGNORECASE):
            return hint
    # If page has Brightcove/Bizzabo but no video content loaded
    if 'brightcove' in html.lower() and 'video' not in html.lower():
        return '页面使用了Brightcove视频播放器，但视频内容可能需要登录后加载'
    return ''


def _extract_with_playwright(url):
    """使用 Playwright 无头浏览器渲染页面，拦截网络请求中的 m3u8/mp4 URL。

    这能处理 requests 无法看到的 JS 动态内容：
    - 执行页面中的 JavaScript（Brightcove/Bizzabo等播放器会初始化）
    - 拦截浏览器发出的网络请求（m3u8 manifest、mp4 视频流）
    - 自动从 Chrome 复用 cookie（登录态）
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    video_urls = []       # m3u8/mp4 URLs intercepted from network
    page_title = ""
    page_poster = ""

    try:
        with sync_playwright() as p:
            # Use persistent context with Chrome's user data — inherits ALL login cookies
            # No need to manually read Chrome's cookie database
            chrome_profile = os.path.expanduser("~/Library/Application Support/Google/Chrome")
            if os.path.exists(chrome_profile):
                context = p.chromium.launch_persistent_context(
                    chrome_profile,
                    headless=True,
                    channel="chrome",
                    args=["--disable-blink-features=AutomationControlled"],
                )
            else:
                # Fallback: fresh browser without cookies
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()

            # Intercept network responses to capture video URLs
            def handle_response(response):
                resp_url = response.url
                # Capture m3u8 manifests (highest priority — adaptive bitrate)
                if '.m3u8' in resp_url:
                    video_urls.append(("m3u8", resp_url))
                # Capture mp4/webm video streams (must be > 500KB to be real video, not a thumbnail)
                elif any(ext in resp_url for ext in ['.mp4', '.webm']):
                    try:
                        cl = int(response.headers.get("content-length", "0") or "0")
                        if cl > 500000:  # > 500KB = likely video stream
                            video_urls.append(("direct", resp_url))
                    except Exception:
                        video_urls.append(("direct", resp_url))

            page = context.new_page()
            page.on("response", handle_response)

            try:
                page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(5000)
                except Exception:
                    pass

            # Extract title from the rendered page
            try:
                page_title = page.title() or ""
                # Clean up title (remove site name suffix)
                for suffix in [" | Gartner", " - Bizzabo", " | Bizzabo", " — Vimeo", " | YouTube"]:
                    if suffix in page_title:
                        page_title = page_title.split(suffix)[0].strip()
            except Exception:
                pass

            # Extract poster/thumbnail from rendered DOM
            try:
                page_poster = page.evaluate("""
                    () => {
                        const og = document.querySelector('meta[property="og:image"]');
                        return og ? og.content : '';
                    }
                """) or ""
            except Exception:
                pass

            # Try to find <video> element in rendered DOM
            try:
                video_src = page.evaluate("""
                    () => {
                        const v = document.querySelector('video');
                        if (v) return v.src || v.currentSrc || '';
                        const s = document.querySelectorAll('video source');
                        if (s.length) return s[0].src || '';
                        return '';
                    }
                """)
                if video_src and not any(u[1] == video_src for u in video_urls):
                    vtype = "m3u8" if '.m3u8' in video_src else "direct"
                    video_urls.append((vtype, video_src))
            except Exception:
                pass

            # Click play button if present (triggers video stream loading)
            try:
                play_btn = page.query_selector(
                    '[class*="play"], [aria-label*="Play"], [aria-label*="播放"], '
                    '.vjs-big-play-button, .play-btn, .play-button, [data-play]'
                )
                if play_btn:
                    play_btn.click()
                    page.wait_for_timeout(3000)
            except Exception:
                pass

            context.close()

    except Exception:
        return None

    if not video_urls:
        return None

    # Deduplicate and prefer m3u8 (most versatile)
    seen = set()
    best_url = ""
    best_type = ""
    all_urls = []  # for format selection
    for vtype, vurl in video_urls:
        if vurl in seen:
            continue
        seen.add(vurl)
        all_urls.append({"url": vurl, "type": vtype})
        if vtype == "m3u8" and (not best_url or best_type != "m3u8"):
            best_url = vurl
            best_type = "m3u8"
        elif vtype == "direct" and not best_url:
            best_url = vurl
            best_type = "direct"

    if not best_url:
        return None

    title = page_title or _extract_title_from_url(url)

    return {
        "ok": True, "title": title, "poster": page_poster,
        "m3u8_url": best_url, "magnet": "", "type": best_type,
    }


def _extract_title_from_url(url):
    """Fallback: derive a title from the URL path."""
    from urllib.parse import urlparse
    path = urlparse(url).path.rstrip('/')
    segments = path.split('/')
    return segments[-1] if segments else "视频"



def _extract_with_ytdlp(url):
    """使用 yt-dlp（带浏览器 cookie）回退提取视频。

    当通用解析器无法从HTML中提取视频时，yt-dlp可以：
    1. 自动识别网站类型（支持1800+站点）
    2. 从浏览器 cookie 中获取登录态（Chrome/Safari）
    3. 处理需登录的付费内容
    """
    try:
        import yt_dlp
    except ImportError:
        return None

    # Try with browser cookies first (handles login-required content)
    for browser in ['chrome', 'safari']:
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False,
                'skip_download': True,
                'cookiesfrombrowser': (browser,),
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            if not info:
                continue

            title = info.get('title', '') or '未知视频'
            thumbnail = info.get('thumbnail', '') or ''
            duration = info.get('duration', 0) or 0
            uploader = info.get('uploader', '') or ''
            description = (info.get('description', '') or '')[:200]
            platform = info.get('extractor_key', '') or ''

            # Build format list for preview card
            formats_raw = info.get('formats', []) or []
            formats = []
            seen_heights = set()
            for f in formats_raw:
                height = f.get('height') or 0
                ext = f.get('ext', '') or ''
                vcodec = f.get('vcodec', '') or ''
                acodec = f.get('acodec', '') or ''
                vbr = f.get('vbr') or 0
                abr = f.get('abr') or 0
                tbr = f.get('tbr') or 0
                format_id = f.get('format_id', '') or ''
                filesize = f.get('filesize') or f.get('filesize_approx') or 0

                is_video_audio = vcodec != 'none' and acodec != 'none'
                is_video_only = vcodec != 'none' and (acodec == 'none' or not acodec)
                is_audio_only = vcodec == 'none' and acodec != 'none'

                dedup_key = None
                if is_video_audio:
                    dedup_key = ('va', height, ext)
                elif is_video_only:
                    dedup_key = ('v', height, ext)
                elif is_audio_only:
                    dedup_key = ('a', int(abr), ext)
                if dedup_key and dedup_key in seen_heights:
                    continue
                if dedup_key:
                    seen_heights.add(dedup_key)

                # Build display label
                label = ''
                if height:
                    label = f'{height}P'
                elif is_audio_only and abr:
                    label = f'{int(abr)}kbps'
                elif tbr:
                    label = f'{int(tbr)}kbps'

                if label and ext:
                    label += f' · {ext}'
                if label and filesize:
                    label += f' · {_fmt_size(filesize)}'

                if not label:
                    label = format_id or 'unknown'

                formats.append({
                    'format_id': format_id,
                    'label': label,
                    'is_video_audio': is_video_audio,
                    'is_video_only': is_video_only,
                    'is_audio_only': is_audio_only,
                    'height': height,
                    'width': f.get('width') or 0,
                    'ext': ext,
                    'tbr': tbr,
                    'filesize': filesize,
                })

            # Keep format list manageable
            formats = formats[:15]

            return {
                "ok": True, "type": "yt_media",
                "title": title, "poster": thumbnail,
                "m3u8_url": "", "magnet": "",
                "duration": duration, "uploader": uploader,
                "description": description, "platform": platform or 'yt-dlp',
                "url": url, "formats": formats,
            }

        except Exception:
            continue

    # All browsers failed — try without cookies (for public content yt-dlp handles better)
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'skip_download': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info:
            title = info.get('title', '') or '未知视频'
            thumbnail = info.get('thumbnail', '') or ''
            platform = info.get('extractor_key', '') or ''
            formats_raw = info.get('formats', []) or []
            formats = []
            for f in formats_raw[:10]:
                height = f.get('height') or 0
                ext = f.get('ext', '') or ''
                label = f'{height}P · {ext}' if height else f.get('format_id', '')
                formats.append({
                    'format_id': f.get('format_id', ''),
                    'label': label,
                    'is_video_audio': f.get('vcodec', 'none') != 'none' and f.get('acodec', 'none') != 'none',
                    'is_video_only': f.get('vcodec', 'none') != 'none' and f.get('acodec', 'none') == 'none',
                    'is_audio_only': f.get('vcodec', 'none') == 'none' and f.get('acodec', 'none') != 'none',
                })
            return {
                "ok": True, "type": "yt_media",
                "title": title, "poster": thumbnail,
                "m3u8_url": "", "magnet": "",
                "duration": info.get('duration', 0) or 0,
                "uploader": info.get('uploader', '') or '',
                "description": (info.get('description', '') or '')[:200],
                "platform": platform or 'yt-dlp',
                "url": url, "formats": formats,
            }
    except Exception:
        pass

    return None


def _fmt_size(size):
    """Format file size in human-readable format."""
    if not size:
        return ''
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} TB'


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
        # 磁力链接：匹配 href 属性中的链接，以及页面文本中的磁力链接
        magnet_match = re.search(r'(?:href=")?(magnet:\?xt=urn:btih:[a-fA-F0-9]{40}[^"<\s]*)(?:"|\b)', html)
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

        # 策略 5: 页面中有磁力链接（无视频时仍可下载种子）
        if magnet:
            title = _extract_title(html)
            return {"ok": True, "title": title, "poster": poster,
                    "magnet": magnet, "m3u8_url": "", "type": "torrent"}

        # 策略 5.5: Playwright 无头浏览器渲染 — 执行JS，拦截m3u8/mp4网络请求
        pw_result = _extract_with_playwright(url)
        if pw_result and pw_result.get("ok"):
            return pw_result

        # 策略 6: yt-dlp 回退 — 用浏览器 cookie 尝试提取需登录的视频
        yt_result = _extract_with_ytdlp(url)
        if yt_result and yt_result.get("ok"):
            return yt_result

        hint = _detect_login_required(html)
        if hint:
            hint += '\n提示：如果你在浏览器中已登录该网站，再次粘贴同一链接，工具会自动从浏览器读取登录Cookie来提取视频。'
        return {"ok": False, "error": "未解析出可下载内容", "hint": hint}

    except requests.RequestException as e:
        return {"ok": False, "error": "未解析出可下载内容"}
    except Exception as e:
        return {"ok": False, "error": "未解析出可下载内容"}


TYPES = {
    "torrent": "种子/磁力",
    "http": "HTTP 直链",
    "ftp": "FTP",
    "ed2k": "电驴→种子搜索",
    "yt_media": "YouTube 视频",
    "bilibili": "B站视频",
    "weibo": "微博视频",
    "douyin": "抖音视频",
    "tiktok": "TikTok 视频",
    "facebook": "Facebook 视频",
    "spotify": "Spotify 音乐",
    "netease": "网易音乐",
    "kuaishou": "快手视频",
    "xiaohongshu": "小红书",
    "ixigua": "西瓜视频",
    "quark": "夸克网盘",
    "cloud": "网盘链接",
    "thunder": "迅雷链接",
    "m3u8": "M3U8 流媒体",
    "direct": "直接视频",
}
UNSUPPORTED = {"quark", "cloud", "thunder"}
UNSUPPORTED_MSG = {
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
#  Bilibili video extraction (DASH stream API + ffmpeg merge)
# --------------------------------------------------------------------------- #

BILI_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://www.bilibili.com',
}

BILI_QUALITY_MAP = {
    127: '8K 超清', 120: '4K 超清', 116: '1080P 60帧',
    112: '1080P 高码率', 80: '1080P 清晰', 74: '720P 高码率',
    64: '720P', 48: '720P 60帧', 32: '480P', 16: '360P',
}
BILI_CODEC_MAP = {7: 'H264', 12: 'H265', 13: 'AV1'}
BILI_AUDIO_MAP = {30250: '杜比全景声', 30251: 'Hi-Res', 30232: '192Kbps', 30216: '128Kbps'}

# B站扫码登录 — 生成二维码、轮询扫码状态、自动保存Cookie
BILI_QR_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://passport.bilibili.com',
    'Origin': 'https://passport.bilibili.com',
}

# B站 SESSDATA cookie（可选，提供后可获取1080P以上画质）
# 存在 ~/.bilibili_cookie.json 中，格式: {"SESSDATA": "..."}
_BILI_COOKIE_FILE = os.path.join(os.path.expanduser("~"), ".bilibili_cookie.json")


def _get_bili_cookie():
    """读取用户保存的B站 SESSDATA cookie（用于获取1080P+画质）。"""
    if os.path.exists(_BILI_COOKIE_FILE):
        try:
            data = json.load(open(_BILI_COOKIE_FILE))
            sessdata = data.get("SESSDATA", "")
            if sessdata:
                return {"SESSDATA": sessdata}
        except Exception:
            pass
    return {}


def _bili_qr_generate():
    """生成B站扫码登录二维码，返回 qrcode_key + 二维码URL。
    二维码图片由前端JS生成（避免依赖 PIL）。"""
    gen_url = 'https://passport.bilibili.com/x/passport-login/web/qrcode/generate'
    r = requests.get(gen_url, headers=BILI_QR_HEADERS, timeout=10)
    data = r.json()
    if data.get('code') != 0:
        return {"ok": False, "error": f"生成二维码失败：{data.get('message', '未知错误')}"}

    result = data.get('data', {}) or {}
    qrcode_key = result.get('qrcode_key', '')
    qr_url = result.get('url', '')

    return {
        "ok": True,
        "qrcode_key": qrcode_key,
        "qr_url": qr_url,
    }


def _bili_qr_poll(qrcode_key):
    """轮询B站扫码状态，返回扫码结果。
    状态码：86101=未扫码，86102=已扫码待确认，0=已确认成功
    成功时返回 SESSDATA 等 cookie 信息，自动保存到文件。
    """
    poll_url = f'https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}'
    r = requests.get(poll_url, headers=BILI_QR_HEADERS, timeout=10)
    data = r.json()

    result = data.get('data', {}) or {}
    code = result.get('code', -1)
    message = result.get('message', '')
    # Refresh_token for long-term validity
    refresh_token = result.get('refresh_token', '')

    if code == 0:
        # Success! Extract SESSDATA from response cookies
        sessdata = ''
        # The API returns cookies in the 'Set-Cookie' header of the response
        # or in the response body's 'url' field
        # Let's parse from response headers
        cookie_headers = r.headers.get('Set-Cookie', '')
        for part in cookie_headers.split(','):
            part = part.strip()
            if 'SESSDATA' in part:
                # Extract value: "SESSDATA=xxx; Path=..."
                m = re.match(r'SESSDATA=([^;]+)', part)
                if m:
                    sessdata = m.group(1)
                    break

        # If SESSDATA not in headers, try parsing from the response body
        if not sessdata:
            # Some B站 API versions return it differently
            # Check the redirect_url for cookie info
            redirect_url = result.get('url', '')
            if redirect_url:
                # Parse SESSDATA from URL parameters
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(redirect_url)
                qs = parse_qs(parsed.query)
                if 'SESSDATA' in qs:
                    sessdata = qs['SESSDATA'][0]

        # Also check the response JSON body directly (newer API)
        if not sessdata and result.get('SESSDATA'):
            sessdata = result['SESSDATA']

        if sessdata:
            # Auto-save to cookie file
            cookie_data = {"SESSDATA": sessdata}
            if refresh_token:
                cookie_data["refresh_token"] = refresh_token
            json.dump(cookie_data, open(_BILI_COOKIE_FILE, "w"))
            return {
                "ok": True,
                "code": 0,
                "message": "登录成功！Cookie已自动保存，下次解析B站视频将使用登录身份获取1080P+画质。",
                "SESSDATA": sessdata,
            }
        else:
            # Login confirmed but couldn't extract SESSDATA
            # The Set-Cookie might be missing because we're polling, not the actual login page
            # Try to extract from the full response text
            return {
                "ok": False,
                "code": 0,
                "message": "扫码确认成功，但无法提取登录Cookie。请手动设置Cookie。",
                "error": "Cookie提取失败",
            }

    elif code == 86101:
        return {"ok": True, "code": 86101, "message": "等待扫码"}
    elif code == 86102:
        return {"ok": True, "code": 86102, "message": "已扫码，等待确认"}
    else:
        return {"ok": False, "code": code, "message": message or "扫码登录失败"}


def _resolve_bili_url(url):
    """从B站 URL 中提取 BV 号或 bangumi epid。
    支持: bilibili.com/video/BVxxxx, b23.tv/短链, bilibili.com/bangumi/play/epxxxx
    返回: (mode, id)  mode='video'|'bangumi', id=BV号或epid
    """
    u = url.strip()
    # b23.tv short link → follow redirect
    if re.match(r'https?://b23\.tv/', u):
        try:
            r = requests.get(u, headers=BILI_HEADERS, timeout=10, allow_redirects=True)
            u = r.url
        except Exception:
            pass
    # bilibili.com/video/BVxxxx
    m = re.search(r'/video/(BV[A-Za-z0-9]+)', u)
    if m:
        return ('video', m.group(1))
    # bilibili.com/bangumi/play/epxxxx
    m = re.search(r'/bangumi/play/ep(\d+)', u)
    if m:
        return ('bangumi', int(m.group(1)))
    # bilibili.com/bangumi/play/ssxxxx (season)
    m = re.search(r'/bangumi/play/ss(\d+)', u)
    if m:
        return ('season', int(m.group(1)))
    return None


def _extract_bili_video(bvid, page=1, sessdata=None):
    """从B站视频 BV 号提取元数据和可用画质。

    策略：先用 fnval=0（legacy mp4直链，无需登录可获取720P）
    再用 fnval=16（DASH分片，登录后可获取1080P+），合并两种结果供前端选择。
    """
    cookies = sessdata or _get_bili_cookie()
    headers = {**BILI_HEADERS}
    if cookies:
        headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())

    # Step 1: Get video metadata
    api_url = f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}'
    r = requests.get(api_url, headers=headers, timeout=10)
    data = r.json()
    if data.get('code') != 0:
        return {"ok": False, "error": f"获取视频信息失败：{data.get('message', '未知错误')}", "type": "bilibili"}

    info = data['data']
    title = info.get('title', '') or '未知视频'
    pic = info.get('pic', '') or ''
    duration = info.get('duration', 0) or 0
    owner = (info.get('owner') or {}).get('name', '') or ''
    desc = (info.get('desc', '') or '')[:200]

    # Multi-page (分P) support
    pages = info.get('pages', []) or []
    page_list = []
    for p in pages:
        page_list.append({
            'page': p.get('page', 1),
            'title': p.get('part', '') or f'P{p.get("page", 1)}',
            'cid': p.get('cid'),
            'duration': p.get('duration', 0),
        })

    # Select the requested page
    selected_page = None
    for p in page_list:
        if p['page'] == page:
            selected_page = p
            break
    if not selected_page:
        selected_page = page_list[0] if page_list else {'page': 1, 'title': title, 'cid': info.get('cid'), 'duration': duration}
    cid = selected_page.get('cid') or info.get('cid')
    aid = info.get('aid')

    # Step 2: Get legacy mp4 direct links (fnval=0) — NO LOGIN needed, gives 720P!
    # ParseVideo 的做法：直接用 fnval=0 获取 mp4 直链
    legacy_formats = []
    seen_legacy_qn = set()
    for qn in [80, 64, 32, 16]:
        playurl = f'https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={qn}&fnval=0'
        r_leg = requests.get(playurl, headers=headers, timeout=10)
        leg_data = r_leg.json()
        if leg_data.get('code') != 0:
            continue
        d = leg_data['data']
        actual_qn = d.get('quality', 0)
        if actual_qn in seen_legacy_qn:
            continue
        seen_legacy_qn.add(actual_qn)
        durl_list = d.get('durl', []) or []
        total_size = sum(du.get('size', 0) for du in durl_list)
        direct_urls = [du.get('url', '') for du in durl_list if du.get('url')]
        # Verify first URL is accessible (needs Referer)
        if direct_urls:
            try:
                r_test = requests.get(direct_urls[0], headers={**headers, 'Range': 'bytes=0-1023'}, timeout=5)
                if r_test.status_code in (200, 206):
                    qlabel = BILI_QUALITY_MAP.get(actual_qn, f'{actual_qn}P')
                    legacy_formats.append({
                        'quality_id': actual_qn,
                        'codec_id': 0,
                        'label': qlabel + ' (直链)',
                        'codec': 'H264 直链',
                        'width': 0, 'height': 0, 'bandwidth': 0,
                        'direct_url': direct_urls[0],
                        'direct_urls': direct_urls,
                        'total_size': total_size,
                        'segments': len(durl_list),
                        'is_legacy': True,
                        'is_video': True,
                    })
            except Exception:
                pass
        # 720P+ already found — no need to check lower qualities
        if seen_legacy_qn and max(seen_legacy_qn) >= 64:
            break

    # Step 3: Get DASH stream URLs (fnval=16) — needs login for 1080P+
    dash_formats = []
    audio_formats = []
    playurl = f'https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn=120&fnval=16&fourk=1'
    r2 = requests.get(playurl, headers=headers, timeout=10)
    stream_data = r2.json()
    if stream_data.get('code') == 0:
        dash_info = stream_data['data']
        dash = dash_info.get('dash', {}) or {}
        video_streams = dash.get('video', []) or []
        audio_streams = dash.get('audio', []) or []

        seen = set()
        for v in video_streams:
            qid = v.get('id', 0)
            codec = v.get('codecid', 0)
            key = (qid, codec)
            if key in seen:
                continue
            seen.add(key)
            # Only include DASH formats that offer BETTER quality than legacy
            # Legacy already gives 720P without login, so skip 480P/360P DASH (no value)
            # Only 1080P+ DASH (needs login) is worth showing
            if qid < 80:
                continue
            qlabel = BILI_QUALITY_MAP.get(qid, f'{qid}P')
            codec_label = BILI_CODEC_MAP.get(codec, f'codec{codec}')
            dash_formats.append({
                'quality_id': qid, 'codec_id': codec,
                'label': qlabel, 'codec': codec_label,
                'width': v.get('width', 0), 'height': v.get('height', 0),
                'bandwidth': v.get('bandwidth', 0),
                'video_url': v.get('baseUrl', '') or '',
                'video_backup_urls': v.get('backupUrl', []) or [],
                'mimeType': v.get('mimeType', ''),
                'is_dash': True, 'is_video': True,
            })

        seen_audio = set()
        for a in audio_streams:
            aid_ = a.get('id', 0)
            if aid_ in seen_audio:
                continue
            seen_audio.add(aid_)
            alabel = BILI_AUDIO_MAP.get(aid_, f'{aid_}Kbps')
            audio_formats.append({
                'audio_id': aid_, 'label': alabel,
                'bandwidth': a.get('bandwidth', 0),
                'audio_url': a.get('baseUrl', '') or '',
                'audio_backup_urls': a.get('backupUrl', []) or [],
                'mimeType': a.get('mimeType', ''),
                'is_audio': True,
            })

    # Combine: legacy formats first (no login needed!), then DASH formats
    all_formats = legacy_formats + dash_formats

    return {
        "ok": True, "type": "bilibili",
        "title": title, "poster": pic,
        "m3u8_url": "", "magnet": "",
        "duration": duration, "uploader": owner, "description": desc,
        "platform": "B站",
        "bvid": bvid, "aid": aid, "cid": cid,
        "selected_page": selected_page, "page_list": page_list,
        "formats": all_formats, "audio_formats": audio_formats,
        "has_login": bool(cookies),
        "has_legacy": bool(legacy_formats),
        "bili_source": True,
    }


def _extract_bili_bangumi(epid, sessdata=None):
    """从B站番剧 ep 链接提取视频信息。
    番剧/影视剧需要 pgc API 获取 episode → cid，再用 playurl 获取流。
    """
    cookies = sessdata or _get_bili_cookie()
    headers = {**BILI_HEADERS}
    if cookies:
        headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())

    # Get episode info from pgc API
    api_url = f'https://api.bilibili.com/pgc/view/web/episode?ep_id={epid}'
    r = requests.get(api_url, headers=headers, timeout=10)
    data = r.json()
    if data.get('code') != 0:
        # Try alternate: get season info, find the ep
        return {"ok": False, "error": f"获取番剧信息失败：{data.get('message', '未知错误')}。可能需要登录或该番剧不可用。", "type": "bilibili"}

    result = data.get('result', {}) or {}
    title = result.get('share_copy', '') or result.get('long_title', '') or '番剧'
    pic = result.get('cover', '') or ''
    duration = result.get('duration', 0) or 0
    bvid = result.get('bvid', '') or ''
    cid = result.get('cid', 0) or 0
    aid = result.get('aid', 0) or 0

    if not bvid or not cid:
        return {"ok": False, "error": "无法获取番剧视频信息，可能需要登录Cookie。", "type": "bilibili"}

    # Reuse the video extraction logic with the obtained bvid/cid
    playurl = f'https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn=120&fnval=16&fourk=1'
    r2 = requests.get(playurl, headers=headers, timeout=10)
    stream_data = r2.json()
    if stream_data.get('code') != 0:
        return {"ok": False, "error": f"获取视频流失败：{stream_data.get('message', '未知错误')}", "type": "bilibili"}

    dash = stream_data['data'].get('dash', {}) or {}
    video_streams = dash.get('video', []) or []
    audio_streams = dash.get('audio', []) or []

    formats = []
    seen = set()
    for v in video_streams:
        qid, codec = v.get('id', 0), v.get('codecid', 0)
        if (qid, codec) in seen: continue
        seen.add((qid, codec))
        formats.append({
            'quality_id': qid, 'codec_id': codec,
            'label': BILI_QUALITY_MAP.get(qid, f'{qid}P'),
            'codec': BILI_CODEC_MAP.get(codec, f'codec{codec}'),
            'width': v.get('width', 0), 'height': v.get('height', 0),
            'bandwidth': v.get('bandwidth', 0),
            'video_url': v.get('baseUrl', ''), 'video_backup_urls': v.get('backupUrl', []),
            'mimeType': v.get('mimeType', ''), 'is_video': True,
        })

    audio_formats = []
    seen_audio = set()
    for a in audio_streams:
        aid_ = a.get('id', 0)
        if aid_ in seen_audio: continue
        seen_audio.add(aid_)
        audio_formats.append({
            'audio_id': aid_, 'label': BILI_AUDIO_MAP.get(aid_, f'{aid_}Kbps'),
            'bandwidth': a.get('bandwidth', 0),
            'audio_url': a.get('baseUrl', ''), 'audio_backup_urls': a.get('backupUrl', []),
            'mimeType': a.get('mimeType', ''), 'is_audio': True,
        })

    return {
        "ok": True, "type": "bilibili",
        "title": title, "poster": pic,
        "m3u8_url": "", "magnet": "",
        "duration": duration, "uploader": '', "description": '',
        "platform": "B站番剧", "bvid": bvid, "aid": aid, "cid": cid,
        "selected_page": {'page': 1, 'title': title, 'cid': cid, 'duration': duration},
        "page_list": [{'page': 1, 'title': title, 'cid': cid, 'duration': duration}],
        "formats": formats, "audio_formats": audio_formats,
        "has_login": bool(cookies), "bili_source": True, "is_bangumi": True,
    }


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
#  ed2k link → magnet/torrent converter
# --------------------------------------------------------------------------- #
def parse_ed2k_url(url):
    """Parse an ed2k:// link to extract file name, size, and hash."""
    m = re.match(
        r'ed2k://\|file\|([^|]+)\|(\d+)\|([A-Fa-f0-9]{32})\|',
        url.strip()
    )
    if m:
        return {
            "name": m.group(1),
            "size": int(m.group(2)),
            "hash": m.group(3).upper(),
        }
    return None


def search_magnet_for_ed2k(file_name, file_size):
    """Search for a magnet/torrent link for the same file as an ed2k link.

    Primary strategy: use FlareSolverr (local Docker service on port 8191)
    to bypass Cloudflare, search 1337x for torrent detail pages, then visit
    the detail page to extract the magnet link.

    Fallback: direct HTTP requests (likely blocked by Cloudflare).
    """
    import urllib.parse

    # Clean up file name for search
    search_name = re.sub(r'\.\w{1,4}$', '', file_name)
    search_query = re.sub(r'[\.\-_]+', ' ', search_name).strip()
    if len(search_query) > 60:
        search_query = search_query[:60]

    search_lower = search_query.lower()

    # ---- Strategy 1: FlareSolverr (bypasses Cloudflare with real Chrome) ----
    flaresolverr_url = "http://localhost:8191/v1"

    def _fs_get(url, max_timeout=30000):
        """Send a request through FlareSolverr."""
        try:
            r = requests.post(flaresolverr_url, json={
                "cmd": "request.get",
                "url": url,
                "maxTimeout": max_timeout,
            }, timeout=max_timeout / 1000 + 10)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "ok":
                    return data.get("solution", {}).get("response", "")
            return None
        except Exception:
            return None

    # 1a: Search 1337x for torrent detail page links
    search_url = f"https://1337x.to/search/{urllib.parse.quote_plus(search_query)}/1/"
    search_html = _fs_get(search_url, max_timeout=60000)

    if search_html:
        # 1337x search results contain links to detail pages like /torrent/ID/SLUG/
        detail_links = re.findall(r'href="/torrent/(\d+)/([^"/]+)/"', search_html)

        # Also extract display names from search result rows
        # 1337x uses class="coll-5 name" in table rows
        row_data = re.findall(
            r'href="/torrent/\d+/([^"/]+)/"[^>]*>\s*([^<]+)',
            search_html
        )

        # Visit each detail page (limit 3) to find magnet link
        for idx, (torrent_id, slug) in enumerate(detail_links[:3]):
            detail_url = f"https://1337x.to/torrent/{torrent_id}/{slug}/"
            detail_html = _fs_get(detail_url, max_timeout=60000)

            if not detail_html:
                continue

            # Extract magnet links from detail page
            # 1337x uses &amp; for & in HTML, need to handle that
            magnets = re.findall(
                r'magnet:\?xt=urn:btih:[a-fA-F0-9]{40}(?:&amp;[^"<\s]*|[^"<\s]*)',
                detail_html
            )
            # Clean up &amp; → &
            magnets = [m.replace('&amp;', '&') for m in magnets]

            if not magnets:
                continue

            # Try name matching
            for magnet in magnets:
                dn_match = re.search(r'dn=([^&]+)', magnet)
                magnet_name = ""
                if dn_match:
                    magnet_name = urllib.parse.unquote(dn_match.group(1))

                if magnet_name:
                    clean_m = re.sub(r'[\.\-_]', ' ', magnet_name.lower())
                    # Check if ed2k file name appears in torrent name
                    keywords = search_lower.split()
                    if any(k in clean_m for k in keywords[:3]) or search_lower[:15] in clean_m:
                        return {
                            "ok": True,
                            "title": magnet_name,
                            "magnet": magnet,
                            "size": file_size,
                            "source": "torrent_search",
                        }

            # Name match failed but magnets exist on a relevant page
            page_title = re.search(r'<title[^>]*>(.*?)</title>', detail_html, re.IGNORECASE)
            if page_title and search_lower[:15] in page_title.group(1).lower() and magnets:
                first_magnet = magnets[0]
                dn_match = re.search(r'dn=([^&]+)', first_magnet)
                title = urllib.parse.unquote(dn_match.group(1)) if dn_match else search_name
                return {
                    "ok": True, "title": title,
                    "magnet": first_magnet, "size": file_size,
                    "source": "torrent_search",
                }

        # No detail page had magnets — try YTS for movies
        yts_url = f"https://yts.mx/browse-movies/0/{urllib.parse.quote_plus(search_query)}/all/all/0/downloads"
        yts_html = _fs_get(yts_url, max_timeout=60000)
        if yts_html:
            magnets = re.findall(
                r'magnet:\?xt=urn:btih:[a-fA-F0-9]{40}(?:&amp;[^"<\s]*|[^"<\s]*)',
                yts_html
            )
            magnets = [m.replace('&amp;', '&') for m in magnets]
            for magnet in magnets:
                dn_match = re.search(r'dn=([^&]+)', magnet)
                magnet_name = urllib.parse.unquote(dn_match.group(1)) if dn_match else ""
                clean_m = re.sub(r'[\.\-_]', ' ', magnet_name.lower())
                if any(k in clean_m for k in search_lower.split()[:3]):
                    return {
                        "ok": True, "title": magnet_name,
                        "magnet": magnet, "size": file_size,
                        "source": "torrent_search",
                    }

    # ---- Strategy 2: Direct requests (fallback, likely blocked by Cloudflare) ----
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}

    # DuckDuckGo search → visit result pages
    ddg_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(search_query)}+magnet+torrent"
    try:
        r = requests.get(ddg_url, headers=headers, timeout=12, allow_redirects=True)
        if r.status_code == 200:
            result_urls = re.findall(r'uddg=(https?://[^&"\']+)', r.text)
            result_urls = [urllib.parse.unquote(u) for u in result_urls]
            torrent_domains = ('1337x', 'thepiratebay', 'torrentgalaxy', 'yts',
                              'nyaa', 'torlock', 'magnetdl', 'limetorrents', 'bt4g', 'ibit')
            for result_url in result_urls[:5]:
                url_lower = result_url.lower()
                skip = ('wikipedia', 'github', 'stackoverflow', 'reddit',
                        'amazon', 'youtube', 'facebook', 'twitter')
                if any(s in url_lower for s in skip):
                    continue
                try:
                    page_r = requests.get(result_url, headers=headers, timeout=10, allow_redirects=True)
                    if page_r.status_code != 200:
                        continue
                    magnet_matches = re.findall(r'(magnet:\?xt=urn:btih:[a-fA-F0-9]{40}[^"<\s]*)', page_r.text)
                    if not magnet_matches:
                        magnet_matches = re.findall(r'(magnet:\?xt=urn:btih:[A-Z2-7]{32}[^"<\s]*)', page_r.text)
                    if not magnet_matches:
                        continue
                    for magnet in magnet_matches:
                        dn_match = re.search(r'dn=([^&]+)', magnet)
                        if dn_match:
                            magnet_name = urllib.parse.unquote(dn_match.group(1))
                            clean_m = re.sub(r'[\.\-_]', ' ', magnet_name.lower())
                            if any(k in clean_m for k in search_lower.split()[:3]):
                                return {"ok": True, "title": magnet_name, "magnet": magnet, "size": file_size, "source": "torrent_search"}
                    page_title = re.search(r'<title[^>]*>(.*?)</title>', page_r.text, re.IGNORECASE)
                    if page_title and search_lower[:15] in page_title.group(1).lower() and magnet_matches:
                        first_magnet = magnet_matches[0]
                        dn_match = re.search(r'dn=([^&]+)', first_magnet)
                        title = urllib.parse.unquote(dn_match.group(1)) if dn_match else search_name
                        return {"ok": True, "title": title, "magnet": first_magnet, "size": file_size, "source": "torrent_search"}
                except Exception:
                    continue
    except Exception:
        pass

    return {"ok": False, "error": "在种子网络中未找到该文件的源。可能原因：\n1. 该文件仅在电驴(eDonkey)网络可用，种子网络无源\n2. 种子搜索网站暂时不可访问\n\n建议：复制文件名到浏览器搜索其他下载方式。"}


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
        self.yt_tasks = {}      # gid -> {url, title, state, progress, ...}
        self._yt_counter = 0
        self.bili_tasks = {}    # gid -> {url, title, state, progress, ...}
        self._bili_counter = 0

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
            # Session persistence - save for crash recovery (not restored on restart)
            "--save-session=" + os.path.join(BASE_DIR, ".aria2_session"),
            "--save-session-interval=30",
        ]
        # Always start fresh: clear any stale session file from previous run
        session_file = os.path.join(BASE_DIR, ".aria2_session")
        if os.path.exists(session_file):
            os.remove(session_file)
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
                        re.match(r'^[0-9a-f]{40}\.torrent$', os.path.basename(p).lower()) is not None or
                        p.startswith("[METADATA]")
                        for p in paths
                    ) or bt_name.lower().endswith(".torrent")
                    # Skip metadata-only tasks - they should never appear in history
                    if is_torrent_meta:
                        continue
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
        # ed2k links: search for matching magnet/torrent
        if t == "ed2k":
            return self._convert_ed2k(url)
        # YouTube/X links: use yt-dlp to extract info for preview
        if t == "yt_media":
            return self._extract_yt_info(url)
        # All yt-dlp supported platforms (微博/抖音/TikTok/Facebook/Spotify/etc)
        yt_dlp_types = {"weibo", "douyin", "tiktok", "facebook", "spotify",
                        "netease", "kuaishou", "xiaohongshu", "ixigua"}
        if t in yt_dlp_types:
            result = self._extract_yt_info(url)
            if result and result.get("ok"):
                result["type"] = t
            return result
        # Bilibili links: use DASH API to extract info for preview
        if t == "bilibili":
            return self._extract_bili_info(url)
        opts = {"dir": self.save_path,
                "max-connection-per-server": str(self.connections),
                "split": str(self.connections)}
        existing_files = []
        # 本地 .torrent 文件 → 直接读取内容，交给 aria2.addTorrent
        if t == "torrent_file":
            torrent_path = os.path.expanduser(url.strip())
            if not os.path.exists(torrent_path):
                return {"ok": False, "error": f"种子文件不存在：{torrent_path}", "type": t}
            try:
                with open(torrent_path, "rb") as f:
                    torrent_data = f.read()
                gid = self.aria.add_torrent(torrent_data, {
                    "dir": self.save_path, "pause": "true",
                    "max-connection-per-server": str(self.connections),
                    "split": str(self.connections),
                })
                self.tasks[gid] = {"type": "torrent", "url": torrent_path, "submitted": True,
                                   "picked": False, "pending": True,
                                   "added_at": time.time()}
                return {"ok": True, "gid": gid, "type": "torrent",
                        "name": os.path.basename(torrent_path)}
            except Exception as e:
                return {"ok": False, "error": f"读取种子文件失败：{e}", "type": t}
        # 远程 .torrent 文件 URL → 下载种子文件后交给 aria2.addTorrent
        if t == "torrent_url":
            try:
                r = requests.get(url.strip(), timeout=30, headers={
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
                })
                if r.status_code != 200 or len(r.content) < 100:
                    return {"ok": False, "error": f"下载种子文件失败（HTTP {r.status_code}）", "type": t}
                torrent_data = r.content
                gid = self.aria.add_torrent(torrent_data, {
                    "dir": self.save_path, "pause": "true",
                    "max-connection-per-server": str(self.connections),
                    "split": str(self.connections),
                })
                self.tasks[gid] = {"type": "torrent", "url": url.strip(), "submitted": True,
                                   "picked": False, "pending": True,
                                   "added_at": time.time()}
                return {"ok": True, "gid": gid, "type": "torrent",
                        "name": os.path.basename(url.strip().split("?")[0])}
            except Exception as e:
                return {"ok": False, "error": f"获取远程种子文件失败：{e}", "type": t}
        if t == "torrent":
            # Check if we already have a .torrent file for this magnet
            ih = None
            if "btih:" in url.lower():
                ih = url.lower().split("btih:")[1].split("&")[0].strip()
                torrent_path = os.path.join(self.save_path, ih + ".torrent")
                if os.path.exists(torrent_path) and os.path.getsize(torrent_path) > 0:
                    try:
                        with open(torrent_path, "rb") as f:
                            torrent_data = f.read()
                        gid = self.aria.add_torrent(torrent_data, {
                            "dir": self.save_path, "pause": "true",
                            "max-connection-per-server": str(self.connections),
                            "split": str(self.connections),
                        })
                        self.tasks[gid] = {"type": t, "url": url, "submitted": True,
                                           "picked": False, "pending": True,
                                           "added_at": time.time()}
                        existing_files = self._check_existing_files(ih)
                        return {"ok": True, "gid": gid, "type": t,
                                "cached": True, "existing_files": existing_files}
                    except Exception:
                        pass  # fall through to normal magnet add
            if ih:
                existing_files = self._check_existing_files(ih)
                # 启动后台线程从种子缓存服务获取元数据，加速磁力解析
                threading.Thread(target=self._fetch_torrent_cache, args=(ih,), daemon=True).start()
        gid = self.aria.add(url, opts)
        self.tasks[gid] = {"type": t, "url": url, "submitted": True, "picked": False,
                           "pending": (t == "torrent"), "added_at": time.time()}
        return {"ok": True, "gid": gid, "type": t, "existing_files": existing_files}

    def _check_existing_files(self, info_hash):
        """Check if files for this torrent exist and are still being downloaded."""
        existing = []
        try:
            # Only warn about files that are still being downloaded (.aria2 control file exists)
            for fn in os.listdir(self.save_path):
                if fn.endswith(".aria2"):
                    main_fp = os.path.join(self.save_path, fn[:-6])
                    if os.path.exists(main_fp):
                        existing.append({"name": fn[:-6], "size": os.path.getsize(main_fp)})
        except Exception:
            pass
        return existing

    def _fetch_torrent_cache(self, info_hash):
        """从种子缓存服务获取 torrent 元数据，加速磁力链接解析。"""
        ih = info_hash.upper()
        sources = [
            f"https://itorrents.org/torrent/{ih}.torrent",
            f"https://torrage.info/torrent/{ih}.torrent",
            f"https://torcache.net/torrent/{ih}.torrent",
        ]
        for src in sources:
            try:
                r = requests.get(src, timeout=15, headers={
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
                })
                if r.status_code == 200 and len(r.content) > 100:
                    torrent_path = os.path.join(self.save_path, info_hash.lower() + ".torrent")
                    with open(torrent_path, "wb") as f:
                        f.write(r.content)
                    # 找到 aria2 中正在等待解析的磁力任务，用种子文件替换
                    try:
                        active = self.aria.active()
                        waiting = self.aria.waiting()
                        for s in (active + waiting):
                            gid = s.get("gid", "")
                            task_info = self.tasks.get(gid, {})
                            if task_info.get("pending") and not task_info.get("picked"):
                                task_url = task_info.get("url", "").lower()
                                if info_hash.lower() in task_url:
                                    new_gid = self.aria.add_torrent(r.content, {
                                        "dir": self.save_path, "pause": "true",
                                    })
                                    # 链接新旧 gid，snapshot 会用 converted_to 保持前端 gid 稳定
                                    self.tasks[new_gid] = {"type": "torrent", "url": task_info.get("url", ""),
                                                          "submitted": True, "picked": False, "pending": True}
                                    self.tasks[gid] = {"type": "torrent", "url": task_info.get("url", ""),
                                                       "submitted": True, "picked": False, "pending": True,
                                                       "converted_to": new_gid}
                                    return
                    except Exception:
                        pass
                    return
            except Exception:
                continue

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

    def stop(self, gid, skip_trash=False):
        info = self.tasks.get(gid)
        real_gid = info.get("converted_to", gid) if info else gid
        if not skip_trash:
            # Save task info as a history record before removing
            try:
                s = self.aria.status(real_gid)
                t = self._fmt_status(s)
                if t:
                    rec = {
                        "gid": gid, "type": t.get("type", "http"),
                        "name": t.get("name", ""), "url": info.get("url", "") if info else "",
                        "dir": t.get("dir", ""), "paths": t.get("paths", []),
                        "size": t.get("size", 0), "completed_at": int(time.time()),
                    }
                    self.records["trash"].insert(0, rec)
                    self._save_records()
            except Exception:
                pass
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
            # Metadata is ready when aria2 reports the info dictionary name.
            has_metadata = bool(bt_info.get("name"))
            state_map = {"active": "downloading", "waiting": "queued",
                         "paused": "paused", "complete": "finished",
                         "removed": "removed", "error": "error"}
            return {
                "gid": gid,
                "type": info.get("type") or ("torrent" if (bt_info and bt_info.get("name")) else classify(info.get("url", ""))),
                "name": name,
                "state": state_map.get(st, st),
                "progress": round(100 * done / total, 1) if total else 0,
                "size": total,
                "completed_size": done,
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
                "added_at": info.get("added_at", 0),
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
                # If a new torrent task appears (auto-created by aria2 from magnet),
                # mark it as pending for file picking.
                if not info and t["type"] == "torrent" and t.get("has_metadata") and t.get("files"):
                    self.tasks[t["gid"]] = {"type": "torrent", "url": "", "submitted": True,
                                            "picked": False, "pending": True}
                    info = self.tasks[t["gid"]]
                    is_pending = True
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
        # 合并 m3u8 流媒体下载任务，已完成/错误的自动归档
        for gid, info in list(self.m3u8_tasks.items()):
            if info.get('state') == 'removed':
                continue
            state = info.get('state', 'downloading')
            output = info.get('output', '')
            # 已完成 → 自动移入下载记录
            if state == 'finished':
                if gid not in existing:
                    existing.add(gid)
                    self.records["history"].insert(0, {
                        "gid": gid, "type": "m3u8", "name": info.get('title', '视频下载'),
                        "url": info.get('url', ''), "dir": self.save_path,
                        "paths": [output] if output else [],
                        "size": info.get('size', 0), "completed_at": int(time.time()),
                    })
                del self.m3u8_tasks[gid]
                continue
            # 错误 → 保留在任务列表，不自动归档
            items.append({
                "gid": gid,
                "type": "m3u8",
                "name": info.get('title', '视频下载'),
                "state": info.get('state', 'downloading'),
                "progress": info.get('progress', 0),
                "size": info.get('size', 0),
                "completed_size": info.get('size', 0),
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
                "added_at": info.get('added_at', 0),
            })
        # 合并 yt-dlp 下载任务
        for gid, info in list(self.yt_tasks.items()):
            if info.get('state') == 'removed':
                continue
            state = info.get('state', 'downloading')
            output = info.get('output', '')
            if state == 'finished':
                if gid not in existing:
                    existing.add(gid)
                    self.records["history"].insert(0, {
                        "gid": gid, "type": "yt_media", "name": info.get('title', '视频下载'),
                        "url": info.get('url', ''), "dir": self.save_path,
                        "paths": [output] if output else [],
                        "size": info.get('size', 0), "completed_at": int(time.time()),
                    })
                del self.yt_tasks[gid]
                continue
            downloaded = info.get('_downloaded', 0)
            total = info.get('size', 0)
            pct = info.get('progress', 0)
            if total > 0 and pct == 0 and downloaded > 0:
                pct = round(100 * downloaded / total, 1)
            items.append({
                "gid": gid,
                "type": "yt_media",
                "name": info.get('title', '视频下载'),
                "state": state,
                "progress": pct,
                "size": total,
                "completed_size": downloaded,
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
                "added_at": info.get('added_at', 0),
                "format_id": info.get('format_id', ''),
                "is_audio_only": info.get('_is_audio_only', False),
                "platform": info.get('platform', ''),
            })
        # 合并 Bilibili 下载任务
        for gid, info in list(self.bili_tasks.items()):
            if info.get('state') == 'removed':
                continue
            state = info.get('state', 'downloading')
            output = info.get('output', '')
            if state == 'finished':
                if gid not in existing:
                    existing.add(gid)
                    self.records["history"].insert(0, {
                        "gid": gid, "type": "bilibili", "name": info.get('title', 'B站视频'),
                        "url": info.get('url', ''), "dir": self.save_path,
                        "paths": [output] if output else [],
                        "size": info.get('size', 0), "completed_at": int(time.time()),
                    })
                del self.bili_tasks[gid]
                continue
            pct = info.get('progress', 0)
            items.append({
                "gid": gid,
                "type": "bilibili",
                "name": info.get('title', 'B站视频'),
                "state": state,
                "progress": pct,
                "size": info.get('size', 0),
                "completed_size": 0,
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
                "added_at": info.get('added_at', 0),
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
                rec = self.records["trash"].pop(i)
                paths = rec.get("paths", [])
                file_exists = any(os.path.exists(p) for p in paths)
                if file_exists:
                    # 文件存在 → 已下载完成，恢复到下载记录
                    self.records["history"].insert(0, rec)
                    self._save_records()
                    return {"action": "history", "ok": True}
                else:
                    # 文件不存在 → 未下载完成，重新添加到下载队列
                    url = rec.get("url", "")
                    if url:
                        result = self.add(url)
                        self._save_records()
                        return {"action": "task", "ok": result.get("ok", False),
                                "gid": result.get("gid"), "type": result.get("type")}
                    else:
                        self.records["history"].insert(0, rec)
                        self._save_records()
                        return {"action": "history", "ok": True}
        return {"ok": False}

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
                # Also remove from aria2 session to prevent re-appearing on restart
                try:
                    self.aria.remove(gid)
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
        # 停止所有 yt-dlp 下载
        for gid in list(self.yt_tasks.keys()):
            self.stop_yt_download(gid, skip_trash=True)
        # 停止所有 Bilibili 下载
        for gid in list(self.bili_tasks.keys()):
            self.stop_bili_download(gid, skip_trash=True)

    def clear_all_history(self):
        """Move all history records to trash."""
        self.records["trash"] = self.records["history"] + self.records["trash"]
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
        """通过解析 m3u8 播放列表中的 EXTINF 标签来计算总时长（秒）。
        支持主播放列表（master playlist）递归解析。"""
        try:
            r = requests.get(m3u8_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            text = r.text
            # 检查是否为主播放列表（包含 EXT-X-STREAM-INF）
            if '#EXT-X-STREAM-INF' in text:
                # 取第一个（通常最高码率）变体播放列表
                from urllib.parse import urljoin
                best_bandwidth = 0
                best_url = None
                for line in text.split('\n'):
                    if line.startswith('#EXT-X-STREAM-INF'):
                        import re
                        bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                        bw = int(bw_match.group(1)) if bw_match else 0
                        if bw > best_bandwidth:
                            best_bandwidth = bw
                    elif best_bandwidth > 0 and line.strip() and not line.startswith('#'):
                        best_url = urljoin(m3u8_url, line.strip())
                        break
                if best_url:
                    return self._get_m3u8_duration(best_url)
                return None
            total = 0.0
            for line in text.split('\n'):
                if line.startswith('#EXTINF:'):
                    dur = line.split(':')[1].split(',')[0]
                    total += float(dur)
            return total if total > 0 else None
        except Exception:
            return None

    def _download_m3u8_thread(self, gid, m3u8_url, title, resume_from=0):
        """在后台线程中运行 ffmpeg 下载 m3u8 流。resume_from>0 表示续传。"""
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title).strip() or "视频"
        output_path = os.path.join(self.save_path, f"{safe_title}.mp4")
        is_resume = resume_from > 0

        if is_resume:
            # 续传：使用已有文件路径，下载剩余部分后拼接
            old_info = self.m3u8_tasks.get(gid, {})
            existing_output = old_info.get('output', '')
            if existing_output and os.path.exists(existing_output) and os.path.getsize(existing_output) > 1024:
                output_path = existing_output
            else:
                # 部分文件不存在或太小，改为从头开始
                is_resume = False
                resume_from = 0
            if is_resume:
                part1_path = output_path
                part2_path = output_path.replace('.mp4', '_part2.mp4')
                counter = 1
                while os.path.exists(part2_path):
                    part2_path = output_path.replace('.mp4', f'_part2_{counter}.mp4')
                    counter += 1
                actual_output = part2_path
        else:
            counter = 1
            while os.path.exists(output_path):
                output_path = os.path.join(self.save_path, f"{safe_title}_{counter}.mp4")
                counter += 1
            actual_output = output_path

        duration = self._get_m3u8_duration(m3u8_url)

        info = {
            'url': m3u8_url, 'title': title, 'output': output_path,
            'state': 'downloading', 'progress': resume_from / duration * 100 if duration and resume_from else 0,
            'size': 0, 'download_rate': 0,
            'current_time': resume_from, 'duration': duration,
            'proc': None, '_last_size': 0, '_last_time': time.time(), '_smooth_rate': 0,
            'resume_from': resume_from, 'part1': output_path if is_resume else None,
            'part2': actual_output if is_resume else None,
            'added_at': (self.m3u8_tasks.get(gid, {}).get('added_at') if is_resume else None) or time.time(),
        }
        self.m3u8_tasks[gid] = info

        cmd = [
            'ffmpeg', '-y',
        ]
        if resume_from > 0:
            cmd += ['-ss', str(resume_from)]
        cmd += [
            '-i', m3u8_url,
            '-c', 'copy', '-bsf:a', 'aac_adtstoasc',
            '-movflags', '+faststart',
            '-progress', 'pipe:1', '-nostats', '-loglevel', 'error',
            actual_output
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
                        self.m3u8_tasks[gid]['current_time'] = resume_from + seconds
                        if duration:
                            self.m3u8_tasks[gid]['progress'] = min(round(100 * (resume_from + seconds) / duration, 1), 99.9)
                    except (ValueError, IndexError):
                        pass
                elif line.startswith('speed='):
                    pass
                elif line.startswith('total_size='):
                    try:
                        size = int(line.split('=')[1])
                        self.m3u8_tasks[gid]['size'] = size
                        now = time.time()
                        last_size = self.m3u8_tasks[gid].get('_last_size', 0)
                        last_time = self.m3u8_tasks[gid].get('_last_time', now)
                        if now > last_time and size > last_size:
                            instant = (size - last_size) / (now - last_time)
                            prev = self.m3u8_tasks[gid].get('_smooth_rate', 0)
                            alpha = 0.3 if prev > 0 else 0.8
                            self.m3u8_tasks[gid]['_smooth_rate'] = alpha * instant + (1 - alpha) * prev
                            self.m3u8_tasks[gid]['download_rate'] = int(self.m3u8_tasks[gid]['_smooth_rate'])
                        self.m3u8_tasks[gid]['_last_size'] = size
                        self.m3u8_tasks[gid]['_last_time'] = now
                    except (ValueError, KeyError):
                        pass

            proc.wait()

            # 如果是用户暂停杀进程，不覆盖状态
            if gid in self.m3u8_tasks and self.m3u8_tasks[gid].get('state') == 'paused':
                return

            if proc.returncode == 0:
                if is_resume:
                    # 拼接 part1 + part2 → 最终文件
                    concat_list = output_path + '.concat.txt'
                    with open(concat_list, 'w') as f:
                        f.write(f"file '{part1_path}'\n")
                        f.write(f"file '{part2_path}'\n")
                    final_path = output_path.replace('.mp4', '_merged.mp4')
                    concat_cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                                  '-i', concat_list, '-c', 'copy', '-movflags', '+faststart', final_path]
                    result = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=300)
                    if result.returncode == 0:
                        os.replace(final_path, output_path)
                        # 清理临时文件
                        for fp in [part2_path, concat_list]:
                            if os.path.exists(fp):
                                try: os.remove(fp)
                                except Exception: pass
                self.m3u8_tasks[gid]['state'] = 'finished'
                self.m3u8_tasks[gid]['progress'] = 100
                if os.path.exists(output_path):
                    self.m3u8_tasks[gid]['size'] = os.path.getsize(output_path)
            else:
                self.m3u8_tasks[gid]['state'] = 'error'
        except Exception as e:
            self.m3u8_tasks[gid]['state'] = 'error'
            self.m3u8_tasks[gid]['error'] = str(e)

    def start_m3u8_download(self, m3u8_url, title, paused=False):
        """启动 m3u8 流媒体下载。paused=True 时仅创建任务不开始下载。"""
        gid = f"m3u8_{self._m3u8_counter}"
        self._m3u8_counter += 1
        if paused:
            safe_title = re.sub(r'[<>:"/\\|?*]', '_', title).strip() or "视频"
            output_path = os.path.join(self.save_path, f"{safe_title}.mp4")
            self.m3u8_tasks[gid] = {
                'url': m3u8_url, 'title': title, 'output': output_path,
                'state': 'paused', 'progress': 0, 'size': 0,
                'download_rate': 0, 'current_time': 0, 'duration': None,
                'proc': None, '_last_size': 0, '_last_time': time.time(),
                '_smooth_rate': 0, 'resume_from': 0, 'part1': None, 'part2': None,
                'added_at': time.time(),
            }
        else:
            t = threading.Thread(target=self._download_m3u8_thread,
                                 args=(gid, m3u8_url, title), daemon=True)
            t.start()
        return gid

    def stop_m3u8_download(self, gid, skip_trash=False):
        """停止 m3u8 下载，保留部分文件并归档到回收站。"""
        if gid in self.m3u8_tasks:
            info = self.m3u8_tasks[gid]
            proc = info.get('proc')
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            if not skip_trash:
                output = info.get('output', '')
                self.records["trash"].insert(0, {
                    "gid": gid, "type": "m3u8", "name": info.get('title', '视频下载'),
                    "url": info.get('url', ''), "dir": self.save_path,
                    "paths": [output] if output and os.path.exists(output) else [],
                    "size": info.get('size', 0), "completed_at": int(time.time()),
                })
                self._save_records()
            else:
                # 跳过回收站时删除部分文件
                output = info.get('output', '')
                if output and os.path.exists(output):
                    try:
                        os.remove(output)
                    except Exception:
                        pass
            del self.m3u8_tasks[gid]
            return True
        return False

    def pause_m3u8_download(self, gid):
        """暂停 m3u8 下载：杀进程，保留部分文件，任务留在列表中。"""
        if gid in self.m3u8_tasks:
            info = self.m3u8_tasks[gid]
            proc = info.get('proc')
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            info['state'] = 'paused'
            info['proc'] = None
            return True
        return False

    def resume_m3u8_download(self, gid):
        """续传 m3u8 下载：从上次中断位置继续。"""
        if gid in self.m3u8_tasks:
            info = self.m3u8_tasks[gid]
            if info.get('state') not in ('paused', 'error'):
                return False
            url = info.get('url', '')
            title = info.get('title', '视频下载')
            resume_from = info.get('current_time', 0)
            t = threading.Thread(target=self._download_m3u8_thread,
                                 args=(gid, url, title, resume_from), daemon=True)
            t.start()
            return True
        return False

    # ---- YouTube/X video extraction & download (yt-dlp) ----
    def _extract_yt_info(self, url):
        """Use yt-dlp to extract video metadata (title, thumbnail, available formats).
        Returns a preview card dict for the frontend.
        Tries without cookies first, then with browser cookies for sites requiring login."""
        try:
            import yt_dlp
        except ImportError:
            return {"ok": False, "error": "yt-dlp 未安装", "type": classify(url) or "yt_media"}

        # Try without cookies first (fast, works for public content like YouTube)
        info = None
        drm_detected = False
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False,
                'skip_download': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            err = str(e)
            if 'DRM' in err:
                drm_detected = True
            info = None

        if drm_detected:
            return {"ok": False, "error": "该内容受DRM保护，无法直接下载",
                    "type": classify(url) or "yt_media",
                    "hint": "Spotify等平台有数字版权保护。可尝试在网页中搜索同名歌曲的YouTube版本。"}

        # If no-cookie attempt failed, try with browser cookies
        if not info:
            for browser in ('chrome', 'safari'):
                try:
                    ydl_opts = {
                        'quiet': True,
                        'no_warnings': True,
                        'extract_flat': False,
                        'skip_download': True,
                        'cookiesfrombrowser': (browser,),
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                    if info:
                        break
                except Exception:
                    continue

        if not info:
            # yt-dlp failed — try Playwright with Chrome persistent context
            # Works for sites w/ encrypted cookies (抖音/微博) and JS-only video players
            result = _extract_with_playwright(url)
            if result and result.get("ok"):
                result["type"] = classify(url) or "yt_media"
                return result
            return {"ok": False, "error": "未获取到视频信息", "type": classify(url) or "yt_media"}

        title = info.get('title', '') or '未知视频'
        thumbnail = info.get('thumbnail', '') or ''
        duration = info.get('duration', 0) or 0
        uploader = info.get('uploader', '') or ''
        description = (info.get('description', '') or '')[:200]

        # Collect available formats for user selection
        formats_raw = info.get('formats', []) or []
        formats = []
        seen_heights = set()
        for f in formats_raw:
            ftype = f.get('type', 'unknown')
            # Skip storyboards, trailers, and purely audio-only formats without video
            if ftype == 'storyboard' or f.get('acodec') == 'none' and f.get('vcodec') == 'none':
                continue
            height = f.get('height') or 0
            ext = f.get('ext', '') or ''
            vcodec = f.get('vcodec', '') or ''
            acodec = f.get('acodec', '') or ''
            filesize = f.get('filesize') or f.get('filesize_approx') or 0
            vbr = f.get('vbr') or 0
            abr = f.get('abr') or 0
            tbr = f.get('tbr') or 0
            format_id = f.get('format_id', '') or ''
            format_note = f.get('format_note', '') or ''

            # Determine display category
            is_video_audio = vcodec != 'none' and acodec != 'none'
            is_video_only = vcodec != 'none' and (acodec == 'none' or not acodec)
            is_audio_only = vcodec == 'none' and acodec != 'none'

            # Deduplicate same-height video+audio combined formats (prefer best)
            # and same-bitrate audio-only formats
            dedup_key = None
            if is_video_audio:
                dedup_key = ('va', height, ext)
            elif is_video_only:
                dedup_key = ('v', height, ext)
            elif is_audio_only:
                dedup_key = ('a', int(abr), ext)

            if dedup_key and dedup_key in seen_heights:
                continue

            # Only include meaningful formats
            label = ''
            if height:
                label = f'{height}p'
            elif is_audio_only and abr:
                label = f'{int(abr)}kbps'
            else:
                label = format_note or ext

            # Quality score for sorting (higher = better)
            quality = (height or 0) * 100 + (tbr or 0)

            if is_video_audio or is_video_only or is_audio_only:
                if dedup_key:
                    seen_heights.add(dedup_key)
                formats.append({
                    'format_id': format_id,
                    'label': label,
                    'ext': ext,
                    'height': height,
                    'filesize': filesize,
                    'tbr': int(tbr) if tbr else 0,
                    'vcodec': vcodec,
                    'acodec': acodec,
                    'is_video_audio': is_video_audio,
                    'is_video_only': is_video_only,
                    'is_audio_only': is_audio_only,
                    'quality': quality,
                })

        # Sort: video+audio best first, then video-only, then audio-only
        formats.sort(key=lambda f: (
            0 if f['is_video_audio'] else (1 if f['is_video_only'] else 2),
            -f['quality']
        ))

        # Limit to top 20 formats to keep UI manageable
        formats = formats[:20]

        # Determine if URL is YouTube or X
        is_youtube = bool(re.match(r'https?://(www\.)?(youtube\.com|youtu\.be)/', url.lower()))
        is_x = bool(re.match(r'https?://(www\.)?(x\.com|twitter\.com|t\.co)/', url.lower()))
        platform = 'YouTube' if is_youtube else ('X/Twitter' if is_x else '视频平台')

        return {
            "ok": True,
            "type": classify(url) or "yt_media",
            "title": title,
            "poster": thumbnail,
            "m3u8_url": "",
            "magnet": "",
            "duration": duration,
            "uploader": uploader,
            "description": description,
            "platform": platform,
            "formats": formats,
            "url": url,
            "yt_source": True,
        }

    def _download_yt_thread(self, gid, url, format_id, title):
        """Download YouTube/X video in background thread using yt-dlp."""
        import yt_dlp

        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title).strip() or '视频'
        output_template = os.path.join(self.save_path, f"{safe_title}.%(ext)s")

        info = {
            'url': url, 'title': title, 'state': 'downloading',
            'progress': 0, 'size': 0, 'download_rate': 0,
            'format_id': format_id, 'output': '',
            'proc': None, '_last_size': 0, '_last_time': time.time(),
            '_smooth_rate': 0, 'added_at': time.time(),
        }
        self.yt_tasks[gid] = info

        def progress_hook(d):
            if gid not in self.yt_tasks:
                return
            task = self.yt_tasks[gid]
            if d['status'] == 'downloading':
                downloaded = d.get('downloaded_bytes', 0) or 0
                total = d.get('total_bytes', 0) or d.get('total_bytes_estimate', 0) or 0
                speed = d.get('speed', 0) or 0
                task['size'] = total
                task['_downloaded'] = downloaded
                task['download_rate'] = int(speed) if speed else 0
                if total > 0:
                    task['progress'] = round(100 * downloaded / total, 1)
                # ETA
                eta = d.get('eta', 0) or 0
                task['_eta'] = eta
            elif d['status'] == 'finished':
                task['progress'] = 99.9
                task['_downloaded'] = task.get('size', 0)
                filename = d.get('filename', '') or ''
                task['output'] = filename
            elif d['status'] == 'error':
                task['state'] = 'error'
                task['error'] = d.get('message', '下载出错')

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'progress_hooks': [progress_hook],
            'outtmpl': output_template,
            'overwrites': True,
        }

        # Format selection
        if format_id:
            # Check if format is video+audio combined or needs merging
            # For combined formats: download single stream
            # For video-only: merge with best audio via yt-dlp's default behavior
            ydl_opts['format'] = format_id + '+bestaudio/bestaudio/' + format_id
        else:
            # Default: best quality video+audio
            ydl_opts['format'] = 'bestvideo+bestaudio/best'

        # Merge into mp4 for video, keep original ext for audio-only
        task_info = self.yt_tasks.get(gid, {})
        if task_info.get('_is_audio_only'):
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
            }]
        else:
            ydl_opts['merge_output_format'] = 'mp4'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }]

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            if gid in self.yt_tasks:
                task = self.yt_tasks[gid]
                if task.get('state') != 'error':
                    task['state'] = 'finished'
                    task['progress'] = 100
                    # Find actual output file
                    if not task.get('output') or not os.path.exists(task['output']):
                        # yt-dlp may change extension after post-processing
                        for ext in ('.mp4', '.mkv', '.webm', '.mp3', '.m4a', '.opus'):
                            candidate = os.path.join(self.save_path, f"{safe_title}{ext}")
                            if os.path.exists(candidate):
                                task['output'] = candidate
                                task['size'] = os.path.getsize(candidate)
                                break
                    else:
                        if os.path.exists(task['output']):
                            task['size'] = os.path.getsize(task['output'])
        except Exception as e:
            if gid in self.yt_tasks:
                self.yt_tasks[gid]['state'] = 'error'
                self.yt_tasks[gid]['error'] = str(e)

    def start_yt_download(self, url, format_id=None, title=None, is_audio_only=False):
        """Start a YouTube/X video download. Returns gid."""
        gid = f"yt_{self._yt_counter}"
        self._yt_counter += 1
        self.yt_tasks[gid] = {
            'url': url, 'title': title or '视频下载',
            'state': 'downloading', 'progress': 0,
            'size': 0, 'download_rate': 0, 'format_id': format_id,
            'output': '', '_is_audio_only': is_audio_only,
            '_downloaded': 0, '_eta': 0,
            'added_at': time.time(),
        }
        t = threading.Thread(target=self._download_yt_thread,
                             args=(gid, url, format_id, title or '视频下载'),
                             daemon=True)
        t.start()
        return gid

    def stop_yt_download(self, gid, skip_trash=False):
        """Stop a yt-dlp download."""
        if gid in self.yt_tasks:
            info = self.yt_tasks[gid]
            # yt-dlp doesn't have a process we can kill easily in thread mode
            # Mark as removed so the thread will check and stop updating
            info['state'] = 'removed'
            if not skip_trash:
                output = info.get('output', '')
                self.records["trash"].insert(0, {
                    "gid": gid, "type": "yt_media", "name": info.get('title', '视频下载'),
                    "url": info.get('url', ''), "dir": self.save_path,
                    "paths": [output] if output and os.path.exists(output) else [],
                    "size": info.get('size', 0), "completed_at": int(time.time()),
                })
                self._save_records()
            else:
                output = info.get('output', '')
                if output and os.path.exists(output):
                    try:
                        os.remove(output)
                    except Exception:
                        pass
            del self.yt_tasks[gid]
            return True
        return False

    # ---- Bilibili DASH download (ffmpeg merge video+audio) ----
    def _extract_bili_info(self, url):
        """Parse Bilibili URL, extract video metadata and stream formats."""
        resolved = _resolve_bili_url(url)
        if not resolved:
            return {"ok": False, "error": "无法识别B站链接格式", "type": "bilibili"}
        mode, id_ = resolved
        if mode == 'video':
            return _extract_bili_video(id_)
        elif mode == 'bangumi':
            return _extract_bili_bangumi(id_)
        elif mode == 'season':
            # For season URLs, we'd need more logic; return basic error
            return {"ok": False, "error": "番剧系列链接请使用具体剧集链接（ep开头）", "type": "bilibili"}
        return {"ok": False, "error": "无法识别B站链接", "type": "bilibili"}

    def _download_bili_thread(self, gid, video_url, audio_url, title, video_backup_urls=None, audio_backup_urls=None):
        """Download Bilibili DASH streams (video + audio) and merge with ffmpeg."""
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title).strip() or '视频'
        # Use unique output path
        output_path = os.path.join(self.save_path, f"{safe_title}.mp4")
        counter = 1
        while os.path.exists(output_path):
            output_path = os.path.join(self.save_path, f"{safe_title}_{counter}.mp4")
            counter += 1

        # Create temp files for video and audio streams
        video_tmp = output_path + '.video.tmp.mp4'
        audio_tmp = output_path + '.audio.tmp.m4a'

        info = {
            'url': '', 'title': title, 'state': 'downloading',
            'progress': 0, 'size': 0, 'download_rate': 0,
            'output': output_path,
            'added_at': time.time(),
        }
        self.bili_tasks[gid] = info

        headers_args = [
            '-user_agent', BILI_HEADERS['User-Agent'],
            '-referer', BILI_HEADERS['Referer'],
        ]

        # Try primary URL, fallback to backup URLs if primary fails
        v_url = video_url
        a_url = audio_url

        def _try_url_with_fallback(primary, backups, label):
            """Try primary URL; if ffmpeg fails, try backup URLs."""
            urls = [primary] + (backups or [])
            for url in urls:
                if not url:
                    continue
                return url
            return None

        v_url = _try_url_with_fallback(v_url, video_backup_urls, 'video')
        a_url = _try_url_with_fallback(a_url, audio_backup_urls, 'audio')

        if not v_url and not a_url:
            self.bili_tasks[gid]['state'] = 'error'
            self.bili_tasks[gid]['error'] = '无可用视频/音频流'
            return

        # Build ffmpeg command:
        # If both video and audio → download both separately, then merge
        # If only audio → download audio only
        if v_url and a_url:
            # Merge: download video → tmp, download audio → tmp, concat with ffmpeg
            # Use ffmpeg to download both streams simultaneously via two inputs + merge
            cmd = [
                'ffmpeg', '-y',
                *headers_args,
                '-i', v_url,
                *headers_args,
                '-i', a_url,
                '-c:v', 'copy', '-c:a', 'copy',
                '-movflags', '+faststart',
                '-progress', 'pipe:1', '-nostats', '-loglevel', 'error',
                output_path,
            ]
        elif v_url:
            # Video only (no audio stream available)
            cmd = [
                'ffmpeg', '-y',
                *headers_args,
                '-i', v_url,
                '-c', 'copy',
                '-movflags', '+faststart',
                '-progress', 'pipe:1', '-nostats', '-loglevel', 'error',
                output_path,
            ]
        elif a_url:
            # Audio only → save as m4a
            output_path = output_path.replace('.mp4', '.m4a')
            self.bili_tasks[gid]['output'] = output_path
            cmd = [
                'ffmpeg', '-y',
                *headers_args,
                '-i', a_url,
                '-c:a', 'copy',
                '-movflags', '+faststart',
                '-progress', 'pipe:1', '-nostats', '-loglevel', 'error',
                output_path,
            ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
            self.bili_tasks[gid]['proc'] = proc
            # Get total duration from the Bilibili API data (already known)
            bili_duration = self.bili_tasks[gid].get('duration', 0) or 0

            for line in proc.stdout:
                line = line.strip()
                if line.startswith('out_time='):
                    time_str = line.split('=')[1]
                    parts = time_str.split(':')
                    try:
                        seconds = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                        if bili_duration > 0:
                            self.bili_tasks[gid]['progress'] = min(round(100 * seconds / bili_duration, 1), 99.9)
                    except (ValueError, IndexError):
                        pass
                elif line.startswith('total_size='):
                    try:
                        size = int(line.split('=')[1])
                        self.bili_tasks[gid]['size'] = size
                        now = time.time()
                        last_size = self.bili_tasks[gid].get('_last_size', 0)
                        last_time = self.bili_tasks[gid].get('_last_time', now)
                        if now > last_time and size > last_size:
                            instant = (size - last_size) / (now - last_time)
                            prev = self.bili_tasks[gid].get('_smooth_rate', 0)
                            alpha = 0.3 if prev > 0 else 0.8
                            self.bili_tasks[gid]['_smooth_rate'] = alpha * instant + (1 - alpha) * prev
                            self.bili_tasks[gid]['download_rate'] = int(self.bili_tasks[gid]['_smooth_rate'])
                        self.bili_tasks[gid]['_last_size'] = size
                        self.bili_tasks[gid]['_last_time'] = now
                    except (ValueError, KeyError):
                        pass

            proc.wait()

            if gid in self.bili_tasks and self.bili_tasks[gid].get('state') == 'paused':
                return

            if proc.returncode == 0:
                self.bili_tasks[gid]['state'] = 'finished'
                self.bili_tasks[gid]['progress'] = 100
                if os.path.exists(output_path):
                    self.bili_tasks[gid]['size'] = os.path.getsize(output_path)
            else:
                # ffmpeg failed — try backup URLs if available
                stderr = proc.stderr.read() if proc.stderr else ''
                self.bili_tasks[gid]['state'] = 'error'
                self.bili_tasks[gid]['error'] = f"下载失败：{stderr[:200]}"
                # Clean up partial file
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except Exception:
                        pass
        except Exception as e:
            self.bili_tasks[gid]['state'] = 'error'
            self.bili_tasks[gid]['error'] = str(e)

    def start_bili_download(self, bvid, cid, quality_id, codec_id, audio_id, title, aid=None, is_legacy=False):
        """Start a Bilibili download. Returns gid.
        is_legacy=True: use fnval=0 direct mp4 URL (aria2 download, no login needed)
        is_legacy=False: use fnval=16 DASH streams (ffmpeg merge, may need login)
        """
        gid = f"bili_{self._bili_counter}"
        self._bili_counter += 1
        cookies = _get_bili_cookie()
        headers = {**BILI_HEADERS}
        if cookies:
            headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())

        self.bili_tasks[gid] = {
            'url': f'https://www.bilibili.com/video/{bvid}',
            'title': title, 'state': 'downloading',
            'progress': 0, 'size': 0, 'download_rate': 0,
            'output': '', 'duration': 0, 'proc': None,
            '_last_size': 0, '_last_time': time.time(), '_smooth_rate': 0,
            'added_at': time.time(),
        }

        info_url = f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}'
        try:
            r_info = requests.get(info_url, headers=headers, timeout=10)
            info_data = r_info.json().get('data', {})
            self.bili_tasks[gid]['duration'] = info_data.get('duration', 0) or 0
        except Exception:
            pass

        if is_legacy or codec_id == 0:
            # LEGACY: fnval=0 -> direct mp4 URL -> aria2 download
            playurl = f'https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={quality_id}&fnval=0'
            r = requests.get(playurl, headers=headers, timeout=10)
            leg_data = r.json()
            if leg_data.get('code') != 0:
                self.bili_tasks[gid]['state'] = 'error'
                self.bili_tasks[gid]['error'] = f"获取视频流失败：{leg_data.get('message', '')}"
                return {"ok": False, "error": self.bili_tasks[gid]['error']}
            durl_list = leg_data['data'].get('durl', []) or []
            direct_urls = [du.get('url', '') for du in durl_list if du.get('url')]
            if not direct_urls:
                self.bili_tasks[gid]['state'] = 'error'
                self.bili_tasks[gid]['error'] = '无可用视频流'
                return {"ok": False, "error": '无可用视频流'}
            # Single segment -> aria2
            opts = {
                "dir": self.save_path,
                "header": ["Referer:https://www.bilibili.com", "User-Agent:" + BILI_HEADERS['User-Agent']],
                "max-connection-per-server": str(self.connections),
                "split": str(self.connections),
            }
            try:
                aria2_gid = self.aria.add(direct_urls[0], opts)
                self.bili_tasks[gid]['_aria2_gid'] = aria2_gid
                self.tasks[aria2_gid] = {"type": "bilibili", "url": direct_urls[0],
                                         "submitted": True, "picked": True,
                                         "_bili_gid": gid, "added_at": time.time()}
                return {"ok": True, "gid": gid, "type": "bilibili"}
            except Exception as e:
                self.bili_tasks[gid]['state'] = 'error'
                self.bili_tasks[gid]['error'] = str(e)
                return {"ok": False, "error": str(e)}
        else:
            # DASH: fnval=16 -> video+audio merge with ffmpeg
            playurl = f'https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn=120&fnval=16&fourk=1'
            r = requests.get(playurl, headers=headers, timeout=10)
            stream_data = r.json()
            if stream_data.get('code') != 0:
                self.bili_tasks[gid]['state'] = 'error'
                self.bili_tasks[gid]['error'] = f"获取视频流失败：{stream_data.get('message', '')}"
                return {"ok": False, "error": self.bili_tasks[gid]['error']}
            dash = stream_data['data'].get('dash', {}) or {}
            video_streams = dash.get('video', []) or []
            audio_streams = dash.get('audio', []) or []
            selected_video = None
            for v in video_streams:
                if v.get('id') == quality_id and v.get('codecid') == codec_id:
                    selected_video = v; break
            if not selected_video:
                for v in video_streams:
                    if v.get('id') == quality_id: selected_video = v; break
            if not selected_video and video_streams:
                for v in video_streams:
                    if v.get('codecid') == 7: selected_video = v; break
                if not selected_video: selected_video = video_streams[0]
            selected_audio = None
            if audio_id:
                for a in audio_streams:
                    if a.get('id') == audio_id: selected_audio = a; break
            if not selected_audio and audio_streams:
                selected_audio = max(audio_streams, key=lambda a: a.get('bandwidth', 0))
            video_url = selected_video.get('baseUrl', '') if selected_video else ''
            video_backup_urls = selected_video.get('backupUrl', []) if selected_video else []
            audio_url = selected_audio.get('baseUrl', '') if selected_audio else ''
            audio_backup_urls = selected_audio.get('backupUrl', []) if selected_audio else []
            t = threading.Thread(target=self._download_bili_thread,
                                 args=(gid, video_url, audio_url, title,
                                       video_backup_urls, audio_backup_urls),
                                 daemon=True)
            t.start()
            return {"ok": True, "gid": gid, "type": "bilibili"}


    def stop_bili_download(self, gid, skip_trash=False):
        """Stop a Bilibili download."""
        if gid in self.bili_tasks:
            info = self.bili_tasks[gid]
            proc = info.get('proc')
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            info['state'] = 'removed'
            if not skip_trash:
                output = info.get('output', '')
                self.records["trash"].insert(0, {
                    "gid": gid, "type": "bilibili", "name": info.get('title', 'B站视频'),
                    "url": info.get('url', ''), "dir": self.save_path,
                    "paths": [output] if output and os.path.exists(output) else [],
                    "size": info.get('size', 0), "completed_at": int(time.time()),
                })
                self._save_records()
            else:
                output = info.get('output', '')
                if output and os.path.exists(output):
                    try:
                        os.remove(output)
                    except Exception:
                        pass
            del self.bili_tasks[gid]
            return True
        return False

    # ---- ed2k → magnet conversion ----
    def _convert_ed2k(self, url):
        """Convert an ed2k link to a magnet/torrent download by searching
        public torrent indexes for the same file."""
        parsed = parse_ed2k_url(url)
        if not parsed:
            return {"ok": False, "error": "无法解析 ed2k 链接格式", "type": "ed2k"}
        # Search for a matching torrent/magnet link
        result = search_magnet_for_ed2k(parsed["name"], parsed["size"])
        if result.get("ok"):
            # Found a magnet link — return it for the preview card
            return {
                "ok": True,
                "title": result["title"],
                "poster": "",
                "magnet": result["magnet"],
                "m3u8_url": "",
                "type": "torrent",
                "ed2k_source": True,
                "ed2k_name": parsed["name"],
                "ed2k_hash": parsed["hash"],
                "ed2k_size": parsed["size"],
            }
        else:
            # No torrent found — include file info in error so user knows what they wanted
            size_str = self._fmt_size(parsed["size"])
            error_msg = (
                f"文件：{parsed['name']}\n"
                f"大小：{size_str}\n"
                f"ed2k Hash：{parsed['hash']}\n\n"
                + result["error"]
            )
            return {"ok": False, "error": error_msg, "type": "ed2k"}

    @staticmethod
    def _fmt_size(size):
        """Format bytes to human-readable size string."""
        if not size:
            return "0 B"
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        i = 0
        v = size
        while v >= 1024 and i < len(units) - 1:
            v /= 1024
            i += 1
        return f"{v:.1f} {units[i]}"


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


@app.route("/api/ed2k_parse", methods=["POST"])
def api_ed2k_parse():
    """Parse an ed2k link to extract file name/size/hash (no search)."""
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    parsed = parse_ed2k_url(url)
    if parsed:
        return jsonify(ok=True, name=parsed["name"], size=parsed["size"], hash=parsed["hash"])
    return jsonify(ok=False)


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
    gid = data.get("gid")
    if gid and gid.startswith("m3u8_"):
        return jsonify(ok=engine.pause_m3u8_download(gid))
    if gid and gid.startswith("yt_"):
        return jsonify(ok=False, error="yt-dlp 下载不支持暂停")
    if gid and gid.startswith("bili_"):
        return jsonify(ok=False, error="B站视频下载不支持暂停")
    return jsonify(ok=engine.pause(data.get("gid")))


@app.route("/api/resume", methods=["POST"])
def api_resume():
    data = request.get_json(force=True)
    gid = data.get("gid")
    if gid and gid.startswith("m3u8_"):
        return jsonify(ok=engine.resume_m3u8_download(gid))
    return jsonify(ok=engine.resume(data.get("gid")))


@app.route("/api/retry", methods=["POST"])
def api_retry():
    """重试失败的任务。m3u8 任务使用续传，aria2 任务调用 resume。"""
    data = request.get_json(force=True)
    gid = data.get("gid")
    if gid and gid.startswith("m3u8_"):
        return jsonify(ok=engine.resume_m3u8_download(gid))
    else:
        return jsonify(ok=engine.resume(gid))


@app.route("/api/stop", methods=["POST"])
def api_stop():
    data = request.get_json(force=True)
    gid = data.get("gid")
    skip_trash = data.get("skip_trash", False)
    if gid and gid.startswith("m3u8_"):
        engine.stop_m3u8_download(gid, skip_trash=skip_trash)
    elif gid and gid.startswith("yt_"):
        engine.stop_yt_download(gid, skip_trash=skip_trash)
    elif gid and gid.startswith("bili_"):
        engine.stop_bili_download(gid, skip_trash=skip_trash)
    else:
        engine.stop(gid, skip_trash=skip_trash)
    return jsonify(ok=True)


@app.route("/api/trash", methods=["POST"])
def api_trash():
    data = request.get_json(force=True)
    return jsonify(ok=engine.to_trash(data.get("gid")))


@app.route("/api/restore", methods=["POST"])
def api_restore():
    data = request.get_json(force=True)
    result = engine.restore(data.get("gid"))
    if isinstance(result, dict):
        return jsonify(result)
    return jsonify(ok=result)


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
    paused = data.get("paused", False)
    if not url:
        return jsonify(ok=False, error="请输入 m3u8 地址"), 400
    gid = engine.start_m3u8_download(url, title or "视频下载", paused=paused)
    return jsonify(ok=True, gid=gid, type="m3u8")


@app.route("/api/download_yt", methods=["POST"])
def api_download_yt():
    """启动 YouTube/X 视频下载。"""
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    title = data.get("title", "").strip()
    format_id = data.get("format_id", "") or None
    is_audio_only = data.get("is_audio_only", False)
    if not url:
        return jsonify(ok=False, error="请输入视频链接"), 400
    gid = engine.start_yt_download(url, format_id=format_id, title=title or "视频下载",
                                    is_audio_only=is_audio_only)
    return jsonify(ok=True, gid=gid, type="yt_media")


@app.route("/api/yt_info", methods=["POST"])
def api_yt_info():
    """提取 YouTube/X 视频信息（格式列表），不自动下载。"""
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    if not url:
        return jsonify(ok=False, error="请输入视频链接"), 400
    result = engine._extract_yt_info(url)
    return jsonify(result)


@app.route("/api/bili_info", methods=["POST"])
def api_bili_info():
    """提取B站视频信息（画质列表+分P），不自动下载。"""
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    page = data.get("page", 1)
    if not url:
        return jsonify(ok=False, error="请输入B站视频链接"), 400
    resolved = _resolve_bili_url(url)
    if not resolved:
        return jsonify(ok=False, error="无法识别B站链接格式")
    mode, id_ = resolved
    if mode == 'video':
        result = _extract_bili_video(id_, page=page)
    elif mode == 'bangumi':
        result = _extract_bili_bangumi(id_)
    else:
        result = {"ok": False, "error": "暂不支持该链接类型"}
    return jsonify(result)


@app.route("/api/download_bili", methods=["POST"])
def api_download_bili():
    """启动B站视频下载（DASH流+ffmpeg合并）。"""
    data = request.get_json(force=True)
    bvid = data.get("bvid", "").strip()
    cid = data.get("cid", 0)
    aid = data.get("aid", 0)
    title = data.get("title", "").strip()
    quality_id = data.get("quality_id", 32)  # default 480P
    codec_id = data.get("codec_id", 7)  # default H264
    audio_id = data.get("audio_id", 0)  # 0 = auto best
    if not bvid:
        return jsonify(ok=False, error="缺少B站视频BV号"), 400
    result = engine.start_bili_download(bvid, cid, quality_id, codec_id, audio_id,
                                        title=title or "B站视频", aid=aid)
    return jsonify(result)


@app.route("/api/bili_qr_generate")
def api_bili_qr_generate():
    """生成B站扫码登录二维码图片。"""
    return jsonify(_bili_qr_generate())


@app.route("/api/bili_qr_poll")
def api_bili_qr_poll():
    """轮询B站扫码登录状态。qrcode_key 作为 query param。"""
    qrcode_key = request.args.get("qrcode_key", "").strip()
    if not qrcode_key:
        return jsonify(ok=False, error="缺少 qrcode_key"), 400
    return jsonify(_bili_qr_poll(qrcode_key))


@app.route("/api/bili_login_status")
def api_bili_login_status():
    """检查B站登录状态：是否已保存Cookie，以及是否仍然有效。"""
    cookies = _get_bili_cookie()
    if not cookies:
        return jsonify(ok=True, logged_in=False, message="未登录")
    # Validate cookie by calling nav API
    headers = {**BILI_HEADERS}
    headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())
    try:
        r = requests.get('https://api.bilibili.com/x/web-interface/nav', headers=headers, timeout=10)
        nav_data = r.json()
        is_login = nav_data.get('data', {}).get('isLogin', False)
        uname = nav_data.get('data', {}).get('uname', '')
        vip_type = nav_data.get('data', {}).get('vipType', 0)  # 0=无, 1=月度, 2=年度
        vip_label = nav_data.get('data', {}).get('vipLabel', {})
        vip_status = vip_label.get('text', '')
        if is_login:
            vip_info = vip_status or ('大会员' if vip_type in (1, 2) else '非会员')
            return jsonify(ok=True, logged_in=True, username=uname, vip=vip_info, message=f"已登录：{uname}（{vip_info}）")
        else:
            # Cookie expired — delete it
            if os.path.exists(_BILI_COOKIE_FILE):
                try:
                    os.remove(_BILI_COOKIE_FILE)
                except Exception:
                    pass
            return jsonify(ok=True, logged_in=False, message="Cookie已过期，请重新登录")
    except Exception as e:
        return jsonify(ok=True, logged_in=False, message="检查登录状态失败")


@app.route("/api/bili_logout", methods=["POST"])
def api_bili_logout():
    """删除保存的B站Cookie（退出登录）。"""
    if os.path.exists(_BILI_COOKIE_FILE):
        try:
            os.remove(_BILI_COOKIE_FILE)
        except Exception:
            pass
    return jsonify(ok=True, message="已退出B站登录")


@app.route("/api/bili_cookie", methods=["POST"])
def api_bili_cookie():
    """保存B站 SESSDATA cookie（用于获取1080P+画质）。"""
    data = request.get_json(force=True)
    sessdata = data.get("SESSDATA", "").strip()
    if not sessdata:
        # Delete the cookie file
        if os.path.exists(_BILI_COOKIE_FILE):
            try:
                os.remove(_BILI_COOKIE_FILE)
            except Exception:
                pass
        return jsonify(ok=True, message="已删除Cookie")
    # Save to file
    try:
        json.dump({"SESSDATA": sessdata}, open(_BILI_COOKIE_FILE, "w"))
        return jsonify(ok=True, message="Cookie已保存，下次解析B站视频将使用登录身份获取1080P+画质")
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/bili_poster")
def api_bili_poster():
    """代理B站封面图片（B站CDN要求Referer，浏览器img标签无法直接加载）。"""
    url = request.args.get("url", "").strip()
    if not url or not url.startswith("http"):
        return "", 400
    # Force HTTPS
    url = url.replace("http://", "https://")
    try:
        r = requests.get(url, headers=BILI_HEADERS, timeout=10)
        content_type = r.headers.get("Content-Type", "image/jpeg")
        return Response(r.content, content_type=content_type)
    except Exception:
        return "", 404@app.route("/api/choose_dir", methods=["POST"])
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


@app.route("/api/open_folder", methods=["POST"])
def api_open_folder():
    """在 Finder 中打开下载目录。"""
    data = request.get_json(force=True)
    path = data.get("path", engine.save_path)
    try:
        subprocess.Popen(["open", "-R", path])
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/play_file", methods=["POST"])
def api_play_file():
    """用默认应用打开文件（播放视频等）。"""
    data = request.get_json(force=True)
    path = data.get("path", "")
    if not path or not os.path.exists(path):
        return jsonify(ok=False, error="文件不存在")
    try:
        subprocess.Popen(["open", path])
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


if __name__ == "__main__":
    os.makedirs(DEFAULT_SAVE, exist_ok=True)
    print("=" * 60)
    print("  磁力/P2P/HTTP 下载器已启动:  http://127.0.0.1:5566")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5566, debug=False, threaded=True)

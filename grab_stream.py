#!/usr/bin/env python3
"""独立抓流脚本：在子进程中用 Playwright 拦截爱奇艺/腾讯视频/抖音直播的播放器请求。
与主进程隔离，Playwright 崩溃/卡顿不会影响 Flask 服务。
用法: python grab_stream.py <platform> <url>
platform: iqiyi | tencent | douyin | douyin_live
输出: 一行 JSON 到 stdout，随后 os._exit 强制退出（避免 browser.close 卡住）。"""
import sys, json, os

_IS_WIN = sys.platform == "win32"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
       ) if _IS_WIN else (
       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _launch():
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    args = [
        "--disable-blink-features=AutomationControlled", "--no-first-run",
        "--no-default-browser-check", "--disable-dev-shm-usage",
        "--no-sandbox", "--no-proxy-server"]
    last_error = None
    # PyInstaller packages do not install Playwright's Chromium copy. Windows
    # normally has Edge and many machines have Chrome, so use them as fallbacks.
    for options in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
        try:
            b = p.chromium.launch(headless=True, args=args, **options)
            ctx = b.new_context(user_agent=_UA)
            return p, b, ctx
        except Exception as e:
            last_error = e
    raise last_error


def grab_iqiyi(url):
    p, b, ctx = _launch()
    page = ctx.new_page()
    st = {'segs': [], 'title': '', 'vip': None, 'poster': ''}
    def on_resp(r):
        if 'playervideoinfo' in r.url:
            try:
                d = json.loads(r.text()).get('data', {}) or {}
                if d.get('vn'): st['title'] = d['vn']
                if d.get('vipType') is not None: st['vip'] = d['vipType']
                if d.get('vpic'): st['poster'] = d['vpic']
            except Exception:
                pass
    def on_req(r):
        u = r.url; ul = u.lower()
        if ('.ts' in ul or '.f4v' in ul) and '71edge' in ul:
            if u not in st['segs']: st['segs'].append(u)
    page.on('response', on_resp)
    page.on('request', on_req)
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=20000)
        page.wait_for_timeout(4000)
        try: page.click('canvas, [class*=play], .container', timeout=2500)
        except Exception: pass
        page.wait_for_timeout(8000)
        if not st['title']: st['title'] = page.title().split('-')[0].strip()
    except Exception as e:
        st['error'] = 'page: ' + str(e)[:120]
    try:
        st['cookies'] = ctx.cookies()
    except Exception:
        st['cookies'] = []
    return st


def grab_tencent(url):
    p, b, ctx = _launch()
    page = ctx.new_page()
    st = {'proxy': None, 'title': '', 'poster': '', 'duration': 0}
    def on_resp(r):
        if 'proxyhttp' in r.url and '.m3u8' not in r.url.lower() and not st['proxy']:
            try: st['proxy'] = r.text()
            except Exception: pass
    page.on('response', on_resp)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(4000)
        try: page.click('canvas, [class*=play], .container', timeout=2500)
        except Exception: pass
        page.wait_for_timeout(8000)
        # 提取 og:title / og:image（封面图），独立 try 避免影响主流程
        # 提取 og:title / og:image（封面图），用 page.locator 避免引号问题
        try:
            t_el = page.query_selector('meta[property="og:title"]')
            i_el = page.query_selector('meta[property="og:image"]')
            meta = {
                'title': t_el.get_attribute('content') if t_el else '',
                'poster': i_el.get_attribute('content') if i_el else '',
            }
        except Exception:
            meta = {}
        # 同时用 page.title() 兜底
        raw_title = meta.get('title', '') or page.title()
        import re as _re
        # 清理腾讯视频标题的各种后缀模式
        raw_title = _re.sub(r'_(综艺|电视剧|纪录片|电影|动漫|少儿)_高清完整版视频在线观看(_腾讯视频)?$', '', raw_title)
        raw_title = _re.sub(r'_高清完整版视频在线观看(_腾讯视频)?$', '', raw_title)
        raw_title = _re.sub(r'[-_]腾讯视频$', '', raw_title)
        raw_title = raw_title.strip()
        st['title'] = raw_title.strip() or page.title().split('-')[0].strip()
        st['poster'] = meta.get('poster', '')
        if st['proxy']:
            try:
                outer = json.loads(st['proxy'])
                vi = json.loads(outer.get('vinfo', '{}')) if isinstance(outer.get('vinfo'), str) else outer.get('vinfo', {})
                st['duration'] = int(vi.get('s', 0) or vi.get('duration', 0) or 0)
            except Exception: pass
    except Exception as e:
        st['error'] = 'page: ' + str(e)[:120]
    try:
        st['cookies'] = ctx.cookies()
    except Exception:
        st['cookies'] = []
    return st


def _collect_live_streams(value, out, prefix=''):
    """Walk Douyin page/API JSON and collect HLS/FLV playback URLs."""
    if isinstance(value, dict):
        for key, child in value.items():
            path = (prefix + '.' + key).lstrip('.')
            if isinstance(child, str) and child.startswith(('http://', 'https://')):
                low = child.lower()
                is_hls = ('hls' in key.lower() or 'm3u8' in key.lower() or '.m3u8' in low)
                is_flv = ('flv' in key.lower() or '.flv' in low)
                if is_hls or is_flv:
                    label = 'HLS' if is_hls else 'FLV'
                    if 'hd' in key.lower() or 'hd' in path.lower():
                        label += ' 高清'
                    elif 'sd' in key.lower() or 'sd' in path.lower():
                        label += '标清'
                    out.append((label, child))
            else:
                _collect_live_streams(child, out, path)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _collect_live_streams(child, out, f'{prefix}.{i}')


def grab_douyin_live(url):
    """Douyin live extraction: render the room and capture its playback API/requests."""
    import re as _re

    p, b, ctx = _launch()
    page = ctx.new_page()
    st = {'error': None, 'streams': [], 'title': '', 'poster': '', 'uploader': '',
          'room_status': None}

    def current_room(payload):
        data = payload.get('data', {}) or {}
        rooms = data.get('data', []) or []
        if isinstance(rooms, list):
            return rooms[0] if rooms else {}
        if isinstance(rooms, dict):
            return rooms
        return {}

    def on_response(resp):
        u = resp.url
        low = u.lower()
        if '.m3u8' in low:
            st['streams'].append(('HLS', u))
        elif '.flv' in low:
            st['streams'].append(('FLV', u))
        elif '/webcast/room/web/enter/' in low:
            try:
                data = json.loads(resp.text())
                room = current_room(data)
                st['room_status'] = room.get('status')
                if room.get('title'):
                    st['title'] = room['title']
                user = (data.get('data', {}) or {}).get('user', {}) or {}
                if user.get('nickname'):
                    st['uploader'] = user['nickname']
                # similar_rooms also carries playback URLs. Ignore them so a
                # stopped room cannot be replaced by a recommended room.
                _collect_live_streams(room, st['streams'], 'room')
            except Exception:
                pass

    page.on('response', on_response)
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=20000)
        page.wait_for_timeout(5000)
        try:
            page.click('canvas, [class*=play], [class*=player], [class*=container]', timeout=2500)
        except Exception:
            pass
        page.wait_for_timeout(8000)

        try:
            title_el = page.query_selector('meta[property="og:title"]')
            poster_el = page.query_selector('meta[property="og:image"]')
            title = title_el.get_attribute('content') if title_el else ''
            poster = poster_el.get_attribute('content') if poster_el else ''
        except Exception:
            title, poster = '', ''
        st['title'] = (title or page.title() or '').strip()
        st['poster'] = poster
    except Exception as e:
        st['error'] = 'page: ' + str(e)[:120]

    # Prefer HLS: ffmpeg can remux an HLS live event to MP4 without re-encoding.
    unique = {}
    for label, stream_url in st['streams']:
        clean = stream_url.replace('\\u002F', '/').replace('\\/', '/')
        if clean not in unique:
            unique[clean] = label
    hls = [(u, label) for u, label in unique.items() if '.m3u8' in u.lower() or label.startswith('HLS')]
    flv = [(u, label) for u, label in unique.items() if u not in dict(hls)]

    def codec_rank(stream_url):
        low = stream_url.lower()
        if 'vcodec=h264' in low or 'codec=h264' in low:
            return 0
        if 'vcodec=h265' in low or 'vcodec=hevc' in low:
            return 2
        return 1

    hls.sort(key=lambda item: codec_rank(item[0]))
    flv.sort(key=lambda item: codec_rank(item[0]))
    if hls and (not flv or codec_rank(hls[0][0]) <= codec_rank(flv[0][0])):
        chosen = hls[0][0]
    elif flv:
        chosen = flv[0][0]
    else:
        chosen = ''
    if not chosen:
        if st.get('room_status') not in (None, 2):
            st['error'] = '直播间当前不可播放（状态 %s）' % st.get('room_status')
        else:
            st['error'] = st.get('error') or '未捕获到当前直播间流，直播间可能已结束、需要登录或触发风控验证'
        return st

    formats = []
    for stream_url, label in (hls + flv):
        stream_type = 'HLS' if stream_url in dict(hls) else 'FLV'
        formats.append({
            'format_id': 'douyin_live_' + stream_type.lower(),
            'label': label,
            'url': stream_url,
        })
    return {
        'title': st.get('title') or '抖音直播',
        'poster': st.get('poster', ''),
        'uploader': st.get('uploader', ''),
        'm3u8_url': chosen,
        'formats': formats,
        'is_live': True,
    }




def grab_douyin(url):
    """抖音提取：访问首页拿 cookie + webmssdk，requests 解析短链接，
    page.evaluate fetch 调 detail API（webmssdk 自动加 a_bogus 签名）。"""
    import re as _re
    import requests as _req
    p, b, ctx = _launch()
    page = ctx.new_page()
    st = {'error': None}
    try:
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=15000)
    except Exception as e:
        st['error'] = 'homepage: ' + str(e)[:80]
    for _ in range(10):
        page.wait_for_timeout(1000)
        if any(c.get('name') == 's_v_web_id' for c in page.context.cookies()):
            break
    final_url = url
    if 'v.douyin.com' in url or 'iesdouyin.com' in url:
        try:
            rr = _req.head(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8,
                           allow_redirects=True, proxies={'http': None, 'https': None})
            if rr.url:
                final_url = rr.url
        except Exception:
            pass
    m = _re.search(r'/video/(\d+)', final_url) or _re.search(r'/note/(\d+)', final_url)
    if not m:
        st['error'] = st.get('error') or ('cannot extract aweme_id from ' + final_url[:60])
        return st
    aweme_id = m.group(1)
    js = """
    async () => {
        const params = new URLSearchParams({
            aweme_id: "%s", device_platform: "webapp", aid: "6383",
            channel: "channel_pc_web", pc_client_type: "1",
            version_code: "170400", update_version_code: "170400",
            cover_format: "0", support_h265: "1", support_dash: "1",
        });
        try {
            const r = await fetch("/aweme/v1/web/aweme/detail/?" + params.toString());
            return {status: r.status, body: await r.text()};
        } catch(e) { return {error: e.message}; }
    }
    """ % aweme_id
    result = page.evaluate(js)
    if not result or result.get('error'):
        st['error'] = 'fetch: ' + str(result.get('error', '') if result else 'no result')[:100]
        return st
    if result.get('status') != 200 or not result.get('body'):
        st['error'] = 'api status=%s body_len=%d' % (result.get('status'), len(result.get('body', '')))
        return st
    try:
        data = json.loads(result['body'])
    except Exception as e:
        st['error'] = 'json parse: ' + str(e)[:80]
        return st
    detail = data.get('aweme_detail')
    if not detail:
        st['error'] = 'aweme_detail null, filter=' + str(data.get('filter_detail', {}).get('filter_reason', ''))[:50]
        return st
    video = detail.get('video', {}) or {}
    play_addr = video.get('play_addr', {}) or {}
    play_urls = play_addr.get('url_list', []) or []
    if not play_urls:
        for br in (video.get('bit_rate', []) or []):
            br_pa = br.get('play_addr') or {}
            if br_pa.get('url_list'):
                play_urls = br_pa['url_list']
                break
    if not play_urls:
        st['error'] = 'no play urls'
        return st
    formats = []
    seen = set()
    for br in (video.get('bit_rate', []) or []):
        br_pa = br.get('play_addr', {}) or {}
        br_urls = br_pa.get('url_list', []) or []
        if not br_urls or br_urls[0] in seen:
            continue
        seen.add(br_urls[0])
        gear = br.get('gear_name', '') or ''
        qt = br.get('quality_type', 0) or 0
        height = (br.get('play_addr_h264', {}) or {}).get('height', 0) or video.get('height', 0) or 0
        formats.append({'format_id': 'douyin_%s' % qt, 'label': gear or ('%dp' % height if height else ''),
                        'url': br_urls[0], 'height': height})
    if not formats:
        formats.append({'format_id': 'douyin_default', 'label': '默认', 'url': play_urls[0]})
    cover = (video.get('cover', {}) or {}).get('url_list', []) or []
    poster = cover[0] if cover else ''
    desc = (detail.get('desc', '') or '').strip()
    author = (detail.get('author', {}) or {}).get('nickname', '') or ''
    return {'title': desc or '抖音视频', 'poster': poster, 'uploader': author,
            'duration': (video.get('duration', 0) or 0) // 1000,
            'm3u8_url': play_urls[0], 'formats': formats, 'aweme_id': aweme_id}

if __name__ == '__main__':
    import os, signal
    platform = sys.argv[1]; url = sys.argv[2]
    try:
        if platform == 'iqiyi':
            r = grab_iqiyi(url)
        elif platform == 'tencent':
            r = grab_tencent(url)
        elif platform == 'douyin':
            r = grab_douyin(url)
        elif platform == 'douyin_live':
            r = grab_douyin_live(url)
        else:
            r = {'error': 'unknown platform: ' + platform}
        sys.stdout.write(json.dumps(r, ensure_ascii=False) + '\n')
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(json.dumps({'error': str(e)[:200]}, ensure_ascii=False) + '\n')
        sys.stdout.flush()
    # 只用 os._exit 退出，不用 killpg（os.getpgid(0) 在 start_new_session 未生效时
    # 可能返回父进程的 pgid，killpg 会误杀 Flask 主进程）。残留 chromium 由
    # _cleanup_chromium 在下次抓流前清理。
    os._exit(0)

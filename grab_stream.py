#!/usr/bin/env python3
"""独立抓流脚本：在子进程中用 Playwright 拦截爱奇艺/腾讯视频的播放器请求。
与主进程隔离，Playwright 崩溃/卡顿不会影响 Flask 服务。
用法: python grab_stream.py <platform> <url>
platform: iqiyi | tencent
输出: 一行 JSON 到 stdout，随后 os._exit 强制退出（避免 browser.close 卡住）。"""
import sys, json, os


def _launch():
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    b = p.chromium.launch(headless=True, args=[
        "--disable-blink-features=AutomationControlled", "--no-first-run",
        "--no-default-browser-check", "--disable-dev-shm-usage",
        "--no-sandbox", "--no-proxy-server"])
    ctx = b.new_context(user_agent=
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    return p, b, ctx


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
        if ('.ts' in ul or '.f4v' in ul) and '71edge' in ul and '/vts/' in ul:
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
    st['cookies'] = ctx.cookies()
    return st


def grab_tencent(url):
    p, b, ctx = _launch()
    page = ctx.new_page()
    st = {'proxy': None, 'title': '', 'poster': ''}
    def on_resp(r):
        if 'proxyhttp' in r.url and '.m3u8' not in r.url.lower() and not st['proxy']:
            try: st['proxy'] = r.text()
            except Exception: pass
    page.on('response', on_resp)
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=20000)
        page.wait_for_timeout(4000)
        try: page.click('canvas, [class*=play], .container', timeout=2500)
        except Exception: pass
        page.wait_for_timeout(8000)
        st['title'] = page.title().split('-')[0].strip()
    except Exception as e:
        st['error'] = 'page: ' + str(e)[:120]
    st['cookies'] = ctx.cookies()
    return st


if __name__ == '__main__':
    platform = sys.argv[1]; url = sys.argv[2]
    try:
        r = grab_iqiyi(url) if platform == 'iqiyi' else grab_tencent(url)
        sys.stdout.write(json.dumps(r, ensure_ascii=False) + '\n')
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(json.dumps({'error': str(e)[:200]}, ensure_ascii=False) + '\n')
        sys.stdout.flush()
    # 结果已 flush 到 pipe，父进程可读到。退出前 killpg 杀掉整个进程组
    # （含 chromium 孙进程），避免 os._exit 漏掉子进程造成孤儿堆积。
    # 注：不调用 browser.close()——本机环境下它会卡住，这也是当初用 os._exit 的原因。
    import signal
    try:
        os.killpg(os.getpgid(0), signal.SIGKILL)
    except Exception:
        pass
    os._exit(0)

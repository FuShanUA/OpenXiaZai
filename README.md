# OpenXiaZai

开源的桌面下载器，支持磁力链接、种子、HTTP/HTTPS、FTP、ed2k (电驴) 等多种协议，基于 aria2 + aMule + Flask + pywebview 构建。

## 特性

- **多协议支持**：磁力链接 (magnet)、种子 (torrent)、HTTP/HTTPS 直链、FTP、ed2k (电驴)、M3U8 流媒体、YouTube/X 视频 (yt-dlp)、B站视频 (DASH流API)、爱奇艺/腾讯视频/抖音 (Playwright 拦流)、抖音直播 (HLS/FLV 录制)、微信视频号视频与直播 (Windows 内存抓取 / macOS 代理捕获)
- **种子文件选择**：添加磁力/种子后弹出文件选择窗口，可勾选需要下载的文件，支持按类型筛选（视频/音乐/图片/文档）
- **多任务并行**：支持同时下载最多 3 个任务，每个任务 16 线程
- **DHT 加速**：内置 DHT 启动节点、持久化路由表、扩展 Tracker 列表，加速磁力链接解析
- **会话持久化**：重启后自动恢复下载任务，支持断点续传
- **历史记录**：下载完成后自动归档，支持回收站和彻底删除
- **原生体验**：macOS 原生窗口，可拖拽文件选择弹窗，支持系统文件夹选择对话框

## 安装

### 依赖

#### macOS

```bash
brew install aria2 ffmpeg node
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

源码运行时，`mitmproxy` 由 Python 虚拟环境提供；打包后的 `.app` 不内置 `mitmproxy` 和 Node.js，使用 macOS 视频号捕获前需要安装：

```bash
brew install mitmproxy node
```

#### Windows

```bash
# 安装 aria2 和 ffmpeg（任选一种方式）
# 方式1: scoop
scoop install aria2 ffmpeg
# 方式2: chocolatey
choco install aria2 ffmpeg
# 方式3: 手动下载放入 PATH 或项目目录
#   aria2c.exe: https://github.com/aria2/aria2/releases
#   ffmpeg.exe: https://ffmpeg.org/download.html

pip install flask pywebview requests yt-dlp playwright
```

### 克隆

#### macOS / Linux

```bash
git clone https://github.com/FuShanUA/OpenXiaZai.git
cd OpenXiaZai
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

#### Windows

```cmd
git clone https://github.com/FuShanUA/OpenXiaZai.git
cd OpenXiaZai
python -m venv venv
venv\Scripts\activate
pip install flask pywebview requests yt-dlp playwright
python -m playwright install chromium
```

## 使用

### 桌面应用

macOS:

```bash
# 使用启动脚本
./start.sh
```

Windows:

```cmd
:: 双击 start.bat 或在命令行运行
start.bat
```

### 命令行

macOS / Linux:

```bash
cd OpenXiaZai
venv/bin/python launcher.py
```

Windows:

```cmd
cd OpenXiaZai
venv\Scripts\python.exe launcher.py
```

或仅启动 Web 服务（在浏览器中使用）：

```bash
python app.py
# 打开 http://127.0.0.1:5566
```

### macOS 微信视频号

macOS 不能复用 Windows 的微信进程内存扫描方案。OpenXiaZai 使用本地 HTTPS 代理捕获微信视频号页面请求，注入脚本取得 `decode_key`，再调用微信官方 WASM 组件解密视频文件。

首次使用流程：

1. 安装 Node.js 20 或更高版本；源码运行还需安装 `requirements.txt`，打包版需另装 `mitmproxy`。
2. 在主界面点击「视频号」，先点击「下载解密组件」，再运行「自检」。组件来自微信 CDN，保存在本机 `vendor/wechat_files/raw`，仓库不分发这些专有文件。
3. 启动一次捕获，让 mitmproxy 生成 `~/.mitmproxy/mitmproxy-ca-cert.pem`。
4. 点击「导入证书」，选择 `login` 钥匙串，然后在钥匙串访问中把 mitmproxy CA 的 SSL 信任设为「始终信任」，最后完全退出并重启微信。
5. 重新启动捕获，在微信桌面版里播放视频号视频。捕获列表出现视频后选择清晰度并点击「下载」。

视频号直播会在捕获列表旁出现「直播录制」页。当前频道排在列表首位，右侧通过单一 HLS 预览进程同步播放直播画面。点击「开始录制」后，需要结束时点击「停止录制」，应用会用 ffmpeg 保存为 MP4。

捕获服务会把直播媒体 CDN 加入系统代理绕过列表。页面请求仍走本地代理用于捕获，直播视频流则直连，避免代理影响直播播放。
重启下载器时会从捕获记录恢复最近一次加入的直播间，已打开的直播页不需要重新执行 joinLive 才能出现在列表里。

捕获运行时会临时把当前网络服务的 HTTP/HTTPS 代理设为 `127.0.0.1:9090`，停止捕获或退出 OpenXiaZai 时恢复原设置。视频默认保存到下载目录的 `WeChatChannels` 子目录。微信签名的 CDN 链接会过期，列表提示过期时需要回到微信重新播放。

### Windows 微信视频号

Windows 版沿用桌面微信进程内存中的视频号直链扫描。先在微信里播放视频，再点击「视频号」抓取链接。

### 抖音直播

粘贴 `https://live.douyin.com/<room_id>` 直播间链接后点击「添加下载」。应用会用 Playwright 打开直播间，拦截 HLS 或 FLV 播放地址，再交给 ffmpeg 直接复制流并保存为 MP4。直播没有固定时长，需要手动点击任务卡片上的「停止录制」；停止后已录制文件会保留在下载目录，任务记录进入回收站。

### 打包

```bash
# macOS: 生成 .app + .dmg
python build.py mac

# Windows: 生成 .exe（自动捆绑 aria2c.exe 和 ffmpeg.exe）
python build.py win
```

macOS `.app` 会捆绑 aria2、ffmpeg 等已有二进制，但不捆绑 `mitmdump` 和 Node.js。构建时若未找到它们，会提示 macOS 视频号捕获不可用。

## 方案来源

- [passby6someone/wechat-channels-downloader](https://github.com/passby6someone/wechat-channels-downloader)：macOS 代理捕获、页面注入和 WASM 解密实现，MIT。
- [Evil0ctal/WeChat-Channels-Video-File-Decryption](https://github.com/Evil0ctal/WeChat-Channels-Video-File-Decryption)：视频文件解密参考，MIT。
- [xuncv/WeChatDownloader](https://github.com/xuncv/WeChatDownloader)：Windows 微信进程内存抓取机制参考，Apache-2.0。
- [putyy/res-downloader](https://github.com/putyy/res-downloader)：成熟跨平台方案，Apache-2.0；因引入 Go/Wails 技术栈，暂作为外部工具和交互参考。
- [ltaoo/wx_channels_download](https://github.com/ltaoo/wx_channels_download)：macOS/Windows 方案，MIT 加 Commons Clause，限制将该功能作为收费产品或服务出售，因此不直接并入本项目。

## 技术栈

| 组件 | 用途 |
|------|------|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | YouTube/X 视频下载引擎 |
| [wechat-channels-downloader](https://github.com/passby6someone/wechat-channels-downloader) | macOS 微信视频号捕获与解密 |
| [aria2](https://github.com/aria2/aria2) | 下载引擎，支持多协议、多线程、DHT |
| [aMule](https://github.com/amule-project/amule) | ed2k/eDonkey 下载引擎（可选） |
| [Flask](https://flask.palletsprojects.com/) | Web 后端，提供 REST API |
| [pywebview](https://pywebview.flowrl.com/) | 原生桌面窗口（macOS WKWebView / Windows WebView2） |

## 项目结构

```
OpenXiaZai/
├── app.py              # Flask 后端 + aria2 RPC 客户端
├── grab_stream.py      # 爱奇艺/腾讯视频/抖音 抓流子进程（Playwright 拦截播放器请求）
├── wechat_finder.py    # Windows 桌面版微信视频号直链抓取
├── wechat_capture/     # macOS 微信视频号 mitmproxy 捕获与解密
├── launcher.py         # 桌面应用启动器（pywebview）
├── templates/
│   └── index.html      # 前端界面
├── assets/             # 图标源文件
├── icon.iconset/       # macOS 图标集
├── app_icon.icns       # macOS 应用图标
└── .gitignore
```

## License

MIT

# OpenXiaZai

开源的桌面下载器，支持磁力链接、种子、HTTP/HTTPS、FTP、ed2k (电驴) 等多种协议，基于 aria2 + aMule + Flask + pywebview 构建。

## 特性

- **多协议支持**：磁力链接 (magnet)、种子 (torrent)、HTTP/HTTPS 直链、FTP、ed2k (电驴)、M3U8 流媒体、YouTube/X 视频 (yt-dlp)、B站视频 (DASH流API)、爱奇艺/腾讯视频/抖音 (Playwright 拦流)
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
brew install aria2 ffmpeg
pip install flask pywebview requests yt-dlp playwright
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
python -m venv venv
source venv/bin/activate
pip install flask pywebview requests yt-dlp playwright
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

### 打包

```bash
# macOS: 生成 .app + .dmg
python build.py mac

# Windows: 生成 .exe（自动捆绑 aria2c.exe 和 ffmpeg.exe）
python build.py win
```

## 技术栈

| 组件 | 用途 |
|------|------|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | YouTube/X 视频下载引擎 |
| [aria2](https://github.com/aria2/aria2) | 下载引擎，支持多协议、多线程、DHT |
| [aMule](https://github.com/amule-project/amule) | ed2k/eDonkey 下载引擎（可选） |
| [Flask](https://flask.palletsprojects.com/) | Web 后端，提供 REST API |
| [pywebview](https://pywebview.flowrl.com/) | 原生桌面窗口（macOS WKWebView / Windows WebView2） |

## 项目结构

```
OpenXiaZai/
├── app.py              # Flask 后端 + aria2 RPC 客户端
├── grab_stream.py      # 爱奇艺/腾讯视频/抖音 抓流子进程（Playwright 拦截播放器请求）
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
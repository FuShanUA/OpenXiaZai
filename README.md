# OpenXiaZai

开源的桌面下载器，支持磁力链接、种子、HTTP/HTTPS、FTP、ed2k (电驴) 等多种协议，基于 aria2 + aMule + Flask + pywebview 构建。

## 特性

- **多协议支持**：磁力链接 (magnet)、种子 (torrent)、HTTP/HTTPS 直链、FTP、ed2k (电驴)、M3U8 流媒体、YouTube/X 视频 (yt-dlp)
- **种子文件选择**：添加磁力/种子后弹出文件选择窗口，可勾选需要下载的文件，支持按类型筛选（视频/音乐/图片/文档）
- **多任务并行**：支持同时下载最多 3 个任务，每个任务 16 线程
- **DHT 加速**：内置 DHT 启动节点、持久化路由表、扩展 Tracker 列表，加速磁力链接解析
- **会话持久化**：重启后自动恢复下载任务，支持断点续传
- **历史记录**：下载完成后自动归档，支持回收站和彻底删除
- **原生体验**：macOS 原生窗口，可拖拽文件选择弹窗，支持系统文件夹选择对话框

## 安装

### 依赖

```bash
# macOS
brew install aria2
brew install amule   # 可选：ed2k 电驴下载支持

# Python 依赖
pip install flask pywebview requests yt-dlp yt-dlp
```

### 克隆

```bash
git clone https://github.com/FuShanUA/OpenXiaZai.git
cd OpenXiaZai
python -m venv venv
source venv/bin/activate
pip install flask pywebview requests yt-dlp
```

## 使用

### 桌面应用

双击 `OpenXiaZai.app`（需先创建桌面快捷方式）：

```bash
# 创建桌面快捷方式（macOS）
mkdir -p ~/Desktop/OpenXiaZai.app/Contents/{MacOS,Resources}
cp app_icon.icns ~/Desktop/OpenXiaZai.app/Contents/Resources/
# 编辑 ~/Desktop/OpenXiaZai.app/Contents/MacOS/OpenXiaZai 指向项目目录
```

### 命令行

```bash
cd OpenXiaZai
venv/bin/python launcher.py
```

或仅启动 Web 服务（在浏览器中使用）：

```bash
venv/bin/python app.py
# 打开 http://127.0.0.1:5566
```

## 技术栈

| 组件 | 用途 |
|------|------|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | YouTube/X 视频下载引擎 |
| [aria2](https://github.com/aria2/aria2) | 下载引擎，支持多协议、多线程、DHT |
| [aMule](https://github.com/amule-project/amule) | ed2k/eDonkey 下载引擎（可选） |
| [Flask](https://flask.palletsprojects.com/) | Web 后端，提供 REST API |
| [pywebview](https://pywebview.flowrl.com/) | 原生桌面窗口，WKWebView 承载前端 |

## 项目结构

```
OpenXiaZai/
├── app.py              # Flask 后端 + aria2 RPC 客户端
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
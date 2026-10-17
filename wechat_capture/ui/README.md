# work/ui —— Web 抓取台

挂在 mitmproxy 插件**已经**跑着的 `127.0.0.1:18765` HTTP 服务上,不新增进程、不新增依赖。

> **这是可选组件。** 删掉整个 `work/ui/` 目录,插件照常工作(只是没有页面)。
> 插件用 `_load_ui_router()` 探测 `ui/api.py` 是否存在,失败只打日志不中断。

---

## 文件

| 文件 | 作用 |
|---|---|
| `api.py` | 路由 + SSE 广播。`Router.handle(owner, method, path, headers, body)` 返回 `Response` 或 `None`(交给抓取端点) |
| `downloads.py` | 档位探测、下载任务与进度、Range 文件服务。**不 import 插件**,只通过 `owner` 读写 |
| `static/index.html` | 页面骨架 + `<template>` |
| `static/app.js` | 原生 JS。一个 state 对象、一个 `render()` 入口 |
| `static/style.css` | 只放 Pico 覆盖不到的部分 |
| `static/vendor/pico.min.css` | Pico CSS v2.1.1,MIT,本地化(不走 CDN) |

## 设计约束

照着 [`../../docs/WEBUI.md`](../../docs/WEBUI.md) 实现,几条硬性约定:

1. **零构建、无框架。** 不引入 npm、不引入 Alpine/React/Vue。改完直接刷新页面,
   所以 `static/` 里就是最终产物。
2. **一个 state,一个 render。** 事件回调只改 `state`,然后统一渲染;
   唯一例外是下载进度条,它每秒更新几次,按 id 就地改 `value` 而不重建节点。
3. **下载侧不用 Range**(一次全量 GET,分块读只为喂进度条);
   **预览侧实现 Range**(本地文件 206/416)。两者方向相反,别混。
4. **端口只绑 `127.0.0.1`**,不做鉴权 —— 依赖"本机 + 已装微信客户端"这个前提。

## 接口

```
GET  /                                     页面
GET  /static/<path>                        静态资源
GET  /api/state                            会话、计数器与 UI 版本
GET  /api/videos                           捕获列表(含档位、体积、链接年龄)
GET  /api/videos/<key>                     单个详情
GET  /api/videos/<key>/stream?variant=<q>  本地文件,支持 Range
GET  /api/videos/<key>/probe               重新探测档位体积
GET  /api/tasks                            下载任务
GET  /api/events                           SSE
GET  /api/log?tail=200                     捕获日志尾部
POST /api/videos/<key>/download            {"quality":"best"}
POST /api/selftest                         跑解密器自检
```

SSE 事件类型:`hello`(带 `version`)/ `videos_changed` / `hook` / `download` / `session`。

## 列表过滤的两个层次

每个视频有两个独立字段,含义不同,别混:

| 字段 | 来源 | 含义 |
|---|---|---|
| `watched` | addon 的 `viewing_objects` | 有**评论接口**触达(`getcommentlist` / `getcommentdetail` / `asyncgetcommentinfo` / `getbulletcomment`) → 这个视频在屏幕上 |
| `client_downloaded` | addon 的 `observed_downloads` | 客户端为它拉过分片 —— **可能是自动播放的预载** |
| `downloaded` | 磁盘 sidecar | **你自己点过下载**,有成品 |

默认视图只看 `watched`;`watch_evidence` 记录是哪个接口触达的,详情页会显示,便于溯源误判。

> 为什么不用下载字节判"看过":客户端为了让自动播放无缝,会把**下一个视频整片下完**。
> 实测三个视频都是 100% 覆盖、请求严格串行,和"真看了"在网络层完全一样。

## 版本握手(改前端必读)

`api.py` 的 `UI_VERSION` 与 `static/app.js` 的 `APP_VERSION` **必须同步递增**。

前后端各自会被缓存:旧 mitmdump 进程没有新字段,旧页面跑着旧脚本,两者都会**静默**给出错结果。
所以:

- SSE 的 `hello` 帧带后端版本;页面发现不一致会提示并**自动重载**
- `/api/videos` 若缺 `watched` 字段,页面顶部弹红条要求重启 mitmdump
- 副标题常显 `页面 vN/后端 vN`,一眼可核对
- `session_start` 记录 `ui_version`,事后可从 JSONL 判断某次抓取跑的是哪一版

## 手工验证

```bash
# 起插件(会打印 Web UI 地址)
mitmdump -s work/mitmproxy_wechat_capture.py --listen-port 9090

curl -s localhost:18765/api/state | python3 -m json.tool
curl -s localhost:18765/api/videos | python3 -m json.tool
curl -N localhost:18765/api/events                      # 应立刻收到 hello 帧

# Range 必须是 206 + 正确的 Content-Range
curl -D - -o /dev/null -H "Range: bytes=0-15" \
  "localhost:18765/api/videos/<encfilekey>/stream?variant=best"
```

## 更新 Pico

```bash
curl -sSL -o work/ui/static/vendor/pico.min.css \
  https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css
```

# 知乎 / 微信 / YouTube 内容提取（集成版）

以提供的 `app.py`、`index.html`、`zhihu_extract.py` 为基础，直接接入 YouTube 字幕和音频功能。原始下载目录文件未修改；本目录是一套完整可运行的副本。

## 启动

安装 Python 3.11+、Node.js 22+（含 npm），解压后双击 **start.cmd**。首次启动自动创建 `.venv` 并安装依赖、准备浏览器；默认打开 `http://127.0.0.1:8320/`。Windows 优先使用 Edge / Chrome。保留启动窗口，Ctrl+C 停止。

已有 Python 环境也可执行：

```powershell
python -m pip install -r requirements.txt
npm install --prefix .runtime --no-save playwright@1.62.1
python app.py
```

没有 Edge / Chrome 时先执行 `node .runtime/node_modules/playwright/cli.js install chromium` 并设置 `BROWSER_CHANNEL=chromium`。macOS / Linux 也适用该手动流程。知乎原有的可选 Python 浏览器模式另需 `pip install playwright`、`python -m playwright install chromium`。

## 使用

粘贴链接或分享文字，点击“提取文本”，支持知乎、微信公众号、YouTube watch / youtu.be / Shorts。保留原来的剪贴板一键获取、复制、文本 / Markdown / JSON 输出和知乎 Cookie 管理。

YouTube 默认匿名、简体字幕优先：可用时选择简体字幕，其次其他中文、原语言等；繁体正文会通过 OpenCC 转成简体。字幕不是语音转写，也不会凭空翻译外语。网页没有语言菜单时使用当前字幕并转换繁体汉字。TXT、SRT、复制内容使用相同转换后的正文。

YouTube 文本包含标题、原链接、作者、发布日期、已返回的点赞 / 观看 / 评论 / 频道订阅、时长或估算时间范围、语言、采集时间、频道链接、视频简介（最多 2,000 字符）和分段字幕。平台隐藏或未返回的数值不会伪造为 0，也不会额外抓取评论。字幕缓存 15 分钟，数据为采集时快照。

获取不到字幕时，同一个任务自动获取音频，由主服务上传到 `http://192.168.31.197:5840` 的转写队列。页面等待转写完成后展示文本，可一键复制或下载 TXT / Markdown / JSON。优先读取转写服务的 `article`，为空时依次使用 `raw`、`captions`；没有文本时明确报错。原有字幕可用时直接展示，不调用转写服务。

进度条显示当前阶段：提取字幕或音频、转接音频、上传、排队、语音识别、整理文本。上传和识别使用实际百分比，未提供百分比的阶段显示活动进度条；百分比代表当前阶段。点击“取消任务”会终止后续转接，并向已创建的远端转写任务提交取消请求；如果正在等待上传响应，拿到任务编号后立即取消。“清空”在任务运行时先取消，停止后清空。取消失败会显示错误，不会报告为已取消。

运行时显示已用时、音频时长、已识别时长及实时处理倍速（服务提供时）。结果区显示本次任务总用时，以及转写服务提供的处理总用时、语音识别、音频预处理、模型加载、文本整理耗时、音频时长、处理倍速、模型、分段数、转写字数和音频大小。本次总用时包含 YouTube 获取、转接上传、排队和转写等待；转写处理用时使用服务返回的 `total_elapsed`。未返回的数据不显示，合法的零耗时会保留；任务结束或取消后计时停止。JSON 下载同时保留总用时及转写统计。

YouTube 节点默认 `http://192.168.31.243:8765`，由 `YOUTUBE_API_URL` 覆盖。音频上传、状态查询和取消均由主服务发起，不要求浏览器访问两个内网节点。转写请求不走 YouTube 代理。Opus 音频以 `.ogg` 文件名上传以符合转写接口的格式限制。配置示例：

```powershell
$env:TRANSCRIPTION_API_URL = 'http://192.168.31.197:5840'
$env:TRANSCRIPTION_MODEL_PROFILE = 'daily' # 或 accurate
python app.py
```

后台任务由主服务进程维护，关闭网页后仍继续；主服务重启后不会恢复本地任务引用。任务等待上限为 6 小时，已结束任务的本地文本保留约 1 小时，仅创建任务的浏览器会话可访问。远端转写记录由转写服务保留，取消不会删除它。

## YouTube 专用代理

默认 **`http://192.168.31.21:8758`**。仅 YouTube 浏览器、字幕请求、公开信息与 yt-dlp 媒体请求使用此代理；知乎逻辑、原项目的 `WECHAT_PROXY` 设置各自独立。代理连通探测超时为 3 秒，本机实测页面约 5 秒提示失败，不会自动改走直连。

可在运行前覆盖：

```powershell
$env:YOUTUBE_PROXY = 'http://192.168.31.21:8758'
python app.py
```

服务应运行在能访问该内网代理的电脑或网络中。本次执行环境连接该地址超时，因此尚未完成通过此代理获取真实 YouTube 字幕或音频的验收。代理可达也不保证所有视频接口均可用；平台可能要求验证。

## 文件与接口

- `app.py`：FastAPI 主服务、原有微信逻辑、统一入口与 YouTube 任务 / 下载接口。
- `video_pipeline.py`：YouTube 音频转接、流式上传、转写队列轮询、进度和取消。
- `index.html`：单页界面，内置样式与脚本。
- `zhihu_extract.py`：保留原项目知乎提取器。
- `youtube_extract.py`：独立 YouTube 引擎、任务进程、限额、转换和压缩。
- `transcript_browser.cjs`：匿名网页字幕、语言选择与公开视频数据。
- `requirements.txt`、`start.cmd`：依赖与一键启动。

`POST /api/extract` 保持原有参数 `url`、`cookie`、`format`，新增 `max_mb`（默认 20）和 `codec`（mp3 / opus）。文章同步返回；YouTube 返回 `state=running`、`job_id`，随后查询 `GET /api/youtube/jobs/{job_id}`，读取 `stage`、`phase`、`percent`。成功返回文本 `content`、`metadata`、`downloads`，音频转写结果标记 `result_type=transcription`，不再自动下载音频。`POST /api/youtube/jobs/{job_id}/cancel` 提交取消，状态先为 `cancelling`，随后为 `cancelled` 或 `failed`。`DELETE /api/youtube/jobs/{job_id}` 清理已结束任务，对运行任务则提交取消。下载需要携带创建任务时的会话 Cookie；知乎 Cookie 不会传给 YouTube 或转写服务。

转写节点使用 `POST /api/jobs`（multipart 字段 `file`、`model_profile`）、`GET /api/jobs/{id}` 和 `POST /api/jobs/{id}/cancel`。本地 HTTP 集成测试不依赖内网节点：安装 `httpx` 后运行 `python -m unittest discover -s tests -v`。

## 限额与验证

音频输出 1–50 MiB，默认 20 MiB；单声道 24 kHz，MP3 最低 32 kbps，Opus 最低 20 kbps。过小的上限会提示调整，避免截断或把语音压到无法辨识的目标码率。来源最多 160 MiB、最长 2 小时，同时最多 2 个任务，临时总预算 512 MiB，任务超时 8 分钟。字幕 SRT 的网页时间范围可能按下一段开始时间估算。

本次通过 17 项集成检查，涵盖简体优先、繁简转换、完整文本与元数据、文章接口兼容、会话隔离、来源域名、大小校验、字幕失败后真实本地音频下载和 FFmpeg 压缩。浏览器使用本地样本验证一键复制、手机布局与自动音频下载；首次 `start.cmd` 自动安装依赖并成功启动也已实测。真实代理连接结果单独记录为不可达，不把样本结果当作 YouTube 实网成功。

运行时生成 `.venv`、`.runtime`、`instance`，不在交付 ZIP 中。默认仅监听本机。环境变量还支持 `PORT`、`HOST`、`OPEN_BROWSER`、`YOUTUBE_DATA_ROOT`、`PLAYWRIGHT_MODULE_PATH`、`BROWSER_CHANNEL` 和原项目的知乎 / 微信设置。

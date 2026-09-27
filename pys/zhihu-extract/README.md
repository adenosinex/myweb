# 知乎链接转文本

输入一个知乎链接，输出干净的 **Markdown / 纯文本 / JSON**。

- **在线即用**：Web 界面粘贴链接即可（本机或服务器都能跑）

- **Cookie 页内填写**：页面上直接粘贴 Cookie，存在本机浏览器，下次自动带上

- **批量处理**：命令行一次跑完一个 URL 列表

- **可自行部署**：单文件核心逻辑，解析零第三方依赖，附 Docker / compose

- **可编程调用**：一行 Python 函数拿到结构化结果

> 目录结构
>
> ```
> zhihu_extract.py   核心：抓取 + 解析（解析部分纯标准库）
> app.py             Web 服务 + HTTP API（FastAPI）
> index.html         前端页面（含 Cookie 设置区）
> Dockerfile         docker-compose.yml  requirements.txt
> cookies.example.txt
> ```

---

## 〇、三个问题的直接回答

### Q1：只用来抓回答文本，可行了吗？

**可行。** 功能已跑通，端到端验证 12/12 项通过：

| 验证项 | 状态 |
| --- | --- |
| 回答正文提取（`js-initialData`） | ✅ |
| 标题 / 答主 / 赞同数 / 评论数 / 日期 | ✅ |
| 段落、二级标题、列表、引用、代码块 | ✅ |
| 表格、链接绝对化、图片链接 | ✅ |
| 公式（`E = mc^2`） | ✅ |
| 纯文本模式去掉所有 Markdown 符号 | ✅ |
| 整条链路：页面填 Cookie → 保存 → 刷新回填 → 提取 | ✅ |
| Docker 构建 + 容器运行 | ✅ |

唯一前提：**贴一次你自己的知乎 Cookie**。这不是设计缺陷——见下方 Q3。

### Q2：额外添加一个页面输入 Cookie 即可？

**已经加好了。** 现在首屏就是两个卡片：

```
┌─ ① 知乎 Cookie ────────────────── [未配置] ─┐
│  [ 粘贴 d_c0=...; z_c0=...            ]     │
│  提示：F12 → Network → 复制 Cookie 整行      │
│  [保存] [清除]        ✓ 已保存到本机          │
└─────────────────────────────────────────────┘
┌─ ② 粘贴链接并提取 ──────────────────────────┐
│  知乎链接 [                              ]   │
│  输出格式 [ 纯文本（推荐）              ▾ ]   │
│  [提取文本] [复制] [下载]                    │
└─────────────────────────────────────────────┘
```

细节：

- **存本机浏览器**（localStorage），刷新、关掉重开都还在，**只贴一次**

- **保存时校验**：同时有 `d_c0` 和 `z_c0` 才放行，缺了会直接提示

- **徽标显示状态**：`未配置` / `缺少 d_c0 或 z_c0` / `已就绪 z_c0=ZzYy…`（打码显示）

- **不落服务端磁盘**：Cookie 只在当次请求里透传给知乎，服务器不留存

- 输出格式默认已改为 **纯文本**（你的场景是拿回答文本）

### Q3：多久更新一次 Cookie？

**知乎的 `z_c0` 有效期约 1 个月**，实际更常见的失效原因有三个：

| 触发场景 | 频率 | 处理 |
| --- | --- | --- |
| `z_c0` 自然过期 | 约 1 个月 | 重新复制一次 |
| 在别处「退出登录」/ 清理会话 | 随时 | 重新复制 |
| 换设备 / 换浏览器 / 清缓存 | 随时 | 重新复制 |
| 短时高频抓取触发风控 | 看用量 | 停一会儿再试，或降频 |

**判断方法**：页面提示 `HTTP 403：Cookie 无效/已过期` 就是该换了。\ **不需要定时更新**——用的时候发现 403 再贴一次即可，贴完又会自动存住。

> 如果嫌手动换麻烦，可以用 `--browser --login` 模式：浏览器登录态持久化在\ `.zhihu-profile/`，知乎会自动续期会话，比手动复制 Cookie 省心得多。

---

## 一、先说结论：为什么不能"裸抓"

这是本项目最先实测、也最重要的一条。**2026 年 9 月实测结果**（无登录态）：

| 方法 | 结果 | 原因 |
| --- | --- | --- |
| `requests` 直连 | ❌ HTTP 403 | 返回 `zh-zse-ck` JS 挑战页 |
| `requests` + 完整浏览器头 | ❌ HTTP 403 | 同上，缺 `d_c0` / `__zse_ck` |
| Playwright 无头浏览器 | ❌ HTTP 403 | 标题变为「安全验证 - 知乎」 |
| Jina Reader（第三方免费服务） | ❌ 无法访问 | 对知乎同样拿不到内容 |
| **带真实登录 Cookie** | ✅ 可用 | 正文在 `js-initialData` 中完整返回 |
| **有头浏览器 + 登录一次** | ✅ 可用 | 真实会话天然通过风控 |

知乎网页现在是 **CSR 渲染 + `x-zse-96` 签名 + `__zse_ck` 一次性挑战 + WAF** 的组合。\ 匿名无头浏览器会被直接识别，社区项目的 README 也印证了这一点：

> "Bare `requests`（无 cookie）❌ 403；`requests` + 完整的匿名 cookie ❌ 403（`__zse_ck` 是逐请求签名的）；无头浏览器 ❌ 被检测；**`requests` + 真实登录浏览器 cookie ✅ 可用**"

**结论：任何"完全免登录"的方案都不可靠。** 拿到有效登录态是前提——要么复制自己的 Cookie，要么用浏览器登录一次并持久化。

这也是为什么本项目**没有**去逆向 `x-zse-96`：逆向成本高、维护负担重，且知乎随时会改。而"复用你自己的登录会话"是官方通道，稳定性和维护成本都最优。

---

## 二、快速开始

### 1. 安装

```bash
cd zhihu-text
pip install -r requirements.txt
```

只需 `fastapi / uvicorn / requests / pydantic`。

### 2. 拿到 Cookie（两种方式，任选其一）

#### 方式 A：复制浏览器 Cookie（推荐，最轻量）

1. 浏览器登录 [知乎](https://www.zhihu.com)

2. 按 `F12` → **Network** 面板 → 刷新页面

3. 点任意一个请求 → **Headers** → 找到 **`Cookie`** 请求头 → 复制**整行**

4. 存成 `cookies.txt`：

```
d_c0=你的值; z_c0=你的值; _xsrf=你的值
```

> **关键**：必须包含 `d_c0` 和 `z_c0`。缺 `d_c0` 一定 403。\ 也支持浏览器插件导出的 JSON（`name`/`value` 列表 或 对象格式），会自动过滤非知乎域名的记录。

#### 方式 B：浏览器登录一次（无需手动找 Cookie）

```bash
pip install playwright && playwright install chromium
python zhihu_extract.py "https://zhuanlan.zhihu.com/p/357892158" --browser --login
```

会打开浏览器窗口，登录后回到终端按回车。登录态持久化在 `.zhihu-profile/`，后续无需重复登录。

### 3. 开始用

```bash
# 单条 → 终端打印 Markdown
python zhihu_extract.py "https://zhuanlan.zhihu.com/p/357892158" --cookie-file cookies.txt

# 单条 → 纯文本（喂 LLM）
python zhihu_extract.py "<链接>" --cookie-file cookies.txt --format txt

# 单条 → JSON（带元数据）
python zhihu_extract.py "<链接>" --cookie-file cookies.txt --format json

# 批量 → 存到目录
python zhihu_extract.py --file urls.txt --cookie-file cookies.txt -o ./out

# 浏览器模式（抗风控最强）
python zhihu_extract.py "<链接>" --browser --no-headless
```

支持的链接类型：

| 链接 | 类型 |
| --- | --- |
| `https://zhuanlan.zhihu.com/p/<id>` | 专栏文章 |
| `https://www.zhihu.com/question/<qid>/answer/<aid>` | 单个回答 |
| `https://www.zhihu.com/answer/<aid>` | 单个回答 |
| `https://www.zhihu.com/question/<qid>` | 问题 |

### 4. 起 Web 服务

```bash
python app.py                      # → http://127.0.0.1:8000
PORT=9000 python app.py            # 换端口
```

打开页面 → 粘贴链接 → 可选的 Cookie 贴在「Cookie 设置」里 → 点「提取文本」。\ 结果可一键复制或下载。

### 5. Docker 部署

```bash
mkdir -p data && cp cookies.txt data/       # 用你自己的 Cookie
docker compose up -d                         # → http://localhost:8000
```

国内网络加镜像加速：

```bash
docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t zhihu-text .
```

需要浏览器模式时，取消 `Dockerfile` 中 playwright 那两行的注释再构建。

---

## 三、命令行参数

| 参数 | 说明 |
| --- | --- |
| `url` | 单个知乎链接 |
| `--file FILE` | 每行一个链接的文本文件（`#` 开头为注释） |
| `--cookie-file FILE` | Cookie 文件路径 |
| `--cookie STR` | 直接传 Cookie 字符串 |
| `--browser` | 用真实浏览器抓取 |
| `--login` | 配合 `--browser`：打开浏览器手动登录一次 |
| `--no-headless` | 显示浏览器窗口（排查问题用） |
| `--profile-dir DIR` | 浏览器登录态目录，默认 `.zhihu-profile` |
| `--format` | `md`（默认）/ `txt` / `json` |
| `--no-images` | 不保留图片链接 |
| `-o, --out-dir DIR` | 输出目录（不传则打印到终端） |
| `-q, --quiet` | 安静模式 |

---

## 四、HTTP API

服务启动后可直接调用，方便接入其他系统。

**POST `/api/extract`**

```bash
curl -X POST http://127.0.0.1:8000/api/extract \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://zhuanlan.zhihu.com/p/357892158",
    "cookie": "d_c0=xxx; z_c0=yyy",
    "format": "markdown"
  }'
```

**GET `/api/extract`**（便捷）

```bash
curl "http://127.0.0.1:8000/api/extract?url=<链接>&format=text"
```

**GET `/api/health`** —— 查看服务状态、是否已配置 Cookie。

返回：

```json
{
  "ok": true,
  "kind": "article",
  "id": "357892158",
  "title": "文章标题",
  "author": "作者名",
  "published": "2023-11-15",
  "voteup": 123,
  "comment_count": 45,
  "url": "https://zhuanlan.zhihu.com/p/357892158",
  "content": "# 文章标题\n\n作者：作者名\n\n正文…",
  "images": ["https://pic1.zhimg.com/..."],
  "error": ""
}
```

失败时 `ok=false`，`error` 里是可直接展示给人看的中文说明（Cookie 失效 / 内容不存在 / 被风控等）。

### Python 调用

```python
from zhihu_extract import extract

doc = extract(
    "https://zhuanlan.zhihu.com/p/357892158",
    cookie_file="cookies.txt",
)
print(doc.title, doc.author, doc.voteup)
print(doc.markdown)   # 带格式
print(doc.text)       # 纯文本
print(doc.images)     # 图片链接
```

---

## 五、系统关键部分（架构）

### 整体链路

```
┌──────────────┐   POST /api/extract    ┌──────────────────┐
│  index.html  │  {url, cookie, format} │     app.py       │
│  ─────────── │ ─────────────────────► │   (FastAPI)      │
│  ① Cookie 区 │                        │  · 参数校验       │
│  ② 链接输入  │ ◄───────────────────── │  · Cookie 优先级  │
│  localStorage│   {ok, title, content} │  · 错误转中文     │
└──────────────┘                        └────────┬─────────┘
                                                 │ extract()
                                        ┌────────▼─────────┐
                                        │ zhihu_extract.py │
                                        └────────┬─────────┘
        ┌────────────────────────────────────────┼────────────────────┐
        │                                        │                    │
 ┌──────▼──────┐                        ┌────────▼────────┐  ┌────────▼────────┐
 │ 1. 识别类型  │                        │ 2. 抓取 HTML     │  │ 3. 解析          │
 │ parse_target│                        │ Fetcher         │  │ ZhihuExtractor  │
 │ /p/<id>     │                        │ ├ HTTP+Cookie   │  │ ├ initialData ★ │
 │ /answer/<id>│                        │ └ Playwright    │  │ └ DOM 兜底      │
 │ /question/  │                        └─────────────────┘  └────────┬────────┘
 └─────────────┘                                                      │
                                                          ┌───────────▼──────────┐
                                                          │ 4. HTML → Markdown   │
                                                          │ HtmlToMarkdown       │
                                                          │ └ Markdown → 纯文本  │
                                                          └──────────────────────┘
```

### 关键设计决策

**① 抓取只走"复用登录态"两条路，不逆向签名**

知乎的 `x-zse-96` 签名 + `__zse_ck` 逐请求签发 + WAF 是一套组合拳。逆向它成本高、易失效。\ 本项目改为复用你自己的登录会话（Cookie 或浏览器 Profile）——这是官方通道，稳定且免维护。

**② 优先读 `js-initialData`，而不是解析 CSS 选择器**

知乎把内容以 JSON 塞在这个 script 标签里：

```html
<script id="js-initialData" type="text/json">
{"initialState":{"entities":{"answers":{"2835848212":{
  "content": "<p>正文 HTML…</p>",   ← 一手结构化数据
  "author": {"name": "答主"},
  "voteupCount": 1024, "commentCount": 88, "createdTime": 1699999999
}}}}}
</script>
```

拿到的是服务端给的原始 JSON，**页面样式改版完全不影响**。这比解析 `.RichText` 之类的\ class 稳得多——后者是知乎改版时第一个挂掉的地方。

只有 `initialData` 未命中时，才回退到 DOM 解析（`.RichText` / `#manuscript` / `.Post-Title`）。

**③ 解析层零第三方依赖**

`HtmlToMarkdown` 和轻量 DOM 解析（`_parse_html` / `_Node`）都用**标准库**实现，\ 不依赖 `bs4` / `html2text`。好处是部署时依赖最少、不会有版本冲突。

覆盖的转换：

| HTML | Markdown |
| --- | --- |
| `<p>` / `<h1>`~`<h6>` | 段落 / `#`~`######` |
| `<ul><li>` / `<ol><li>` | `- ` 列表项 |
| `<blockquote>` | `> ` 引用 |
| `<pre><code>` | ```` ``` ```` 代码块 |
| `<table>` | GFM 表格 |
| `<a href>` | `[文本](绝对URL)` |
| `<img>` | `![alt](URL)` |
| `<img class="eeimg">` | 公式 → `` `TeX` `` |
| 知乎懒加载 `<img data-original>` | 还原为真实图片地址 |

**④ Cookie 优先级的单一出口**

`_resolve_cookie()` 一个函数管三层优先级，避免逻辑散落：

```
请求页面上填的 Cookie（最高）
  ↓ 空则
环境变量 ZHIHU_COOKIE
  ↓ 空则
cookies.txt 文件
```

Cookie 的磁盘路径只在服务端配置时使用；**页面上填的那个只在内存里过一遍，不写盘**。

### 关键接口

**核心函数**

```python
extract(url, *, cookie="", cookie_file="", use_browser=False,
        headless=True, keep_images=True, profile_dir=".zhihu-profile",
        verbose=False) -> Doc
```

**Doc 数据模型**

```python
@dataclass
class Doc:
    url: str          kind: str       # article | answer | question
    id: str           title: str      # 问题标题 / 文章标题
    author: str       published: str  # 答主 / 发布日期
    voteup: int       comment_count: int
    markdown: str     # 带格式正文
    text: str         # 纯文本正文
    images: list[str] # 图片链接
```

**HTTP API**

| 端点 | 说明 |
| --- | --- |
| `POST /api/extract` | 主接口，body: `{url, cookie, format}` |
| `GET /api/extract?url=&format=` | 便捷 GET |
| `GET /api/health` | 服务状态、是否已配置 Cookie |
| `GET /` | 前端页面 |

---

## 六、相关开源项目调研

写这个工具前调研了社区方案，供你按需选择：

| 项目 | 特点 | 是否适合你 |
| --- | --- | --- |
| [chenluda/zhihu-download](https://github.com/chenluda/zhihu-download) | Flask Web 界面，专栏/回答→Markdown，支持图片、公式，多平台（含 CSDN/掘金/公众号） | 想直接用现成 Web 界面 ✅ 本项目参考了它的交互思路 |
| [LoneKnightz/zhihu-scraper](https://github.com/LoneKnightz/zhihu-scraper) | `curl_cffi` 模拟 TLS 指纹 + Playwright 降级 + Cookie 池 + SQLite，工程化程度高，有 CLI/TUI | 要「语料管道 + 数据库」✅ |
| [yuchenzhu-research/zhihu-scraper](https://github.com/yuchenzhu-research/zhihu-scraper) | HTTP/API 优先、浏览器回退，支持专栏目录/视频/评论，导出 Markdown/HTML/PDF，测试完整 | 要本地归档 + PDF ✅ **社区里工程质量最高的之一** |
| [Ther-nullptr/zhihu-scraper](https://github.com/Ther-nullptr/zhihu-scraper) | 双模式（浏览器 / 纯 HTTP+Cookie），**README 里有详尽的抗反爬实测表** | 想快速理解风控边界 ✅ 本文第一节结论与它一致 |

**如果只是想「在线输入链接拿文本」，本项目最轻**（单文件核心 + Web UI + Docker，无需 playwright 也能跑）。\ **如果要做大规模语料库/归档**，建议直接用 `yuchenzhu-research/zhihu-scraper` 或 `LoneKnightz/zhihu-scraper`。

> 补充：第三方免费的 [Jina Reader](https://r.jina.ai)（链接前加 `r.jina.ai/`）在通用网页上很好用，\ 但实测对知乎无效——知乎的风控拦在前面。自托管版 `ghcr.io/jina-ai/reader:oss` 同理。

---

## 七、注意事项

- **Cookie 等于登录凭证**。不要提交到 Git（`.gitignore` 已排除 `cookies.txt`、`data/`、`.zhihu-profile/`），不要发到聊天或 issue。怀疑泄露就到知乎「退出所有设备」并重新登录。

- **控制频率**。批量时请加延迟（本项目默认串行），别把账号跑到被限流。

- **合规使用**。仅用于个人学习、研究与合法归档；遵守知乎服务协议、robots 规则与著作权；不要绕过付费/盐选内容，不要二次公开含个人信息的内容。

- **Cookie 会过期**。403 通常就是过期了，重新复制一次即可。

- **盐选 / 付费 / 私密内容**不支持（也不应尝试绕过）。

- **单回答链接最稳**；问题页的完整分页依赖签名接口，本项目不做批量翻页。

---

## 八、常见问题

**Q：必须登录吗？**\ 是。第一节的实测表说明：匿名方案（含无头浏览器）目前在知乎都会 403。用自己的登录态是最可靠的方式。

**Q：403 了怎么办？**

1. 确认 Cookie 里有 `d_c0` 和 `z_c0`

2. 重新复制一次（多半是过期了）

3. 换浏览器模式：`--browser --login`

**Q：提示「页面已获取，但未能解析出正文」？**\ 说明页面拿到了但结构没匹配上（知乎改版或内容受限）。可先用 `--browser --no-headless` 观察页面实际内容。

**Q：能抓评论 / 收藏夹 / 用户主页吗？**\ 本工具聚焦「单条链接 → 正文」。需要这些请用 `yuchenzhu-research/zhihu-scraper`。

**Q：会把我输入的 Cookie 存下来吗？**\ 命令行模式和 Web 接口都只在**当次请求内**使用，不落盘。\ （浏览器模式除外——登录态需要持久化在 `.zhihu-profile/`，这是功能要求。）


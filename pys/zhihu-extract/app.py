#!/usr/bin/env python3
"""
app.py — 知乎 / 微信公众号文章链接转文本 Web 服务

启动：
    python app.py                 # 默认 http://127.0.0.1:8320

环境变量：
    ZHIHU_COOKIE      直接提供 Cookie 字符串（可选）
    ZHIHU_COOKIE_FILE Cookie 文件路径（可选，默认 cookies.txt）
    ZHIHU_BROWSER=1   知乎改用浏览器模式（抗风控，较慢）
    ZHIHU_HEADLESS=0  浏览器模式下显示窗口

接口：
    GET    /api/health    服务与 Cookie 状态
    GET    /api/cookie    查看服务器本地 Cookie 状态
    POST   /api/cookie    把 Cookie 写入服务器本地文件
    DELETE /api/cookie    删除服务器本地 Cookie 文件
    POST   /api/extract   提取正文（自动识别知乎 / 微信）
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import time
import traceback
from html.parser import HTMLParser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import zhihu_extract as ze

APP_DIR = Path(__file__).parent.resolve()
DEFAULT_COOKIE_FILE = APP_DIR / "cookies.txt"

app = FastAPI(title="知乎 / 微信文章转文本", version="1.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"],
)

# 微信请求代理（可通过环境变量 WECHAT_PROXY 覆盖；设为空字符串则不走代理）
WECHAT_PROXY = os.environ.get("WECHAT_PROXY", "http://192.168.31.204:8758").strip()
# =====================================================================
#  微信文章提取（UA 伪装方案，纯 requests，无浏览器依赖）
# =====================================================================

WECHAT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148 MicroMessenger/8.0.34(0x16082222) "
    "NetType/WIFI Language/zh_CN"
)


class WeChatError(Exception):
    """微信文章提取失败。"""


def is_wechat_url(url: str) -> bool:
    return "mp.weixin.qq.com" in (url or "").lower()


def _grab(text: str, pattern: str, group: int = 1) -> str:
    m = re.search(pattern, text)
    return m.group(group) if m else ""


class _WxArticleParser(HTMLParser):
    """扫描微信文章 HTML，抽取标题 / 作者 / 时间 / 正文原始 HTML。

    用 convert_charrefs=False 保留原始实体，避免二次解码错乱。
    """

    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.title = ""
        self.author = ""
        self.published = ""
        self.content_html = ""

        self._capture = ""
        self._buf: list[str] = []
        self._cap_depth = 0

        self._in_content = False
        self._depth = 0
        self._content_buf: list[str] = []

    # ------------------------------------------------------------- 捕获
    def _start(self, name: str) -> None:
        self._capture = name
        self._buf = []
        self._cap_depth = 1

    def _finish(self) -> None:
        if not self._capture:
            return
        raw = "".join(self._buf)
        text = html_lib.unescape(raw)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            setattr(self, self._capture, text)
        self._capture = ""
        self._buf = []
        self._cap_depth = 0

    # ------------------------------------------------------------- 钩子
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if self._capture:
            if tag not in self._VOID:
                self._cap_depth += 1
            return

        if self._in_content:
            if tag not in self._VOID:
                self._depth += 1
            self._content_buf.append(self.get_starttag_text() or f"<{tag}>")
            return

        elem_id = attrs.get("id", "")

        if tag == "div" and elem_id == "js_content":
            self._in_content = True
            self._depth = 0
            self._content_buf = []
            return
        if tag == "h1" and elem_id == "activity-name":
            self._start("title")
            return
        if elem_id == "js_name":
            self._start("author")
            return
        if elem_id == "publish_time":
            self._start("published")
            return

    def handle_endtag(self, tag):
        if self._capture:
            if tag not in self._VOID:
                self._cap_depth -= 1
                if self._cap_depth <= 0:
                    self._finish()
            return

        if self._in_content:
            if tag == "div" and self._depth == 0:
                self._in_content = False
                self.content_html = "".join(self._content_buf)
                self._content_buf = []
                return
            if tag not in self._VOID:
                self._depth = max(0, self._depth - 1)
            self._content_buf.append(f"</{tag}>")

    def handle_data(self, data):
        if self._in_content:
            self._content_buf.append(data)
        elif self._capture:
            self._buf.append(data)

    def handle_entityref(self, name):
        if self._in_content:
            self._content_buf.append(f"&{name};")
        elif self._capture:
            self._buf.append(f"&{name};")

    def handle_charref(self, name):
        if self._in_content:
            self._content_buf.append(f"&#{name};")
        elif self._capture:
            self._buf.append(f"&#{name};")

    def close(self):
        super().close()
        if self._in_content and not self.content_html:
            self.content_html = "".join(self._content_buf)
        if self._capture:
            self._finish()


class _WxHtmlToMarkdown(HTMLParser):
    """把文章正文 HTML 转成轻量 Markdown。"""

    _BLOCK = {"p", "div", "section", "article", "header", "footer", "main",
              "aside", "figure", "figcaption", "table", "thead", "tbody",
              "tr", "td", "th", "ul", "ol", "dl", "dt", "dd", "pre",
              "blockquote", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[str] = []
        self._skip = 0
        self._href: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript", "iframe", "svg"):
            self._skip += 1
            return
        if self._skip:
            return

        if tag == "br":
            self.parts.append("\n")
        elif tag == "hr":
            self.parts.append("\n---\n")
        elif tag == "img":
            src = (attrs.get("data-src") or attrs.get("src") or "").strip()
            if src:
                if src.startswith("//"):
                    src = "https:" + src
                self.images.append(src)
                self.parts.append(f"\n![图片]({src})\n")
        elif tag == "a":
            self._href.append(attrs.get("href", ""))
            self.parts.append("[")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "blockquote":
            self.parts.append("\n> ")
        elif tag in ("strong", "b"):
            self.parts.append("**")
        elif tag in ("em", "i"):
            self.parts.append("*")
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "iframe", "svg"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "a":
            href = self._href.pop() if self._href else ""
            self.parts.append(f"]({href})" if href else "]")
        elif tag in ("strong", "b"):
            self.parts.append("**")
        elif tag in ("em", "i"):
            self.parts.append("*")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section",
                     "li", "blockquote", "tr", "pre", "ul", "ol", "figure"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)


def _flatten(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _to_plain(md: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"[*`>]+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class WeChatDoc:
    """与 zhihu_extract 返回结构保持兼容。"""

    def __init__(self, **kw):
        self.url = kw.get("url", "")
        self.kind = kw.get("kind", "wechat")
        self.id = kw.get("id", "")
        self.title = kw.get("title", "")
        self.author = kw.get("author", "")
        self.published = kw.get("published", "")
        self.voteup = int(kw.get("voteup", 0) or 0)
        self.comment_count = int(kw.get("comment_count", 0) or 0)
        self.text = kw.get("text", "")
        self.markdown = kw.get("markdown", "")
        self.images = list(kw.get("images", []) or [])

    def to_dict(self) -> dict:
        return {
            "url": self.url, "kind": self.kind, "id": self.id,
            "title": self.title, "author": self.author,
            "published": self.published, "voteup": self.voteup,
            "comment_count": self.comment_count,
            "text": self.text, "markdown": self.markdown,
            "images": self.images,
        }


def extract_wechat(url: str, cookie: str = "", timeout: int = 15,
                   verbose: bool = False) -> WeChatDoc:
    """抓取微信公众号文章并转成文本。

    - 通过 WECHAT_PROXY 指定的代理发送请求（默认 192.168.31.124:8758）。
    - 代理不可用时自动回退为直连，尽量保证可用性。
    """
    try:
        import requests
    except ImportError:
        raise WeChatError("缺少 requests 库：pip install requests")

    headers = {
        "User-Agent": WECHAT_UA,
        "Referer": "https://mp.weixin.qq.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie

    proxies = None
    if WECHAT_PROXY:
        proxies = {"http": WECHAT_PROXY, "https": WECHAT_PROXY}

    resp = None
    last_err: Exception | None = None

    # 先走代理
    if proxies:
        try:
            if verbose:
                print(f"[wechat] 使用代理 {WECHAT_PROXY} 请求 {url}")
            resp = requests.get(url, headers=headers, timeout=timeout,
                                allow_redirects=True, proxies=proxies)
        except requests.RequestException as e:
            last_err = e
            if verbose:
                print(f"[wechat] 代理请求失败：{e}，尝试直连")

    # 代理未配置 / 代理失败 → 直连兜底
    if resp is None:
        try:
            if verbose:
                print(f"[wechat] 直连请求 {url}")
            resp = requests.get(url, headers=headers, timeout=timeout,
                                allow_redirects=True)
        except requests.RequestException as e:
            if last_err is not None:
                raise WeChatError(
                    f"网络请求失败：代理 {WECHAT_PROXY} 与直连均不可用"
                    f"（代理错误：{last_err}；直连错误：{e}）"
                ) from e
            raise WeChatError(f"网络请求失败：{e}") from e

    if resp.status_code != 200:
        raise WeChatError(f"微信返回 HTTP {resp.status_code}")

    page = resp.text

    # ---- 反爬 / 失效页识别
    if "环境异常" in page and "去验证" in page:
        raise WeChatError("触发微信验证（环境异常）。请稍后重试，或降低请求频率 / 更换网络。")
    if "该内容已被发布者删除" in page:
        raise WeChatError("该文章已被发布者删除。")
    if "此内容因违规无法查看" in page:
        raise WeChatError("该文章因违规无法查看。")
    if "参数错误" in page and "js_content" not in page:
        raise WeChatError("链接参数错误或已失效。")
    if "js_content" not in page:
        raise WeChatError("未找到正文内容，可能链接已失效或被拦截。")

    parser = _WxArticleParser()
    parser.feed(page)
    parser.close()

    # ---- 标题（多级兜底）
    title = parser.title
    if not title:
        title = _grab(page, r"var\s+msg_title\s*=\s*['\"]([^'\"]+)")
    if not title:
        title = _grab(page, r'<meta\s+property="og:title"\s+content="([^"]*)"')
    title = html_lib.unescape(title or "").strip()

    # ---- 作者
    author = parser.author
    if not author:
        author = _grab(page, r"var\s+nickname\s*=\s*['\"]([^'\"]+)")
    if not author:
        author = _grab(page, r'<meta\s+property="og:article:author"\s+content="([^"]*)"')
    author = html_lib.unescape(author or "").strip()

    # ---- 发布时间
    published = parser.published
    if not published:
        ct = _grab(page, r"var\s+ct\s*=\s*['\"](\d{10})")
        if ct:
            try:
                published = time.strftime("%Y-%m-%d %H:%M",
                                          time.localtime(int(ct)))
            except (ValueError, OSError):
                published = ""

    # ---- 正文
    md_parser = _WxHtmlToMarkdown()
    md_parser.feed(parser.content_html or "")
    md_parser.close()

    markdown = _flatten("".join(md_parser.parts))
    text = _to_plain(markdown)

    # ---- 文章 id
    aid = (_grab(url, r"[?&]sn=([0-9a-fA-F]+)")
           or _grab(url, r"/s/([A-Za-z0-9_\-]+)")
           or "")

    return WeChatDoc(
        url=url, kind="wechat", id=aid,
        title=title, author=author, published=published,
        text=text, markdown=markdown, images=md_parser.images,
    )

# =====================================================================
#  请求 / 响应模型
# =====================================================================


class ExtractReq(BaseModel):
    url: str = Field(..., description="知乎或微信公众号文章链接")
    cookie: str = Field("", description="可选，本次请求使用的 Cookie")
    format: str = Field("markdown", description="markdown | text | json")


class ExtractResp(BaseModel):
    ok: bool
    kind: str = ""
    id: str = ""
    title: str = ""
    author: str = ""
    published: str = ""
    voteup: int = 0
    comment_count: int = 0
    url: str = ""
    content: str = ""
    images: list[str] = []
    error: str = ""


class CookieReq(BaseModel):
    cookie: str = Field("", description="完整 Cookie 请求头字符串")


# =====================================================================
#  Cookie 本地存取
# =====================================================================


def _cookie_file() -> Path:
    return Path(os.environ.get("ZHIHU_COOKIE_FILE", str(DEFAULT_COOKIE_FILE)))


def normalize_cookie(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    s = re.sub(r"(?im)^\s*cookie\s*:\s*", "", s)
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s*;\s*", "; ", s)
    return s.strip().strip(";").strip()


def missing_fields(cookie: str) -> list[str]:
    return [k for k in ("d_c0", "z_c0")
            if not re.search(r"(?:^|;\s*)" + k + r"=", cookie)]


def mask_cookie(cookie: str) -> str:
    if not cookie.strip():
        return ""
    parts = []
    for key in ("d_c0", "z_c0"):
        m = re.search(r"(?:^|;\s*)" + key + r"=([^;]*)", cookie)
        val = m.group(1).strip() if m else ""
        parts.append(f"{key}=—" if not val else f"{key}={val[:6]}…({len(val)}字符)")
    return " · ".join(parts)


def stored_cookie() -> str:
    path = _cookie_file()
    if not path.exists():
        return ""
    try:
        return ze.load_cookie(str(path)).strip()
    except (ze.ZhihuError, OSError, ValueError):
        return ""


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _resolve_cookie(req_cookie: str) -> str:
    if req_cookie.strip():
        return req_cookie.strip()
    env_cookie = os.environ.get("ZHIHU_COOKIE", "").strip()
    if env_cookie:
        return env_cookie
    return stored_cookie()


def _use_browser() -> bool:
    return os.environ.get("ZHIHU_BROWSER", "").strip() in ("1", "true", "yes")


# =====================================================================
#  接口
# =====================================================================


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((APP_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict:
    stored = stored_cookie()
    env_set = bool(os.environ.get("ZHIHU_COOKIE", "").strip())
    cookie_ready = bool(stored or env_set)
    return {
        "ok": True,
        "browser_mode": _use_browser(),
        "cookie_configured": cookie_ready,
        "cookie_source": "env" if env_set else ("file" if stored else ""),
        "cookie_file": str(_cookie_file()),
        "cookie_masked": mask_cookie(stored),
         "wechat_proxy": WECHAT_PROXY or "(直连)",   # ← 新增
        "hint": ("已就绪" if cookie_ready or _use_browser()
                 else "尚未配置 Cookie：抓取知乎前请在页面末尾保存，"
                      "或放置 cookies.txt。微信文章无需 Cookie。"),
    }


@app.post("/api/extract", response_model=ExtractResp)
def api_extract(req: ExtractReq) -> ExtractResp:
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="请提供链接")

    wechat = is_wechat_url(url)
    zhihu = "zhihu.com" in url
    if not (wechat or zhihu):
        raise HTTPException(
            status_code=400,
            detail="仅支持 zhihu.com 与 mp.weixin.qq.com 域名链接",
        )

    cookie = _resolve_cookie(req.cookie)
    use_browser = _use_browser()

    # 微信文章不需要登录态；知乎需要
    if zhihu and not cookie and not use_browser:
        return ExtractResp(
            ok=False,
            error="服务端未配置 Cookie。请在页面末尾「知乎 Cookie」中粘贴并点「保存」，"
                  "或直接把 cookies.txt 放到项目目录，"
                  "或设置环境变量 ZHIHU_BROWSER=1 改用浏览器模式。",
        )

    try:
        if wechat:
            doc = extract_wechat(url, cookie=req.cookie.strip(), verbose=False)
        else:
            doc = ze.extract(
                url,
                cookie=cookie,
                use_browser=use_browser,
                headless=os.environ.get("ZHIHU_HEADLESS", "1") != "0",
                profile_dir=str(APP_DIR / ".zhihu-profile"),
                verbose=False,
            )
    except (ze.ZhihuError, WeChatError) as e:
        return ExtractResp(ok=False, error=str(e))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return ExtractResp(ok=False, error=f"服务器内部错误：{type(e).__name__}: {e}")

    fmt = req.format.lower()
    if fmt == "text":
        content = doc.text
    elif fmt == "json":
        content = json.dumps(doc.to_dict(), ensure_ascii=False, indent=2)
    else:
        content = doc.markdown

    return ExtractResp(
        ok=True, kind=doc.kind, id=doc.id, title=doc.title, author=doc.author,
        published=doc.published, voteup=doc.voteup,
        comment_count=doc.comment_count, url=doc.url,
        content=content, images=doc.images,
    )


@app.get("/api/extract")
def api_extract_get(url: str, format: str = "markdown",
                    cookie: str = "") -> JSONResponse:
    resp = api_extract(ExtractReq(url=url, format=format, cookie=cookie))
    return JSONResponse(content=resp.model_dump())


# =====================================================================
#  Cookie 接口
# =====================================================================


@app.get("/api/cookie")
def api_get_cookie() -> dict:
    path = _cookie_file()
    stored = stored_cookie()
    env_cookie = os.environ.get("ZHIHU_COOKIE", "").strip()
    effective = stored or env_cookie
    return {
        "ok": True,
        "configured": bool(effective),
        "source": "env" if env_cookie else ("file" if stored else ""),
        "file": str(path),
        "file_exists": path.exists(),
        "masked": mask_cookie(stored),
        "missing": missing_fields(effective) if effective else ["d_c0", "z_c0"],
        "env_override": bool(env_cookie),
    }


@app.post("/api/cookie")
def api_set_cookie(req: CookieReq) -> dict:
    cookie = normalize_cookie(req.cookie)
    if not cookie:
        raise HTTPException(status_code=400, detail="Cookie 不能为空")

    miss = missing_fields(cookie)
    if miss:
        raise HTTPException(
            status_code=400,
            detail="Cookie 缺少必要字段：" + "、".join(miss)
                   + "（请从已登录的浏览器完整复制整行 Cookie）",
        )

    path = _cookie_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(cookie + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        _restrict(path)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写入失败：{e}") from e

    return {
        "ok": True,
        "file": str(path),
        "source": "file",
        "masked": mask_cookie(cookie),
        "hint": "已保存到服务器本地，后续提取会自动使用",
    }


@app.delete("/api/cookie")
def api_delete_cookie() -> dict:
    path = _cookie_file()
    if not path.exists():
        return {"ok": True, "removed": False, "file": str(path),
                "hint": "服务器本地没有 Cookie 文件"}
    try:
        path.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"删除失败：{e}") from e
    return {"ok": True, "removed": True, "file": str(path),
            "hint": "已删除服务器本地 Cookie 文件"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8320"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"\n  知乎 / 微信文章转文本服务已启动 →  http://127.0.0.1:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
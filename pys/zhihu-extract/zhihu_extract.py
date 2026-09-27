#!/usr/bin/env python3
"""
zhihu_extract.py — 知乎链接 → 纯文本/Markdown

两种模式
  cookie 模式 : 提供登录后的 Cookie，纯 HTTP 请求，快、省资源（推荐批量/服务器部署）
  browser 模式: 用 Playwright 驱动真实浏览器，首次手动登录一次并持久化，稳定抗风控

用法
  python zhihu_extract.py "https://zhuanlan.zhihu.com/p/123456" --cookie-file cookies.txt
  python zhihu_extract.py "https://zhuanlan.zhihu.com/p/123456" --browser --login
  python zhihu_extract.py --file urls.txt -o ./out-json
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------- 异常


class ZhihuError(RuntimeError):
    """抓取过程中的可预期错误（Cookie 失效、被风控、内容不存在等）。"""


# ---------------------------------------------------------------- 数据模型


@dataclass
class Doc:
    """统一的内容模型。"""

    url: str
    kind: str = "unknown"  # article | answer | question
    id: str = ""
    title: str = ""
    author: str = ""
    published: str = ""
    voteup: int = 0
    comment_count: int = 0
    markdown: str = ""
    text: str = ""
    images: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- URL 识别

# 支持：/p/<id>、/answer/<id>、/question/<id>/answer/<id>、/question/<id>、/zvideo/<id>
PATTERNS = [
    ("article", re.compile(r"zhuanlan\.zhihu\.com/p/(\d+)")),
    ("answer", re.compile(r"zhihu\.com/question/(\d+)/answer/(\d+)")),
    ("answer", re.compile(r"zhihu\.com/answer/(\d+)")),
    ("question", re.compile(r"zhihu\.com/question/(\d+)(?:/)?(?:\?|$)")),
]


def parse_target(url: str) -> tuple[str, tuple[str, ...]]:
    """识别 URL 类型，返回 (kind, 捕获组)。"""
    url = url.strip()
    for kind, pat in PATTERNS:
        m = pat.search(url)
        if m:
            return kind, m.groups()
    raise ZhihuError(
        f"无法识别的知乎链接：{url}\n"
        "支持：专栏文章 /p/<id>、回答 /question/<qid>/answer/<aid>、"
        "问题 /question/<qid>"
    )


def normalize_url(url: str) -> str:
    """去掉查询串与尾斜杠，得到规范化 URL。"""
    p = urlparse(url.strip())
    path = p.path.rstrip("/")
    return f"{p.scheme}://{p.netloc}{path}"


# ---------------------------------------------------------------- HTML → Markdown

_BLOCK_TAGS = {
    "p", "div", "section", "article", "blockquote", "pre", "figure",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "tr",
}
_HEADING_RE = re.compile(r"^h([1-6])$")


class HtmlToMarkdown:
    """轻量 HTML → Markdown 转换器，针对知乎正文结构优化。

    只依赖标准库，避免额外依赖；覆盖段落/标题/列表/引用/代码/
    表格/链接/图片/公式等知乎正文常见结构。
    """

    def __init__(self, keep_images: bool = True) -> None:
        self.keep_images = keep_images
        self.images: list[str] = []

    # -- 公开入口
    def convert(self, html: str) -> str:
        self.images = []
        html = self._preprocess(html)
        root = _parse_html(html)
        if root is None:
            return ""
        md = self._walk(root, depth=0)
        md = self._tidy(md)
        return md

    # -- 预处理：把知乎特有的结构规整成标准标签
    def _preprocess(self, html: str) -> str:
        # 知乎图片懒加载：data-original / data-actualsrc 才是真图
        html = re.sub(
            r'<img([^>]*?)data-original="([^"]+)"([^>]*?)>',
            lambda m: f'<img{m.group(1)} src="{m.group(2)}"{m.group(3)}>', html)
        html = re.sub(
            r'<img([^>]*?)data-actualsrc="([^"]+)"([^>]*?)>',
            lambda m: f'<img{m.group(1)} src="{m.group(2)}"{m.group(3)}>', html)
        # 公式：知乎用 img 承载 TeX，优先取 alt / eeimg
        html = re.sub(r'<img[^>]*class="[^"]*eeimg[^"]*"[^>]*alt="([^"]*)"[^>]*/?>',
                      lambda m: f"<code>{html_mod.escape(m.group(1))}</code>", html)
        # <br> → 换行
        html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
        # 去掉脚本与样式
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html,
                      flags=re.I | re.S)
        return html

    # -- 递归遍历
    def _walk(self, node, depth: int) -> str:
        from html.parser import HTMLParser  # noqa: F401  (类型提示占位)

        if isinstance(node, str):
            return _unescape(node)

        tag = node.tag
        children = node.children

        if tag in ("script", "style"):
            return ""

        if tag == "img":
            src = node.attrs.get("src", "")
            alt = node.attrs.get("alt", "")
            if src and not src.startswith("data:"):
                src = _absolutize(src)
                self.images.append(src)
                return f"![{alt}]({src})" if self.keep_images else ""
            return ""

        if tag == "a":
            text = "".join(self._walk(c, depth) for c in children).strip()
            href = node.attrs.get("href", "")
            if not text:
                return ""
            if href and not href.startswith("javascript"):
                return f"[{text}]({_absolutize(href)})"
            return text

        if tag == "code" or tag == "tt":
            inner = "".join(self._walk(c, depth) for c in children)
            return f"`{inner.strip()}`" if inner.strip() else ""

        if tag == "pre":
            # 代码块内沿用 <code> 的文本，但不要带上行内反引号
            inner = self._raw_text(node).strip("\n")
            return f"\n```\n{inner}\n```\n"

        if tag == "hr":
            return "\n\n---\n\n"

        if tag == "br":
            return "\n"

        if tag == "li":
            inner = "".join(self._walk(c, depth) for c in children).strip()
            return f"- {inner}\n"

        if tag == "blockquote":
            inner = "".join(self._walk(c, depth) for c in children).strip()
            quoted = "\n".join("> " + ln for ln in inner.splitlines())
            return f"\n\n{quoted}\n\n"

        if tag == "table":
            return "\n\n" + self._table(node) + "\n\n"

        if tag and _HEADING_RE.match(tag):
            level = int(_HEADING_RE.match(tag).group(1))  # type: ignore[union-attr]
            inner = "".join(self._walk(c, depth) for c in children).strip()
            return f"\n\n{'#' * level} {inner}\n\n"

        # 默认：递归拼接，块级标签后补空行
        inner = "".join(self._walk(c, depth) for c in children)
        if tag in _BLOCK_TAGS:
            if inner.strip():
                return inner.strip() + "\n\n"
            return ""
        return inner

    # -- 取纯文本（不含任何 Markdown 标记），用于代码块等场景
    def _raw_text(self, node) -> str:
        out: list[str] = []

        def walk(n) -> None:
            if isinstance(n, str):
                out.append(_unescape(n))
                return
            if n.tag == "br":
                out.append("\n")
                return
            for c in n.children:
                walk(c)

        walk(node)
        return "".join(out)

    # -- 表格
    def _table(self, node) -> str:
        rows: list[list[str]] = []

        def collect(n) -> None:
            if isinstance(n, str):
                return
            if n.tag == "tr":
                cells = []
                for c in n.children:
                    if not isinstance(c, str) and c.tag in ("td", "th"):
                        cells.append(
                            "".join(self._walk(x, 0) for x in c.children).strip()
                            .replace("\n", " "))
                if cells:
                    rows.append(cells)
                return
            for c in n.children:
                collect(c)

        collect(node)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |",
               "| " + " | ".join(["---"] * width) + " |"]
        for r in rows[1:]:
            out.append("| " + " | ".join(r) + " |")
        return "\n".join(out)

    # -- 收尾清理
    @staticmethod
    def _tidy(md: str) -> str:
        md = re.sub(r"[ \t]+\n", "\n", md)
        md = re.sub(r"\n{3,}", "\n\n", md)
        return md.strip()


def _unescape(s: str) -> str:
    return html_mod.unescape(s)


def _absolutize(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.zhihu.com" + url
    return url


# ---------------------------------------------------------------- 极简 HTML 解析


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent=None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Any] = []
        self.parent = parent


_VOID = {"br", "img", "hr", "meta", "link", "input", "source", "col"}


def _parse_html(html: str) -> Optional[_Node]:
    """用标准库 HTMLParser 构建轻量 DOM，避免引入 bs4 依赖。"""
    from html.parser import HTMLParser

    root = _Node("#root", {})
    stack = [root]

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            node = _Node(tag, {k: (v or "") for k, v in attrs}, stack[-1])
            stack[-1].children.append(node)
            if tag not in _VOID:
                stack.append(node)

        def handle_startendtag(self, tag, attrs):
            node = _Node(tag, {k: (v or "") for k, v in attrs}, stack[-1])
            stack[-1].children.append(node)

        def handle_endtag(self, tag):
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].tag == tag:
                    del stack[i:]
                    break

        def handle_data(self, data):
            stack[-1].children.append(data)

    p = P(convert_charrefs=True)
    try:
        p.feed(html)
        p.close()
    except Exception:
        return root
    return root


def _find_by_id(root: _Node, target_id: str) -> Optional[_Node]:
    if root.attrs.get("id") == target_id:
        return root
    for c in root.children:
        if isinstance(c, str):
            continue
        found = _find_by_id(c, target_id)
        if found is not None:
            return found
    return None


def _find_all_by_class(root: _Node, cls: str) -> list[_Node]:
    out: list[_Node] = []

    def walk(n: _Node) -> None:
        for c in n.children:
            if isinstance(c, str):
                continue
            classes = (c.attrs.get("class") or "").split()
            if cls in classes:
                out.append(c)
            walk(c)

    walk(root)
    return out


# ---------------------------------------------------------------- 提取器


class ZhihuExtractor:
    """从页面 HTML 中抽取正文，不依赖任何第三方解析库。"""

    def __init__(self, keep_images: bool = True) -> None:
        self.conv = HtmlToMarkdown(keep_images=keep_images)

    # ---------- 从 js-initialData 中提取（最稳，结构化）
    def from_initial_data(self, html_text: str, kind: str,
                          ids: tuple[str, ...]) -> Optional[Doc]:
        m = re.search(
            r'<script[^>]+id="js-initialData"[^>]*>(.*?)</script>', html_text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None

        try:
            entities = data["initialState"]["entities"]
        except (KeyError, TypeError):
            return None

        if kind == "article":
            art = (entities.get("articles") or {}).get(ids[0])
            if not art:
                return None
            return self._doc_from_article(art, entities)

        if kind == "answer":
            aid = ids[1] if len(ids) > 1 else ids[0]
            ans = (entities.get("answers") or {}).get(aid)
            if not ans:
                return None
            return self._doc_from_answer(ans, entities)

        return None

    def _doc_from_article(self, art: dict, entities: dict) -> Doc:
        author = (entities.get("users") or {}).get(str(art.get("author", {}).get("id", ""))) or {}
        name = art.get("author", {}).get("name") or author.get("name", "")
        content = art.get("content", "") or art.get("excerpt", "")
        md = self.conv.convert(content)
        doc = Doc(
            url=f"https://zhuanlan.zhihu.com/p/{art.get('id', '')}",
            kind="article",
            id=str(art.get("id", "")),
            title=art.get("title", ""),
            author=name,
            published=_ts_to_str(art.get("created") or art.get("updated")),
            voteup=int(art.get("voteupCount", 0) or 0),
            comment_count=int(art.get("commentCount", 0) or 0),
            markdown=_with_title(art.get("title", ""), name, md),
            text=_md_to_text(md),
            images=list(self.conv.images),
        )
        return doc

    def _doc_from_answer(self, ans: dict, entities: dict) -> Doc:
        author = ans.get("author", {}) or {}
        question = ans.get("question", {}) or {}
        content = ans.get("content", "")
        md = self.conv.convert(content)
        title = question.get("title", "")
        name = author.get("name", "")
        doc = Doc(
            url=f"https://www.zhihu.com/question/{question.get('id', '')}"
                f"/answer/{ans.get('id', '')}",
            kind="answer",
            id=str(ans.get("id", "")),
            title=title,
            author=name,
            published=_ts_to_str(ans.get("createdTime") or ans.get("updatedTime")),
            voteup=int(ans.get("voteupCount", 0) or 0),
            comment_count=int(ans.get("commentCount", 0) or 0),
            markdown=_with_title(title, name, md),
            text=_md_to_text(md),
            images=list(self.conv.images),
        )
        doc.extra["question_id"] = str(question.get("id", ""))
        return doc

    # ---------- 从渲染后的 DOM 文本提取（兜底）
    def from_dom(self, html_text: str, kind: str, url: str) -> Optional[Doc]:
        root = _parse_html(html_text)
        if root is None:
            return None

        # 标题
        title = ""
        for tid in ("zh-question-title",):
            n = _find_by_id(root, tid)
            if n is not None:
                title = _node_text(n).strip()
                break
        if not title:
            m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S)
            if m:
                title = _unescape(m.group(1)).replace(" - 知乎", "").strip()
        if not title:
            h1 = _find_all_by_class(root, "Post-Title")
            if h1:
                title = _node_text(h1[0]).strip()

        # 正文
        body: Optional[_Node] = None
        for cid in ("zh-question-answer-wrap", "manuscript"):
            body = _find_by_id(root, cid)
            if body is not None:
                break
        if body is None:
            for cls in ("RichText", "RichContent-inner", "Post-RichText"):
                found = _find_all_by_class(root, cls)
                if found:
                    body = found[0]
                    break
        if body is None:
            return None

        md = self.conv.convert(_serialize(body))
        if len(md) < 80:
            return None

        return Doc(
            url=url, kind=kind, id=url.rstrip("/").rsplit("/", 1)[-1],
            title=title, markdown=_with_title(title, "", md),
            text=_md_to_text(md), images=list(self.conv.images),
        )


def _node_text(node: _Node) -> str:
    parts: list[str] = []

    def walk(n: _Node) -> None:
        for c in n.children:
            if isinstance(c, str):
                parts.append(c)
            else:
                walk(c)

    walk(node)
    return re.sub(r"\s+", " ", "".join(parts))


def _serialize(node: _Node) -> str:
    """把 DOM 子树还原成 HTML 字符串（供 Markdown 转换器复用）。"""
    if isinstance(node, str):  # pragma: no cover
        return node
    attrs = "".join(f' {k}="{html_mod.escape(str(v))}"'
                    for k, v in node.attrs.items())
    inner = "".join(_serialize(c) if not isinstance(c, str)
                    else html_mod.escape(c) for c in node.children)
    return f"<{node.tag}{attrs}>{inner}</{node.tag}>"


def _with_title(title: str, author: str, md: str) -> str:
    head = []
    if title:
        head.append(f"# {title}")
    if author:
        head.append(f"作者：{author}")
    if head:
        return "\n\n".join(head) + "\n\n" + md
    return md


def _md_to_text(md: str) -> str:
    """Markdown → 纯文本，方便喂给 LLM 或做全文检索。"""
    t = re.sub(r"```.*?```", "", md, flags=re.S)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    # 表格：把 | 分隔的行转成空格分隔，去掉分隔线
    t = re.sub(r"^\s*\|[\s:|-]+\|\s*$", "", t, flags=re.M)
    t = re.sub(r"^\s*\|(.*)\|\s*$", lambda m: m.group(1).replace("|", " "), t, flags=re.M)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _ts_to_str(ts: Any) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
    except (ValueError, TypeError, OSError):
        return str(ts)


# ---------------------------------------------------------------- 抓取引擎


class Fetcher:
    """统一的抓取接口，内部按模式分派。"""

    def __init__(self, cookie_header: str = "", use_browser: bool = False,
                 headless: bool = True, profile_dir: str = ".zhihu-profile",
                 verbose: bool = False) -> None:
        self.cookie_header = cookie_header
        self.use_browser = use_browser
        self.headless = headless
        self.profile_dir = profile_dir
        self.verbose = verbose

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"    · {msg}", file=sys.stderr)

    # ---- cookie 模式
    def _headers(self) -> dict[str, str]:
        h = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.zhihu.com/",
            "Upgrade-Insecure-Requests": "1",
        }
        if self.cookie_header:
            h["Cookie"] = self.cookie_header
        return h

    def fetch_http(self, url: str) -> str:
        import requests
        r = requests.get(url, headers=self._headers(), timeout=30)
        if r.status_code == 403:
            raise ZhihuError(
                "HTTP 403：Cookie 无效/已过期，或该内容需要登录。\n"
                "请重新从已登录的浏览器复制 Cookie（务必包含 d_c0 与 z_c0）。")
        if r.status_code == 404:
            raise ZhihuError(f"HTTP 404：内容不存在或已删除 —— {url}")
        if r.status_code != 200:
            raise ZhihuError(f"HTTP {r.status_code} —— {url}")
        return r.text

    # ---- browser 模式
    def fetch_browser(self, url: str, login_wait: bool = False) -> str:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                self.profile_dir,
                headless=self.headless,
                locale="zh-CN",
                viewport={"width": 1440, "height": 900},
                user_agent=UA,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            page = ctx.new_page()
            try:
                if login_wait:
                    page.goto("https://www.zhihu.com/signin",
                              wait_until="domcontentloaded", timeout=60000)
                    print("\n  请在弹出的浏览器中完成登录，"
                          "登录成功后回到终端按 Enter 继续…", file=sys.stderr)
                    input()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                status = resp.status if resp else None
                if status == 403:
                    page.wait_for_timeout(5000)
                else:
                    page.wait_for_timeout(2500)
                # 触发懒加载
                try:
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
                html = page.content()
                if "安全验证" in page.title() or status == 403:
                    raise ZhihuError(
                        "被知乎风控拦截（安全验证）。请改用 --login 先手动登录，"
                        "或确认 Cookie 模式下的登录态有效。")
                return html
            finally:
                ctx.close()

    # ---- 统一入口
    def fetch(self, url: str, login_wait: bool = False) -> str:
        if self.use_browser:
            self._log(f"浏览器模式抓取 {url}")
            return self.fetch_browser(url, login_wait=login_wait)
        self._log(f"HTTP 模式抓取 {url}")
        return self.fetch_http(url)


# ---------------------------------------------------------------- 对外 API


def extract(url: str, *, cookie: str = "", cookie_file: str = "",
            use_browser: bool = False, headless: bool = True,
            keep_images: bool = True, profile_dir: str = ".zhihu-profile",
            verbose: bool = False) -> Doc:
    """把一条知乎链接转成 Doc（含 markdown / text）。

    示例
        doc = extract("https://zhuanlan.zhihu.com/p/357892158",
                      cookie_file="cookies.txt")
        print(doc.markdown)
    """
    if not cookie and cookie_file:
        cookie = load_cookie(cookie_file)
    if not use_browser and not cookie:
        raise ZhihuError(
            "需要 Cookie 才能抓取知乎内容。\n"
            "  · 复制浏览器 Cookie 到 cookies.txt，然后传 --cookie-file cookies.txt\n"
            "  · 或者加 --browser --login 用浏览器登录一次（自动持久化）\n"
            "详见 README.md「如何获取 Cookie」。")

    kind, ids = parse_target(url)
    norm = normalize_url(url)
    fetcher = Fetcher(cookie_header=cookie, use_browser=use_browser,
                      headless=headless, profile_dir=profile_dir,
                      verbose=verbose)
    html_text = fetcher.fetch(norm)

    ex = ZhihuExtractor(keep_images=keep_images)
    doc = ex.from_initial_data(html_text, kind, ids)
    if doc is None:
        if verbose:
            print("    · initialData 未命中，回退 DOM 解析", file=sys.stderr)
        doc = ex.from_dom(html_text, kind, norm)
    if doc is None:
        raise ZhihuError(
            "页面已获取，但未能解析出正文。可能是页面结构变化，"
            "或该内容受到限制（盐选/付费/已删除）。")

    doc.url = norm or doc.url
    return doc


def load_cookie(path: str) -> str:
    """读取 Cookie 文件，兼容三种常见格式：

    1. 完整的 Cookie 请求头：`d_c0=xxx; z_c0=yyy; _xsrf=zzz`
    2. JSON（浏览器插件导出的 name/value 列表 或 对象）
    3. 单行 `NAME=VALUE` 列表（每行一条）
    """
    p = Path(path)
    if not p.exists():
        raise ZhihuError(f"Cookie 文件不存在：{path}")
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        raise ZhihuError(f"Cookie 文件为空：{path}")

    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ZhihuError(f"Cookie JSON 解析失败：{e}") from e
        if isinstance(data, dict) and "name" not in data:
            return "; ".join(f"{k}={v}" for k, v in data.items())
        items = data if isinstance(data, list) else [data]
        pairs = []
        for it in items:
            if isinstance(it, dict) and "name" in it and "value" in it:
                dom = it.get("domain", "")
                if dom and "zhihu.com" not in dom:
                    continue
                pairs.append(f"{it['name']}={it['value']}")
        if not pairs:
            raise ZhihuError(f"Cookie JSON 中没有找到知乎记录：{path}")
        return "; ".join(pairs)

    if "\n" in raw and ";" not in raw.split("\n")[0]:
        parts = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        return "; ".join(parts)

    return raw


# ---------------------------------------------------------------- CLI


def main() -> int:
    ap = argparse.ArgumentParser(
        description="知乎链接 → 纯文本 / Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("url", nargs="?", help="知乎链接")
    ap.add_argument("--file", help="包含多个链接的文本文件（每行一条）")
    ap.add_argument("--cookie-file", default="", help="Cookie 文件路径")
    ap.add_argument("--cookie", default="", help="直接传 Cookie 字符串")
    ap.add_argument("--browser", action="store_true",
                    help="用真实浏览器抓取（抗风控最强）")
    ap.add_argument("--login", action="store_true",
                    help="配合 --browser：打开浏览器手动登录一次")
    ap.add_argument("--no-headless", action="store_true",
                    help="显示浏览器窗口（便于排查/登录）")
    ap.add_argument("--profile-dir", default=".zhihu-profile",
                    help="浏览器登录态持久化目录")
    ap.add_argument("--no-images", action="store_true", help="不保留图片")
    ap.add_argument("-o", "--out-dir", default="", help="输出目录")
    ap.add_argument("--format", choices=["md", "txt", "json"],
                    default="md", help="输出格式")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    urls: list[str] = []
    if args.url:
        urls.append(args.url)
    if args.file:
        urls += [ln.strip() for ln in Path(args.file).read_text(
            encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    if not urls:
        ap.print_help()
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    verbose = not args.quiet
    failures = 0
    for i, u in enumerate(urls, 1):
        if verbose:
            print(f"[{i}/{len(urls)}] {u}", file=sys.stderr)
        try:
            doc = extract(
                u,
                cookie=args.cookie,
                cookie_file=args.cookie_file,
                use_browser=args.browser,
                headless=not args.no_headless,
                keep_images=not args.no_images,
                profile_dir=args.profile_dir,
                verbose=verbose,
            )
        except ZhihuError as e:
            failures += 1
            print(f"    ✗ {e}", file=sys.stderr)
            continue
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"    ✗ 未预期错误 {type(e).__name__}: {e}", file=sys.stderr)
            continue

        if verbose:
            print(f"    ✓ {doc.kind} | {doc.title[:50]} | "
                  f"{len(doc.text)} 字 | {len(doc.images)} 图", file=sys.stderr)

        if args.format == "json":
            payload = json.dumps(doc.to_dict(), ensure_ascii=False, indent=2)
        elif args.format == "txt":
            payload = doc.text
        else:
            payload = doc.markdown

        if out_dir:
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", doc.title or doc.id)[:60]
            path = out_dir / f"{doc.kind}_{doc.id}_{safe}.{args.format}"
            path.write_text(payload, encoding="utf-8")
            if verbose:
                print(f"    → {path}", file=sys.stderr)
        else:
            print(payload)

    return 1 if failures and failures == len(urls) else 0


if __name__ == "__main__":
    sys.exit(main())

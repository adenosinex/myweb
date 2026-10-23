#!/usr/bin/env python3
"""
app.py — 知乎 / 微信公众号 / YouTube 链接转文本与音频下载

启动：
    python app.py                 # 默认 http://127.0.0.1:8320

环境变量：
    ZHIHU_COOKIE      直接提供 Cookie 字符串（可选）
    ZHIHU_COOKIE_FILE Cookie 文件路径（可选，默认 cookies.txt）
    ZHIHU_BROWSER=1   知乎改用浏览器模式（抗风控，较慢）
    ZHIHU_HEADLESS=0  浏览器模式下显示窗口
    ZHIHU_AUTO_REFRESH=0  关闭"每次提取时自动续期 Cookie"（默认开启）
    WECHAT_PROXY      微信文章请求代理（默认 http://192.168.31.204:8758）
    YOUTUBE_API_URL   独立 YouTube 服务地址，默认 http://192.168.31.243:8765
    TRANSCRIPTION_API_URL 音频转写地址，默认 http://192.168.31.197:5840
    TRANSCRIPTION_MODEL_PROFILE 转写模式：daily（默认）/ accurate

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
import threading
import time
import traceback
import secrets
from urllib.parse import urlsplit, parse_qs
from html.parser import HTMLParser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field

import zhihu_extract as ze
from video_pipeline import RemoteError, TranscriptionClient, YouTubePipeline
# ==================== YouTube remote API client (embedded) ====================
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fastapi.responses import StreamingResponse, Response
from starlette.background import BackgroundTask

HOSTS={'youtube.com','www.youtube.com','m.youtube.com','music.youtube.com','youtu.be','www.youtu.be',
       'youtube-nocookie.com','www.youtube-nocookie.com'}
TTL=3600
MAX_JSON=12*1024*1024

@dataclass
class Reference:
    owner: str
    remote_id: str
    created: float
    max_mb: int

class YouTubeClient:
    def __init__(self,root):
        config_path=Path(root)/'youtube_service.json'
        config={}
        if config_path.exists():
            try:config=json.loads(config_path.read_text(encoding='utf-8-sig'))
            except (ValueError,OSError):raise RuntimeError('youtube_service.json 格式错误')
            if not isinstance(config,dict):raise RuntimeError('youtube_service.json 必须为对象')
        default_url=config.get('api_url') or f"http://{config.get('host','192.168.31.243')}:{config.get('port',8765)}"
        self.url=os.environ.get('YOUTUBE_API_URL',default_url).strip().rstrip('/')
        self.references={}
        self.lock=threading.RLock()

    def public_config(self):
        return {'configured':bool(self.url),'api_url':self.url,'mode':'remote','auth_required':False}

    def _configured(self):
        if not self.url:
            raise RemoteError('请在 youtube_service.json 中配置 YouTube 节点的 IP 和端口。',503)
        parsed=urlsplit(self.url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RemoteError('YouTube 服务地址格式错误，请填写 HTTP / HTTPS 服务根地址。',503)

    def _request(self,method,path,read_timeout=12,**kwargs):
        self._configured()
        session=requests.Session()
        session.trust_env=False  # The configured API machine is separate from the YouTube proxy.
        try:
            response=session.request(method,self.url+path,
                        timeout=(3,read_timeout),allow_redirects=False,**kwargs)
        except requests.RequestException as exc:
            session.close()
            import logging
            logging.getLogger(__name__).exception(
                "YouTube API 请求失败：method=%s url=%s%s",
                method, self.url, path
            )
            raise RemoteError(
                f"YouTube API 请求失败：{type(exc).__name__}: {exc}",
                502
            ) from exc
        response._client_session=session
        if not 200<=response.status_code<300:
            status=response.status_code
            message={401:'节点拒绝了请求，请确认节点已更新为免密钥版本。',404:'任务或文件不存在，可能已过期。',
                     429:'YouTube 服务繁忙，请稍后重试。'}.get(status,'YouTube 服务返回错误。')
            if status in {400,422,503}:
                try:
                    detail=response.json().get('detail')
                    if isinstance(detail,str):message=detail[:500]
                except ValueError:pass
            response.close();session.close()
            raise RemoteError(message,404 if status==404 else 502)
        return response

    def _json(self,method,path,**kwargs):
        response=self._request(method,path,stream=True,**kwargs)
        try:
            payload=bytearray()
            for chunk in response.iter_content(65536):
                payload.extend(chunk)
                if len(payload)>MAX_JSON:raise RemoteError('YouTube 服务响应超过大小限制。')
            value=json.loads(payload)
            if not isinstance(value,dict):raise ValueError('not an object')
            return value
        except (ValueError,requests.RequestException):
            raise RemoteError('YouTube 服务响应不完整或格式错误。')
        finally:
            response.close();response._client_session.close()

    def _clean(self):
        now=time.time()
        for jid,ref in list(self.references.items()):
            if now-ref.created>=TTL:self.references.pop(jid,None)

    def _owned(self,jid,owner):
        with self.lock:
            self._clean()
            ref=self.references.get(jid)
            if not ref or ref.owner!=owner:raise RemoteError('任务不存在或已过期。',404)
            return ref

    def submit(self,owner,url,fmt,max_mb,codec):
        with self.lock:
            self._clean()
            if len(self.references)>=64:raise RemoteError('本机任务数量已满，请清理旧任务。',429)
            # Reserve before the HTTP call, so concurrent submissions respect the bound.
            jid=secrets.token_hex(16)
            ref=Reference(owner,'',time.time(),max_mb)
            self.references[jid]=ref
        try:
            data=self._json('POST','/v1/jobs',json={'url':url,'format':fmt if fmt in {'text','markdown','json'} else 'text',
                                                      'max_mb':max_mb,'codec':codec})
            remote_id=data.get('job_id','')
            if not isinstance(remote_id,str) or len(remote_id)!=32 or any(c not in '0123456789abcdef' for c in remote_id):
                raise RemoteError('YouTube 服务未返回有效任务编号。')
            ref.remote_id=remote_id
            return {'ok':True,'kind':'youtube','state':'running','job_id':jid,'stage':data.get('stage','远程服务正在提取字幕')}
        except Exception:
            with self.lock:self.references.pop(jid,None)
            raise

    def status(self,jid,owner,format=None):
        ref=self._owned(jid,owner)
        data=self._json('GET','/v1/jobs/'+ref.remote_id,params={'format':format} if format else {})
        old='/v1/jobs/'+ref.remote_id
        new='/api/youtube/jobs/'+jid
        # Rewriting only this job's prefix keeps all browser downloads on the main app.
        if isinstance(data.get('content'),str):data['content']=data['content'].replace(old,new)
        for item in data.get('downloads') or []:
            path=item.get('url','')
            if not isinstance(path,str) or not path.startswith(old+'/'):raise RemoteError('YouTube 服务下载地址不正确。')
            item['url']=new+path[len(old):]
        audio=data.get('auto_download') or ''
        if audio and (not isinstance(audio,str) or not audio.startswith(old+'/files/audio.')):
            raise RemoteError('YouTube 服务音频地址不正确。')
        data['auto_download']=new+audio[len(old):] if audio else ''
        data['job_id']=jid
        return data

    def delete(self,jid,owner):
        ref=self._owned(jid,owner)
        try:
            data=self._json('DELETE','/v1/jobs/'+ref.remote_id)
        except RemoteError as exc:
            if exc.status!=404:raise
            data={'ok':True,'deleted':True}
        with self.lock:self.references.pop(jid,None)
        return data

    def download(self,jid,owner,name=None,format='text'):
        ref=self._owned(jid,owner)
        if name is not None and name not in {'transcript.txt','subtitles.srt','audio.mp3','audio.opus'}:
            raise RemoteError('文件不存在。',404)
        fmt=format if format in {'text','markdown','json'} else 'text'
        if name is None:
            data=self.status(jid,owner,format=fmt)
            if data.get('state')!='done':raise RemoteError('文件尚未生成。',404)
            content=data.get('content','')
            if not isinstance(content,str) or len(content.encode('utf-8'))>MAX_JSON:raise RemoteError('文本超过大小限制。')
            ext={'text':'txt','markdown':'md','json':'json'}[fmt]
            return Response(content,media_type='text/plain; charset=utf-8',headers={
                'Content-Disposition':f'attachment; filename="YouTube_{jid}.{ext}"','Cache-Control':'no-store'})
        path='/v1/jobs/'+ref.remote_id+('/files/'+name if name else '/document')
        response=self._request('GET',path,stream=True,read_timeout=30)
        limit=ref.max_mb*1024*1024 if name and name.startswith('audio.') else MAX_JSON
        try:
            length=int(response.headers.get('Content-Length','0'))
            if length<0 or length>limit:raise ValueError('size')
        except ValueError:
            response.close();response._client_session.close()
            raise RemoteError('远程文件超过大小限制。')
        ext=name.rsplit('.',1)[-1] if name else {'text':'txt','markdown':'md','json':'json'}[fmt]
        def stream():
            size=0
            try:
                for chunk in response.iter_content(65536):
                    size+=len(chunk)
                    if size>limit:raise RuntimeError('Remote file exceeds size limit')
                    yield chunk
            finally:
                response.close();response._client_session.close()
        headers={'Content-Disposition':f'attachment; filename="YouTube_{jid}.{ext}"','Cache-Control':'no-store'}
        if length:headers['Content-Length']=str(length)
        def close():
            response.close();response._client_session.close()
        return StreamingResponse(stream(),media_type={'mp3':'audio/mpeg','opus':'audio/ogg','srt':'application/x-subrip'}.get(ext,'text/plain'),headers=headers,background=BackgroundTask(close))

# ==================== Original article application ====================


APP_DIR = Path(__file__).parent.resolve()
DEFAULT_COOKIE_FILE = APP_DIR / "cookies.txt"

app = FastAPI(title="知乎 / 微信 / YouTube 转文本", version="3.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=[], allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get('SECRET_KEY') or secrets.token_hex(32),
                   session_cookie='extractor_session', same_site='strict')
transcription_client = TranscriptionClient()
video_client = YouTubePipeline(YouTubeClient(APP_DIR), transcription_client)

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
    return urlsplit(url).hostname == 'mp.weixin.qq.com'


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

    - 通过 WECHAT_PROXY 指定的代理发送请求（默认 192.168.31.204:8758）。
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
    url: str = Field(..., max_length=8000, description="知乎、微信或 YouTube 链接")
    cookie: str = Field("", description="可选，本次请求使用的 Cookie")
    format: str = Field("markdown", description="markdown | text | json")
    max_mb: int = Field(20, ge=1, le=50)
    codec: str = Field('mp3', pattern='^(mp3|opus)$')


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
    state: str = "done"
    job_id: str = ""
    result_type: str = "text"
    stage: str = ""
    percent: float | None = None
    phase: str = ""
    elapsed: float | None = None
    metadata: dict = Field(default_factory=dict)
    downloads: list[dict] = Field(default_factory=list)
    auto_download: str = ""


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


def _resolve_cookie_with_source(req_cookie: str) -> tuple[str, str]:
    """返回 (cookie, source)。

    source ∈ {"request", "env", "file", ""}
      · request — 调用方本次请求带过来的
      · env     — 从环境变量 ZHIHU_COOKIE 读到
      · file    — 从服务器本地 cookies.txt 读到（只有这种才写回盘）
    """
    if req_cookie.strip():
        return req_cookie.strip(), "request"
    env_cookie = os.environ.get("ZHIHU_COOKIE", "").strip()
    if env_cookie:
        return env_cookie, "env"
    stored = stored_cookie()
    if stored:
        return stored, "file"
    return "", ""


def _use_browser() -> bool:
    return os.environ.get("ZHIHU_BROWSER", "").strip() in ("1", "true", "yes")


# =====================================================================
#  知乎 Cookie 自动续期（每次提取请求后静默执行）
# =====================================================================

# 只接受知乎会话相关的键，过滤统计类噪音
_ZHIHU_COOKIE_KEYS = {
    "z_c0", "d_c0", "_zap", "_xsrf", "__zse_ck", "q_c1",
    "BEC", "SESSIONID", "captcha_session_v2", "__snaker__id", "z_c0_f",
}

_ZHIHU_REFRESH_URL = "https://www.zhihu.com/"
_ZHIHU_REFRESH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 续期节流：两次刷新的最小间隔（秒），防止高并发把续期打成风暴
_REFRESH_MIN_INTERVAL = 300

_refresh_lock = threading.Lock()
_last_refresh_ts = 0.0


def _auto_refresh_enabled() -> bool:
    return os.environ.get("ZHIHU_AUTO_REFRESH", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _parse_cookie_pairs(raw: str) -> dict[str, str]:
    """把 Cookie 字符串解析成 {k: v}（保留原始顺序）。"""
    out: dict[str, str] = {}
    for piece in re.split(r"[;\n]", raw or ""):
        piece = piece.strip()
        if "=" not in piece:
            continue
        k, v = piece.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out[k] = v
    return out


def _collect_set_cookie(resp) -> dict[str, str]:
    """从 requests 响应头里收集知乎会话相关的 Set-Cookie 键值。"""
    out: dict[str, str] = {}
    try:
        raw_list = resp.raw.headers.getlist("Set-Cookie")
    except Exception:  # noqa: BLE001
        raw_list = []

    for raw in raw_list:
        # 一条 Set-Cookie 里 name=value 在最前面，后面是 Path/Expires 等属性
        for piece in raw.split(";"):
            piece = piece.strip()
            if "=" not in piece:
                continue
            k, v = piece.split("=", 1)
            k, v = k.strip(), v.strip()
            # 空值代表"清除"，绝不能覆盖本地已有的有效值
            if k in _ZHIHU_COOKIE_KEYS and v:
                out[k] = v
    return out


def refresh_zhihu_cookie(cookie: str, timeout: int = 10) -> tuple[str, list[str]]:
    """带现有 Cookie 访问一次知乎，抓取服务端下发的新 Cookie 并合并。

    返回 (合并后的 Cookie 字符串, 实际发生变化的键名列表)。
    网络错误 / 未登录 / 无新 Cookie 时原样返回。
    """
    if not cookie.strip():
        return cookie, []

    try:
        import requests
    except ImportError:
        return cookie, []

    headers = {
        "User-Agent": _ZHIHU_REFRESH_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.zhihu.com/",
        "Cookie": cookie,
    }

    try:
        resp = requests.get(_ZHIHU_REFRESH_URL, headers=headers,
                            timeout=timeout, allow_redirects=True)
    except Exception:  # noqa: BLE001
        return cookie, []

    new_pairs = _collect_set_cookie(resp)
    if not new_pairs:
        return cookie, []

    current = _parse_cookie_pairs(cookie)
    changed: list[str] = []
    for k, v in new_pairs.items():
        if current.get(k) != v:
            current[k] = v
            changed.append(k)

    if not changed:
        return cookie, []

    merged = "; ".join(f"{k}={v}" for k, v in current.items())
    return merged, sorted(set(changed))


def _persist_cookie(new_cookie: str) -> bool:
    """把续期后的 Cookie 原子写回本地文件。"""
    path = _cookie_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new_cookie + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        _restrict(path)
        return True
    except OSError:
        return False


def _maybe_refresh_cookie(cookie: str, source: str) -> None:
    """静默续期：仅在 Cookie 来源为本地文件时才写回磁盘。

    请求自带 Cookie / 环境变量 Cookie 只做内存刷新、不回写，
    避免把调用方的临时凭证覆盖到服务端本地文件里。
    """
    global _last_refresh_ts

    if source != "file" or not cookie:
        return
    if not _auto_refresh_enabled():
        return

    now = time.time()
    if now - _last_refresh_ts < _REFRESH_MIN_INTERVAL:
        return

    with _refresh_lock:
        # 双重检查：拿到锁后可能已被其他请求刷新过
        if time.time() - _last_refresh_ts < _REFRESH_MIN_INTERVAL:
            return
        try:
            new_cookie, changed = refresh_zhihu_cookie(cookie)
        except Exception:  # noqa: BLE001
            _last_refresh_ts = time.time()   # 失败也计数，防止风暴
            return

        _last_refresh_ts = time.time()
        if not changed:
            return
        if _persist_cookie(new_cookie):
            print(f"[cookie] 自动续期：更新 {len(changed)} 项 "
                  f"({', '.join(changed)})")


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
        "wechat_proxy": WECHAT_PROXY or "(直连)",
        "youtube_service": video_client.public_config(),
        "transcription_service": transcription_client.public_config(),
        "youtube_language": "简体优先，繁体转换为简体",
        "auto_refresh": _auto_refresh_enabled(),
        "last_refresh_at": (
            time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(_last_refresh_ts))
            if _last_refresh_ts else ""
        ),
        "hint": ("已就绪" if cookie_ready or _use_browser()
                 else "尚未配置 Cookie：抓取知乎前请在页面末尾保存，"
                      "或放置 cookies.txt。微信文章无需 Cookie。"),
    }


def video_owner(request: Request) -> str:
    if 'video_owner' not in request.session:
        request.session['video_owner'] = secrets.token_hex(24)
    return request.session['video_owner']


def is_youtube_url(url: str) -> bool:
    try:return urlsplit(url).hostname in HOSTS
    except ValueError:return False

@app.get('/api/youtube/jobs/{jid}')
def video_status(jid: str,request: Request):
    try:return video_client.status(jid,video_owner(request))
    except RemoteError as exc:
        if exc.status==404:raise HTTPException(404,str(exc))
        return {'ok':False,'state':'failed','error':str(exc),'kind':'youtube','job_id':jid}

@app.delete('/api/youtube/jobs/{jid}')
def video_delete(jid: str,request: Request):
    try:return video_client.delete(jid,video_owner(request))
    except RemoteError as exc:raise HTTPException(exc.status,str(exc))

@app.post('/api/youtube/jobs/{jid}/cancel')
def video_cancel(jid: str,request: Request):
    try:return video_client.cancel(jid,video_owner(request))
    except RemoteError as exc:raise HTTPException(exc.status,str(exc))

@app.get('/api/youtube/jobs/{jid}/files/{name}')
def video_file(jid: str,name: str,request: Request):
    try:return video_client.download(jid,video_owner(request),name=name)
    except RemoteError as exc:raise HTTPException(exc.status,str(exc))

@app.get('/api/youtube/jobs/{jid}/document')
def video_text(jid: str,request: Request,format: str='text'):
    try:return video_client.download(jid,video_owner(request),format=format)
    except RemoteError as exc:raise HTTPException(exc.status,str(exc))

@app.post("/api/extract", response_model=ExtractResp)
def api_extract(req: ExtractReq, request: Request) -> ExtractResp:
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="请提供链接")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('invalid URL')
    except ValueError:
        raise HTTPException(400, '请提供有效的 HTTP / HTTPS 链接')

    if is_youtube_url(url):
        try:
            return ExtractResp(**video_client.submit(video_owner(request),url,req.format,req.max_mb,req.codec))
        except RemoteError as exc:
            return ExtractResp(ok=False,error=str(exc),state='failed')

    wechat = is_wechat_url(url)
    host = urlsplit(url).hostname or ''
    zhihu = host == 'zhihu.com' or host.endswith('.zhihu.com')
    if not (wechat or zhihu):
        raise HTTPException(
            status_code=400,
            detail="仅支持知乎、微信公众号与 YouTube 视频链接",
        )

    cookie, cookie_source = _resolve_cookie_with_source(req.cookie)
    use_browser = _use_browser()

    # 微信文章不需要登录态；知乎需要
    if zhihu and not cookie and not use_browser:
        return ExtractResp(
            ok=False,
            error="服务端未配置 Cookie。请在页面末尾「知乎 Cookie」中粘贴并点「保存」，"
                  "或直接把 cookies.txt 放到项目目录，"
                  "或设置环境变量 ZHIHU_BROWSER=1 改用浏览器模式。",
        )

    # ── 无论提取成败，都顺手给 Cookie 续一次期 ──
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
    finally:
        if zhihu and cookie:
            try:
                _maybe_refresh_cookie(cookie, cookie_source)
            except Exception:  # noqa: BLE001
                pass    # 续期失败绝不影响主流程

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
def api_extract_get(request: Request, url: str, format: str = "markdown",
                    cookie: str = "") -> JSONResponse:
    resp = api_extract(ExtractReq(url=url, format=format, cookie=cookie), request)
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
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"\n  知乎 / 微信 / YouTube 服务已启动 →  http://127.0.0.1:{port}\n")
    if os.environ.get('OPEN_BROWSER', '1') != '0':
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(f'http://127.0.0.1:{port}/')).start()
    uvicorn.run(app, host=host, port=port, log_level="info")

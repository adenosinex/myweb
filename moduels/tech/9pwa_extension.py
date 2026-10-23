"""Flask 动态 PWA 蓝图：把可信的单页 HTML 包装成可安装的 PWA。

配置：
    app.register_blueprint(dynamic_bp)
    环境变量 PWA_TOOLS_DIR 可选（默认 app.root_path/db/dynamic_tools）

注意：此版本不验证管理身份，管理页面和创建/修改/删除接口直接开放。
仅推荐局域网、可信环境使用；请勿将管理 API 无防护地暴露到公网。
上传代码会在网站同源执行。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
from functools import lru_cache
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from flask import (
    Blueprint, Response, abort, current_app, jsonify, redirect, request,
    send_file, send_from_directory, url_for,
)
from PIL import Image, ImageDraw, ImageFont


# ========= 网址资源抓取和 HTML 单文件整合（原 url_importer.py，现已内置） =========

import base64
import http.client
import re
import socket
import ssl
import time
import zlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.dammit import UnicodeDammit

# 个人内网使用：允许 HTTP/HTTPS 目标，包括 localhost、本机、内网及保留 IP。
# 不设文件大小、资源数量、端口、请求次数或总抓取时长额度。
# 单次 socket 超时属于故障检测，不是整页抓取额度。
SOCKET_TIMEOUT = 30


class ImportFailed(Exception):
    """面向用户的导入失败。"""


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self.pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self.pinned_ip, self.port), timeout=self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self.pinned_ip = ip

    def connect(self):
        sock = socket.create_connection((self.pinned_ip, self.port), timeout=self.timeout)
        try:
            # TLS SNI 与证书校验始终使用原始主机名，而非 IP。
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def canonical_url(url: str, base: str | None = None) -> str:
    url = urljoin(base, url.strip()) if base else url.strip()
    try:
        u = urlsplit(url)
        port = u.port
    except (ValueError, TypeError) as exc:
        raise ImportFailed('URL 或端口格式错误') from exc
    if u.scheme.lower() not in ('http', 'https') or not u.hostname:
        raise ImportFailed('只支持 http:// 或 https:// 网页链接')
    if u.username is not None or u.password is not None:
        raise ImportFailed('URL 不允许包含用户名或密码')
    if '\x00' in url:
        raise ImportFailed('URL 包含非法空字符')
    if any(ch.isspace() for ch in u.hostname):
        raise ImportFailed('非法域名')
    # 仅校验 URL 结构，不按公网、内网或保留地址分类拦截。
    return urlunsplit((u.scheme.lower(), u.netloc, u.path or '/', u.query, ''))


def _resolve_target_ip(host: str, port: int) -> str:
    """解析任何可连接的地址，包括 loopback / RFC1918 / 链路本地等。"""
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ImportFailed(f'无法解析网站地址：{host}') from exc
    addresses = list(dict.fromkeys(row[4][0] for row in records))
    if not addresses:
        raise ImportFailed('域名没有有效的 IP 地址')
    # 优先 IPv4，可兼容无 IPv6 出口的内部部署环境。
    return sorted(addresses, key=lambda ip: ':' in ip)[0]


def _unzip(data: bytes, encoding: str) -> bytes:
    encoding = encoding.strip().lower()
    if encoding in ('', 'identity'):
        return data
    if encoding not in ('gzip', 'x-gzip', 'deflate'):
        raise ImportFailed('资源使用了不支持的压缩编码')
    try:
        code = 16 + zlib.MAX_WBITS if encoding in ('gzip', 'x-gzip') else zlib.MAX_WBITS
        return zlib.decompress(data, wbits=code)
    except zlib.error as exc:
        raise ImportFailed('资源解压失败') from exc


@dataclass
class Download:
    url: str
    content: bytes
    mime: str
    charset: str | None


class SafeFetcher:
    def __init__(self):
        self.requests = 0
        self.memo: dict[str, Download] = {}

    def get(self, url: str, *, max_bytes: int | None = None, kind: str = 'asset') -> Download:
        url = canonical_url(url)
        if kind != 'page' and url in self.memo:
            return self.memo[url]
        visited: set[str] = set()
        while True:
            if url in visited:
                raise ImportFailed('远程网站存在循环跳转')
            visited.add(url)
            u = urlsplit(url)
            port = u.port or (443 if u.scheme == 'https' else 80)
            ip = _resolve_target_ip(u.hostname, port)
            self.requests += 1
            conn = (_PinnedHTTPS if u.scheme == 'https' else _PinnedHTTP)(u.hostname, port, ip, SOCKET_TIMEOUT)
            try:
                host_header = u.hostname.encode('idna').decode('ascii')
                if ':' in host_header:
                    host_header = f'[{host_header}]'
                if u.port:
                    host_header += f':{u.port}'
                conn.request('GET', (u.path or '/') + ('?' + u.query if u.query else ''), headers={
                    'Host': host_header,
                    'User-Agent': 'Mozilla/5.0 (compatible; PWA-HTML-Importer/1.0)',
                    'Accept': 'text/html,text/css,application/javascript,image/*,*/*;q=0.5',
                    'Accept-Encoding': 'identity',
                    'Connection': 'close',
                })
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    next_url = response.getheader('Location', '')
                    if not next_url:
                        raise ImportFailed('远程网站跳转缺少 Location')
                    url = canonical_url(next_url, url)
                    continue
                if response.status != 200:
                    raise ImportFailed(f'远程资源返回 HTTP {response.status}')
                raw = response.read()
                raw = _unzip(raw, response.getheader('Content-Encoding', ''))
                mime_header = response.getheader('Content-Type', 'application/octet-stream')
                mime = mime_header.split(';', 1)[0].strip().lower()
                match = re.search(r'charset\s*=\s*["\']?([\w.-]+)', mime_header, re.I)
                charset = match.group(1) if match else None
                if kind == 'page' and mime not in ('text/html', 'application/xhtml+xml', 'application/octet-stream'):
                    raise ImportFailed('该链接返回的不是 HTML 页面')
                output = Download(url, raw, mime, charset)
                if kind != 'page':
                    self.memo[url] = output
                return output
            except (OSError, ssl.SSLError, TimeoutError, http.client.HTTPException) as exc:
                raise ImportFailed(f'网络连接失败：{str(exc)[:130]}') from exc
            finally:
                conn.close()


def decode_text(data: Download) -> str:
    if data.charset:
        try:
            return data.content.decode(data.charset, errors='replace')
        except LookupError:
            pass
    return UnicodeDammit(data.content).unicode_markup or data.content.decode('utf-8', errors='replace')


def _data_uri(raw: bytes, mime: str) -> str:
    return f'data:{mime};base64,{base64.b64encode(raw).decode("ascii")}'


def _resource_mime(mime: str, kind: str) -> bool:
    if kind == 'image':
        return mime.startswith('image/')
    if kind == 'css':
        return mime in ('text/css', 'text/plain', 'application/octet-stream')
    if kind == 'script':
        return mime in ('text/javascript', 'application/javascript', 'application/x-javascript',
                        'text/plain', 'application/octet-stream')
    if kind == 'font-or-image':
        return (mime.startswith('image/') or mime.startswith('font/') or
                mime in ('application/font-woff', 'application/font-woff2', 'application/vnd.ms-fontobject',
                         'application/x-font-ttf', 'application/octet-stream'))
    return False


class PageBundler:
    def __init__(self, fetcher: SafeFetcher):
        self.fetcher = fetcher
        self.success = 0
        self.warnings: list[str] = []
        self.css_seen: set[str] = set()

    def warn(self, text: str):
        if len(self.warnings) < 150 and text not in self.warnings:
            self.warnings.append(text)

    def absolute(self, maybe: str, base: str) -> str:
        if not maybe or maybe.startswith(('#', 'data:', 'blob:', 'javascript:', 'mailto:', 'tel:')):
            return maybe
        try:
            return canonical_url(maybe, base)
        except ImportFailed:
            return maybe

    def inline_url(self, raw_url: str, base: str, kind: str) -> str:
        if not raw_url or raw_url.startswith(('data:', '#', 'blob:')):
            return raw_url
        absolute = self.absolute(raw_url, base)
        if not absolute.startswith(('http://', 'https://')):
            return absolute
        try:
            download = self.fetcher.get(absolute, max_bytes=None)
            if not _resource_mime(download.mime, kind):
                raise ImportFailed(f'文件类型不匹配 ({download.mime})')
            self.success += 1
            return _data_uri(download.content, download.mime)
        except ImportFailed as exc:
            self.warn(f'未内联：{absolute[:120]}（{exc}）')
            return absolute

    _css_import = re.compile(r'''@import\s+(?:url\(\s*['\"]?([^'\")\s]+)['\"]?\s*\)|['\"]([^'\"]+)['\"])\s*([^;]*);''', re.I)
    _css_url = re.compile(r'''url\(\s*(?:"([^"]+)"|'([^']+)'|([^\s)]+))\s*\)''', re.I)

    def css(self, source: str, base: str, depth: int = 0) -> str:
        if depth > 10:
            self.warn('CSS @import 嵌套超过 10 级，部分样式保持在线加载')
            return source

        def import_repl(match):
            path = match.group(1) or match.group(2)
            media = (match.group(3) or '').strip()
            absolute = self.absolute(path, base)
            if absolute in self.css_seen:
                return '/* duplicate CSS import skipped */'
            self.css_seen.add(absolute)
            try:
                resp = self.fetcher.get(absolute, max_bytes=None)
                if not _resource_mime(resp.mime, 'css'):
                    raise ImportFailed('返回的不是样式表')
                self.success += 1
                merged = self.css(decode_text(resp), resp.url, depth + 1)
                return '@media ' + media + '{\n' + merged + '\n}' if media and media.lower() != 'all' else merged
            except ImportFailed as exc:
                self.warn(f'未合并 CSS：{absolute[:120]}（{exc}）')
                return f'@import url("{absolute}") {media};'

        source = self._css_import.sub(import_repl, source)

        def url_repl(match):
            value = match.group(1) or match.group(2) or match.group(3) or ''
            # 不加载 css filter/javascript scheme 等非资源链接。
            if not value or value.startswith(('#', 'data:', 'blob:')):
                return match.group(0)
            resolved = self.inline_url(value, base, 'font-or-image')
            return 'url("' + resolved.replace('"', '%22') + '")'

        return self._css_url.sub(url_repl, source)

    def bundle(self, page: Download) -> tuple[str, dict]:
        original = decode_text(page)
        soup = BeautifulSoup(original, 'html.parser')
        if soup.html is None:
            shell = BeautifulSoup('<!DOCTYPE html><html><head></head><body></body></html>', 'html.parser')
            for node in list(soup.contents):
                shell.body.append(node.extract())
            soup = shell
        if soup.head is None:
            soup.html.insert(0, soup.new_tag('head'))
        if soup.body is None:
            soup.html.append(soup.new_tag('body'))
        # 去掉原站点的 <base>，避免影响 PWA 路径；所有可见链接改为绝对地址。
        base = page.url
        for tag in list(soup.select('base[href]')):
            candidate = self.absolute(str(tag.get('href')), base)
            if candidate.startswith(('http://', 'https://')):
                base = candidate
            tag.decompose()
        # 保留文档来源，不会影响脚本运行。
        origin_meta = soup.new_tag('meta')
        origin_meta.attrs = {'name': 'pwa-original-url', 'content': page.url}
        soup.head.append(origin_meta)
        # 原站 manifest 和 CSP 通常不适用于新域名；删除以允许内联资源及 PWA manifest 注入。
        for tag in list(soup.select('link[rel], meta[http-equiv]')):
            if tag.name == 'link' and 'manifest' in (tag.get('rel') or []):
                tag.decompose()
            elif tag.name == 'meta' and str(tag.get('http-equiv', '')).lower() == 'content-security-policy':
                tag.decompose()
        for tag in list(soup.find_all('link', href=True)):
            href = str(tag.get('href', ''))
            rel = [str(x).lower() for x in (tag.get('rel') or [])]
            if 'stylesheet' in rel:
                absolute = self.absolute(href, base)
                try:
                    resp = self.fetcher.get(absolute, max_bytes=None)
                    if not _resource_mime(resp.mime, 'css'):
                        raise ImportFailed('返回的不是 CSS 文件')
                    style = soup.new_tag('style')
                    style['data-imported-from'] = resp.url
                    if tag.has_attr('media'):
                        style['media'] = tag['media']
                    css = self.css(decode_text(resp), resp.url)
                    # 防止 CSS 内容含 HTML 风格闭合标签导致插入页面异常。
                    style.string = css.replace('</style', '<\\/style')
                    tag.replace_with(style)
                    self.success += 1
                except ImportFailed as exc:
                    self.warn(f'样式需联网：{absolute[:120]}（{exc}）')
                    tag['href'] = absolute
            elif 'icon' in rel or 'apple-touch-icon' in rel:
                tag['href'] = self.inline_url(href, base, 'image')
            elif 'preload' in rel or 'modulepreload' in rel or 'prefetch' in rel or 'preconnect' in rel or 'dns-prefetch' in rel:
                # 内联后不应再预加载源站资源。JS modulepreload 可能仍依赖网络，后续会有警告。
                tag.decompose()
            else:
                tag['href'] = self.absolute(href, base)
        for tag in list(soup.find_all('script', src=True)):
            raw_src = str(tag['src'])
            absolute = self.absolute(raw_src, base)
            if tag.get('type', '').lower() in ('application/ld+json', 'importmap', 'speculationrules'):
                tag['src'] = absolute
                self.warn('结构化数据、Import Map 或浏览器特殊脚本不自动合并')
                continue
            try:
                resp = self.fetcher.get(absolute, max_bytes=None)
                if not _resource_mime(resp.mime, 'script'):
                    raise ImportFailed('返回的不是 JavaScript')
                js = decode_text(resp)
                if tag.has_attr('async') or tag.has_attr('defer'):
                    self.warn('异步 / defer 外部脚本已转换为内联脚本，执行顺序可能改变')
                if tag.get('type', '').lower() == 'module' and re.search(r'\b(?:import|export)\b', js):
                    self.warn('检测到 JavaScript 模块引用：模块依赖未打包，可能仍需联网')
                    # 对常见的 from './x.js'、import './x.js' 改为原站绝对地址，避免请求 /pwa/slug/x.js。
                    js = re.sub(r'''(\bfrom\s*|\bimport\s*)([\"'])(\.{1,2}/[^\"']+)(\2)''',
                                lambda m: m.group(1) + m.group(2) + self.absolute(m.group(3), resp.url) + m.group(4), js)
                    js = re.sub(r'''(\bimport\s*\(\s*)([\"'])(\.{1,2}/[^\"']+)(\2)''',
                                lambda m: m.group(1) + m.group(2) + self.absolute(m.group(3), resp.url) + m.group(4), js)
                if re.search(r'\b(?:fetch\s*\(|XMLHttpRequest\b|import\s*\()', js):
                    self.warn('检测到动态请求或模块加载：这些运行时资源不会自动合并，可能仍需联网')
                tag.attrs.pop('src', None)
                for attr in ('integrity', 'crossorigin', 'referrerpolicy', 'defer', 'async'):
                    tag.attrs.pop(attr, None)
                tag['data-imported-from'] = resp.url
                tag.string = js.replace('</script', '<\\/script')
                self.success += 1
            except ImportFailed as exc:
                self.warn(f'脚本需联网：{absolute[:120]}（{exc}）')
                tag['src'] = absolute
        for tag in soup.find_all('style'):
            if tag.get('data-imported-from'):
                continue
            if tag.string:
                tag.string = self.css(str(tag.string), base).replace('</style', '<\\/style')
        for tag in soup.find_all(style=True):
            tag['style'] = self.css(str(tag['style']), base)
        for tag in soup.find_all('img', src=True):
            tag['src'] = self.inline_url(str(tag['src']), base, 'image')
        for tag in soup.find_all('input', src=True):
            if str(tag.get('type', '')).lower() == 'image':
                tag['src'] = self.inline_url(str(tag['src']), base, 'image')
            else:
                tag['src'] = self.absolute(str(tag['src']), base)
        for tag in soup.find_all('source', src=True):
            kind = str(tag.get('type', '')).lower()
            if kind.startswith('image/'):
                tag['src'] = self.inline_url(str(tag['src']), base, 'image')
            else:
                tag['src'] = self.absolute(str(tag['src']), base)
        for tag in soup.find_all(['img', 'source'], srcset=True):
            # 原始 srcset 以逗号区分多个候选；转换前拆分，避免把 data URI 的逗号再次解析。
            candidates = []
            for part in str(tag['srcset']).split(','):
                part = part.strip()
                if not part:
                    continue
                bits = part.split()
                candidates.append(self.inline_url(bits[0], base, 'image') + (' ' + ' '.join(bits[1:]) if len(bits) > 1 else ''))
            tag['srcset'] = ', '.join(candidates)
        for tag in soup.find_all(['video'], poster=True):
            tag['poster'] = self.inline_url(str(tag['poster']), base, 'image')
        for tag in soup.find_all(background=True):
            tag['background'] = self.inline_url(str(tag['background']), base, 'image')
        for tag in soup.find_all(['a', 'form', 'iframe']):
            attr = 'action' if tag.name == 'form' else 'src' if tag.name == 'iframe' else 'href'
            if tag.has_attr(attr):
                tag[attr] = self.absolute(str(tag[attr]), base)
            if tag.name == 'iframe':
                self.warn('iframe 内的内容不能合并为一个 HTML，仍依赖联网')
        # 媒体文件通常很大，为避免 OOM 保留在线链接并提示。
        for tag in soup.find_all(['video', 'audio', 'source']):
            if tag.has_attr('src') and not str(tag['src']).startswith('data:'):
                tag['src'] = self.absolute(str(tag['src']), base)
                if tag.name in ('video', 'audio', 'source'):
                    self.warn('音视频资源未内联，离线时可能无法播放')
        output = str(soup)
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        title = title[:100] or (urlsplit(page.url).hostname or '导入的网页')
        return output, {
            'name': title,
            'source_url': page.url,
            'resource_count': self.success,
            'http_requests': self.fetcher.requests,
            'size_bytes': len(output.encode('utf-8')),
            'warnings': self.warnings,
        }


def import_webpage(url: str) -> tuple[str, dict]:
    client = SafeFetcher()
    page = client.get(url, max_bytes=None, kind='page')
    return PageBundler(client).bundle(page)

# ========= PWA 蓝图路由 =========

dynamic_bp = Blueprint("dynamic_pwa", __name__, url_prefix="/pwa")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
RESERVED = {"api", "manager"}
IMPORT_LIMITER = threading.BoundedSemaphore(value=2)


def tools_root() -> Path:
    location = os.environ.get("PWA_TOOLS_DIR")
    return Path(location).resolve() if location else Path(current_app.root_path, "db", "dynamic_tools").resolve()


def valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.fullmatch(slug)) and slug not in RESERVED


def tool_path(slug: str) -> Path:
    if not valid_slug(slug):
        abort(404)
    return tools_root() / slug


def source_path(slug: str) -> Path:
    folder = tool_path(slug)
    if folder.is_symlink():
        abort(404)
    path = folder / "index.html"
    if not path.is_file() or path.is_symlink():
        abort(404)
    return path


def read_html(slug: str) -> str:
    return source_path(slug).read_text(encoding="utf-8")


def no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@dynamic_bp.errorhandler(400)
@dynamic_bp.errorhandler(404)
@dynamic_bp.errorhandler(409)
@dynamic_bp.errorhandler(413)
@dynamic_bp.errorhandler(429)
@dynamic_bp.errorhandler(503)
def handle_error(err):
    # 管理 API 返回 JSON，其它路由仍正常返回 HTTP 错误
    if request.path.startswith(url_for("dynamic_pwa.api_tools")) or request.path.startswith(url_for("dynamic_pwa.api_import_url")):
        return no_store(jsonify(error=err.description)), err.code
    return err


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.in_title = False
        self.description = ""
        self.theme_color = "#6c5ce7"

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag.lower() == "title":
            self.in_title = True
        if tag.lower() == "meta":
            name = (attributes.get("name") or "").lower()
            value = attributes.get("content") or ""
            if name == "description":
                self.description = value[:500]
            elif name == "theme-color" and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                self.theme_color = value

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)


def metadata(html: str, slug: str) -> dict:
    parser = MetadataParser()
    parser.feed(html)
    title = " ".join("".join(parser.title_parts).split()) or slug.replace("-", " ").title()
    return {
        "name": title[:100],
        "short_name": title[:12],
        "description": parser.description,
        "theme_color": parser.theme_color,
        "background_color": "#ffffff",
    }


def manifest_for(slug: str, html: str) -> dict:
    info = metadata(html, slug)
    return {
        **info,
        "id": url_for("dynamic_pwa.serve_tool", slug=slug),
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "orientation": "any",
        "prefer_related_applications": False,
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }


def insert_before_closing(html: str, closing_tag: str, addition: str) -> str:
    pattern = rf"</{closing_tag}\s*>"
    if re.search(pattern, html, re.IGNORECASE):
        return re.sub(pattern, lambda m: addition + "\n" + m.group(0), html, count=1, flags=re.IGNORECASE)
    if closing_tag == "head":
        head = "<head>\n" + addition + "\n</head>\n"
        if re.search(r"<html\b[^>]*>", html, re.IGNORECASE):
            return re.sub(r"<html\b[^>]*>", lambda m: m.group(0) + "\n" + head,
                          html, count=1, flags=re.IGNORECASE)
        return "<!DOCTYPE html>\n<html lang='zh-CN'>\n" + head + "<body>\n" + html + "\n</body></html>"
    return html + "\n" + addition


def build_pwa_page(slug: str, html: str) -> str:
    info = metadata(html, slug)
    # 原 HTML 如已有 manifest，先去除，避免浏览器读取了旧 manifest。
    # 仅替换 link 标签，不重组 html/head/body 属性。
    def drop_old_manifest(match):
        tag = match.group(0)
        rel = re.search(r"\brel\s*=\s*([\"'])(.*?)\1", tag, re.IGNORECASE | re.DOTALL)
        return '' if rel and 'manifest' in rel.group(2).lower().split() else tag
    html = re.sub(r'<link\b[^>]*>', drop_old_manifest, html, flags=re.IGNORECASE)
    extra_meta = []
    if not re.search(r'<meta\b[^>]*\bname\s*=\s*[\"\']viewport[\"\']', html, re.IGNORECASE):
        extra_meta.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    head = "\n".join(extra_meta + [
        '<meta name="mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-capable" content="yes">',
        f'<meta name="apple-mobile-web-app-title" content="{escape(info["short_name"], quote=True)}">',
        f'<link rel="apple-touch-icon" href="{escape(url_for("dynamic_pwa.icon", slug=slug, size=192), quote=True)}">',
        f'<link rel="manifest" href="{escape(url_for("dynamic_pwa.serve_manifest", slug=slug), quote=True)}">',
    ])
    result = insert_before_closing(html, "head", head)
    script_url = escape(url_for("dynamic_pwa.install_js", slug=slug), quote=True)
    result = insert_before_closing(result, "body", f'<script defer src="{script_url}"></script>')
    return result


def atomic_write(path: Path, contents: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(contents)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def read_payload() -> tuple[str, str]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="必须提交 JSON 对象")
    slug = data.get("slug", "")
    html = data.get("html", "")
    if not isinstance(slug, str) or not valid_slug(slug):
        abort(400, description="slug 只能使用小写字母、数字、连字符，长度 1～63")
    if not isinstance(html, str) or not html.strip():
        abort(400, description="HTML 不能为空")
    return slug, html


@dynamic_bp.route("/manager", methods=["GET"])
def manager():
    # 管理页面直接可用，无需登录。
    page_file = Path(__file__).with_name("pwa_manager.html")
    return no_store(send_file(page_file, mimetype="text/html"))


@dynamic_bp.route("/api/import-url", methods=["POST"])
def api_import_url():
    """抓取任意可访问的 HTTP/HTTPS 网页并转换单文件 HTML。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="请使用 JSON 提交链接")
    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        abort(400, description="请输入 http:// 或 https:// 网页链接")
    save = data.get("save", False)
    if not isinstance(save, bool):
        abort(400, description="save 必须是布尔值")
    slug = data.get("slug", "")
    if save and (not isinstance(slug, str) or not valid_slug(slug)):
        abort(400, description="导入创建需要合法的工具标识 slug")
    if not IMPORT_LIMITER.acquire(blocking=False):
        abort(429, description="已有导入任务正在执行，请稍后再试")
    try:
        try:
            html, report = import_webpage(url)
        except ImportFailed as exc:
            abort(400, description=str(exc))
    finally:
        IMPORT_LIMITER.release()
    if not save:
        return no_store(jsonify(html=html, **report))
    # 导入成功后才落盘；同名工具不覆盖，避免破坏已有 PWA。
    root = tools_root()
    root.mkdir(parents=True, exist_ok=True)
    folder = tool_path(slug)
    try:
        folder.mkdir()
    except FileExistsError:
        abort(409, description="此工具标识已经存在，请更换 slug")
    try:
        atomic_write(folder / "index.html", html)
    except Exception:
        shutil.rmtree(folder)
        raise
    return no_store(jsonify(message="链接导入并创建成功", slug=slug,
                                 url=url_for("dynamic_pwa.serve_tool", slug=slug), **report)), 201


@dynamic_bp.route("/api/tools", methods=["GET"])
def api_tools():
    root = tools_root()
    output = []
    if root.is_dir():
        for folder in root.iterdir():
            if not folder.is_dir() or folder.is_symlink() or not valid_slug(folder.name):
                continue
            source = folder / "index.html"
            if not source.is_file() or source.is_symlink():
                continue
            try:
                html = source.read_text(encoding="utf-8")
                output.append({
                    "slug": folder.name,
                    "name": metadata(html, folder.name)["name"],
                    "url": url_for("dynamic_pwa.serve_tool", slug=folder.name),
                })
            except (OSError, UnicodeError):
                continue
    return no_store(jsonify(sorted(output, key=lambda item: item["slug"])))


@dynamic_bp.route("/api/tools", methods=["POST"])
def api_create_tool():
    slug, html = read_payload()
    root = tools_root()
    root.mkdir(parents=True, exist_ok=True)
    folder = tool_path(slug)
    try:
        folder.mkdir()
    except FileExistsError:
        abort(409, description="工具已存在，请使用编辑功能")
    try:
        atomic_write(folder / "index.html", html)
    except Exception:
        shutil.rmtree(folder)
        raise
    return no_store(jsonify(message="已创建", url=url_for("dynamic_pwa.serve_tool", slug=slug))), 201


@dynamic_bp.route("/api/tools/<slug>", methods=["GET"])
def api_get_tool(slug):
    html = read_html(slug)
    return no_store(jsonify(slug=slug, html=html, **metadata(html, slug)))


@dynamic_bp.route("/api/tools/<slug>", methods=["PUT"])
def api_update_tool(slug):
    old = source_path(slug)
    submitted_slug, html = read_payload()
    if submitted_slug != slug:
        abort(400, description="编辑时不能修改 slug")
    atomic_write(old, html)
    return no_store(jsonify(message="已更新", url=url_for("dynamic_pwa.serve_tool", slug=slug)))


@dynamic_bp.route("/api/tools/<slug>", methods=["DELETE"])
def api_delete_tool(slug):
    folder = tool_path(slug)
    if not folder.is_dir() or folder.is_symlink():
        abort(404)
    shutil.rmtree(folder)
    return no_store(jsonify(message="已删除"))


@dynamic_bp.route("/<slug>/", strict_slashes=False, methods=["GET"])
def serve_tool(slug):
    if not request.path.endswith("/"):
        return redirect(url_for("dynamic_pwa.serve_tool", slug=slug), code=308)
    html = read_html(slug)
    return no_store(Response(build_pwa_page(slug, html), mimetype="text/html"))


@dynamic_bp.route("/<slug>/index.html", methods=["GET"])
def serve_index(slug):
    return serve_tool(slug)


@dynamic_bp.route("/<slug>/manifest.json", methods=["GET"])
def serve_manifest(slug):
    html = read_html(slug)
    contents = json.dumps(manifest_for(slug, html), ensure_ascii=False)
    return no_store(Response(contents, content_type="application/manifest+json; charset=utf-8"))


@lru_cache(maxsize=256)
def make_icon(slug: str, size: int) -> bytes:
    digest = hashlib.sha256(slug.encode("utf-8")).digest()
    background = (60 + digest[0] % 110, 60 + digest[1] % 110, 90 + digest[2] % 95)
    image = Image.new("RGB", (size, size), background)
    pen = ImageDraw.Draw(image)
    pad = round(size * 0.20)
    pen.rounded_rectangle((pad, pad, size - pad, size - pad), radius=size // 9, fill=(255, 255, 255))
    letters = slug[:2].upper()
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", int(size * 0.26))
    except OSError:
        font = ImageFont.load_default()
    bounds = pen.textbbox((0, 0), letters, font=font)
    x = (size - (bounds[2] - bounds[0])) / 2 - bounds[0]
    y = (size - (bounds[3] - bounds[1])) / 2 - bounds[1]
    pen.text((x, y), letters, fill=background, font=font)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


@dynamic_bp.route("/<slug>/icon-<int:size>.png", methods=["GET"])
def icon(slug, size):
    source_path(slug)
    if size not in (192, 512):
        abort(404)
    response = Response(make_icon(slug, size), mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=86400"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@dynamic_bp.route("/<slug>/sw.js", methods=["GET"])
def service_worker(slug):
    html = read_html(slug)
    home = url_for("dynamic_pwa.serve_tool", slug=slug)
    manifest = url_for("dynamic_pwa.serve_manifest", slug=slug)
    installer = url_for("dynamic_pwa.install_js", slug=slug)
    icons = [url_for("dynamic_pwa.icon", slug=slug, size=n) for n in (192, 512)]
    required = [home]
    optional = [manifest, installer, *icons]

    # 根据 HTML、实际静态资源内容和代码版本生成缓存名，编辑页面后触发新 SW。
    fingerprint = hashlib.sha256(b"dynamic-pwa-offline-v3\x00" + html.encode("utf-8"))
    folder = tool_path(slug)
    count, total = 0, 0
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.is_symlink() or any(part.startswith(".") for part in path.relative_to(folder).parts):
            continue
        if path.name == "index.html":
            continue
        size = path.stat().st_size
        # 避免太多文件使 SW 安装卡住，超限的文件依然按需联网缓存。
        if count >= 128 or total + size > 15 * 1024 * 1024:
            continue
        relative = path.relative_to(folder).as_posix()
        fingerprint.update(relative.encode("utf-8") + b"\x00")
        fingerprint.update(path.read_bytes())
        optional.append(url_for("dynamic_pwa.serve_asset", slug=slug, filename=relative))
        total += size
        count += 1

    version = fingerprint.hexdigest()[:18]
    prefix = f"dynamic-pwa:{slug}:"
    code = r"""
const CACHE_PREFIX = __PREFIX__;
const CACHE_NAME = CACHE_PREFIX + __VERSION__;
const HOME_URL = __HOME__;
const SCOPE_URL = __SCOPE__;
const MUST_CACHE = __REQUIRED__;
const EXTRA_CACHE = __OPTIONAL__;

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    // 核心主页必须缓存成功；失败时不能错误报告支持离线。
    for (const url of MUST_CACHE) await cache.add(new Request(url, { cache: 'reload' }));
    // 图标/附加文件失败不会阻止核心 HTML 的离线安装。
    await Promise.allSettled(EXTRA_CACHE.map(async url => {
      try { await cache.add(new Request(url, { cache: 'reload' })); }
      catch (err) { console.warn('[PWA] 附加资源未缓存:', url, err); }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    // 只删本工具的旧缓存，不干扰其它工具的离线内容。
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k.startsWith(CACHE_PREFIX) && k !== CACHE_NAME)
      .map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith(SCOPE_URL)) return;

  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const navigation = req.mode === 'navigate';

    if (navigation) {
      // 页面内容以最新线上版本为准；离线时回退到主页。
      try {
        const response = await fetch(req);
        if (response.ok && response.type === 'basic') {
          try { await cache.put(HOME_URL, response.clone()); } catch (e) { console.warn(e); }
        }
        return response;
      } catch (err) {
        return (await cache.match(req, {ignoreSearch: true})) ||
               (await cache.match(HOME_URL)) ||
               new Response('<h1>页面未完成离线缓存</h1><p>请联网打开页面一次再试。</p>', {
                 status: 503, headers: {'Content-Type': 'text/html; charset=utf-8'}
               });
      }
    }

    // 静态内容先查缓存：断网也能快速返回。
    const hit = await cache.match(req, {ignoreSearch: true});
    if (hit) return hit;
    try {
      const response = await fetch(req);
      if (response.ok && response.type === 'basic') {
        try { await cache.put(req, response.clone()); } catch (e) { console.warn(e); }
      }
      return response;
    } catch (err) {
      return Response.error();
    }
  })());
});
"""
    code = (code.replace("__PREFIX__", json.dumps(prefix))
                .replace("__VERSION__", json.dumps(version))
                .replace("__HOME__", json.dumps(home))
                .replace("__SCOPE__", json.dumps(home))
                .replace("__REQUIRED__", json.dumps(required))
                .replace("__OPTIONAL__", json.dumps(optional)))
    response = Response(code, mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = home
    return no_store(response)


@dynamic_bp.route("/<slug>/install.js", methods=["GET"])
def install_js(slug):
    source_path(slug)
    sw_url = url_for("dynamic_pwa.service_worker", slug=slug)
    scope = url_for("dynamic_pwa.serve_tool", slug=slug)
    manifest = url_for("dynamic_pwa.serve_manifest", slug=slug)
    prefix = f"dynamic-pwa:{slug}:"
    code = r"""
(() => {
  'use strict';
  const SW_URL = __SW_URL__;
  const SCOPE = __SCOPE__;
  const MANIFEST_URL = __MANIFEST_URL__;
  const CACHE_PREFIX = __CACHE_PREFIX__;
  const ua = navigator.userAgent || '';
  const isIOS = /iPhone|iPad|iPod/.test(ua) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const standalone = () => window.matchMedia('(display-mode: standalone)').matches ||
    window.matchMedia('(display-mode: fullscreen)').matches || navigator.standalone === true;
  let installEvent = null;
  let installButton = null;
  let registrationError = null;

  // 注册必须使用当前工具的 URL + scope。独立工具互不覆盖。
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    installEvent = event;
    if (installButton) installButton.textContent = '安装应用';
  });
  window.addEventListener('appinstalled', () => {
    installEvent = null;
    if (installButton) installButton.hidden = true;
  });

  async function registerSW() {
    if (!('serviceWorker' in navigator)) {
      registrationError = '当前浏览器不支持 Service Worker';
      return;
    }
    if (!window.isSecureContext) {
      registrationError = '当前连接不是安全上下文，请使用 HTTPS 或 localhost/127.0.0.1';
      return;
    }
    try {
      const reg = await navigator.serviceWorker.register(SW_URL, {scope: SCOPE, updateViaCache: 'none'});
      await reg.update().catch(() => {});
      registrationError = null;
      console.info('[PWA] Service Worker 已注册:', reg.scope);
    } catch (error) {
      registrationError = String(error);
      console.error('[PWA] Service Worker 注册失败:', error);
    }
  }

  function openDialog(title, lines) {
    document.getElementById('__pwa_dialog')?.remove();
    const overlay = document.createElement('div');
    overlay.id = '__pwa_dialog';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:#0009;display:flex;align-items:center;justify-content:center;padding:18px';
    const card = document.createElement('section');
    card.style.cssText = 'width:100%;max-width:430px;max-height:85vh;overflow:auto;border-radius:14px;background:white;color:#20212a;padding:20px;font:14px/1.7 system-ui,sans-serif;white-space:pre-wrap;box-shadow:0 12px 45px #0005';
    const heading = document.createElement('h2');
    heading.textContent = title;
    heading.style.cssText = 'margin:0 0 10px;font:600 19px system-ui,sans-serif';
    const content = document.createElement('div');
    content.textContent = lines.join('\n');
    const close = document.createElement('button');
    close.type = 'button';
    close.textContent = '关闭';
    close.style.cssText = 'margin-top:18px;padding:8px 18px;border:0;border-radius:8px;background:#6c5ce7;color:#fff;cursor:pointer;font:600 14px system-ui';
    close.addEventListener('click', () => overlay.remove());
    overlay.addEventListener('click', event => { if (event.target === overlay) overlay.remove(); });
    card.append(heading, content, close);
    overlay.append(card);
    document.body.append(overlay);
  }

  function installHelp() {
    const lines = [];
    if (!window.isSecureContext) lines.push('当前不是 HTTPS 安全连接，PWA 安装和离线功能可能不可用。');
    if (isIOS) {
      lines.push('iPhone / iPad：在 Safari 打开此页，使用分享菜单 → 添加到主屏幕；部分版本或浏览器的菜单位置不同。');
    } else {
      lines.push('Chrome / Edge：点击地址栏的安装图标，或打开浏览器菜单，选择「安装应用」「将网页安装为应用」等选项。');
      lines.push('只有浏览器触发 beforeinstallprompt 时，网页才能直接打开原生安装弹窗；不能强制弹出。');
    }
    lines.push('请确认当前 URL 是工具页：' + location.origin + SCOPE);
    lines.push('如已安装此应用，浏览器可能不再提供安装提示。');
    openDialog('安装 PWA', lines);
  }

  async function showDiagnostics() {
    const lines = [
      '页面：' + location.href,
      'HTTPS / 安全上下文：' + (window.isSecureContext ? '是' : '否'),
      'Service Worker API：' + ('serviceWorker' in navigator ? '支持' : '不支持'),
      '应用运行模式：' + (standalone() ? '已安装的独立模式' : '浏览器标签页'),
      '原生安装事件：' + (installEvent ? '已就绪' : '尚未触发（不等于不可安装）'),
      '联网状态：' + (navigator.onLine ? '系统报告在线' : '系统报告离线'),
      'Manifest：' + location.origin + MANIFEST_URL
    ];
    if (registrationError) lines.push('注册错误：' + registrationError);
    if ('serviceWorker' in navigator && window.isSecureContext) {
      try {
        const reg = await navigator.serviceWorker.getRegistration(SCOPE);
        lines.push('SW 注册状态：' + (reg ? (reg.active?.state || reg.installing?.state || reg.waiting?.state || '等待中') : '未注册'));
        lines.push('当前页面被 SW 控制：' + (navigator.serviceWorker.controller ? '是' : '否（首次注册后请刷新页面）'));
      } catch (e) { lines.push('SW 查询错误：' + e); }
      try {
        const keys = (await caches.keys()).filter(k => k.startsWith(CACHE_PREFIX));
        const entries = await Promise.all(keys.map(async name => ({name, cache: await caches.open(name)})));
        let cached = false;
        for (const item of entries) {
          if (await item.cache.match(SCOPE)) { cached = true; break; }
        }
        lines.push('当前工具缓存：' + (keys.length ? keys.join(', ') : '不存在'));
        lines.push('离线主页缓存：' + (cached ? '已存在' : '未生成'));
      } catch (e) { lines.push('缓存查询错误：' + e); }
    }
    lines.push('测试离线：先联网打开一次并等待缓存建立，再进入开发者工具 Network → Offline，刷新页面。');
    lines.push('仅内联的 HTML/CSS/JS 可确保脱离网络运行，远程 API 或第三方脚本不自动离线。');
    openDialog('PWA 安装与离线诊断', lines);
  }

  async function clickInstall() {
    if (installEvent) {
      const event = installEvent;
      installEvent = null;
      try {
        await event.prompt();
        const choice = await event.userChoice;
        if (choice?.outcome !== 'accepted') installHelp();
      } catch (error) {
        console.warn('[PWA] 安装弹窗未能打开:', error);
        installHelp();
      }
    } else {
      installHelp();
    }
  }

  function init() {
    if (standalone() || document.getElementById('__pwa_actions')) return;
    const wrap = document.createElement('div');
    wrap.id = '__pwa_actions';
    wrap.style.cssText = 'position:fixed;z-index:2147483600;bottom:16px;right:16px;display:flex;gap:8px;align-items:center;font:14px system-ui';
    const install = document.createElement('button');
    install.id = '__pwa_install';
    install.type = 'button';
    install.textContent = '安装应用';
    install.style.cssText = 'display:inline-block;padding:11px 16px;border:0;border-radius:10px;background:#6c5ce7;color:#fff;box-shadow:0 4px 14px #0003;cursor:pointer;font:600 14px system-ui';
    install.addEventListener('click', clickInstall);
    const debug = document.createElement('button');
    debug.type = 'button';
    debug.textContent = 'PWA检测';
    debug.style.cssText = 'display:inline-block;padding:11px 12px;border:1px solid #c9cbd2;border-radius:10px;background:white;color:#222;box-shadow:0 4px 14px #0002;cursor:pointer;font:600 14px system-ui';
    debug.addEventListener('click', showDiagnostics);
    wrap.append(debug, install);
    document.body.append(wrap);
    installButton = install;
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
  else init();
  registerSW();
})();
"""
    code = (code.replace("__SW_URL__", json.dumps(sw_url))
                .replace("__SCOPE__", json.dumps(scope))
                .replace("__MANIFEST_URL__", json.dumps(manifest))
                .replace("__CACHE_PREFIX__", json.dumps(prefix)))
    return no_store(Response(code, mimetype="application/javascript"))


@dynamic_bp.route("/<slug>/<path:filename>", methods=["GET"])
def serve_asset(slug, filename):
    # 支持手工放到工具目录的 CSS/JS/图片；所有隐藏文件和符号链接都拒绝。
    folder = tool_path(slug)
    if folder.is_symlink() or not (folder / "index.html").is_file():
        abort(404)
    pieces = Path(filename).parts
    if not pieces or any(x.startswith(".") for x in pieces) or filename == "index.html":
        abort(404)
    resolved = (folder / filename).resolve()
    if not resolved.is_relative_to(folder.resolve()) or not resolved.is_file():
        abort(404)
    return send_from_directory(folder, filename)

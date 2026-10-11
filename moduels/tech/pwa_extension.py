"""Flask 动态 PWA 蓝图：把可信的单页 HTML 包装成可安装的 PWA。

配置：
    app.register_blueprint(dynamic_bp)
    管理密钥首次启动自动生成并保存为本地文件，重启后继续使用
    环境变量 PWA_ADMIN_TOKEN 可选（仅首次创建时用作初始管理密钥）
    环境变量 PWA_ADMIN_TOKEN_FILE 可选（自定义本地密钥文件路径）
    环境变量 PWA_TOOLS_DIR 可选（默认 app.root_path/db/dynamic_tools）

注意：仅允许发布自己信任的 HTML。上传代码会在网站同源执行。
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import stat
import shutil
import tempfile
from functools import lru_cache
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from flask import (
    Blueprint, Response, abort, current_app, jsonify, redirect, request,
    send_file, send_from_directory, url_for,
)
from PIL import Image, ImageDraw, ImageFont


dynamic_bp = Blueprint("dynamic_pwa", __name__, url_prefix="/pwa")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
RESERVED = {"api", "manager"}
MAX_HTML_BYTES = 2 * 1024 * 1024


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


def admin_token_path() -> Path:
    """管理密钥的本地持久化位置；与工具数据保持在同一持久化存储区域。"""
    configured = os.environ.get("PWA_ADMIN_TOKEN_FILE", "").strip()
    if configured:
        # 自定义路径可使用绝对路径；相对路径以 Flask 项目根目录为基准。
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(current_app.root_path) / path
        return path.absolute()
    return tools_root().parent / ".pwa_admin_token"


def read_saved_token(path: Path) -> str:
    """读取密钥时禁止文件符号链接，避免误读其他敏感文件。"""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise RuntimeError(f"PWA 管理密钥不是普通文件：{path}")
        if os.name == "posix" and info.st_mode & 0o077:
            # 已有文件的权限自动收紧为仅运行服务的用户可读写。
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            token = stream.read(1024).strip()
        if not token or len(token) > 512 or "\n" in token or "\r" in token:
            raise RuntimeError(f"PWA 管理密钥文件为空或内容无效：{path}")
        return token
    finally:
        if fd >= 0:
            os.close(fd)


def admin_token() -> str:
    """首次原子生成密钥文件；已有文件永不被环境变量覆盖。"""
    path = admin_token_path()
    try:
        return read_saved_token(path)
    except FileNotFoundError:
        pass

    # 仅在没有保存的密钥文件时，才读取环境变量作为初始值。
    initial = os.environ.get("PWA_ADMIN_TOKEN", "").strip()
    if not initial:
        initial = secrets.token_urlsafe(32)
    if len(initial) > 512 or "\n" in initial or "\r" in initial:
        raise RuntimeError("PWA_ADMIN_TOKEN 必须是不含换行且长度不超过 512 的字符串")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".pwa-token-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(initial + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # 使用硬链接做原子“首次创建”：多进程同时启动也不会覆盖已有密钥。
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(tmp_name)
    return read_saved_token(path)


@dynamic_bp.record_once
def initialize_pwa_admin(state) -> None:
    # 注册蓝图时就写盘，避免第一次打开管理器时才创建文件。
    with state.app.app_context():
        admin_token()


def require_admin() -> None:
    supplied = request.headers.get("X-PWA-Admin-Token", "")
    if not hmac.compare_digest(supplied, admin_token()):
        abort(401, description="管理密钥无效")


@dynamic_bp.errorhandler(400)
@dynamic_bp.errorhandler(401)
@dynamic_bp.errorhandler(403)
@dynamic_bp.errorhandler(404)
@dynamic_bp.errorhandler(409)
@dynamic_bp.errorhandler(413)
@dynamic_bp.errorhandler(503)
def handle_error(err):
    # 管理 API 返回 JSON，其它路由仍正常返回 HTTP 错误
    if request.path.startswith(url_for("dynamic_pwa.api_tools")):
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
    if request.content_length and request.content_length > MAX_HTML_BYTES * 2:
        abort(413, description="提交内容太大")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="必须提交 JSON 对象")
    slug = data.get("slug", "")
    html = data.get("html", "")
    if not isinstance(slug, str) or not valid_slug(slug):
        abort(400, description="slug 只能使用小写字母、数字、连字符，长度 1～63")
    if not isinstance(html, str) or not html.strip():
        abort(400, description="HTML 不能为空")
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        abort(413, description="HTML 超过 2 MB 限制")
    return slug, html


@dynamic_bp.route("/manager", methods=["GET"])
def manager():
    # 管理页面本身没有密钥；密钥只能手动输入，且仅保存在前端内存中
    page_file = Path(__file__).with_name("pwa_manager.html")
    return no_store(send_file(page_file, mimetype="text/html"))


@dynamic_bp.route("/api/tools", methods=["GET"])
def api_tools():
    require_admin()
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
    require_admin()
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
    require_admin()
    html = read_html(slug)
    return no_store(jsonify(slug=slug, html=html, **metadata(html, slug)))


@dynamic_bp.route("/api/tools/<slug>", methods=["PUT"])
def api_update_tool(slug):
    require_admin()
    old = source_path(slug)
    submitted_slug, html = read_payload()
    if submitted_slug != slug:
        abort(400, description="编辑时不能修改 slug")
    atomic_write(old, html)
    return no_store(jsonify(message="已更新", url=url_for("dynamic_pwa.serve_tool", slug=slug)))


@dynamic_bp.route("/api/tools/<slug>", methods=["DELETE"])
def api_delete_tool(slug):
    require_admin()
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

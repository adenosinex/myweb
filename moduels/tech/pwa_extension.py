import os
import re
import json
import struct
import zlib
import shutil
from flask import Blueprint, request, current_app, make_response, jsonify, send_from_directory

# 蓝图定义，注册时设置 url_prefix，如 /pwa
dynamic_bp = Blueprint('dynamic_pwa', __name__, url_prefix='/pwa')

# ---------- 配置 ----------
def get_tools_dir():
    return os.path.join(current_app.root_path, 'db', 'dynamic_tools')

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

# ---------- 动态生成纯色 PNG 图标 ----------
def create_png(width, height, color):
    def chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    header = b'\x89PNG\r\n\x1a\n'
    ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    raw = b''
    for y in range(height):
        raw += b'\x00' + bytes(color) * width
    idat = chunk(b'IDAT', zlib.compress(raw))
    iend = chunk(b'IEND', b'')
    return header + ihdr + idat + iend

ICON_COLOR = (0x6c, 0x5c, 0xe7)  # 紫色

# ---------- 元数据提取 ----------
def parse_html_meta(html):
    meta = {
        'name': 'Dynamic PWA Tool',
        'short_name': 'Tool',
        'theme_color': '#000000',
        'background_color': '#ffffff',
        'description': '',
        'icon': None
    }
    title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = title_match.group(1).strip()
        if title:
            meta['name'] = title
            meta['short_name'] = title[:12]
    theme_match = re.search(r'<meta\s+name=["\']theme-color["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)
    if theme_match:
        meta['theme_color'] = theme_match.group(1)
    desc_match = re.search(r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)
    if desc_match:
        meta['description'] = desc_match.group(1)
    icon_match = re.search(r'<link\s+rel=["\'](?:shortcut\s+)?icon["\']\s+href=["\'](.*?)["\']', html, re.IGNORECASE)
    if not icon_match:
        icon_match = re.search(r'<link\s+rel=["\']apple-touch-icon["\']\s+href=["\'](.*?)["\']', html, re.IGNORECASE)
    if icon_match:
        icon = icon_match.group(1)
        # 如果是 data URI 则忽略，避免无法加载
        if icon.startswith('data:'):
            icon = None
        meta['icon'] = icon
    return meta

# ---------- 生成 manifest（使用相对路径，彻底修复问题）----------
def generate_manifest(slug, meta):
    # 始终包含方形默认图标（相对 manifest 文件路径）
    icons = [
        {
            "src": "../icon-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any maskable"
        },
        {
            "src": "../icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable"
        }
    ]

    # 如果用户提供了有效图标（非 data URI），追加到列表
    if meta['icon'] and not meta['icon'].startswith('data:'):
        icons.append({
            "src": meta['icon'],
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable"
        })

    # 截图（丰富安装 UI）
    screenshots = [
        {
            "src": "../screenshot-wide.png",
            "sizes": "1280x720",
            "type": "image/png",
            "form_factor": "wide",
            "label": "桌面版预览"
        },
        {
            "src": "../screenshot-narrow.png",
            "sizes": "750x1334",
            "type": "image/png",
            "form_factor": "narrow",
            "label": "移动版预览"
        }
    ]

    return {
        "name": meta['name'],
        "short_name": meta['short_name'],
        "description": meta['description'],
        "start_url": "https://apple.su7.dpdns.org/",               # 相对 manifest 文件本身，即为当前工具根目录
        "id": "./",                      # 与 start_url 一致
        "display": "standalone",
        "display_override": ["window-controls-overlay"],
        "theme_color": meta['theme_color'],
        "background_color": meta['background_color'],
        "icons": icons,
        "screenshots": screenshots
    }

# ---------- 构建完整的 PWA 页面 ----------
def build_pwa_page(slug, html):
    head_match = re.search(r'<head>(.*?)</head>', html, re.IGNORECASE | re.DOTALL)
    body_match = re.search(r'<body>(.*?)</body>', html, re.IGNORECASE | re.DOTALL)
    head_content = head_match.group(1) if head_match else ''
    body_content = body_match.group(1) if body_match else html

    meta = parse_html_meta(html)
    manifest = generate_manifest(slug, meta)

    page = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    {head_content}
    <link rel="manifest" href="./manifest.json">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="default">
    <meta name="apple-mobile-web-app-title" content="{manifest['name']}">
</head>
<body>
    {body_content}
    <!-- 自定义安装按钮 -->
    <button id="pwa-install-btn" style="display:none;position:fixed;bottom:20px;right:20px;z-index:9999;padding:12px 20px;background:#6c5ce7;color:white;border:none;border-radius:8px;font-weight:bold;cursor:pointer;">安装应用</button>
    <script>
        if ('serviceWorker' in navigator) {{
            window.addEventListener('load', () => {{
                navigator.serviceWorker.register('./sw.js', {{ scope: './' }})
                    .then(reg => console.log('SW registered for', reg.scope))
                    .catch(err => console.warn('SW registration failed:', err));
            }});
        }}
        let deferredPrompt;
        window.addEventListener('beforeinstallprompt', (e) => {{
            e.preventDefault();
            deferredPrompt = e;
            const btn = document.getElementById('pwa-install-btn');
            btn.style.display = 'block';
            btn.addEventListener('click', () => {{
                deferredPrompt.prompt();
                deferredPrompt.userChoice.then(() => {{
                    btn.style.display = 'none';
                    deferredPrompt = null;
                }});
            }});
        }});
    </script>
</body>
</html>'''
    return page

# ===================== 默认图标与截图路由 =====================
@dynamic_bp.route('/icon-192.png')
def icon_192():
    png_data = create_png(192, 192, ICON_COLOR)
    resp = make_response(png_data)
    resp.headers['Content-Type'] = 'image/png'
    return resp

@dynamic_bp.route('/icon-512.png')
def icon_512():
    png_data = create_png(512, 512, ICON_COLOR)
    resp = make_response(png_data)
    resp.headers['Content-Type'] = 'image/png'
    return resp

@dynamic_bp.route('/screenshot-wide.png')
def screenshot_wide():
    png_data = create_png(1280, 720, ICON_COLOR)
    resp = make_response(png_data)
    resp.headers['Content-Type'] = 'image/png'
    return resp

@dynamic_bp.route('/screenshot-narrow.png')
def screenshot_narrow():
    png_data = create_png(750, 1334, ICON_COLOR)
    resp = make_response(png_data)
    resp.headers['Content-Type'] = 'image/png'
    return resp

# ===================== 工具页面服务 =====================
@dynamic_bp.route('/<slug>/', strict_slashes=False)
def serve_tool(slug):
    tool_dir = os.path.join(get_tools_dir(), slug)
    index_path = os.path.join(tool_dir, 'index.html')
    if not os.path.exists(index_path):
        return "Tool not found", 404
    with open(index_path, 'r', encoding='utf-8') as f:
        html = f.read()
    return build_pwa_page(slug, html)

@dynamic_bp.route('/<slug>/manifest.json')
def serve_manifest(slug):
    """动态生成 manifest，解决所有 PWA 警告"""
    tool_dir = os.path.join(get_tools_dir(), slug)
    index_path = os.path.join(tool_dir, 'index.html')
    if not os.path.exists(index_path):
        return jsonify({}), 404
    with open(index_path, 'r', encoding='utf-8') as f:
        html = f.read()
    meta = parse_html_meta(html)
    manifest = generate_manifest(slug, meta)
    resp = make_response(json.dumps(manifest, ensure_ascii=False, indent=2))
    resp.headers['Content-Type'] = 'application/manifest+json'
    return resp

@dynamic_bp.route('/<slug>/static/<path:filename>')
def serve_static(slug, filename):
    tool_dir = os.path.join(get_tools_dir(), slug)
    return send_from_directory(tool_dir, filename)

@dynamic_bp.route('/<slug>/sw.js')
def service_worker(slug):
    sw_code = '''
const CACHE_NAME = 'dynamic-pwa-' + '%s' + '-v1';
self.addEventListener('install', event => { self.skipWaiting(); });
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
    ))
  );
  self.clients.claim();
});
self.addEventListener('fetch', event => {
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
''' % slug
    resp = make_response(sw_code)
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp

# ===================== API 路由（管理端） =====================
@dynamic_bp.route('/api/tools', methods=['GET'])
def api_list_tools():
    tools_dir = get_tools_dir()
    if not os.path.exists(tools_dir):
        return jsonify([])
    dirs = [d for d in os.listdir(tools_dir)
            if os.path.isdir(os.path.join(tools_dir, d)) and not d.startswith('.')]
    tools = []
    for slug in dirs:
        index_path = os.path.join(tools_dir, slug, 'index.html')
        name = slug
        if os.path.exists(index_path):
            try:
                with open(index_path, 'r') as f:
                    html = f.read()
                meta = parse_html_meta(html)
                name = meta.get('name', slug)
            except:
                pass
        tools.append({
            'slug': slug,
            'name': name,
            'url': f'{request.script_root}/{slug}/'
        })
    return jsonify(tools)

@dynamic_bp.route('/api/tools', methods=['POST'])
def api_create_tool():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400
    slug = data.get('slug', '').strip()
    html = data.get('html', '').strip()
    if not slug or not html:
        return jsonify({'error': 'slug and html are required'}), 400
    if not re.match(r'^[a-zA-Z0-9\-]+$', slug):
        return jsonify({'error': 'slug 只能包含字母、数字和连字符'}), 400

    tools_dir = get_tools_dir()
    tool_dir = os.path.join(tools_dir, slug)
    ensure_dir(tool_dir)

    with open(os.path.join(tool_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(html)

    # 保存一份静态 manifest 备份（实际服务使用动态生成）
    meta = parse_html_meta(html)
    manifest = generate_manifest(slug, meta)
    with open(os.path.join(tool_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return jsonify({
        'message': '工具创建成功',
        'url': f'{request.script_root}/{slug}/'
    }), 201

@dynamic_bp.route('/api/tools/<slug>', methods=['DELETE'])
def api_delete_tool(slug):
    tool_dir = os.path.join(get_tools_dir(), slug)
    if os.path.exists(tool_dir):
        shutil.rmtree(tool_dir)
        return jsonify({'message': 'deleted'}), 200
    return jsonify({'error': 'Tool not found'}), 404
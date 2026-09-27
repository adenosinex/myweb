# -*- coding: utf-8 -*-
"""
Python 文件网页执行器 —— 后端蓝图（完整版）

功能：
  - 列表 / 运行 / 停止 / 强杀端口
  - 端口占用即视为"正在运行"，不再重复启动
  - 每文件夹 _pys_ports.txt 保存手动端口
  - 主入口识别（app.py / main.py / ...）
  - 文件读取 / 保存
  - 透明端口转发 /pys/port/<port>/...
      · 支持子路径（HTML / JS / CSS / 图片 / JSON / 任意）
      · 注入 <base> 与 JS 补丁（fetch / XHR / sendBeacon / createElement）
      
      · Location 重写
  - Windows 隐藏子进程控制台窗口
  - 运行时上下文：cwd=脚本目录，PYTHONPATH=脚本目录+PY_ROOT
"""

import os, time, sys, re, subprocess, threading, uuid, json
from flask import Blueprint, jsonify, request, Response

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    print("警告: 未安装 psutil，端口占用检测功能将受限。请运行: pip install psutil")

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    print("警告: 未安装 requests，端口转发功能将不可用。请运行: pip install requests")


# =========================================================
# 配置
# =========================================================
PY_PREFIX = os.environ.get("PY_RUNNER_PREFIX", "/pys")
PY_ROOT = os.environ.get("PY_RUNNER_ROOT", "pys")
ALLOWED_ORIGINS = os.environ.get("PY_RUNNER_CORS", "*")

py_runner_bp = Blueprint("py_runner", __name__, url_prefix=PY_PREFIX)

PROCESS_POOL = {}
LOG_DIR = os.path.join(PY_ROOT, ".logs")
os.makedirs(LOG_DIR, exist_ok=True)

EDITABLE_EXT = (".py", ".txt", ".json", ".md", ".html", ".css", ".js",
                ".yaml", ".yml", ".ini", ".cfg", ".toml", ".env", ".sh", ".bat")
MAX_EDIT_SIZE = 2 * 1024 * 1024

PRIMARY_NAMES = {"app.py", "main.py", "server.py", "run.py", "__main__.py", "index.py"}
PORT_CFG_NAME = "_pys_ports.txt"


# =========================================================
# CORS
# =========================================================
@py_runner_bp.after_request
def _add_cors_headers(resp):
    origin = request.headers.get("Origin", "")
    allowed = [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]
    if ALLOWED_ORIGINS.strip() == "*":
        resp.headers["Access-Control-Allow-Origin"] = "*"
    elif origin and origin in allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Max-Age"] = "600"
    return resp


# =========================================================
# 端口配置（每文件夹一个 _pys_ports.txt）
# =========================================================
def _folder_cfg_path(folder):
    return os.path.join(folder, PORT_CFG_NAME)


def read_folder_ports(folder):
    cfg = {}
    path = _folder_cfg_path(folder)
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                try:
                    p = int(v.strip())
                    if 1 <= p <= 65535:
                        cfg[k.strip()] = p
                except ValueError:
                    pass
    except Exception:
        pass
    return cfg


def write_folder_ports(folder, cfg):
    path = _folder_cfg_path(folder)
    try:
        if not cfg:
            if os.path.exists(path):
                os.remove(path)
            return True
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("# _pys_ports.txt —— 手动端口配置（文件名=端口，每行一个）\n")
            for k in sorted(cfg.keys()):
                f.write(f"{k}={cfg[k]}\n")
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"写端口配置失败: {e}")
        return False


# =========================================================
# 工具
# =========================================================
def kill_process_tree(pid):
    if not HAS_PSUTIL:
        return False
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except Exception:
                pass
        parent.kill()
        return True
    except Exception:
        return False


def get_port_occupier(port_str):
    if not port_str or not HAS_PSUTIL:
        return None
    try:
        port = int(port_str)
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr.port == port and conn.status == 'LISTEN':
                try:
                    proc = psutil.Process(conn.pid)
                    return {"pid": conn.pid, "name": proc.name(),
                            "cmdline": " ".join(proc.cmdline())[:100]}
                except Exception:
                    return {"pid": conn.pid, "name": "Unknown", "cmdline": ""}
    except Exception:
        pass
    return None


def extract_static_ports(file_path):
    ports = set()
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception:
        return []

    try:
        import ast
        tree = ast.parse(content, filename=file_path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                else:
                    targets, value = [node.target], node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, int):
                    v = value.value
                    if 1024 <= v <= 65535:
                        for t in targets:
                            if isinstance(t, ast.Name) and 'port' in t.id.lower():
                                ports.add(v)
    except Exception:
        pass

    patterns = [
        r'\bport\s*=\s*(\d{2,5})', r'\bport\s*[:：]\s*(\d{2,5})',
        r'\b\w*port\w*\s*=\s*(\d{2,5})',
        r'\.run\s*\([^)]*?\bport\s*=\s*(\d{2,5})',
        r'http://[^\s:/]+:(\d{2,5})', r'127\.0\.0\.1:(\d{2,5})',
        r'localhost:(\d{2,5})', r'0\.0\.0\.0:(\d{2,5})',
    ]
    for p in patterns:
        try:
            for m in re.finditer(p, content, re.IGNORECASE):
                port = int(m.group(1))
                if 1024 <= port <= 65535:
                    ports.add(port)
        except Exception:
            pass
    return sorted(ports)


def get_port_cache_file():
    return os.path.join(LOG_DIR, '.port_cache.json')


def load_port_cache():
    try:
        with open(get_port_cache_file(), 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_port_cache(cache):
    try:
        tmp = get_port_cache_file() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, get_port_cache_file())
    except Exception:
        pass


def remember_port(file_rel, port):
    if not port:
        return
    try:
        cache = load_port_cache()
        cache[file_rel] = str(port)
        save_port_cache(cache)
    except Exception:
        pass


def get_candidate_ports(file_path, rel_path=None, manual_port=None):
    ports = set(extract_static_ports(file_path))
    if rel_path:
        cached = load_port_cache().get(rel_path)
        if cached:
            try:
                p = int(cached)
                if 1024 <= p <= 65535:
                    ports.add(p)
            except Exception:
                pass
    rest = sorted(p for p in ports if p != manual_port)
    if manual_port and 1 <= manual_port <= 65535:
        return [manual_port] + rest
    return rest


def find_occupied_candidate(file_path, rel_path=None, manual_port=None):
    for port in get_candidate_ports(file_path, rel_path, manual_port):
        info = get_port_occupier(str(port))
        if info:
            return str(port), info
    return None, None


def _safe_target(rel, must_exist=True):
    if not rel:
        return None
    root = os.path.abspath(PY_ROOT)
    target = os.path.normpath(os.path.join(root, rel))
    if not target.startswith(root):
        return None
    if must_exist and not os.path.isfile(target):
        return None
    return target


def _log_path_for(rel):
    safe_name = rel.replace('/', '_').replace('\\', '_')
    return os.path.join(LOG_DIR, f'{safe_name}.log')


def _read_text_with_encoding(path):
    for enc in ("utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read(), enc
        except UnicodeDecodeError:
            continue
        except Exception:
            break
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(), "utf-8"


# =========================================================
# 执行
# =========================================================
def run_script(task_id, target_full, args, log_file):
    script_dir = os.path.dirname(os.path.abspath(target_full))

    env = os.environ.copy()
    env.pop('WERKZEUG_SERVER_FD', None)
    env.pop('WERKZEUG_RUN_MAIN', None)
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'

    extra_paths = [script_dir]
    root_abs = os.path.abspath(PY_ROOT)
    if root_abs != script_dir:
        extra_paths.append(root_abs)
    old_pp = env.get("PYTHONPATH", "")
    if old_pp:
        extra_paths.append(old_pp)
    env["PYTHONPATH"] = os.pathsep.join(p for p in extra_paths if p)

    cmd = [sys.executable, target_full]
    if args:
        cmd.extend(args.split())

    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(f"[Runner] 启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"[Runner] Python: {sys.executable}\n")
            f.write(f"[Runner] 脚本: {target_full}\n")
            f.write(f"[Runner] cwd: {script_dir}\n")
            f.write(f"[Runner] PYTHONPATH: {env['PYTHONPATH']}\n")
            if args:
                f.write(f"[Runner] 参数: {args}\n")
            f.write("\n")
    except Exception:
        pass

    creationflags = 0
    startupinfo = None
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, encoding='utf-8', errors='replace',
            cwd=script_dir, env=env, bufsize=1,
            creationflags=creationflags,
            startupinfo=startupinfo,
            close_fds=True,
        )
        PROCESS_POOL[task_id]["process"] = proc
        PROCESS_POOL[task_id]["status"] = "running"

        first_output = True
        port_pattern = re.compile(
            r'(?:https?://[^:/]+:|Dashboard\s*:\s*http://[^:/]+:|'
            r'Proxy\s*:\s*http://[^:/]+:|port\s*[:=]\s*)(\d{2,5})',
            re.IGNORECASE
        )
        for line in proc.stdout:
            mode = 'w' if first_output else 'a'
            with open(log_file, mode, encoding='utf-8') as f:
                f.write(line)
            first_output = False
            if not PROCESS_POOL[task_id].get("port"):
                m = port_pattern.search(line)
                if m:
                    p = int(m.group(1))
                    if 1024 <= p <= 65535:
                        PROCESS_POOL[task_id]["port"] = str(p)
                        remember_port(PROCESS_POOL[task_id].get("file"), p)

        proc.wait()
        PROCESS_POOL[task_id]["status"] = "completed"
    except Exception as e:
        PROCESS_POOL[task_id]["status"] = "error"
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"\n[Error] {str(e)}")
        except Exception:
            pass


# =========================================================
# 端口转发（反向代理）—— 完全透明
# =========================================================
_REQ_STRIP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host", "content-length", "accept-encoding",
}
_RESP_STRIP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-length", "content-encoding",
}


_PROXY_PATCH_JS = r"""
(function(){
    if (window.__pys_proxy_patched) return;
    window.__pys_proxy_patched = true;
    var PREFIX = "__PROXY_PREFIX__";

    function rewrite(url) {
        if (typeof url !== 'string' || !url) return url;
        if (/^(?:[a-z]+:)?\/\//i.test(url)) return url;
        if (url === PREFIX || url.indexOf(PREFIX + '/') === 0) return url;
        if (url.charAt(0) === '/') return PREFIX + url;
        return url;
    }

    if (window.fetch) {
        var _fetch = window.fetch;
        window.fetch = function(input, init) {
            try {
                if (typeof input === 'string') input = rewrite(input);
                else if (input && typeof input === 'object' && input.url) {
                    var u = rewrite(input.url);
                    if (u !== input.url) input = new Request(u, input);
                }
            } catch (e) {}
            return _fetch.call(this, input, init);
        };
    }

    if (window.XMLHttpRequest && XMLHttpRequest.prototype.open) {
        var _open = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url) {
            try { url = rewrite(url); } catch (e) {}
            var rest = Array.prototype.slice.call(arguments, 2);
            return _open.apply(this, [method, url].concat(rest));
        };
    }

    if (navigator.sendBeacon) {
        var _beacon = navigator.sendBeacon.bind(navigator);
        navigator.sendBeacon = function(url, data) {
            try { url = rewrite(url); } catch (e) {}
            return _beacon(url, data);
        };
    }

    // 动态创建元素的 src / href
    (function(){
        var origCreate = document.createElement.bind(document);
        var REWRITE_TAGS = ['script','link','img','iframe','source','video','audio'];
        document.createElement = function(tag) {
            var el = origCreate(tag);
            var tagLower = (tag || '').toLowerCase();
            if (REWRITE_TAGS.indexOf(tagLower) === -1) return el;
            var origSet = el.setAttribute.bind(el);
            el.setAttribute = function(name, value) {
                if ((name === 'src' || name === 'href')
                    && typeof value === 'string'
                    && value.charAt(0) === '/'
                    && value.indexOf('//') !== 0
                    && value.indexOf(PREFIX) !== 0) {
                    value = PREFIX + value;
                }
                return origSet(name, value);
            };
            return el;
        };
    })();
})();
"""


_PROXY_FLOATING_BAR = r"""
<style>
#__pys_float_bar {
    position: fixed; right: 16px; bottom: 16px;
    z-index: 2147483646;
    background: rgba(37, 99, 235, 0.95); color: #fff;
    padding: 10px 14px; border-radius: 10px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.25);
    font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
    display: flex; align-items: center; gap: 8px;
    backdrop-filter: blur(6px);
    transition: opacity .2s, transform .2s;
}
#__pys_float_bar:hover { transform: translateY(-2px); }
#__pys_float_bar label { font-weight: 600; white-space: nowrap; }
#__pys_float_bar input {
    width: 90px; padding: 5px 8px;
    border: 1px solid rgba(255,255,255,0.4); border-radius: 5px;
    background: rgba(255,255,255,0.15); color: #fff;
    font-family: ui-monospace, monospace; font-size: 12px; outline: none;
}
#__pys_float_bar input::placeholder { color: rgba(255,255,255,0.6); }
#__pys_float_bar input:focus { border-color: #fff; background: rgba(255,255,255,0.25); }
#__pys_float_bar .__pys_open {
    padding: 5px 12px; border: none; border-radius: 5px;
    background: #fff; color: #2563eb; font-weight: 700; font-size: 12px; cursor: pointer;
}
#__pys_float_bar .__pys_open:hover { background: #f1f5f9; }
#__pys_float_bar .__pys_min {
    padding: 2px 6px; border: none; background: transparent;
    color: rgba(255,255,255,0.7); font-size: 14px; cursor: pointer;
}
#__pys_float_bar .__pys_min:hover { color: #fff; }
#__pys_float_bar.__pys_minimized {
    padding: 8px 12px; border-radius: 50%; width: 46px; height: 46px;
    justify-content: center; overflow: hidden;
}
#__pys_float_bar.__pys_minimized label,
#__pys_float_bar.__pys_minimized input,
#__pys_float_bar.__pys_minimized .__pys_open,
#__pys_float_bar.__pys_minimized .__pys_min { display: none; }
#__pys_float_bar.__pys_minimized::before {
    content: "⚡"; font-size: 22px; cursor: pointer;
}
</style>
 
<script>
(function(){
    if (window.__pys_float_bar_loaded) return;
    window.__pys_float_bar_loaded = true;
    window.__pys_open_port = function() {
        var input = document.getElementById('__pys_port_input');
        var port = parseInt(input.value, 10);
        if (!port || port < 1 || port > 65535) {
            alert('请输入 1-65535 之间的端口号'); input.focus(); return;
        }
        window.open('/pys/port/' + port + '/', '_blank');
        input.value = '';
    };
    window.__pys_toggle_min = function(e) {
        if (e) e.stopPropagation();
        var bar = document.getElementById('__pys_float_bar');
        var min = bar.classList.toggle('__pys_minimized');
        try { sessionStorage.setItem('__pys_float_min', min ? '1' : '0'); } catch (err) {}
    };
    try {
        if (sessionStorage.getItem('__pys_float_min') === '1') {
            document.getElementById('__pys_float_bar').classList.add('__pys_minimized');
        }
    } catch (err) {}
    document.getElementById('__pys_float_bar').addEventListener('click', function() {
        if (this.classList.contains('__pys_minimized')) {
            this.classList.remove('__pys_minimized');
            try { sessionStorage.setItem('__pys_float_min', '0'); } catch (err) {}
            setTimeout(function(){
                var inp = document.getElementById('__pys_port_input');
                if (inp) inp.focus();
            }, 30);
        }
    });
    setTimeout(function(){
        var inp = document.getElementById('__pys_port_input');
        if (inp) inp.addEventListener('keydown', function(e){
            if (e.key === 'Enter') { e.preventDefault(); window.__pys_open_port(); }
        });
    }, 0);
})();
</script>
"""


def _rewrite_location(loc, port):
    if not loc:
        return loc
    prefix = f"{PY_PREFIX}/port/{port}"
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
        for scheme in ("http", "https"):
            pre = f"{scheme}://{host}"
            if loc.startswith(pre):
                return prefix + loc[len(pre):]
    if loc.startswith("/") and not loc.startswith("//"):
        return prefix + loc
    return loc


def _rewrite_html(text, port):
    prefix = f"{PY_PREFIX}/port/{port}"

    def repl_attr(m):
        attr, quote, val = m.group(1), m.group(2), m.group(3)
        if val.startswith("//") or "://" in val:
            return m.group(0)
        if val.startswith(prefix):
            return m.group(0)
        return f"{attr}={quote}{prefix}{val}{quote}"

    text = re.sub(
        r'\b(href|src|action)\s*=\s*(["\'])(/[^"\']*)\2',
        repl_attr, text, flags=re.IGNORECASE,
    )

    # ---- 注入 head：base + JS 补丁 ----
    patch_js = _PROXY_PATCH_JS.replace("__PROXY_PREFIX__", prefix)
    patch_tag = f'<script data-pys-proxy="1">{patch_js}</script>'

    if "<base " in text.lower():
        head_inject = patch_tag
    else:
        head_inject = f'<base href="{prefix}/">' + patch_tag

    if re.search(r'<head[^>]*>', text, re.IGNORECASE):
        text = re.sub(r'(<head[^>]*>)', r'\1' + head_inject,
                      text, count=1, flags=re.IGNORECASE)
    else:
        text = head_inject + text

    # ---- 注入 body 尾部：浮动工具栏 ----
    if re.search(r'</body\s*>', text, re.IGNORECASE):
        text = re.sub(r'(</body\s*>)', _PROXY_FLOATING_BAR + r'\1',
                      text, count=1, flags=re.IGNORECASE)
    else:
        text = text + _PROXY_FLOATING_BAR

    return text


# ---------- 关键：三条路由，覆盖 /port/<p>、/port/<p>/、/port/<p>/<sub> ----------
@py_runner_bp.route(
    "/port/<int:port>",
    defaults={"subpath": ""},
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
@py_runner_bp.route(
    "/port/<int:port>/",
    defaults={"subpath": ""},
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
@py_runner_bp.route(
    "/port/<int:port>/<path:subpath>",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
def proxy_to_port(port, subpath):
    if not (1 <= port <= 65535):
        return jsonify({"ok": False, "error": "端口范围 1-65535"}), 400
    if not HAS_REQUESTS:
        return jsonify({
            "ok": False,
            "error": "服务器未安装 requests，请运行: pip install requests"
        }), 500

    target = f"http://127.0.0.1:{port}/"
    if subpath:
        target += subpath
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8", "replace")

    forward_headers = {}
    for k, v in request.headers.items():
        if k.lower() in _REQ_STRIP:
            continue
        forward_headers[k] = v
    forward_headers["Host"] = f"127.0.0.1:{port}"
    forward_headers["X-Forwarded-For"] = request.remote_addr or ""
    forward_headers["X-Forwarded-Proto"] = request.scheme
    forward_headers["X-Forwarded-Host"] = request.host
    forward_headers["X-Forwarded-Prefix"] = f"{PY_PREFIX}/port/{port}"

    body = request.get_data()

    try:
        resp = _requests.request(
            method=request.method, url=target,
            headers=forward_headers, data=body,
            cookies=request.cookies,
            allow_redirects=False, stream=True, timeout=60,
        )
    except _requests.RequestException as e:
        return jsonify({
            "ok": False,
            "error": f"无法连接 127.0.0.1:{port}：{e}"
        }), 502

    out_headers = []
    content_type = resp.headers.get("Content-Type", "")
    for k, v in resp.headers.items():
        lk = k.lower()
        if lk in _RESP_STRIP:
            continue
        if lk == "location":
            v = _rewrite_location(v, port)
        out_headers.append((k, v))

    is_html = "text/html" in content_type.lower()

    if is_html:
        try:
            raw = resp.content
        except Exception as e:
            return jsonify({"ok": False, "error": f"读取响应失败: {e}"}), 502
        enc = resp.encoding or "utf-8"
        try:
            text = raw.decode(enc, errors="replace")
        except Exception:
            text = raw.decode("utf-8", errors="replace")
        text = _rewrite_html(text, port)
        return Response(text, status=resp.status_code, headers=out_headers)

    def generate():
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            try:
                resp.close()
            except Exception:
                pass

    return Response(generate(), status=resp.status_code, headers=out_headers)


# =========================================================
# API：列表
# =========================================================
@py_runner_bp.route("/list")
def list_files():
    groups = {}
    root = os.path.abspath(PY_ROOT)
    if not os.path.exists(root):
        return jsonify({})

    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        py_files = [f for f in files if f.endswith('.py')]
        if not py_files:
            continue

        rel_dir = os.path.relpath(current, root)
        group_name = '根目录' if rel_dir == '.' else rel_dir.replace('\\', '/')
        groups.setdefault(group_name, [])

        folder_ports = read_folder_ports(current)
        has_primary_in_folder = any(f.lower() in PRIMARY_NAMES for f in py_files)

        for file in py_files:
            full_path = os.path.join(current, file)
            rel_path = os.path.relpath(full_path, root).replace('\\', '/')
            manual_port = folder_ports.get(file)
            is_primary = (not has_primary_in_folder) or (file.lower() in PRIMARY_NAMES)

            active_task = None
            running_port = None
            for tid, info in PROCESS_POOL.items():
                if (info.get('file') == rel_path
                        and info.get('status') in ('starting', 'running')):
                    active_task = tid
                    running_port = info.get('port')
                    break

            candidate_ports = get_candidate_ports(full_path, rel_path, manual_port)
            if manual_port:
                preferred_port = str(manual_port)
            elif candidate_ports:
                preferred_port = str(candidate_ports[0])
            else:
                preferred_port = None

            occupied_info = None
            occupied_port = None
            if not active_task:
                for p in candidate_ports:
                    info = get_port_occupier(str(p))
                    if info:
                        occupied_port = str(p)
                        occupied_info = info
                        break

            if active_task:
                is_running = True
                port = running_port or preferred_port
                status_name = 'running'
            elif occupied_info:
                is_running = True
                port = occupied_port
                status_name = 'external_running'
            else:
                is_running = False
                port = preferred_port
                status_name = 'idle'

            log_path = _log_path_for(rel_path)
            has_log = os.path.exists(log_path)

            groups[group_name].append({
                'id': rel_path,
                'file': file,
                'abs_path': full_path,
                'run_cwd': os.path.dirname(full_path),
                'manual_port': manual_port,
                'static_ports': [str(p) for p in extract_static_ports(full_path)],
                'candidate_ports': [str(p) for p in candidate_ports],
                'preferred_port': preferred_port,
                'running_port': running_port,
                'port': port,
                'active_task': active_task,
                'status': status_name,
                'is_running': is_running,
                'occupied': occupied_info is not None,
                'external_running': bool(occupied_info and not active_task),
                'occupier_info': occupied_info,
                'can_run': not is_running,
                'is_primary': is_primary,
                'has_log': has_log,
            })

    return jsonify(groups)


# =========================================================
# API：运行 / 停止
# =========================================================
@py_runner_bp.route("/run", methods=["POST"])
def run():
    data = request.json or {}
    rel = data.get('file', '')
    args = data.get('args', '')

    target_path = _safe_target(rel, must_exist=True)
    if not target_path:
        return jsonify({'error': '非法路径或文件不存在'}), 400

    folder = os.path.dirname(target_path)
    filename = os.path.basename(target_path)
    manual_port = read_folder_ports(folder).get(filename)

    occupied_port, occupied_info = find_occupied_candidate(
        target_path, rel, manual_port
    )
    if occupied_info:
        return jsonify({
            'already_running': True,
            'port': occupied_port,
            'occupier': occupied_info,
            'message': f'端口 {occupied_port} 已被占用，视为正在运行，未重复启动。'
        }), 409

    task_id = str(uuid.uuid4())
    log_file = _log_path_for(rel)

    PROCESS_POOL[task_id] = {
        'file': rel, 'status': 'starting',
        'log_file': log_file, 'port': None, 'process': None
    }

    t = threading.Thread(
        target=run_script,
        args=(task_id, target_path, args, log_file),
        daemon=True
    )
    t.start()

    return jsonify({'task': task_id})


@py_runner_bp.route("/status/<task>")
def status(task):
    info = PROCESS_POOL.get(task)
    if not info:
        return jsonify({"status": "not_found", "output": ""})

    output = ""
    log_file = info.get("log_file")
    if log_file and os.path.exists(log_file):
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            output = f.read()

    return jsonify({
        "status": info.get("status"),
        "output": output,
        "port": info.get("port")
    })


@py_runner_bp.route("/log")
def get_log():
    rel = (request.args.get("file") or "").strip()
    if not rel:
        return jsonify({"ok": False, "error": "缺少 file 参数"}), 400
    if not _safe_target(rel, must_exist=False):
        return jsonify({"ok": False, "error": "非法路径"}), 400

    log_file = _log_path_for(rel)
    if not os.path.exists(log_file):
        return jsonify({"ok": True, "exists": False, "output": ""})

    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            output = f.read()
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取日志失败: {e}"}), 500

    return jsonify({"ok": True, "exists": True, "file": rel,
                    "output": output, "log_path": log_file})


@py_runner_bp.route("/stop", methods=["POST"])
def stop():
    data = request.json or {}
    task_id = data.get("task")
    info = PROCESS_POOL.get(task_id)
    if info and info.get("process"):
        kill_process_tree(info["process"].pid)
        info["status"] = "stopped"
    return jsonify({"ok": True})


@py_runner_bp.route("/stop-port", methods=["POST"])
def stop_port():
    data = request.json or {}
    port = data.get("port")

    try:
        port = int(port)
        if not (1 <= port <= 65535):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "端口必须是 1-65535 之间的数字"}), 400

    occupier = get_port_occupier(str(port))
    if not occupier:
        return jsonify({
            "ok": False,
            "error": f"端口 {port} 当前没有监听进程",
            "port": port
        }), 404

    pid = occupier.get("pid")
    success = kill_process_tree(pid)

    if success:
        for tid, info in PROCESS_POOL.items():
            proc = info.get("process")
            same_pid = False
            try:
                same_pid = bool(proc and proc.pid == pid)
            except Exception:
                pass
            if same_pid or str(info.get("port")) == str(port):
                info["status"] = "stopped"

    time.sleep(0.15)
    released = get_port_occupier(str(port)) is None

    return jsonify({
        "ok": success and released,
        "port": port, "pid": pid,
        "process": occupier.get("name", ""),
        "released": released
    })


@py_runner_bp.route("/kill-port", methods=["POST"])
def kill_port():
    data = request.json or {}
    pid = data.get("pid")
    if pid:
        success = kill_process_tree(int(pid))
        return jsonify({"ok": success})
    return jsonify({"error": "no pid"}), 400


# =========================================================
# API：手动端口
# =========================================================
@py_runner_bp.route("/set-port", methods=["POST"])
def set_port():
    data = request.json or {}
    rel = data.get("file", "")
    port_raw = data.get("port", None)

    target_path = _safe_target(rel, must_exist=True)
    if not target_path:
        return jsonify({"ok": False, "error": "非法路径或文件不存在"}), 400

    folder = os.path.dirname(target_path)
    filename = os.path.basename(target_path)
    cfg = read_folder_ports(folder)

    if port_raw is None or port_raw == "":
        cfg.pop(filename, None)
        write_folder_ports(folder, cfg)
        return jsonify({"ok": True, "file": rel.replace("\\", "/"),
                        "manual_port": None, "message": "已清除手动端口"})

    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "端口必须是数字"}), 400

    if not (1 <= port <= 65535):
        return jsonify({"ok": False, "error": "端口必须在 1-65535 之间"}), 400

    cfg[filename] = port
    if not write_folder_ports(folder, cfg):
        return jsonify({"ok": False, "error": "写入端口配置失败"}), 500

    return jsonify({
        "ok": True, "file": rel.replace("\\", "/"),
        "manual_port": port, "cfg_file": _folder_cfg_path(folder),
        "message": f"已保存推荐端口 {port}"
    })


# =========================================================
# API：文件读取 / 保存
# =========================================================
@py_runner_bp.route("/read")
def read_file():
    rel = request.args.get("file", "")
    target_path = _safe_target(rel, must_exist=True)
    if not target_path:
        return jsonify({"ok": False, "error": "非法路径或文件不存在"}), 400

    try:
        size = os.path.getsize(target_path)
    except Exception:
        size = 0
    if size > MAX_EDIT_SIZE:
        return jsonify({"ok": False,
                        "error": f"文件过大（{size} 字节，上限 {MAX_EDIT_SIZE} 字节）"}), 413

    ext = os.path.splitext(target_path)[1].lower()
    if ext and ext not in EDITABLE_EXT:
        return jsonify({"ok": False, "error": f"暂不支持编辑 {ext} 类型的文件"}), 400

    try:
        content, encoding = _read_text_with_encoding(target_path)
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取失败: {e}"}), 500

    return jsonify({"ok": True, "file": rel.replace('\\', '/'),
                    "size": size, "encoding": encoding, "content": content})


@py_runner_bp.route("/save", methods=["POST"])
def save_file():
    data = request.json or {}
    rel = data.get("file", "")
    content = data.get("content", "")
    encoding = (data.get("encoding") or "utf-8").strip()

    target_path = _safe_target(rel, must_exist=True)
    if not target_path:
        return jsonify({"ok": False, "error": "非法路径或文件不存在"}), 400

    ext = os.path.splitext(target_path)[1].lower()
    if ext and ext not in EDITABLE_EXT:
        return jsonify({"ok": False, "error": f"暂不支持编辑 {ext} 类型的文件"}), 400

    if not isinstance(content, str):
        return jsonify({"ok": False, "error": "content 必须是字符串"}), 400

    if len(content.encode("utf-8")) > MAX_EDIT_SIZE:
        return jsonify({"ok": False, "error": "内容超出大小上限"}), 413

    if encoding not in ("utf-8", "gbk"):
        encoding = "utf-8"

    try:
        if os.path.exists(target_path):
            with open(target_path, "r", encoding=encoding, errors="replace") as f:
                original = f.read()
            with open(target_path + ".bak", "w", encoding=encoding) as f:
                f.write(original)
    except Exception:
        pass

    try:
        with open(target_path, "w", encoding=encoding) as f:
            f.write(content)
    except Exception as e:
        return jsonify({"ok": False, "error": f"保存失败: {e}"}), 500

    return jsonify({"ok": True, "file": rel.replace('\\', '/'),
                    "encoding": encoding,
                    "size": len(content.encode(encoding, errors="replace"))})
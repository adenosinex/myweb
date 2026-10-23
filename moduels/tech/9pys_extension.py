# -*- coding: utf-8 -*-
"""Python Runner Pro v3 — Flask blueprint.

外部进程识别：根据进程启动命令中的脚本路径或常见 module:app 参数匹配，
分离外部运行与无关端口占用；支持 PID、CPU、内存、运行时长实时监控。

依赖：pip install flask requests psutil
接入：from py_runner_blueprint import py_runner_bp; app.register_blueprint(py_runner_bp)
配置：PY_RUNNER_ROOT, PY_RUNNER_PREFIX, PY_RUNNER_TOKEN, PY_RUNNER_CORS。
警告：运行/停止脚本等同远程执行权限，公网环境必须配置访问鉴权。
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time 
from urllib.parse import urlsplit
import uuid

from flask import Blueprint, Response, jsonify, make_response, request, stream_with_context

try:
    import psutil
except ImportError:
    psutil = None
try:
    import requests
except ImportError:
    requests = None

PREFIX = '/' + os.getenv('PY_RUNNER_PREFIX', '/pys').strip('/')
ROOT = Path(os.getenv('PY_RUNNER_ROOT', 'pys')).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
LOG_DIR = ROOT / '.logs'
LOG_DIR.mkdir(exist_ok=True)
TOKEN = os.getenv('PY_RUNNER_TOKEN', '')
CORS = [x.strip() for x in os.getenv('PY_RUNNER_CORS', '').split(',') if x.strip()]
AUTH_COOKIE = 'py_runner_auth'
COOKIE_SECRET = secrets.token_bytes(32)
EDIT_EXT = {'.py', '.txt', '.json', '.md', '.html', '.css', '.js', '.ts',
            '.yaml', '.yml', '.ini', '.cfg', '.toml', '.env', '.sh', '.bat'}
PRIMARY = {'app.py', 'main.py', 'server.py', 'run.py', '__main__.py', 'index.py'}
MAX_EDIT = 2 * 1024 * 1024
MAX_LOG = 4 * 1024 * 1024
CHUNK_SIZE = 128 * 1024
PORT_CFG = '_pys_ports.txt'

py_runner_bp = Blueprint('py_runner', __name__, url_prefix=PREFIX)
TASKS: dict[str, dict] = {}
LOCK = threading.RLock()
PORT_CACHE_LOCK = threading.RLock()
PORT_SCAN_CACHE: dict[str, tuple] = {}
PORT_FILE = LOG_DIR / '.port_cache.json'
PROCESS_SAMPLES = {}  # PID + 进程启动时间 => 上次 CPU 时间与采样时刻
try:
    PORT_CACHE = json.loads(PORT_FILE.read_text(encoding='utf-8'))
    if not isinstance(PORT_CACHE, dict):
        PORT_CACHE = {}
except (OSError, ValueError):
    PORT_CACHE = {}


def _remember_port(rel, port):
    with PORT_CACHE_LOCK:
        PORT_CACHE[rel] = port
        temp = PORT_FILE.with_suffix('.tmp')
        try:
            temp.write_text(json.dumps(PORT_CACHE, ensure_ascii=False), encoding='utf-8')
            os.replace(temp, PORT_FILE)
        except OSError:
            pass
        finally:
            temp.unlink(missing_ok=True)


def _candidates(path, rel, manual):
    cached = _valid_port(PORT_CACHE.get(rel))
    ports = _static_ports(path)
    order = [manual, cached, *ports]
    return list(dict.fromkeys(p for p in order if p)), ports


def _prune_tasks():
    cutoff = time.time() - 24 * 3600
    with LOCK:
        for key, task in list(TASKS.items()):
            if task['status'] not in ('starting', 'running') and task.get('end_time', time.time()) < cutoff:
                TASKS.pop(key, None)


def result(ok=True, *, code=200, **kwargs):
    return jsonify(ok=ok, **kwargs), code


def _auth_signature():
    return hmac.new(COOKIE_SECRET, TOKEN.encode('utf-8'), hashlib.sha256).hexdigest()


def _authorized():
    if not TOKEN:
        return True
    supplied = request.headers.get('X-Py-Runner-Token', '')
    cookie = request.cookies.get(AUTH_COOKIE, '')
    return (bool(supplied) and hmac.compare_digest(supplied, TOKEN)) or (
        bool(cookie) and hmac.compare_digest(cookie, _auth_signature()))


@py_runner_bp.before_request
def _guard():
    if request.method == 'OPTIONS':
        return Response(status=204)
    if request.endpoint in ('py_runner.auth', 'py_runner.health'):
        return None
    if not _authorized():
        return result(False, code=401, error='需要访问密钥', auth_required=True)
    # 带 Cookie 的修改请求做同源检查，防止来自别的站点的表单请求。
    if TOKEN and request.method not in ('GET', 'HEAD'):
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            return result(False, code=403, error='禁止跨站修改请求')
    return None


@py_runner_bp.after_request
def _headers(response):
    origin = request.headers.get('Origin')
    if origin and origin in CORS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Vary'] = 'Origin'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Py-Runner-Token'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    if request.path.startswith(PREFIX + '/port/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@py_runner_bp.route('/health')
def health():
    return result(version='3.0', auth_required=bool(TOKEN), root_ready=ROOT.is_dir())


@py_runner_bp.route('/auth', methods=['GET', 'POST', 'DELETE'])
def auth():
    if request.method == 'GET':
        return result(authorized=_authorized(), required=bool(TOKEN))
    if request.method == 'DELETE':
        response = make_response(jsonify(ok=True))
        response.delete_cookie(AUTH_COOKIE, path=PREFIX)
        return response
    supplied = (request.get_json(silent=True) or {}).get('token', '')
    if TOKEN and (not isinstance(supplied, str) or not hmac.compare_digest(supplied, TOKEN)):
        return result(False, code=401, error='密钥错误')
    response = make_response(jsonify(ok=True))
    if TOKEN:
        response.set_cookie(AUTH_COOKIE, _auth_signature(), httponly=True,
                            secure=request.is_secure, samesite='Strict', path=PREFIX)
    return response


def _target(rel, *, suffix=None):
    if not isinstance(rel, str) or not rel or '\x00' in rel or '\\' in rel:
        return None
    try:
        file = (ROOT / rel).resolve(strict=True)
        if not file.is_file() or not file.is_relative_to(ROOT):
            return None
        if suffix and file.suffix.lower() != suffix:
            return None
        return file
    except (OSError, ValueError, RuntimeError):
        return None


def _python_environments(path):
    """Discover virtual environments in the script directory and its project ancestors."""
    folders = [path.parent]
    parent = path.parent
    while parent != ROOT and ROOT in parent.parents:
        parent = parent.parent
        folders.append(parent)
    found = []
    seen = set()
    for folder in folders:
        for name in ('.venv', 'venv', 'env'):
            env_dir = folder / name
            if not env_dir.is_dir() or env_dir.is_symlink():
                continue
            candidates = ([env_dir / 'Scripts' / 'python.exe'] if sys.platform == 'win32'
                          else [env_dir / 'bin' / 'python', env_dir / 'bin' / 'python3'])
            for exe in candidates:
                if exe.is_file() and (sys.platform == 'win32' or os.access(exe, os.X_OK)):
                    resolved = str(exe.absolute())
                    if resolved not in seen:
                        seen.add(resolved)
                        found.append({'id': resolved, 'label': str(env_dir.relative_to(ROOT)) +
                                      (' (当前目录)' if folder == path.parent else ' (上级目录)'),
                                      'python': resolved})
                    break
    return found


def _choose_python(path, selection):
    envs = _python_environments(path)
    if selection in (None, '', 'auto'):
        return (envs[0]['python'] if envs else sys.executable), envs
    if selection == 'system':
        return sys.executable, envs
    for env in envs:
        if selection == env['id']:
            return env['python'], envs
    raise ValueError('指定的虚拟环境不存在或已不可用，请刷新列表')


def _relative(path):
    return path.relative_to(ROOT).as_posix()


def _log_path(rel):
    # 使用散列避免不同子目录中的重名脚本覆盖日志。
    digest = hashlib.sha256(rel.encode('utf-8')).hexdigest()[:16]
    return LOG_DIR / (Path(rel).stem[:40] + '_' + digest + '.log')


def _read_ports(folder):
    ports = {}
    try:
        for line in (folder / PORT_CFG).read_text(encoding='utf-8').splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            name, raw = line.split('=', 1)
            port = _valid_port(raw.strip())
            if port:
                ports[name.strip()] = port
    except OSError:
        pass
    return ports


def _save_ports(folder, ports):
    path = folder / PORT_CFG
    if not ports:
        path.unlink(missing_ok=True)
        return
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text('# 文件名=手动端口\n' + ''.join(
            f'{k}={v}\n' for k, v in sorted(ports.items())), encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _valid_port(value):
    try:
        n = int(value)
        return n if 1 <= n <= 65535 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _static_ports(path):
    try:
        stat = path.stat()
        key = str(path)
        cached = PORT_SCAN_CACHE.get(key)
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
        source = path.read_text(encoding='utf-8', errors='replace')[:500_000]
        found = set()
        try:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if isinstance(value, ast.Constant) and type(value.value) is int:
                    for target in targets:
                        if isinstance(target, ast.Name) and 'port' in target.id.lower():
                            if 1024 <= value.value <= 65535:
                                found.add(value.value)
        except (SyntaxError, ValueError):
            pass
        for m in re.finditer(r'\b(?:\w*port\w*)\s*[:=]\s*["\']?(\d{2,5})\b|'
                             r'(?:127\.0\.0\.1|localhost|0\.0\.0\.0):([1-9]\d{3,4})',
                             source, re.IGNORECASE):
            num = _valid_port(m.group(1) or m.group(2))
            if num and num >= 1024:
                found.add(num)
        result_ports = sorted(found)
        PORT_SCAN_CACHE[key] = (stat.st_mtime_ns, stat.st_size, result_ports)
        return result_ports
    except OSError:
        return []


def _process_info(pid, *, with_ports=True):
    """获取实时进程信息；没有权限的属性返回 None，不将未知状态伪装为正常。"""
    if not psutil or not pid:
        return None
    try:
        proc = psutil.Process(int(pid))
        with proc.oneshot():
            created = proc.create_time()
            try:
                cmdline = proc.cmdline()
            except (psutil.Error, OSError):
                cmdline = []
            try:
                cwd = proc.cwd()
            except (psutil.Error, OSError):
                cwd = None
            try:
                exe = proc.exe()
            except (psutil.Error, OSError):
                exe = None
            try:
                username = proc.username()
            except (psutil.Error, OSError):
                username = None
            try:
                memory = proc.memory_info().rss
                memory_mb = round(memory / 1048576, 1)
            except (psutil.Error, OSError):
                memory_mb = None
            try:
                threads = proc.num_threads()
            except (psutil.Error, OSError):
                threads = None
            try:
                times = proc.cpu_times()
                cpu_total = times.user + times.system
            except (psutil.Error, OSError):
                cpu_total = None
            try:
                status = proc.status()
            except (psutil.Error, OSError):
                status = 'unknown'
            name = proc.name()
        now = time.monotonic()
        cpu_percent = None
        if cpu_total is not None:
            sample_key = (int(pid), created)
            with LOCK:
                previous = PROCESS_SAMPLES.get(sample_key)
                if previous and now > previous[0] + .2:
                    cpu_percent = round(max(0., (cpu_total - previous[1]) / (now - previous[0]) * 100), 1)
                PROCESS_SAMPLES[sample_key] = (now, cpu_total)
                if len(PROCESS_SAMPLES) > 512:
                    PROCESS_SAMPLES.clear()
        live_ports = []
        if with_ports:
            try:
                live_ports = sorted({c.laddr.port for c in proc.net_connections(kind='inet')
                                     if c.status == psutil.CONN_LISTEN and c.laddr})
            except (psutil.Error, OSError):
                pass
        return {
            'pid': int(pid), 'name': name, 'cmdline': cmdline[:40],
            'command': subprocess.list2cmdline(cmdline)[:4096] if cmdline else None,
            'cwd': cwd, 'exe': exe, 'username': username,
            'create_time': created, 'uptime_seconds': max(0, int(time.time() - created)),
            'memory_mb': memory_mb, 'threads': threads, 'cpu_percent': cpu_percent,
            'process_status': status, 'listen_ports': live_ports,
            'source': 'psutil', 'alive': proc.is_running() and status != psutil.STATUS_ZOMBIE,
        }
    except (psutil.Error, OSError, ValueError, TypeError):
        return None


def _occupiers(ports):
    """批量获得端口监听者，包括 PID 不可读时的占用标记。"""
    ports = {p for v in ports if (p := _valid_port(v))}
    found = {}
    if not ports:
        return found
    if psutil:
        try:
            connections = psutil.net_connections(kind='inet')
            for conn in connections:
                if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port in ports:
                    port = conn.laddr.port
                    # 优先保存含 PID 的条目。
                    if port not in found or (not found[port].get('pid') and conn.pid):
                        found[port] = {'pid': conn.pid, 'name': '未知进程', 'source': 'psutil'}
        except (psutil.Error, OSError):
            pass
    netstat = ''
    if sys.platform == 'win32' and ports - found.keys():
        try:
            netstat = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                                     text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
    for port in ports - found.keys():
        try:
            if sys.platform == 'win32':
                for line in netstat.splitlines():
                    cols = line.split()
                    if len(cols) >= 5 and cols[3].upper() == 'LISTENING' and cols[1].rsplit(':', 1)[-1] == str(port):
                        found[port] = {'pid': int(cols[4]), 'name': '未知进程', 'source': 'netstat'}
                        break
            else:
                output = subprocess.run(['lsof', '-nP', '-t', f'-iTCP:{port}', '-sTCP:LISTEN'],
                                        capture_output=True, text=True, timeout=3).stdout
                if output.strip():
                    found[port] = {'pid': int(output.splitlines()[0]), 'name': '未知进程', 'source': 'lsof'}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    pid_info = {}
    for port, info in found.items():
        pid = info.get('pid')
        if pid:
            if pid not in pid_info:
                pid_info[pid] = _process_info(pid)
            if pid_info[pid]:
                info.update(pid_info[pid])
        info['listening_port'] = port
    return found


def _referenced_scripts(cmdline, cwd):
    """从 python 脚本参数和常见 ASGI/WSGI module:app 参数解析已存在的 .py 文件。"""
    if not cmdline:
        return
    launchers = ('uvicorn', 'gunicorn', 'hypercorn', 'flask', 'waitress-serve')
    module_launcher = any(name in ' '.join(cmdline[:5]).lower() for name in launchers)
    for raw in cmdline:
        if not isinstance(raw, str):
            continue
        token = raw.strip().strip('"\'')
        if token.startswith('-') and '=' in token:
            token = token.split('=', 1)[1]
        candidates = []
        if token.lower().endswith('.py'):
            candidates.append(token)
        elif module_launcher and ':' in token:
            module = token.split(':', 1)[0]
            if re.fullmatch(r'[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*', module):
                candidates.append(module.replace('.', '/') + '.py')
        for text in candidates:
            try:
                candidate = Path(text)
                if not candidate.is_absolute():
                    if not cwd:
                        continue
                    candidate = Path(cwd) / candidate
                yield candidate.resolve(strict=True)
            except (OSError, ValueError, RuntimeError):
                continue


def _matches_script(cmdline, cwd, target):
    """匹配真实文件路径，不单凭端口判断运行进程属于哪个脚本。"""
    return any(candidate == target for candidate in _referenced_scripts(cmdline, cwd))


def _external_script_processes(paths):
    """一次系统进程遍历，按命令行引用脚本的真实路径进行归属识别。"""
    found = {}
    if not psutil or not paths:
        return found
    path_map = {path: rel for rel, path in paths.items()}
    with LOCK:
        managed_pids = {t['process'].pid for t in TASKS.values()
                        if t['status'] in ('starting', 'running') and t['process'].poll() is None}
    try:
        for process in psutil.process_iter(['pid', 'cmdline', 'cwd']):
            try:
                info = process.info
                pid = info.get('pid')
                if not pid or pid in managed_pids or pid == os.getpid():
                    continue
                cmdline = info.get('cmdline') or []
                cwd = info.get('cwd')
                matched = next((path_map[target] for target in _referenced_scripts(cmdline, cwd)
                                if target in path_map), None)
                if matched is not None:
                    details = _process_info(pid)
                    if details and details['alive']:
                        details['matched_by'] = 'script_reference'
                        found.setdefault(matched, []).append(details)
            except (psutil.Error, OSError, ValueError):
                continue
    except (psutil.Error, OSError):
        pass
    return found


def _select_external(rel, matches, occupiers, candidates):
    """优先选取监听了脚本推荐端口的匹配进程。"""
    options = matches.get(rel) or []
    if not options:
        return None
    port_pids = {occupiers[p]['pid'] for p in candidates if p in occupiers and occupiers[p].get('pid')}
    return next((info for info in options if info['pid'] in port_pids), options[0])


def _current_process_file_info(path):
    """单脚本实时查询：供详情窗口按需更新。"""
    rel = _relative(path)
    manual = _read_ports(path.parent).get(path.name)
    candidates, _ = _candidates(path, rel, manual)
    occupiers = _occupiers(candidates)
    matches = _external_script_processes({rel: path})
    tid, active = _active_task(rel)
    ext = _select_external(rel, matches, occupiers, candidates) if not active else None
    busy = next(((p, occupiers[p]) for p in candidates if p in occupiers), None)
    info = _process_info(active['process'].pid) if active else ext
    listened = info.get('listen_ports', []) if info else []
    running_port = next((p for p in candidates if p in listened), None) or (listened[0] if listened else None)
    return {'file': rel, 'managed': bool(active), 'active_task': tid, 'external_running': bool(ext),
            'port_occupied': bool(busy), 'occupied_port': busy[0] if busy else None,
            'port_conflict': bool(busy and
                (not info or not busy[1].get('pid') or busy[1]['pid'] != info.get('pid'))),
            'status': 'running' if active else ('external_running' if ext else ('occupied' if busy else 'idle')),
            'is_running': bool(active or ext), 'running_port': running_port,
            'port': running_port or (busy[0] if busy else (candidates[0] if candidates else None)),
            'process_info': info, 'port_owner': busy[1] if busy else None,
            'candidate_ports': candidates,
            'can_stop_external': bool(ext and ext.get('matched_by') == 'script_reference'),
            'detected_at': time.time(),
            'note': '外部进程的标准输出无法事后附加；只能查看可访问的历史日志文件。' if ext else None}


def _active_task(rel):
    with LOCK:
        for tid, task in TASKS.items():
            if task['file'] == rel and task['status'] in ('starting', 'running'):
                process = task.get('process')
                if process and process.poll() is not None:
                    task['status'] = 'completed' if process.returncode == 0 else 'error'
                else:
                    return tid, task
    return None, None


def _terminate_tree(pid):
    if not pid or pid in (0, 1, os.getpid()):
        return False
    # 避免通过端口管理误杀 Flask 本身或其祖先进程。
    if psutil:
        try:
            ancestor_pids = {parent.pid for parent in psutil.Process(os.getpid()).parents()}
            if int(pid) in ancestor_pids:
                return False
        except (psutil.Error, OSError):
            pass
    if psutil:
        try:
            p = psutil.Process(pid)
            children = p.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            p.terminate()
            _, alive = psutil.wait_procs(children + [p], timeout=1.5)
            for item in alive:
                try:
                    item.kill()
                except psutil.Error:
                    pass
            return True
        except psutil.NoSuchProcess:
            return True
        except psutil.Error:
            pass
    try:
        if sys.platform == 'win32':
            return subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                                  capture_output=True, timeout=5).returncode == 0
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        return False


def _append_log(path, data):
    with open(path, 'ab') as file:
        file.write(data)
    try:
        if path.stat().st_size > MAX_LOG:
            with open(path, 'rb') as file:
                file.seek(-MAX_LOG // 2, os.SEEK_END)
                tail = file.read()
            with open(path, 'wb') as file:
                file.write('[Runner] 早期日志已截断\n'.encode('utf-8') + tail)
    except OSError:
        pass


def _capture(tid):
    task = TASKS[tid]
    proc = task['process']
    try:
        for line in iter(proc.stdout.readline, b''):
            _append_log(task['log_file'], line)
            if not task.get('port'):
                text = line.decode('utf-8', 'replace')
                match = re.search(r'(?:https?://[^\s/:]+:|\bport\s*[:=]\s*)(\d{2,5})', text, re.I)
                if match:
                    port = _valid_port(match.group(1))
                    if port and port >= 1024:
                        task['port'] = port
                        _remember_port(task['file'], port)
        code = proc.wait()
        with LOCK:
            if task['status'] != 'stopped':
                task['status'] = 'completed' if code == 0 else 'error'
            task['returncode'] = code
            task['end_time'] = time.time()
        _append_log(task['log_file'], f'\n[Runner] 进程退出，代码 {code}\n'.encode())
    except Exception as exc:
        with LOCK:
            task['status'] = 'error'
            task['end_time'] = time.time()
        _append_log(task['log_file'], f'\n[Runner] 日志读取异常：{exc}\n'.encode())
    finally:
        if proc.stdout:
            proc.stdout.close()


def _log_chunk(path, offset=None):
    try:
        size = path.stat().st_size
        if offset is None:
            offset = max(0, size - CHUNK_SIZE)
        reset = offset > size or offset < 0
        if reset:
            offset = max(0, size - CHUNK_SIZE)
        with open(path, 'rb') as file:
            file.seek(offset)
            raw = file.read(CHUNK_SIZE)
        return {'output': raw.decode('utf-8', errors='replace'),
                'offset': offset + len(raw), 'reset': reset,
                'has_more': offset + len(raw) < size}
    except OSError:
        return {'output': '', 'offset': 0, 'reset': True, 'has_more': False}


def _offset():
    raw = request.args.get('offset')
    try:
        return max(0, int(raw)) if raw is not None else None
    except ValueError:
        return None


@py_runner_bp.get('/list')
def list_files():
    _prune_tasks()
    entries = []
    all_ports = set()
    paths = {}
    try:
        for directory, dirs, files in os.walk(ROOT):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not
                             (Path(directory) / d).is_symlink())
            py_files = sorted(f for f in files if f.lower().endswith('.py'))
            if not py_files:
                continue
            folder = Path(directory)
            configured = _read_ports(folder)
            primaries = any(f.lower() in PRIMARY for f in py_files)
            for filename in py_files:
                path = _target(_relative(folder / filename), suffix='.py')
                if path is None:
                    continue
                rel = _relative(path)
                paths[rel] = path
                manual = configured.get(filename)
                candidates, ports = _candidates(path, rel, manual)
                all_ports.update(candidates)
                tid, active = _active_task(rel)
                entries.append((rel, filename, folder, manual, candidates, ports,
                                tid, active, (not primaries or filename.lower() in PRIMARY)))
    except OSError:
        pass
    occupiers = _occupiers(all_ports)
    external_matches = _external_script_processes(paths)
    groups = {}
    for rel, name, folder, manual, candidates, ports, tid, active, primary in entries:
        busy = next(((p, occupiers[p]) for p in candidates if p in occupiers), None)
        external = _select_external(rel, external_matches, occupiers, candidates) if not active else None
        info = _process_info(active['process'].pid) if active else external
        live_ports = info.get('listen_ports', []) if info else []
        running_port = (active.get('port') if active else None) or \
                       next((p for p in candidates if p in live_ports), None) or \
                       (live_ports[0] if live_ports else None)
        port = running_port or (busy[0] if busy else None) or (candidates[0] if candidates else None)
        conflict = bool(busy and (not info or not busy[1].get('pid') or
                                   busy[1]['pid'] != info.get('pid')))
        group = folder.relative_to(ROOT).as_posix()
        if group == '.':
            group = '根目录'
        groups.setdefault(group, []).append({
            'id': rel, 'file': name, 'run_cwd': str(folder),
            'port': port, 'manual_port': manual, 'static_ports': ports,
            'candidate_ports': candidates, 'preferred_port': candidates[0] if candidates else None,
            'running_port': running_port, 'active_task': tid,
            'status': 'running' if active else ('external_running' if external else ('occupied' if busy else 'idle')),
            'is_running': bool(active or external), 'occupied': bool(busy),
            'port_conflict': conflict, 'external_running': bool(external),
            'occupied_port': busy[0] if busy else None,
            'occupier_info': busy[1] if busy else None,
            'process_info': info, 'port_owner': busy[1] if busy else None,
            'can_stop_external': bool(external and external.get('matched_by') == 'script_reference'),
            'can_run': not bool(active or external or busy), 'is_primary': primary,
            'has_log': _log_path(rel).exists(),
            'python_envs': _python_environments(paths[rel]),
        })
    return jsonify(groups)


@py_runner_bp.get('/process-info')
def process_info():
    path = _target(request.args.get('file'), suffix='.py')
    if path is None:
        return result(False, code=400, error='文件不存在或路径非法')
    return result(**_current_process_file_info(path))


@py_runner_bp.post('/stop-external')
def stop_external():
    """只停止确认归属于给定脚本的外部进程，不按文件名模糊查杀。"""
    data = request.get_json(silent=True) or {}
    path = _target(data.get('file'), suffix='.py')
    if path is None:
        return result(False, code=400, error='文件不存在或路径非法')
    try:
        pid = int(data.get('pid'))
        created = float(data.get('create_time'))
    except (TypeError, ValueError):
        return result(False, code=400, error='缺少 PID 或进程启动时间，请先刷新状态')
    if not data.get('confirm'):
        return result(False, code=400, error='请明确确认停止外部进程')
    if not psutil:
        return result(False, code=503, error='缺少 psutil，无法安全验证外部进程身份')
    with LOCK:
        for task in TASKS.values():
            if task['status'] in ('starting', 'running') and task['process'].pid == pid:
                return result(False, code=409, error='该进程由本工具管理，请使用普通停止按钮')
    info = _process_info(pid)
    if not info or abs(info['create_time'] - created) > .01:
        return result(False, code=409, error='进程已退出或 PID 被复用，请刷新状态')
    if not _matches_script(info['cmdline'], info['cwd'], path):
        return result(False, code=409, error='该 PID 已不再与脚本匹配，拒绝结束进程')
    if not _terminate_tree(pid):
        return result(False, code=500, error='进程停止失败（可能权限不足）')
    return result(pid=pid, file=_relative(path), message='已发送停止外部进程指令')


@py_runner_bp.post('/run')
def run():
    data = request.get_json(silent=True) or {}
    path = _target(data.get('file'), suffix='.py')
    if path is None:
        return result(False, code=400, error='文件不存在或路径非法')
    args = data.get('args') or ''
    if not isinstance(args, str) or len(args) > 8192:
        return result(False, code=400, error='启动参数必须是字符串，且不能超过 8192 字符')
    try:
        argv = shlex.split(args, posix=sys.platform != 'win32')
    except ValueError as exc:
        return result(False, code=400, error=f'启动参数不完整：{exc}')
    rel = _relative(path)
    try:
        python_exe, available_envs = _choose_python(path, data.get('python_env', 'auto'))
    except ValueError as exc:
        return result(False, code=400, error=str(exc))
    _prune_tasks()
    with LOCK:
        tid, _ = _active_task(rel)
        if tid:
            return result(False, code=409, already_running=True, error='该脚本已在运行')
        existing = _external_script_processes({rel: path}).get(rel)
        if existing:
            return result(False, code=409, already_running=True, external_running=True,
                          pid=existing[0]['pid'], error='检测到该脚本已由外部进程启动，未重复运行')
        manual = _read_ports(path.parent).get(path.name)
        candidates, _ = _candidates(path, rel, manual)
        occupied = _occupiers(candidates)
        if occupied:
            port = next(p for p in candidates if p in occupied)
            return result(False, code=409, already_running=True, port=port,
                          occupier=occupied[port], error=f'端口 {port} 已被占用，未重复运行')
        env = os.environ.copy()
        env.pop('WERKZEUG_SERVER_FD', None)
        env.pop('WERKZEUG_RUN_MAIN', None)
        env['PYTHONUNBUFFERED'] = '1'
        if python_exe != sys.executable:
            env_dir = str(Path(python_exe).parent.parent)
            env['VIRTUAL_ENV'] = env_dir
            env['PATH'] = str(Path(python_exe).parent) + os.pathsep + env.get('PATH', '')
            env.pop('PYTHONHOME', None)
        else:
            env.pop('VIRTUAL_ENV', None)
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONPATH'] = os.pathsep.join(filter(None,
            (str(path.parent), str(ROOT), env.get('PYTHONPATH', ''))))
        kwargs = {}
        if sys.platform == 'win32':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs['start_new_session'] = True
        try:
            proc = subprocess.Popen([python_exe, str(path), *argv], cwd=path.parent,
                                    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, bufsize=0, **kwargs)
        except OSError as exc:
            return result(False, code=500, error=f'进程启动失败：{exc}')
        tid = uuid.uuid4().hex
        log_path = _log_path(rel)
        header = (f'[Runner] 启动时间：{time.strftime("%Y-%m-%d %H:%M:%S")}\n'
                  f'[Runner] 脚本：{rel}\n[Runner] PID：{proc.pid}\n'
                  f'[Runner] Python：{python_exe}\n'
                  f'[Runner] 启动参数：{args}\n\n').encode('utf-8')
        log_path.write_bytes(header)
        TASKS[tid] = {'file': rel, 'status': 'running', 'port': None,
                      'process': proc, 'python_exe': python_exe, 'log_file': log_path, 'start_time': time.time()}
        threading.Thread(target=_capture, args=(tid,), daemon=True).start()
    return result(task=tid, pid=proc.pid, python=python_exe)


@py_runner_bp.get('/status/<task_id>')
def status(task_id):
    task = TASKS.get(task_id)
    if task is None:
        return result(False, code=404, status='not_found', error='任务不存在')
    payload = _log_chunk(task['log_file'], _offset())
    return result(status=task['status'], port=task.get('port'),
                  returncode=task.get('returncode'), **payload)


@py_runner_bp.get('/log')
def get_log():
    path = _target(request.args.get('file'), suffix='.py')
    if path is None:
        return result(False, code=400, error='文件不存在或路径非法')
    log_path = _log_path(_relative(path))
    return result(exists=log_path.exists(), **_log_chunk(log_path, _offset()))


@py_runner_bp.post('/stop')
def stop():
    tid = (request.get_json(silent=True) or {}).get('task')
    with LOCK:
        task = TASKS.get(tid)
        if not task:
            return result(False, code=404, error='任务不存在或服务已重启')
        proc = task['process']
        if proc.poll() is not None:
            return result(False, code=409, error='进程已经结束')
        success = _terminate_tree(proc.pid)
        if success:
            task['status'] = 'stopped'
    return result(success, code=200 if success else 500,
                  message='停止信号已发送' if success else '停止失败')


@py_runner_bp.route('/stop-port', methods=['POST'])
@py_runner_bp.route('/kill-port', methods=['POST'])
def stop_port():
    data = request.get_json(silent=True) or {}
    port = _valid_port(data.get('port'))
    if not port:
        return result(False, code=400, error='请输入 1-65535 的有效端口')
    info = _occupiers([port]).get(port)
    if not info:
        return result(False, code=404, error='端口未被监听', released=True)
    pid = info.get('pid')
    if not pid:
        return result(False, code=403, error='无法获得占用进程 PID，可能需要更高权限')
    expected_pid = data.get('pid')
    if expected_pid is not None and str(pid) != str(expected_pid):
        return result(False, code=409, error='端口占用进程已变化，请刷新后重试')
    expected_create = data.get('create_time')
    if expected_create is not None:
        try:
            same = info.get('create_time') is not None and abs(float(info['create_time']) - float(expected_create)) <= .01
        except (TypeError, ValueError):
            same = False
        if not same:
            return result(False, code=409, error='端口占用进程已经变化，请刷新后重试')
    success = _terminate_tree(pid)
    with LOCK:
        for task in TASKS.values():
            if task['process'].pid == pid:
                task['status'] = 'stopped'
    time.sleep(.15)
    released = port not in _occupiers([port])
    return result(success and released, code=200 if success and released else 409,
                  port=port, pid=pid, process=info.get('name'), released=released,
                  message='端口已释放' if released else '停止指令已发送，但端口仍被占用')


@py_runner_bp.post('/set-port')
def set_port():
    data = request.get_json(silent=True) or {}
    path = _target(data.get('file'), suffix='.py')
    if path is None:
        return result(False, code=400, error='文件不存在或路径非法')
    raw = data.get('port')
    port = _valid_port(raw) if raw not in ('', None) else None
    if raw not in ('', None) and not port:
        return result(False, code=400, error='端口范围必须为 1-65535')
    try:
        with LOCK:
            ports = _read_ports(path.parent)
            if port is None:
                ports.pop(path.name, None)
            else:
                ports[path.name] = port
            _save_ports(path.parent, ports)
        return result(file=_relative(path), manual_port=port)
    except OSError as exc:
        return result(False, code=500, error=f'保存端口设置失败：{exc}')


def _encoding_read(path):
    raw = path.read_bytes()
    for name in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            return raw.decode(name), name
        except UnicodeDecodeError:
            pass
    raise ValueError('文件编码不受支持（支持 UTF-8 / UTF-8 BOM / GBK）')


def _etag(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@py_runner_bp.get('/read')
def read_file():
    path = _target(request.args.get('file'))
    if path is None:
        return result(False, code=400, error='文件不存在或路径非法')
    if path.suffix.lower() not in EDIT_EXT or path.stat().st_size > MAX_EDIT:
        return result(False, code=400, error='文件类型不支持，或文件超过 2 MB')
    try:
        content, encoding = _encoding_read(path)
        return result(file=_relative(path), content=content, encoding=encoding,
                      size=path.stat().st_size, etag=_etag(path))
    except (OSError, ValueError) as exc:
        return result(False, code=500, error=f'读取文件失败：{exc}')


@py_runner_bp.post('/save')
def save_file():
    data = request.get_json(silent=True) or {}
    path = _target(data.get('file'))
    content = data.get('content')
    encoding = data.get('encoding', 'utf-8')
    if path is None or path.suffix.lower() not in EDIT_EXT:
        return result(False, code=400, error='文件不存在或类型不支持')
    if not isinstance(content, str) or encoding not in ('utf-8', 'utf-8-sig', 'gbk'):
        return result(False, code=400, error='内容或编码不受支持')
    try:
        raw = content.encode(encoding)
    except UnicodeEncodeError:
        return result(False, code=400, error=f'内容无法使用 {encoding} 编码')
    if len(raw) > MAX_EDIT:
        return result(False, code=413, error='文件内容不能超过 2 MB')
    with LOCK:
        try:
            original = path.read_bytes()
            original_hash = hashlib.sha256(original).hexdigest()
            if data.get('etag') and data.get('etag') != original_hash and not data.get('force'):
                return result(False, code=409, conflict=True,
                              error='文件已被其他程序修改。请重新加载或确认覆盖。')
            tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                tmp.write_bytes(raw)
                os.chmod(tmp, path.stat().st_mode)
                (path.parent / (path.name + '.bak')).write_bytes(original)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
            return result(size=len(raw), encoding=encoding, etag=hashlib.sha256(raw).hexdigest())
        except OSError as exc:
            return result(False, code=500, error=f'保存失败：{exc}')


# 同源代理：不支持 WebSocket；仅转发到服务所在机器的 127.0.0.1。
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailers', 'transfer-encoding', 'upgrade', 'host',
               'content-length', 'content-encoding'}


def _rewrite_location(location, port):
    prefix = f'{PREFIX}/port/{port}'
    if location.startswith('/') and not location.startswith('//'):
        return location if location.startswith(prefix + '/') else prefix + location
    parsed = urlsplit(location)
    if parsed.scheme in ('http', 'https') and parsed.hostname in ('localhost', '127.0.0.1', '::1') and parsed.port == port:
        suffix = parsed.path or '/'
        return prefix + suffix + (('?' + parsed.query) if parsed.query else '') + \
               (('#' + parsed.fragment) if parsed.fragment else '')
    return location


_PROXY_SCRIPT = r'''<script data-pys-proxy="1">(function(){
const P='__P__';
function fix(u){if(typeof u!=='string'||!u.startsWith('/')||u.startsWith('//')||u===P||u.startsWith(P+'/'))return u;return P+u;}
const F=window.fetch;if(F)window.fetch=function(input,init){try{if(typeof input==='string')input=fix(input);else if(input instanceof Request){const u=new URL(input.url);if(u.origin===location.origin){const p=fix(u.pathname);if(p!==u.pathname){u.pathname=p;input=new Request(u,input);}}}}catch(e){}return F.call(this,input,init);};
const O=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u,...r){return O.call(this,m,fix(u),...r);};
if(navigator.sendBeacon){const B=navigator.sendBeacon.bind(navigator);navigator.sendBeacon=(u,d)=>B(fix(u),d);}
})();</script>'''


def _rewrite_html(content, port):
    prefix = f'{PREFIX}/port/{port}'
    # 只重写绝对根路径；相对资源由 base 标签处理。
    attr = re.compile(r'''\b(href|src|action|poster)\s*=\s*(["'])(/[^"']*)\2''', re.I)
    content = attr.sub(lambda m: m.group(0) if m.group(3).startswith('//') or
                       m.group(3).startswith(prefix + '/') else
                       f'{m.group(1)}={m.group(2)}{prefix}{m.group(3)}{m.group(2)}', content)
    script = _PROXY_SCRIPT.replace('__P__', prefix)
    base = '' if re.search(r'<base\s', content, re.I) else f'<base href="{prefix}/">'
    addition = base + script
    if re.search(r'<head\b[^>]*>', content, re.I):
        content = re.sub(r'(<head\b[^>]*>)', lambda m: m.group(1) + addition, content, count=1, flags=re.I)
    else:
        content = addition + content
    return content


@py_runner_bp.route('/port/<int:port>', defaults={'subpath': ''}, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'])
@py_runner_bp.route('/port/<int:port>/', defaults={'subpath': ''}, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'])
@py_runner_bp.route('/port/<int:port>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'])
def proxy_to_port(port, subpath):
    if not _valid_port(port):
        return Response('端口范围不正确', status=400)
    if requests is None:
        return Response('未安装 requests：pip install requests', status=503)
    url = f'http://127.0.0.1:{port}/' + subpath
    if request.query_string:
        url += '?' + request.query_string.decode('latin1')
    headers = {k: v for k, v in request.headers if k.lower() not in HOP_HEADERS and
               k.lower() not in ('cookie', 'authorization', 'x-py-runner-token', 'accept-encoding')}
    headers['Accept-Encoding'] = 'identity'
    headers['X-Forwarded-Proto'] = request.scheme
    headers['X-Forwarded-Host'] = request.host
    headers['X-Forwarded-Prefix'] = f'{PREFIX}/port/{port}'
    cookies = {k: v for k, v in request.cookies.items() if k != AUTH_COOKIE}
    try:
        upstream = requests.request(request.method, url, data=request.get_data(),
                                    headers=headers, cookies=cookies, stream=True,
                                    allow_redirects=False, timeout=(5, 90))
    except requests.RequestException as exc:
        return Response(f'连接 127.0.0.1:{port} 失败：{exc}', status=502,
                        content_type='text/plain; charset=utf-8')
    response_headers = []
    for k, value in upstream.headers.items():
        lk = k.lower()
        if lk in HOP_HEADERS or lk in ('content-security-policy', 'content-length'):
            if lk == 'content-security-policy' and 'text/html' not in upstream.headers.get('Content-Type', '').lower():
                response_headers.append((k, value))
            continue
        if lk == 'location':
            value = _rewrite_location(value, port)
        if lk == 'set-cookie':
            continue
        response_headers.append((k, value))
    try:
        for cookie in upstream.raw.headers.getlist('Set-Cookie'):
            cookie = re.sub(r'(?i)(?:;\s*Path=/)(?=;|$)', f'; Path={PREFIX}/port/{port}/', cookie)
            response_headers.append(('Set-Cookie', cookie))
    except AttributeError:
        pass
    is_html = 'text/html' in upstream.headers.get('Content-Type', '').lower()
    if is_html and request.method != 'HEAD':
        try:
            raw = upstream.content
            if len(raw) > 4 * 1024 * 1024:
                return Response('页面过大，无法注入代理兼容脚本', status=502)
            upstream.encoding = upstream.encoding or 'utf-8'
            html = _rewrite_html(raw.decode(upstream.encoding, 'replace'), port)
            return Response(html.encode('utf-8'), status=upstream.status_code,
                            headers=[(k, v) for k, v in response_headers if k.lower() != 'content-type'],
                            content_type='text/html; charset=utf-8')
        finally:
            upstream.close()

    @stream_with_context
    def stream():
        try:
            for part in upstream.iter_content(chunk_size=32768):
                if part:
                    yield part
        finally:
            upstream.close()
    return Response(stream(), status=upstream.status_code, headers=response_headers)

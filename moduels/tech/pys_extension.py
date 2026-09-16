# -*- coding: utf-8 -*-
"""
Python 文件网页执行器 (交互优化版)
修复日志闪烁、端口跳转及UI分组问题
"""

import os,time
import sys
import re
import subprocess
import threading
import uuid
from flask import Blueprint, jsonify, request, render_template_string

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    print("警告: 未安装 psutil，端口占用检测功能将受限。请运行: pip install psutil")

py_runner_bp = Blueprint("py_runner", __name__, url_prefix="/py-runner")

PY_ROOT = "pys"
PROCESS_POOL = {}

PORT_PATTERNS = [
    r'port\s*=\s*(\d+)',
    r'app\.run\([^)]*port\s*=\s*(\d+)',
    r'server\.bind\([^)]*,\s*(\d+)',
    r'listen\(\s*(\d+)',
]

def extract_port_from_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            for pattern in PORT_PATTERNS:
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    p = int(match.group(1))
                    if 1 <= p <= 65535: return str(p)
    except Exception: pass
    
    dir_path = os.path.dirname(file_path)
    if os.path.exists(dir_path):
        for fname in os.listdir(dir_path):
            if fname.lower().endswith(('.txt', '.env', '.conf')):
                try:
                    with open(os.path.join(dir_path, fname), 'r', encoding='utf-8', errors='ignore') as f:
                        m = re.search(r'(?:port|PORT)\s*[=:]\s*(\d+)', f.read())
                        if m:
                            p = int(m.group(1))
                            if 1 <= p <= 65535: return str(p)
                except: continue
    return None

def get_port_occupier(port_str):
    if not port_str or not HAS_PSUTIL: return None
    try:
        port = int(port_str)
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr.port == port and conn.status == 'LISTEN':
                try:
                    proc = psutil.Process(conn.pid)
                    return {"pid": conn.pid, "name": proc.name(), "cmdline": " ".join(proc.cmdline())[:100]}
                except: return {"pid": conn.pid, "name": "Unknown", "cmdline": ""}
    except: pass
    return None

def kill_process_by_pid(pid):
    try:
        if not HAS_PSUTIL: return False
        proc = psutil.Process(int(pid))
        proc.terminate()
        proc.wait(timeout=3)
        return True
    except:
        try:
            proc.kill()
            return True
        except: return False

def scan_py_files():
    """扫描并按目录分组"""
    groups = {} 
    root = os.path.abspath(PY_ROOT)
    if not os.path.exists(root): return {}

    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        py_files = [f for f in files if f.endswith(".py")]
        if not py_files: continue

        rel_dir = os.path.relpath(current, root)
        group_name = "根目录" if rel_dir == "." else rel_dir.replace("\\", "/")

        if group_name not in groups:
            groups[group_name] = []

        for file in py_files:
            full = os.path.join(current, file)
            abs_full = os.path.abspath(full)
            if not abs_full.startswith(root): continue
            
            rel_path = os.path.relpath(full, root).replace("\\", "/")
            port = extract_port_from_file(abs_full)
            occupier = get_port_occupier(port) if port else None

            groups[group_name].append({
                "id": rel_path,
                "file": file,
                "port": port,
                "occupied": occupier is not None,
                "occupier_info": occupier
            })
    
    # 排序
    sorted_groups = {}
    if "根目录" in groups:
        sorted_groups["根目录"] = groups.pop("根目录")
    for key in sorted(groups.keys()):
        sorted_groups[key] = groups[key]
        
    return sorted_groups

def run_script(task_id, target_full, args):
    import subprocess
    try:
        # 关键：设置 cwd 为脚本所在目录，解决相对路径问题
        script_dir = os.path.dirname(target_full)
        
        # 关键：清除 Werkzeug 相关的环境变量，防止子进程继承无效的 fd
        env = os.environ.copy()
        env.pop('WERKZEUG_SERVER_FD', None)
        env.pop('WERKZEUG_RUN_MAIN', None)
        env['PYTHONUNBUFFERED'] = '1' # 确保输出实时刷新

        cmd = ["python3", target_full]
        if args:
            cmd.extend(args.split())

        proc = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            text=True, 
            encoding='utf-8',
            errors='ignore',
            cwd=script_dir,
            env=env
        )
        
        # 读取输出并更新 PROCESS_POOL[task_id]["output"]
        for line in proc.stdout:
            PROCESS_POOL[task_id]["output"] += line
            
        proc.wait()
        PROCESS_POOL[task_id]["status"] = "completed"
        
    except Exception as e:
        PROCESS_POOL[task_id]["status"] = "error"
        PROCESS_POOL[task_id]["output"] += f"\nError: {str(e)}"
HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Python Runner Pro</title>
<style>
    :root { --primary: #2563eb; --success: #16a34a; --danger: #dc2626; --bg: #f8fafc; }
    body{font-family:'Segoe UI',system-ui,sans-serif;margin:0;padding:20px;background:var(--bg);color:#334155;}
    h2{margin-bottom:20px;color:#1e293b;font-weight:700;}
    
    .group-container{margin-bottom:25px;}
    .group-title{font-size:13px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;padding-left:8px;border-left:3px solid #cbd5e1;}
    
    .script-card{background:#fff;border-radius:8px;padding:12px 16px;margin-bottom:12px;box-shadow:0 1px 2px rgba(0,0,0,0.05);border:1px solid #e2e8f0;transition:all 0.2s;}
    .script-card:hover{box-shadow:0 4px 6px -1px rgba(0,0,0,0.1);}
    
    .card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;}
    .file-name{font-weight:600;font-size:14px;color:#0f172a;}
    
    .tag{font-size:11px;padding:2px 6px;border-radius:4px;margin-left:6px;cursor:pointer;display:inline-block;font-weight:500;}
    .tag-free{background:#dcfce7;color:#166534;}
    .tag-busy{background:#fee2e2;color:#991b1b;text-decoration:underline;}
    .tag-none{background:#f1f5f9;color:#94a3b8;}
    
    .controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
    input{padding:6px 10px;border:1px solid #cbd5e1;border-radius:6px;font-size:13px;width:180px;outline:none;transition:border 0.2s;}
    input:focus{border-color:var(--primary);box-shadow:0 0 0 2px rgba(37,99,235,0.1);}
    
    button{padding:6px 14px;border:none;border-radius:6px;cursor:pointer;color:#fff;font-size:12px;font-weight:600;transition:all 0.2s;}
    button:hover{transform:translateY(-1px);box-shadow:0 2px 4px rgba(0,0,0,0.1);}
    button:active{transform:translateY(0);}
    .btn-run{background:var(--primary);}
    .btn-stop{background:var(--danger);}
    .btn-link{background:var(--success);text-decoration:none;padding:6px 14px;line-height:normal;display:inline-block;}
    .btn-toggle{background:#fff;color:#475569;border:1px solid #cbd5e1;}
    .btn-toggle:hover{background:#f8fafc;}
    
    /* 日志区域 */
    .log-wrapper{margin-top:12px;overflow:hidden;transition:all 0.3s ease;}
    .log-wrapper.collapsed{max-height:0;opacity:0;margin-top:0;}
    .log-wrapper.expanded{max-height:400px;opacity:1;}
    
    pre{background:#1e293b;color:#e2e8f0;padding:12px;border-radius:6px;font-size:12px;margin:0;white-space:pre-wrap;height:180px;overflow-y:auto;font-family:'Consolas','Monaco',monospace;line-height:1.5;}
    
    .status-running{border-left:4px solid var(--primary);background:#eff6ff;}
    .status-finished{border-left:4px solid var(--success);}
    .status-error{border-left:4px solid var(--danger);}

    .modal{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);display:none;justify-content:center;align-items:center;z-index:1000;backdrop-filter:blur(2px);}
    .modal-content{background:#fff;padding:24px;border-radius:12px;width:400px;box-shadow:0 20px 25px -5px rgba(0,0,0,0.1);}
    .modal-btns{margin-top:20px;text-align:right;display:flex;justify-content:flex-end;gap:10px;}
</style>
</head>
<body>
<h2>🐍 Python Runner Pro</h2>
<div id="list"></div>

<div id="confirmModal" class="modal">
    <div class="modal-content">
        <h3 style="margin-top:0;color:#1e293b;">⚠️ 端口占用</h3>
        <p style="font-size:14px;color:#64748b;margin-bottom:10px;">端口 <b id="modalPort" style="color:#dc2626;"></b> 正被以下进程占用:</p>
        <pre id="modalInfo" style="background:#f8fafc;color:#334155;border:1px solid #e2e8f0;font-size:12px;height:auto;max-height:150px;"></pre>
        <div class="modal-btns">
            <button onclick="closeModal()" style="background:#94a3b8;">取消</button>
            <button onclick="confirmKill()" style="background:#dc2626;">强制结束</button>
        </div>
    </div>
</div>

<script>
const timers = {};
let killTarget = null;

// 获取基础 URL 的正确方式
function getBaseUrl() {
    const url = new URL(window.location.href);
    return `${url.protocol}//${url.host}`;
}

async function load(){
    try {
        let r = await fetch('/py-runner/list');
        if (!r.ok) throw new Error('Server error');
        let data = await r.json();
        let box = document.getElementById('list');
        
        // 仅在首次加载或手动刷新时清空，避免运行时闪烁
        if(box.children.length === 0) box.innerHTML = '';
        else {
            // 如果已有内容，只更新不存在的组，保留正在运行的卡片
            // 为简化逻辑，这里我们采用增量更新策略：
            // 1. 标记所有现有卡片为 stale
            // 2. 遍历新数据，更新或创建卡片
            // 3. 删除 stale 卡片
            // *注：为保持代码简洁且稳定，本版本采用“非破坏性渲染”：
            // 如果卡片已存在且正在运行，跳过它。否则重新渲染。*
        }
        
        // 简单起见，我们重新构建 HTML，但尝试保留运行中的状态
        // 更好的做法是 Diff 算法，但这里我们用一种技巧：
        // 我们不直接 innerHTML = ''，而是对比 ID
        
        let existingIds = new Set();
        for (const [groupName, files] of Object.entries(data)) {
            let groupDiv = document.getElementById(`group_${groupName.replace(/[^a-zA-Z0-9]/g, '_')}`);
            if (!groupDiv) {
                groupDiv = document.createElement('div');
                groupDiv.className = 'group-container';
                groupDiv.id = `group_${groupName.replace(/[^a-zA-Z0-9]/g, '_')}`;
                groupDiv.innerHTML = `<div class="group-title">${groupName}</div>`;
                box.appendChild(groupDiv);
            }
            
            files.forEach(x => {
                let safeId = x.id.replace(/[\\/\\.]/g,'_');
                existingIds.add(safeId);
                
                let card = document.getElementById(`card_${safeId}`);
                if (card && card.classList.contains('status-running')) {
                    // 如果正在运行，不要重绘，以免丢失日志和状态
                    return; 
                }

                // 如果卡片不存在，或者不是运行状态，则重新创建/更新
                let portHtml = '', linkHtml = '';
                const baseUrl = getBaseUrl();
                
                if(x.port){
                    let targetUrl = `${baseUrl}:${x.port}`;
                    if(x.occupied){
                        let infoJson = JSON.stringify(x.occupier_info);
                        portHtml = `<span class="tag tag-busy" data-port="${x.port}" data-info='${infoJson}' onclick="handlePortClick(this)">Port: ${x.port}</span>`;
                    } else {
                        portHtml = `<span class="tag tag-free">Port: ${x.port}</span>`;
                        linkHtml = `<a class="btn-link" href="${targetUrl}" target="_blank">打开</a>`;
                    }
                } else {
                    portHtml = `<span class="tag tag-none">No Port</span>`;
                }
                
                let newCardHtml = `
                    <div class="card-header">
                        <span class="file-name">${x.file}</span>
                        <div>${portHtml} ${linkHtml}</div>
                    </div>
                    <div class="controls">
                        <input id="arg_${safeId}" placeholder="参数 (可选)">
                        <button class="btn-run" onclick="run('${x.id}')">▶ 运行</button>
                        <button class="btn-stop" onclick="stop('${x.id}')">■ 停止</button>
                        <button class="btn-toggle" onclick="toggleLog('${safeId}')">📄 日志</button>
                    </div>
                    <div class="log-wrapper collapsed" id="log_wrap_${safeId}">
                        <pre id="out_${safeId}">等待运行...</pre>
                    </div>
                `;
                
                if (card) {
                    card.outerHTML = `<div class="script-card" id="card_${safeId}">${newCardHtml}</div>`;
                } else {
                    let wrapper = document.createElement('div');
                    wrapper.className = 'script-card';
                    wrapper.id = `card_${safeId}`;
                    wrapper.innerHTML = newCardHtml;
                    groupDiv.appendChild(wrapper);
                }
            });
        }
        
        // 清理已删除文件的卡片 (可选，为安全起见暂不自动删除，防止误删运行中任务)
        
    } catch (e) {
        console.error(e);
        document.getElementById('list').innerHTML = `<p style="color:red">加载失败: ${e.message}</p>`;
    }
}

function toggleLog(safeId){
    let wrap = document.getElementById(`log_wrap_${safeId}`);
    if(wrap.classList.contains('collapsed')){
        wrap.classList.remove('collapsed');
        wrap.classList.add('expanded');
    } else {
        wrap.classList.remove('expanded');
        wrap.classList.add('collapsed');
    }
}

function handlePortClick(element) {
    let port = element.getAttribute('data-port');
    let infoStr = element.getAttribute('data-info');
    if(infoStr) {
        try {
            let info = JSON.parse(infoStr);
            showKillConfirm(port, info);
        } catch(e) { alert("解析错误"); }
    }
}

async function run(id){
    let safeId = id.replace(/[\\/\\.]/g,'_');
    if(timers[safeId]) clearInterval(timers[safeId]);
    
    // 自动展开日志
    let wrap = document.getElementById(`log_wrap_${safeId}`);
    if(wrap.classList.contains('collapsed')) toggleLog(safeId);

    let arg = document.getElementById("arg_"+safeId).value;
    let pre = document.getElementById("out_"+safeId);
    pre.innerText = "正在启动...\\n";
    
    let card = document.getElementById("card_"+safeId);
    card.className = 'script-card status-running';
    
    try {
        let r = await fetch('/py-runner/run',{
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({file:id, args:arg})
        });
        let d = await r.json();
        if(d.error){ 
            pre.innerText = "错误: " + d.error; 
            card.className = 'script-card status-error';
            return; 
        }
        poll(safeId, d.task);
    } catch(e) { 
        pre.innerText = "请求失败: " + e.message; 
        card.className = 'script-card status-error';
    }
}

function poll(safeId, task){
    timers[safeId] = setInterval(async()=>{
        try{
            let r = await fetch('/py-runner/status/'+task);
            let d = await r.json();
            let pre = document.getElementById("out_"+safeId);
            if(pre) {
                pre.innerText = d.output || "";
                // 关键：自动滚动到底部，防止日志“看不见”
                pre.scrollTop = pre.scrollHeight;
            }
            let card = document.getElementById("card_"+safeId);
            
            if(d.status === 'finished'){ 
                if(card) {
                    card.className = 'script-card status-finished';
                    // 添加完成提示
                    if(pre && !pre.innerText.endsWith('\\n已完成\\n')) {
                        pre.innerText += "\\n--- 执行完毕 ---";
                    }
                }
                clearInterval(timers[safeId]); 
                // 注意：这里不调用 load()，防止日志消失！
                // 用户想刷新端口状态时，可以手动刷新页面或点击其他按钮
            } else if(d.status === 'error'){ 
                if(card) card.className = 'script-card status-error'; 
                clearInterval(timers[safeId]); 
            }
        }catch(e){ clearInterval(timers[safeId]); }
    }, 500);
}

async function stop(id){
    let safeId = id.replace(/[\\/\\.]/g,'_');
    if(timers[safeId]) clearInterval(timers[safeId]);
    await fetch('/py-runner/stop',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({task:id})
    });
    let card = document.getElementById("card_"+safeId);
    if(card) card.className = 'script-card';
    // 停止后可以选择刷新以更新端口状态
    setTimeout(load, 500);
}

function showKillConfirm(port, infoObj){
    document.getElementById('modalPort').innerText = port;
    document.getElementById('modalInfo').innerText = `PID: ${infoObj.pid}\\nName: ${infoObj.name}\\nCmd: ${infoObj.cmdline}`;
    killTarget = {pid: infoObj.pid, port: port};
    document.getElementById('confirmModal').style.display = 'flex';
}

function closeModal(){ document.getElementById('confirmModal').style.display = 'none'; killTarget = null; }

async function confirmKill(){
    if(!killTarget) return;
    try {
        let r = await fetch('/py-runner/kill-port',{
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify(killTarget)
        });
        let d = await r.json();
        if(d.ok){ alert('已清理'); closeModal(); load(); } else { alert('失败'); }
    } catch(e) { alert("请求失败"); }
}

// 初始加载
load();
</script>
</body>
</html>
"""

@py_runner_bp.route("/")
def index():
    return render_template_string(HTML)

@py_runner_bp.route("/list")
def list_files():
    try: return jsonify(scan_py_files())
    except Exception as e: return jsonify({"error": str(e)}), 500

@py_runner_bp.route("/run", methods=["POST"])
def run():
    try:
        data = request.json
        if not data or "file" not in data:
            return jsonify({"error": "缺少必要参数: file"}), 400
            
        rel = data["file"]
        args = data.get("args", "")
        
        root = os.path.abspath(PY_ROOT)
        target_path = os.path.normpath(os.path.join(root, rel))
        
        # 1. 安全性检查：防止路径穿越
        if not target_path.startswith(root):
            return jsonify({"error": "非法路径"}), 403

        # 2. 文件存在性检查 (替代原有的循环查找，效率更高且更稳健)
        if not os.path.isfile(target_path):
            return jsonify({"error": "file not found"}), 404

        task = str(uuid.uuid4())
        
        # 3. 初始化任务状态
        PROCESS_POOL[task] = {
            "file": rel, 
            "status": "running", 
            "output": "",
            "start_time": time.time() # 建议增加时间戳以便后续做超时处理
        }
        
        # 4. 启动后台线程
        # 注意：确保 run_script 内部使用了 subprocess 而不是直接 import 执行
        t = threading.Thread(target=run_script, args=(task, target_path, args), daemon=True)
        t.start()
        
        return jsonify({"task": task})
    
    except Exception as e:
        # 记录详细错误日志有助于排查问题
        import traceback
        print(f"[Run Error] {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

@py_runner_bp.route("/status/<task>")
def status(task):
    info = PROCESS_POOL.get(task, {})
    return jsonify({k: v for k, v in info.items() if k != "process"})

@py_runner_bp.route("/stop", methods=["POST"])
def stop():
    try:
        task = request.json.get("task")
        item = PROCESS_POOL.get(task)
        if not item: return jsonify({"error": "not found"}), 404
        p = item.get("process")
        if p and p.poll() is None:
            try:
                p.terminate()
                item["status"] = "stopped"
            except: pass
        return jsonify({"ok": True})
    except Exception as e: return jsonify({"error": str(e)}), 500

@py_runner_bp.route("/kill-port", methods=["POST"])
def kill_port():
    try:
        pid = request.json.get("pid")
        if not pid: return jsonify({"error": "missing pid"}), 400
        return jsonify({"ok": True}) if kill_process_by_pid(pid) else jsonify({"error": "failed"}), 500
    except Exception as e: return jsonify({"error": str(e)}), 500
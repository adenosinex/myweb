import os
import sqlite3
import json
import time
import requests
import urllib.parse
import re
import csv
import io
import threading
from openai import OpenAI
from flask import Blueprint, request, jsonify, Response, stream_with_context
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

# ==========================================
# 配置与全局变量
# ==========================================
video_bp = Blueprint('video', __name__)
DB_PATH = 'db/universal_data.db'
RESOURCE_NODE_URL = "http://192.168.31.204:8100"
VIDEO_VECTOR_NODE = "http://15x4.zin6.dpdns.org:5003"
VIDEO_VECTOR_NODE_PIC = "http://15x4.zin6.dpdns.org:5004"

# 🌟 一键切换开关：将这里改为 'ollama' 或 'cloud'
ACTIVE_AI_MODE = 'ollama'  

if ACTIVE_AI_MODE == 'cloud':
    AI_MODEL = 'Qwen3-30B-A3B-Instruct-2507' 
    AI_BASE_URL = "https://api.scnet.cn/api/llm/v1"
    AI_API_KEY = os.getenv("CS_API_KEY", "your_cloud_api_key_here")
    MAX_WORKERS, BATCH_SIZE = 6, 8
else:
    AI_MODEL = 'huihui_ai/qwen3.5-abliterated:9b'
    AI_BASE_URL = "http://apple4.zin6.dpdns.org:11434/v1"
    AI_API_KEY = "ollama" 
    MAX_WORKERS, BATCH_SIZE = 1, 6

client = OpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
state_lock = threading.Lock()

ai_scan_state = {
    "is_running": False, "total": 0, "processed": 0,       
    "success_count": 0, "status_msg": "就绪，等待扫描",
    "total_time_sec": 0, "ai_model": AI_MODEL, "recent_results": []
}

# ==========================================
# 🛠️ 通用工具函数
# ==========================================

def _db_execute(query, params=(), fetchall=False, fetchone=False, commit=False, return_dict=False):
    """精简数据库操作样板代码"""
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        if return_dict: conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        if commit: conn.commit()
        if fetchall: return [dict(r) if return_dict else r for r in cur.fetchall()]
        if fetchone: return dict(cur.fetchone()) if return_dict and cur.fetchone() else cur.fetchone()
        return cur

def util_init_video_db():
    _db_execute('CREATE TABLE IF NOT EXISTS video_store (filename TEXT PRIMARY KEY, tags TEXT, category TEXT)', commit=True)
    for col_name, col_type in [("ai_model", "TEXT"), ("ai_time_sec", "REAL"), ("updated_at", "REAL")]:
        try: _db_execute(f'ALTER TABLE video_store ADD COLUMN {col_name} {col_type}', commit=True)
        except sqlite3.OperationalError: pass 
    _db_execute('CREATE TABLE IF NOT EXISTS video_stats (filename TEXT PRIMARY KEY, is_liked INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0, play_count INTEGER DEFAULT 0, last_played_at REAL DEFAULT 0)', commit=True)

util_init_video_db()

def util_fetch_remote_videos():
    try:
        resp = requests.get(f"{RESOURCE_NODE_URL}/api/videos/json", timeout=10, proxies={"http": None, "https": None})
        return resp.json() if resp.status_code == 200 else []
    except: return []

def util_extract_tags_from_filename(fname):
    return [m.group(1) for m in re.finditer(r'#([^#\s.]+)', fname)]

def _fetch_node(url, default=None, **kwargs):
    """通用请求封装"""
    try:
        resp = requests.get(url, proxies={"http": None, "https": None}, timeout=kwargs.pop('timeout', 15), **kwargs)
        return (resp.json(), resp.status_code) if resp.status_code == 200 else (default or {"error": f"HTTP {resp.status_code}"}, resp.status_code)
    except requests.exceptions.Timeout: return ({"error": "请求超时"}, 504)
    except Exception as e: return ({"error": str(e)}, 500)

# ==========================================
# 🌐 路由层 (Route Controllers)
# ==========================================

@video_bp.route('/api/video/scan', methods=['POST'])
def control_scan():
    req = request.get_json(silent=True) or {}
    if req.get('action') == 'start' and not ai_scan_state["is_running"]:
        threading.Thread(target=biz_ai_tag_videos_task, daemon=True).start()
    elif req.get('action') == 'stop':
        ai_scan_state["is_running"] = False
    return jsonify({"status": "ok"})

@video_bp.route('/api/video/scan/status', methods=['GET'])
def get_scan_status(): 
    return jsonify(ai_scan_state)

@video_bp.route('/api/video/stats', methods=['GET'])
def get_video_stats(): 
    return jsonify(biz_get_video_stats())

@video_bp.route('/api/video/list', methods=['GET','POST'])
def get_video_list():
    if request.method == 'POST':
        res, code = biz_get_video_list_by_names((request.get_json(silent=True) or {}).get('names', []))
        return jsonify(res), code
    
    args = request.args
    res, code = biz_get_filtered_video_list(
        args.get('filter', 'all'), args.get('tag', '全部'), args.get('pool', 'mixed'), 
        int(args.get('page', 1)), int(args.get('limit', 10)), args.get('need_tags', '0') == '1'
    )
    return jsonify(res), code

@video_bp.route('/api/video/export_csv', methods=['GET'])
def export_video_csv(): 
    return biz_generate_video_csv(None, 'video_stats.csv')

@video_bp.route('/api/video/export_csv2', methods=['GET'])
def export_video_csv2(): 
    return biz_generate_video_csv(500, 'video_stats_full.csv')

@video_bp.route('/stream/video/<path:video_name>', methods=['GET'])
def proxy_stream_video(video_name):
    try:
        headers = {k: v for k, v in request.headers if k.lower() != 'host'}
        req = requests.get(f"{RESOURCE_NODE_URL}/stream/video/{quote(video_name)}", headers=headers, stream=True, proxies={"http": None, "https": None})
        resp_headers = [(n, v) for n, v in req.raw.headers.items() if n.lower() not in ('content-encoding', 'transfer-encoding', 'connection')]
        return Response(stream_with_context(req.iter_content(chunk_size=1024*1024)), status=req.status_code, headers=resp_headers)
    except Exception as e: 
        return jsonify({"error": str(e)}), 500

@video_bp.route('/api/video/maintenance/list', methods=['GET'])
def get_maintenance_list(): 
    res, code = biz_get_maintenance_list()
    return jsonify(res), code

@video_bp.route('/api/video/maintenance/confirm_delete', methods=['POST'])
def confirm_maintenance_delete(): 
    res, code = biz_confirm_maintenance_delete((request.get_json(silent=True) or {}).get('filenames', []))
    return jsonify(res), code

@video_bp.route('/api/video/sync', methods=['POST'])
def sync_video_actions(): 
    res, code = biz_sync_video_actions(request.get_json(silent=True) or [])
    return jsonify(res), code

@video_bp.route('/test/llm', methods=['GET'])
def test_llm_page(): 
    return biz_get_test_llm_page_html()

@video_bp.route('/api/test/llm/ask', methods=['POST'])
def test_llm_ask(): 
    res, code = biz_test_llm_ask((request.get_json(silent=True) or {}).get('prompt', ''))
    return jsonify(res), code

@video_bp.route('/api/video/recommend', methods=['GET'])
def get_video_recommendations():
    if not (fname := request.args.get('name')): 
        return jsonify({"recommendations": []}), 200
    res, code = _fetch_node(f"{VIDEO_VECTOR_NODE}/api/video/similar?name={quote(fname)}&k=10&threshold=0.85", {"recommendations": []}, timeout=5)
    return jsonify(res), code

@video_bp.route('/api/video/vision_recommend', methods=['GET'])
def get_vision_video_recommendations():
    if not (fname := request.args.get('name')): 
        return jsonify({"error": "缺少 filename 参数"}), 400
    res, code = _fetch_node(f"{VIDEO_VECTOR_NODE_PIC}/api/vision/similar_by_name?name={quote(fname.replace('[NEW2]_',''))}&k={int(request.args.get('k', 15))}")
    if code == 404: 
        return jsonify({"error": "该视频尚未进行视觉特征分析", "target": fname}), 404
    return jsonify(res), code

# ==========================================
# 💼 业务逻辑层
# ==========================================

def biz_process_single_batch(batch, batch_id):
    global ai_scan_state
    if not ai_scan_state["is_running"]: return

    prompt = f"""你是一个短视频内容分析引擎。请根据文件名推断1个【主分类】(如:影视,颜值,素人,舞蹈,职业等)及1-7个【子标签】。
    返回纯 JSON：{{"video.mp4": {{"category": "影视", "tags": ["混剪", "动作"]}}}}
    文件名列表：{json.dumps(batch, ensure_ascii=False)}"""
    
    t0 = time.time()
    try:
        # 恢复 Ollama 原生调用以支持 think=False 参数
        if ACTIVE_AI_MODE == 'ollama':
            ollama_url = AI_BASE_URL.replace('/v1', '/api/chat')
            payload = {
                "model": AI_MODEL,
                "messages": [{'role': 'system', 'content': '你是一个只返回纯 JSON 的分析助手。'}, {'role': 'user', 'content': prompt}],
                "stream": False, "think": False,
                "options": {"temperature": 0.1, "num_predict": 3000}
            }
            resp = requests.post(ollama_url, json=payload, timeout=60)
            resp.raise_for_status()
            result_text = resp.json().get("message", {}).get("content", "").strip()
        else:
            completion = client.chat.completions.create(
                model=AI_MODEL,
                messages=[{'role': 'system', 'content': '你是一个只返回纯 JSON 的分析助手。'}, {'role': 'user', 'content': prompt}],
                max_tokens=3000, temperature=0.1
            )
            result_text = completion.choices[0].message.content.strip()

        elapsed = round((time.time() - t0) / max(len(batch), 1), 2) 
        
        if match := re.search(r'\{.*\}', result_text, re.DOTALL):
            ai_data = json.loads(match.group(0))
            last_fname, last_tags_str = "", ""
            
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                for fname, info in ai_data.items():
                    tags = info.get('tags', [])
                    tags = tags if isinstance(tags, list) else [tags]
                    conn.execute("INSERT OR REPLACE INTO video_store (filename, category, tags, ai_model, ai_time_sec, updated_at) VALUES (?, ?, ?, ?, ?, ?)", 
                                 (fname, info.get('category', '未分类'), json.dumps(tags[:5], ensure_ascii=False), AI_MODEL, elapsed, time.time()))
                    last_fname, last_tags_str = fname, " ".join([f"#{t}" for t in tags[:3]])
            
            with state_lock:
                ai_scan_state["success_count"] += len(ai_data)
                ai_scan_state["processed"] += len(batch)
                ai_scan_state["status_msg"] = f"✅ [{ACTIVE_AI_MODE.upper()}] {last_fname[:15]}... {last_tags_str}"
                
                for fname, info in ai_data.items():
                    tags = info.get('tags', [])
                    ai_scan_state["recent_results"].insert(0, {
                        "filename": fname, "category": info.get('category', '未分类'),
                        "ai_tags": (tags if isinstance(tags, list) else [tags])[:5], "updated_at": time.time() 
                    })
                ai_scan_state["recent_results"] = ai_scan_state["recent_results"][:5]
                
    except Exception as e:
        with state_lock:
            ai_scan_state["processed"] += len(batch)
            ai_scan_state["status_msg"] = f"❌ {ACTIVE_AI_MODE.upper()} 报错：{str(e)[:30]}"
        if "Connection" in str(e) or "RateLimit" in str(e): time.sleep(5)

def biz_ai_tag_videos_task():
    global ai_scan_state
    ai_scan_state.update({"is_running": True, "total": 0, "processed": 0, "success_count": 0, "status_msg": "正在同步列表...", "total_time_sec": 0, "ai_model": AI_MODEL, "recent_results": []})
    t0 = time.time()
    
    if not (all_files := util_fetch_remote_videos()):
        return ai_scan_state.update({"is_running": False, "status_msg": "无法访问资源节点或视频列表为空"})

    existing = {row[0] for row in _db_execute("SELECT filename FROM video_store", fetchall=True)}
    if not (untagged := [f for f in all_files if f not in existing]): 
        return ai_scan_state.update({"is_running": False, "status_msg": "🎉 所有视频均已打标"})

    ai_scan_state.update({"total": len(untagged), "status_msg": f"🚀 {ACTIVE_AI_MODE.upper()} 并发分析已启动"})
    batches = [untagged[i:i+BATCH_SIZE] for i in range(0, len(untagged), BATCH_SIZE)]
    futures = []
    
    for idx, batch in enumerate(batches):
        if not ai_scan_state["is_running"]: break
        futures.append(executor.submit(biz_process_single_batch, batch, idx + 1))
        time.sleep(0.5)
        with state_lock: ai_scan_state["total_time_sec"] = round(time.time() - t0, 1)

    for _ in as_completed(futures):
         with state_lock: ai_scan_state["total_time_sec"] = round(time.time() - t0, 1)
            
    if ai_scan_state["is_running"]:
        ai_scan_state.update({"is_running": False, "status_msg": f"🎉 成功打标 {ai_scan_state['success_count']} 个视频！"})

def biz_get_video_stats():
    deleted_set = {row[0] for row in _db_execute('SELECT filename FROM video_stats WHERE is_deleted = 1', fetchall=True)}
    db_store = {row[0]: {"cat": row[1], "tags": json.loads(row[2]) if row[2] else []} for row in _db_execute('SELECT filename, category, tags FROM video_store', fetchall=True)}

    temp_counts = {}
    for f in util_fetch_remote_videos():
        if f in deleted_set: continue
        meta = db_store.get(f, {"cat": "未分类", "tags": []})
        if meta["cat"] and meta["cat"] != '未分类':
            temp_counts[meta["cat"]] = temp_counts.get(meta["cat"], 0) + 1
        for t in set(meta["tags"] + util_extract_tags_from_filename(f)):
            temp_counts[t] = temp_counts.get(t, 0) + 1
            
    return {k: v for k, v in temp_counts.items() if v > 3}

def biz_get_video_list_by_names(name_list):
    if not name_list: return {"items": [], "total": 0, "has_more": False}, 200
    try:
        rows = _db_execute(f"SELECT * FROM video_store WHERE filename IN ({','.join(['?']*len(name_list))})", name_list, fetchall=True, return_dict=True)
        row_map = {row['filename']: row for row in rows}
        items = [{**row_map[n], 'ai_tags': json.loads(row_map[n]['tags'] or '[]'), 'url': f"/stream/video/{quote(n)}"} for n in name_list if n in row_map]
        return {"items": items, "total": len(items), "has_more": False}, 200
    except Exception as e: return {"error": str(e)}, 500

def biz_get_filtered_video_list(filter_type, tag_filter, pool_type, page, limit, need_tags):
    tag_filter = tag_filter or '全部'
    all_files = util_fetch_remote_videos()
    
    store_dict = {row[0]: {"category": row[1], "tags": json.loads(row[2] or '[]')} for row in _db_execute('SELECT filename, category, tags FROM video_store', fetchall=True)}
    stats_dict = {row[0]: {"is_liked": row[1] or 0, "is_deleted": row[2] or 0, "play_count": row[3] or 0} for row in _db_execute('SELECT filename, is_liked, is_deleted, play_count FROM video_stats', fetchall=True)}

    filtered_list, temp_counts = [], {}
    kw = tag_filter.lower()

    for f in all_files:
        is_new1, is_new2 = f.startswith('[NEW]_'), f.startswith('[NEW2]_')
        if pool_type == 'default' and (is_new1 or is_new2): continue
        if pool_type == 'new' and not is_new1: continue
        if pool_type == 'new2' and not is_new2: continue

        st_data = stats_dict.get(f, {"is_liked": 0, "is_deleted": 0, "play_count": 0})
        is_del, is_lik = st_data["is_deleted"], st_data["is_liked"]
        
        if (filter_type == 'disliked' and is_del != 1) or (filter_type != 'disliked' and is_del == 1) or (filter_type == 'liked' and is_lik != 1):
            continue

        s_data = store_dict.get(f, {"category": "未分类", "tags": []})
        fname_tags = util_extract_tags_from_filename(f)
        merged_tags = list(set(s_data["tags"] + fname_tags))
        
        if kw == '全部' or kw in f.lower() or kw in s_data["category"].lower() or any(kw in t.lower() for t in merged_tags):
            filtered_list.append({
                "filename": f, "url": f"/stream/video/{quote(f, safe='')}", "category": s_data["category"], 
                "ai_tags": s_data["tags"], "filename_tags": fname_tags, "mergedTags": merged_tags, 
                "is_liked": bool(is_lik), "play_count": st_data["play_count"]
            })
            if need_tags:
                cat = s_data["category"]
                if cat and cat != '未分类': temp_counts[cat] = temp_counts.get(cat, 0) + 1
                for t in merged_tags: temp_counts[t] = temp_counts.get(t, 0) + 1

    start = (page - 1) * limit
    return {
        "items": filtered_list[start:start+limit], 
        "tags_count": {k: v for k, v in temp_counts.items() if v >= 5} if need_tags else {}, 
        "has_more": start + limit < len(filtered_list), 
        "total": len(filtered_list)
    }, 200

def biz_generate_video_csv(limit, filename):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['文件名', '主分类', 'AI 标签', '提取标签', '播放次数', '是否喜欢', '是否隐藏', '最后活动时间', 'AI 模型', 'AI 耗时 (秒)', '最后更新时间'])
    
    query = 'SELECT v.filename, v.category, v.tags, s.play_count, s.is_liked, s.is_deleted, s.last_played_at, v.ai_model, v.ai_time_sec, v.updated_at FROM video_store v LEFT JOIN video_stats s ON v.filename = s.filename'
    if limit: query += f' ORDER BY v.updated_at DESC LIMIT {limit}'

    for r in _db_execute(query, fetchall=True):
        writer.writerow([
            r[0], r[1], " ".join(json.loads(r[2] or '[]')), " ".join(util_extract_tags_from_filename(r[0])), r[3] or 0, 
            '是' if r[4] else '否', '是' if r[5] else '否', 
            time.strftime('%Y-%m-%d %H:%M', time.localtime(r[6])) if r[6] else '-', 
            r[7] or '-', r[8] or '-', 
            time.strftime('%Y-%m-%d %H:%M', time.localtime(r[9])) if r[9] else '-' 
        ])
            
    response = Response(output.getvalue().encode('utf-8-sig'), mimetype='text/csv')
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response

def biz_get_maintenance_list():
    try:
        liked = [r[0] for r in _db_execute("SELECT filename FROM video_stats WHERE is_liked = 1 AND is_deleted = 0", fetchall=True)]
        deleted = [r[0] for r in _db_execute("SELECT filename FROM video_stats WHERE is_deleted = 1", fetchall=True)]
        return {"status": "ok", "liked": liked, "deleted": deleted}, 200
    except Exception as e: return {"status": "error", "message": str(e)}, 500

def biz_confirm_maintenance_delete(filenames_to_delete):
    if not filenames_to_delete: return {"status": "ok", "deleted_count": 0}, 200
    try:
        placeholders = ','.join(['?'] * len(filenames_to_delete))
        _db_execute(f"DELETE FROM video_store WHERE filename IN ({placeholders})", filenames_to_delete, commit=True)
        _db_execute(f"DELETE FROM video_stats WHERE filename IN ({placeholders})", filenames_to_delete, commit=True)
        return {"status": "ok", "deleted_count": len(filenames_to_delete)}, 200
    except Exception as e: return {"status": "error", "message": str(e)}, 500

def biz_sync_video_actions(actions):
    if not actions: return {"status": "ok", "processed": 0}, 200
    processed = 0
    sql_map = {
        'like': 'UPDATE video_stats SET is_liked = 1 WHERE filename = ?',
        'unlike': 'UPDATE video_stats SET is_liked = 0 WHERE filename = ?',
        'delete': 'UPDATE video_stats SET is_deleted = 1 WHERE filename = ?',
        'undelete': 'UPDATE video_stats SET is_deleted = 0 WHERE filename = ?',
    }
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            for item in actions:
                fname, action = item.get('filename'), item.get('action')
                if not fname or not action: continue
                cur.execute('INSERT OR IGNORE INTO video_stats (filename) VALUES (?)', (fname,))
                if action in sql_map:
                    cur.execute(sql_map[action], (fname,))
                elif action == 'play':
                    ts = item.get('timestamp', time.time() * 1000)
                    cur.execute('UPDATE video_stats SET play_count = play_count + 1, last_played_at = ? WHERE filename = ?', (ts / 1000.0 if ts > 1e11 else ts, fname))
                processed += 1
            conn.commit()
        return {"status": "ok", "processed": processed}, 200
    except Exception as e: return {"error": str(e)}, 500

def biz_get_test_llm_page_html():
    return f"""
    <!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>LLM 测试</title>
    <style>body{{font-family:system-ui;max-width:800px;margin:20px auto;padding:0 15px;background:#f9f9f9}}textarea{{width:100%;padding:12px;margin-bottom:10px;border:1px solid #ccc;border-radius:5px}}button{{padding:10px 24px;background:#007bff;color:#fff;border:none;border-radius:5px;cursor:pointer}}.box{{background:#fff;padding:15px;border-radius:5px;border:1px solid #ddd;min-height:50px;white-space:pre-wrap}}.stats{{background:#ffeeba;color:#856404;padding:5px 10px;border-radius:3px;font-size:14px;margin-top:10px}}pre{{background:#282c34;color:#98c379;padding:15px;border-radius:5px;overflow-x:auto}}</style>
    </head><body><h2>🤖 LLM 对话测试</h2><div style="background:#e3f2fd;padding:10px;margin-bottom:15px">环境: {ACTIVE_AI_MODE.upper()} | 模型: {AI_MODEL}</div>
    <textarea id="prompt" rows="4" placeholder="输入问题..."></textarea><button onclick="ask()" id="btn">发送</button><span id="loading" style="display:none;color:#007bff;margin-left:10px">⏳ 思考中...</span>
    <h3>回复:</h3><div id="reply" class="box">等待输入...</div><div id="stats" class="stats" style="display:none;"></div><h3>原始响应:</h3><pre id="raw">{{}}</pre>
    <script>async function ask(){{const p=document.getElementById('prompt').value.trim();if(!p)return;const b=document.getElementById('btn'),l=document.getElementById('loading'),r=document.getElementById('reply'),s=document.getElementById('stats'),rw=document.getElementById('raw');b.disabled=true;l.style.display='inline';r.innerText='';s.style.display='none';rw.innerText='';try{{const res=await fetch('/api/test/llm/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{prompt:p}})}});const d=await res.json();if(d.error)r.innerHTML='<span style="color:red">❌ '+d.error+'</span>';else{{r.innerText=d.reply;s.style.display='inline-block';s.innerHTML=`🪙 Token | 提示:${{d.tokens.prompt_tokens}} | 回复:${{d.tokens.completion_tokens}} | 总计:${{d.tokens.total_tokens}}`;rw.innerText=JSON.stringify(d.raw,null,2);}}}}catch(e){{r.innerHTML='<span style="color:red">❌ '+e.message+'</span>';}}finally{{b.disabled=false;l.style.display='none';}}}}</script></body></html>
    """

def biz_test_llm_ask(user_prompt):
    if not user_prompt: return {"error": "Prompt 不能为空"}, 400
    try:
        t0 = time.time()
        
        # 恢复 Ollama 原生调用以支持 think=False 参数
        if ACTIVE_AI_MODE == 'ollama':
            ollama_url = AI_BASE_URL.replace('/v1', '/api/chat')
            payload = {
                "model": AI_MODEL, "messages": [{'role': 'user', 'content': user_prompt}],
                "stream": False, "think": False, "options": {"temperature": 0.7, "num_predict": 2000}
            }
            resp = requests.post(ollama_url, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            raw_response = data.copy()
            raw_response["_backend_cost_time_sec"] = round(time.time() - t0, 2)
            p_tokens, c_tokens = data.get("prompt_eval_count", 0), data.get("eval_count", 0)
            return {"reply": data.get("message", {}).get("content", ""), "tokens": {"prompt_tokens": p_tokens, "completion_tokens": c_tokens, "total_tokens": p_tokens + c_tokens}, "raw": raw_response}, 200
        else:
            completion = client.chat.completions.create(model=AI_MODEL, messages=[{'role': 'user', 'content': user_prompt}], max_tokens=2000, temperature=0.7)
            usage = completion.usage
            raw_response = completion.model_dump() if hasattr(completion, 'model_dump') else json.loads(completion.model_dump_json())
            raw_response["_backend_cost_time_sec"] = round(time.time() - t0, 2)
            return {"reply": completion.choices[0].message.content, "tokens": {"prompt_tokens": getattr(usage, 'prompt_tokens', 0), "completion_tokens": getattr(usage, 'completion_tokens', 0), "total_tokens": getattr(usage, 'total_tokens', 0)}, "raw": raw_response}, 200
    except Exception as e: return {"error": str(e)}, 500
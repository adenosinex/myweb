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

video_bp = Blueprint('video', __name__)
DB_PATH = 'universal_data.db'
RESOURCE_NODE_URL = "http://192.168.31.204:8100"
VIDEO_VECTOR_NODE="http://15x4.zin6.dpdns.org:5003"
# ==========================================
# 🌟 一键切换开关：将这里改为 'ollama' 或 'cloud'
# ==========================================
ACTIVE_AI_MODE = 'ollama'  

if ACTIVE_AI_MODE == 'cloud':
    # --- 云端 API 配置 ---
    AI_MODEL = 'Qwen3-30B-A3B-Instruct-2507' # 之前你用的云端模型
    AI_BASE_URL = "https://api.scnet.cn/api/llm/v1"
    AI_API_KEY = os.getenv("CS_API_KEY", "your_cloud_api_key_here")
    MAX_WORKERS = 6   # 云端支持高并发
    BATCH_SIZE = 8    # 云端一次处理更多文件
else:
    # --- 本地 Ollama 配置 (Mac mini M4) ---
    AI_MODEL = 'huihui_ai/qwen3.5-abliterated:9b'
    AI_BASE_URL = "http://apple4.zin6.dpdns.org:11434/v1"
    AI_API_KEY = "ollama" # 本地免鉴权
    MAX_WORKERS = 1   # M4 本地建议 1-2 个并发，避免内存排队崩溃
    BATCH_SIZE = 6    # 本地小批次更稳定

# 初始化统一的 OpenAI 客户端 (供 Cloud 模式使用)
client = OpenAI(
    api_key=AI_API_KEY,
    base_url=AI_BASE_URL,
)

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
state_lock = threading.Lock()

ai_scan_state = {
    "is_running": False,
    "total": 0,
    "processed": 0,       
    "success_count": 0,   
    "status_msg": "就绪，等待扫描",
    "total_time_sec": 0,
    "ai_model": AI_MODEL,
    "recent_results": []
}

def init_video_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS video_store (filename TEXT PRIMARY KEY, tags TEXT, category TEXT)')
        columns_to_add = [("ai_model", "TEXT"), ("ai_time_sec", "REAL"), ("updated_at", "REAL")]
        for col_name, col_type in columns_to_add:
            try:
                conn.execute(f'ALTER TABLE video_store ADD COLUMN {col_name} {col_type}')
            except sqlite3.OperationalError:
                pass 
        conn.execute('CREATE TABLE IF NOT EXISTS video_stats (filename TEXT PRIMARY KEY, is_liked INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0, play_count INTEGER DEFAULT 0, last_played_at REAL DEFAULT 0)')

init_video_db()

def process_single_batch(batch, batch_id):
    global ai_scan_state
    
    if not ai_scan_state["is_running"]:
        return

    prompt = f"""你是一个短视频内容分析引擎。请根据以下视频文件名，为每个视频推断出 1 个【主分类】(包括但不限于：影视，颜值，素人，演员，舞蹈，职业) 和 1 到 7 个【子标签】根据文件名信息量确定子标签数量。
    务必返回纯 JSON，格式：{{"video.mp4": {{"category": "影视", "tags": ["混剪", "动作"]}}}}
    文件名列表：{json.dumps(batch, ensure_ascii=False)}"""
    
    batch_start_time = time.time()
    current_timestamp = time.time() 
    
    try:
        # 🌟 分流处理：根据当前模式选择底层请求方式
        if ACTIVE_AI_MODE == 'ollama':
            # Ollama 原生 API 方式
            ollama_url = AI_BASE_URL.replace('/v1', '/api/chat')
            payload = {
                "model": AI_MODEL,
                "messages": [
                    {'role': 'system', 'content': '你是一个只返回纯 JSON 的分析助手。'},
                    {'role': 'user', 'content': prompt}
                ],
                "stream": False,
                "think": False, # 🌟 原生关闭思考开关
                "options": {
                    "temperature": 0.1,
                    "num_predict": 1000
                }
            }
            resp = requests.post(ollama_url, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            result_text = data.get("message", {}).get("content", "").strip()
        else:
            # Cloud 模式方式：使用标准 OpenAI 兼容 SDK
            completion = client.chat.completions.create(
                model=AI_MODEL,
                messages=[
                    {'role': 'system', 'content': '你是一个只返回纯 JSON 的分析助手。'},
                    {'role': 'user', 'content': prompt}
                ],
                max_tokens=3000,  
                temperature=0.1  
            )
            result_text = completion.choices[0].message.content.strip()
        
        batch_duration = time.time() - batch_start_time
        count = len(batch) if len(batch) > 0 else 1 
        elapsed = round(batch_duration / count, 2) 
        
        # 增加鲁棒性：使用正则提取 JSON 部分，防止模型返回 Markdown 块
        match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if match:
            ai_data = json.loads(match.group(0))
            last_fname, last_tags_str = "", ""
            
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                for fname, info in ai_data.items():
                    tags = info.get('tags', [])
                    if not isinstance(tags, list): tags = [tags]
                    
                    conn.execute("""
                        INSERT OR REPLACE INTO video_store (filename, category, tags, ai_model, ai_time_sec, updated_at) 
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (fname, info.get('category', '未分类'), json.dumps(tags[:5], ensure_ascii=False), AI_MODEL, elapsed, current_timestamp))
                    
                    last_fname, last_tags_str = fname, " ".join([f"#{t}" for t in tags[:3]])
            
            with state_lock:
                ai_scan_state["success_count"] += len(ai_data)
                ai_scan_state["processed"] += len(batch)
                ai_scan_state["status_msg"] = f"✅ [{ACTIVE_AI_MODE.upper()}] {last_fname[:15]}... {last_tags_str}"
                
                for fname, info in ai_data.items():
                    tags = info.get('tags', [])
                    if not isinstance(tags, list): tags = [tags]
                    ai_scan_state["recent_results"].insert(0, {
                        "filename": fname,
                        "category": info.get('category', '未分类'),
                        "ai_tags": tags[:5],
                        "updated_at": current_timestamp 
                    })
                ai_scan_state["recent_results"] = ai_scan_state["recent_results"][:5]
            
    except Exception as e:
        elapsed = round(time.time() - batch_start_time, 2)
        err_msg = str(e)
        with state_lock:
            ai_scan_state["processed"] += len(batch)
            ai_scan_state["status_msg"] = f"❌ {ACTIVE_AI_MODE.upper()} 报错：{err_msg[:30]}"
        if "Connection" in err_msg or "RateLimit" in err_msg: time.sleep(5)

def ai_tag_videos_task():
    global ai_scan_state
    
    ai_scan_state.update({
        "is_running": True, "total": 0, "processed": 0, 
        "success_count": 0, "status_msg": "正在同步列表...", 
        "total_time_sec": 0, "ai_model": AI_MODEL,
        "recent_results": []
    })
    
    task_start_time = time.time()
    
    try:
        resp = requests.get(f"{RESOURCE_NODE_URL}/api/videos/json", timeout=10, proxies={"http": None, "https": None})
        all_files = resp.json() if resp.status_code == 200 else []
    except Exception as e: 
        ai_scan_state.update({"is_running": False, "status_msg": "无法访问资源节点"})
        return

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filename FROM video_store")
        existing = set(row[0] for row in cursor.fetchall())
        
    untagged = [f for f in all_files if f not in existing]
    
    if not untagged: 
        ai_scan_state.update({"is_running": False, "status_msg": "🎉 所有视频均已打标"})
        return

    ai_scan_state.update({
        "total": len(untagged),
        "status_msg": f"🚀 {ACTIVE_AI_MODE.upper()} 并发分析已启动 (并发:{MAX_WORKERS}, 批次:{BATCH_SIZE})"
    })
    
    # 🌟 使用配置好的动态批次大小
    batches = [untagged[i:i+BATCH_SIZE] for i in range(0, len(untagged), BATCH_SIZE)]
    
    futures = []
    for idx, batch in enumerate(batches):
        if not ai_scan_state["is_running"]:
            break
        futures.append(executor.submit(process_single_batch, batch, idx + 1))
        time.sleep(0.5)
        
        with state_lock:
             ai_scan_state["total_time_sec"] = round(time.time() - task_start_time, 1)

    for future in as_completed(futures):
         with state_lock:
             ai_scan_state["total_time_sec"] = round(time.time() - task_start_time, 1)
            
    if ai_scan_state["is_running"]:
        ai_scan_state["is_running"] = False
        ai_scan_state["status_msg"] = f"🎉 成功打标 {ai_scan_state['success_count']} 个视频！"

@video_bp.route('/api/video/scan', methods=['POST'])
def control_scan():
    global ai_scan_state
    req_data = request.get_json(silent=True) or {}
    if req_data.get('action') == 'start' and not ai_scan_state["is_running"]:
        threading.Thread(target=ai_tag_videos_task, daemon=True).start()
    elif req_data.get('action') == 'stop':
        ai_scan_state["is_running"] = False
    return jsonify({"status": "ok"})

@video_bp.route('/api/video/scan/status', methods=['GET'])
def get_scan_status():
    return jsonify(ai_scan_state)

@video_bp.route('/api/video/stats', methods=['GET'])
def get_video_stats():
    try:
        resp = requests.get(f"{RESOURCE_NODE_URL}/api/videos/json", timeout=5, proxies={"http": None, "https": None})
        all_files = resp.json() if resp.status_code == 200 else []
    except Exception: all_files = []
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT filename, category, tags FROM video_store')
        db_store = {row[0]: {"cat": row[1], "tags": json.loads(row[2]) if row[2] else []} for row in cursor.fetchall()}
        cursor.execute('SELECT filename FROM video_stats WHERE is_deleted = 1')
        deleted_set = set(row[0] for row in cursor.fetchall())

    temp_counts = {}
    def extract_tags(fname): return [m.group(1) for m in re.finditer(r'#([^#\s.]+)', fname)]

    for f in all_files:
        if f in deleted_set: continue
        meta = db_store.get(f, {"cat": "未分类", "tags": []})
        merged = list(set(meta["tags"] + extract_tags(f)))
        if meta["cat"] and meta["cat"] != '未分类':
            temp_counts[meta["cat"]] = temp_counts.get(meta["cat"], 0) + 1
        for t in merged:
            temp_counts[t] = temp_counts.get(t, 0) + 1
            
    return jsonify({k: v for k, v in temp_counts.items() if v > 3})

@video_bp.route('/api/video/list', methods=['GET','POST'])
def get_video_list():
    if request.method == 'POST':
        try:
            data = request.get_json()
            name_list = data.get('names', [])
            
            if not name_list:
                return jsonify({"items": [], "total": 0})

            items = []
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # 构建防注入的 IN 查询
                placeholders = ','.join(['?'] * len(name_list))
                query = f"SELECT * FROM video_store WHERE filename IN ({placeholders})"
                cursor.execute(query, name_list)
                
                rows = cursor.fetchall()
                # 建立映射以保持前端请求的原始顺序（相似度降序）
                row_map = {row['filename']: dict(row) for row in rows}
                
                for name in name_list:
                    if name in row_map:
                        item = row_map[name]
                        # 转换标签字段
                        item['ai_tags'] = json.loads(item['tags']) if item.get('tags') else []
                        # 补全播放 URL
                        item['url'] = f"/stream/video/{quote(item['filename'])}"
                        items.append(item)
            
            return jsonify({
                "items": items,
                "total": len(items),
                "has_more": False
            })

        except Exception as e:
            print(f"[!] POST 批量查询异常: {e}")
            return jsonify({"error": str(e)}), 500
    filter_type = request.args.get('filter', 'all') 
    tag_filter = request.args.get('tag', '全部')
    pool_type = request.args.get('pool', 'mixed') 
    page, limit = int(request.args.get('page', 1)), int(request.args.get('limit', 10))
    need_tags = request.args.get('need_tags', '0') == '1'
    
    try:
        resp = requests.get(f"{RESOURCE_NODE_URL}/api/videos/json", timeout=5, proxies={"http": None, "https": None})
        all_files = resp.json() if resp.status_code == 200 else []
    except Exception: all_files = []
    
    # ==========================================
    # 🌟 核心修复：分离查询，彻底解决未打标视频点赞失效的 BUG
    # ==========================================
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        # 1. 获取 AI 分类标签数据
        cursor.execute('SELECT filename, category, tags FROM video_store')
        store_dict = {row[0]: {
            "category": row[1], 
            "tags": json.loads(row[2]) if row[2] else []
        } for row in cursor.fetchall()}
        
        # 2. 获取用户动作统计数据
        cursor.execute('SELECT filename, is_liked, is_deleted, play_count FROM video_stats')
        stats_dict = {row[0]: {
            "is_liked": row[1] or 0, 
            "is_deleted": row[2] or 0, 
            "play_count": row[3] or 0
        } for row in cursor.fetchall()}
    
    def extract_fname_tags(fname): return [m.group(1) for m in re.finditer(r'#([^#\s.]+)', fname)]
    
    base_list = []
    for f in all_files:
        # 独立获取两张表的状态，哪怕没打标也能获取到点赞信息！
        s_data = store_dict.get(f, {"category": "未分类", "tags": []})
        st_data = stats_dict.get(f, {"is_liked": 0, "is_deleted": 0, "play_count": 0})
        
        # 1. 处理资源池 (New / Default / Mixed)
        is_new_lib = f.startswith('[NEW]_')
        if pool_type == 'default' and is_new_lib: continue
        if pool_type == 'new' and not is_new_lib: continue
        
        # 2. 处理删除状态 (Disliked)
        if filter_type == 'disliked':
            if st_data["is_deleted"] != 1: continue
        else:
            if st_data["is_deleted"] == 1: continue
            
        # 3. 处理喜欢状态 (Liked)
        if filter_type == 'liked' and st_data["is_liked"] != 1: 
            continue 
        
        fname_tags = extract_fname_tags(f)
        merged_tags = list(set(s_data["tags"] + fname_tags))
        base_list.append({
            "filename": f, "url": f"/stream/video/{urllib.parse.quote(f, safe='')}", 
            "category": s_data["category"], "ai_tags": s_data["tags"],
            "filename_tags": fname_tags, "mergedTags": merged_tags, 
            "is_liked": bool(st_data["is_liked"]), 
            "play_count": st_data["play_count"]
        })

    tags_count = {}
    if need_tags:
        temp_counts = {}
        for item in base_list:
            if item["category"] and item["category"] != '未分类':
                temp_counts[item["category"]] = temp_counts.get(item["category"], 0) + 1
            for t in item["mergedTags"]:
                temp_counts[t] = temp_counts.get(t, 0) + 1
        tags_count = {k: v for k, v in temp_counts.items() if v > 10}

    kw = tag_filter.lower()
    filtered_list = [
        i for i in base_list 
        if kw == '全部' 
        or kw in i["category"].lower() 
        or kw in i["filename"].lower() 
        or any(kw in t.lower() for t in i["mergedTags"])
    ]

    start = (page - 1) * limit
    return jsonify({"items": filtered_list[start:start+limit], "tags_count": tags_count, "has_more": start+limit < len(filtered_list), "total": len(filtered_list)})

@video_bp.route('/api/video/export_csv', methods=['GET'])
def export_video_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    # 🌟 修改 5: 表头增加 "最后更新时间"
    writer.writerow(['文件名', '主分类', 'AI 标签', '提取标签', '播放次数', '是否喜欢', '是否隐藏', '最后活动时间', 'AI 模型', 'AI 耗时 (秒)', '最后更新时间'])
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # 🌟 修改 6: SQL 查询增加 v.updated_at
        cursor.execute('SELECT v.filename, v.category, v.tags, s.play_count, s.is_liked, s.is_deleted, s.last_played_at, v.ai_model, v.ai_time_sec, v.updated_at FROM video_store v LEFT JOIN video_stats s ON v.filename = s.filename')
        
        for row in cursor.fetchall():
            # row[6] 是 last_played_at, row[9] 是新的 updated_at
            dt_play = time.strftime('%Y-%m-%d %H:%M', time.localtime(row[6])) if row[6] else '-'
            
            # 🌟 修改 7: 格式化新的更新时间戳
            dt_update = time.strftime('%Y-%m-%d %H:%M', time.localtime(row[9])) if row[9] else '-'
            
            ai_tags = " ".join(json.loads(row[2])) if row[2] else ""
            f_tags = " ".join([m.group(1) for m in re.finditer(r'#([^#\s.]+)', row[0])])
            
            # 🌟 修改 8: 写入行数据，增加 dt_update
            writer.writerow([
                row[0], row[1], ai_tags, f_tags, 
                row[3] or 0, 
                '是' if row[4] else '否', 
                '是' if row[5] else '否', 
                dt_play, 
                row[7] or '-', 
                row[8] or '-',
                dt_update 
            ])
            
    response = Response(output.getvalue().encode('utf-8-sig'), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=video_stats.csv'
    return response

# export_csv2 保持原样或同样修改，这里为了统一也加上
@video_bp.route('/api/video/export_csv2', methods=['GET'])
def export_video_csv2():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['文件名', '主分类', 'AI 标签', '提取标签', '播放次数', '是否喜欢', '是否隐藏', '最后活动时间', 'AI 模型', 'AI 耗时 (秒)', '最后更新时间'])
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT v.filename, v.category, v.tags, s.play_count, s.is_liked, s.is_deleted, s.last_played_at, v.ai_model, v.ai_time_sec, v.updated_at FROM video_store v LEFT JOIN video_stats s ON v.filename = s.filename ORDER BY v.updated_at DESC LIMIT 500')
        
        for row in cursor.fetchall():
            dt_play = time.strftime('%Y-%m-%d %H:%M', time.localtime(row[6])) if row[6] else '-'
            dt_update = time.strftime('%Y-%m-%d %H:%M', time.localtime(row[9])) if row[9] else '-'
            
            ai_tags = " ".join(json.loads(row[2])) if row[2] else ""
            f_tags = " ".join([m.group(1) for m in re.finditer(r'#([^#\s.]+)', row[0])])
            
            writer.writerow([
                row[0], row[1], ai_tags, f_tags, 
                row[3] or 0, 
                '是' if row[4] else '否', 
                '是' if row[5] else '否', 
                dt_play, 
                row[7] or '-', 
                row[8] or '-',
                dt_update 
            ])
            
    response = Response(output.getvalue().encode('utf-8-sig'), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=video_stats_full.csv'
    return response

@video_bp.route('/stream/video/<path:video_name>', methods=['GET'])
def proxy_stream_video(video_name):
    url = f"{RESOURCE_NODE_URL}/stream/video/{urllib.parse.quote(video_name)}"
    headers = {key: value for (key, value) in request.headers if key.lower() != 'host'}
    try:
        req = requests.get(url, headers=headers, stream=True, proxies={"http": None, "https": None})
        excluded = ['content-encoding', 'transfer-encoding', 'connection']
        resp_headers = [(name, value) for (name, value) in req.raw.headers.items() if name.lower() not in excluded]
        return Response(stream_with_context(req.iter_content(chunk_size=1024 * 1024)), status=req.status_code, headers=resp_headers)
    except Exception as e: return jsonify({"error": str(e)}), 500

# ==========================================
# 维护与归档 API (由资源节点客户端调用)
# ==========================================

@video_bp.route('/api/video/maintenance/list', methods=['GET'])
def get_maintenance_list():
    """下发需要物理移动的视频名单"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # 喜欢且未删除的
            cursor.execute("SELECT filename FROM video_stats WHERE is_liked = 1 AND is_deleted = 0")
            liked = [row[0] for row in cursor.fetchall()]
            # 标记为删除的
            cursor.execute("SELECT filename FROM video_stats WHERE is_deleted = 1")
            deleted = [row[0] for row in cursor.fetchall()]
            
        return jsonify({"status": "ok", "liked": liked, "deleted": deleted})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@video_bp.route('/api/video/maintenance/confirm_delete', methods=['POST'])
def confirm_maintenance_delete():
    """接收客户端确认，从数据库彻底抹除记录"""
    req_data = request.get_json(silent=True) or {}
    filenames_to_delete = req_data.get('filenames', [])
    
    if not filenames_to_delete:
        return jsonify({"status": "ok", "deleted_count": 0})
        
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # SQLite 批量删除的占位符生成
            placeholders = ','.join(['?'] * len(filenames_to_delete))
            
            # 删除 AI 标签库记录
            cursor.execute(f"DELETE FROM video_store WHERE filename IN ({placeholders})", filenames_to_delete)
            # 删除播放统计记录
            cursor.execute(f"DELETE FROM video_stats WHERE filename IN ({placeholders})", filenames_to_delete)
            conn.commit()
            
        return jsonify({"status": "ok", "deleted_count": len(filenames_to_delete)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@video_bp.route('/api/video/sync', methods=['POST'])
def sync_video_actions():
    """接收前端传来的 播放、喜欢、删除 等用户动作记录"""
    actions = request.get_json(silent=True) or []
    if not actions:
        return jsonify({"status": "ok", "processed": 0})
        
    processed = 0
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            for item in actions:
                fname = item.get('filename')
                action = item.get('action')
                # JS 传过来的 timestamp 是毫秒级，Python 存一般用秒级
                ts = item.get('timestamp', time.time() * 1000)
                ts_sec = ts / 1000.0 if ts > 1e11 else ts
                
                if not fname or not action:
                    continue
                    
                # 确保在统计表中有这条视频的记录，如果没有则初始化一条
                cursor.execute('INSERT OR IGNORE INTO video_stats (filename) VALUES (?)', (fname,))
                
                # 根据动作更新数据库
                if action == 'like':
                    cursor.execute('UPDATE video_stats SET is_liked = 1 WHERE filename = ?', (fname,))
                elif action == 'unlike':
                    cursor.execute('UPDATE video_stats SET is_liked = 0 WHERE filename = ?', (fname,))
                elif action == 'delete':
                    cursor.execute('UPDATE video_stats SET is_deleted = 1 WHERE filename = ?', (fname,))
                elif action == 'undelete':
                    cursor.execute('UPDATE video_stats SET is_deleted = 0 WHERE filename = ?', (fname,))
                elif action == 'play':
                    cursor.execute('UPDATE video_stats SET play_count = play_count + 1, last_played_at = ? WHERE filename = ?', (ts_sec, fname))
                
                processed += 1
            conn.commit()
            
        return jsonify({"status": "ok", "processed": processed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==========================================
# 🧪 LLM 简单网页测试接口 (新添加)
# ==========================================

@video_bp.route('/test/llm', methods=['GET'])
def test_llm_page():
    """返回一个极简的 HTML 测试页面"""
    html = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>LLM 模型测试接口</title>
        <style>
            body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 800px; margin: 20px auto; padding: 0 15px; background: #f9f9f9; color: #333; }}
            h2 {{ border-bottom: 2px solid #ddd; padding-bottom: 10px; }}
            .info {{ background: #e3f2fd; padding: 10px; border-radius: 5px; margin-bottom: 15px; font-size: 0.9em; }}
            textarea {{ width: 100%; box-sizing: border-box; padding: 12px; margin-bottom: 10px; border: 1px solid #ccc; border-radius: 5px; font-size: 1em; resize: vertical; }}
            button {{ padding: 10px 24px; cursor: pointer; background: #007bff; color: white; border: none; border-radius: 5px; font-size: 1em; transition: 0.2s; }}
            button:hover {{ background: #0056b3; }}
            button:disabled {{ background: #aaa; cursor: not-allowed; }}
            .box {{ background: #fff; padding: 15px; border-radius: 5px; margin-top: 10px; white-space: pre-wrap; word-wrap: break-word; border: 1px solid #ddd; min-height: 50px; }}
            .stats {{ display: inline-block; background: #ffeeba; color: #856404; padding: 5px 10px; border-radius: 3px; font-size: 0.85em; margin-top: 10px; }}
            pre {{ background: #282c34; color: #98c379; padding: 15px; border-radius: 5px; overflow-x: auto; font-size: 0.85em; border: 1px solid #111; }}
        </style>
    </head>
    <body>
        <h2>🤖 LLM 对话与连通性测试 (原生解析)</h2>
        <div class="info">
            <b>当前环境:</b> {ACTIVE_AI_MODE.upper()} <br>
            <b>当前模型:</b> {AI_MODEL}
        </div>
        
        <textarea id="prompt" rows="4" placeholder="输入你想问的问题... (例如：你是谁？)"></textarea>
        <button onclick="ask()" id="btn">发送请求</button>
        <span id="loading" style="display:none; color: #007bff; margin-left: 10px; font-size: 0.9em;">⏳ 思考中，请稍候...</span>

        <h3>模型回复:</h3>
        <div id="reply" class="box" style="font-size: 1.05em;">等待输入...</div>
        <div id="stats" class="stats" style="display:none;"></div>

        <h3>原始响应 (Raw Response):</h3>
        <pre id="raw">{{}}</pre>

        <script>
            async function ask() {{
                const prompt = document.getElementById('prompt').value.trim();
                if (!prompt) return alert('请输入问题');

                const btn = document.getElementById('btn');
                const loading = document.getElementById('loading');
                const replyDiv = document.getElementById('reply');
                const statsDiv = document.getElementById('stats');
                const rawDiv = document.getElementById('raw');

                btn.disabled = true;
                loading.style.display = 'inline';
                replyDiv.innerText = '';
                statsDiv.style.display = 'none';
                rawDiv.innerText = '';

                try {{
                    const res = await fetch('/api/test/llm/ask', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{prompt: prompt}})
                    }});
                    const data = await res.json();

                    if (data.error) {{
                        replyDiv.innerHTML = '<span style="color:red;">❌ 报错: ' + data.error + '</span>';
                    }} else {{
                        replyDiv.innerText = data.reply;
                        
                        const t = data.tokens;
                        statsDiv.style.display = 'inline-block';
                        statsDiv.innerHTML = `🪙 <b>Token 消耗</b> | 提示词: ${{t.prompt_tokens}} | 回复: ${{t.completion_tokens}} | 总计: ${{t.total_tokens}}`;
                        
                        rawDiv.innerText = JSON.stringify(data.raw, null, 2);
                    }}
                }} catch (e) {{
                    replyDiv.innerHTML = '<span style="color:red;">❌ 网络或解析错误: ' + e.message + '</span>';
                }} finally {{
                    btn.disabled = false;
                    loading.style.display = 'none';
                }}
            }}
        </script>
    </body>
    </html>
    """
    return html

@video_bp.route('/api/test/llm/ask', methods=['POST'])
def test_llm_ask():
    """处理前端发来的测试请求并返回详细数据"""
    req_data = request.get_json(silent=True) or {}
    user_prompt = req_data.get('prompt', '')

    if not user_prompt:
        return jsonify({"error": "Prompt 不能为空"}), 400

    try:
        start_time = time.time()
        
        # 🌟 分流处理：根据当前模式选择底层请求方式
        if ACTIVE_AI_MODE == 'ollama':
            # Ollama 原生 API 方式
            ollama_url = AI_BASE_URL.replace('/v1', '/api/chat')
            payload = {
                "model": AI_MODEL,
                "messages": [{'role': 'user', 'content': user_prompt}],
                "stream": False,
                "think": False, # 🌟 原生关闭思考开关
                "options": {
                    "temperature": 0.7,
                    "num_predict": 2000
                }
            }
            resp = requests.post(ollama_url, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            
            elapsed_sec = round(time.time() - start_time, 2)
            reply = data.get("message", {}).get("content", "")
            
            # 适配 Ollama 原生 API 的 Token 统计字段名
            p_tokens = data.get("prompt_eval_count", 0)
            c_tokens = data.get("eval_count", 0)
            tokens = {
                "prompt_tokens": p_tokens,
                "completion_tokens": c_tokens,
                "total_tokens": p_tokens + c_tokens
            }
            raw_response = data
            raw_response["_backend_cost_time_sec"] = elapsed_sec
            
        else:
            # Cloud 模式方式：使用标准 OpenAI 兼容 SDK
            completion = client.chat.completions.create(
                model=AI_MODEL,
                messages=[{'role': 'user', 'content': user_prompt}],
                max_tokens=2000,  
                temperature=0.7
            )
            elapsed_sec = round(time.time() - start_time, 2)
            reply = completion.choices[0].message.content
            usage = completion.usage
            tokens = {
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "total_tokens": usage.total_tokens if usage else 0
            }
            raw_response = completion.model_dump() if hasattr(completion, 'model_dump') else json.loads(completion.model_dump_json())
            raw_response["_backend_cost_time_sec"] = elapsed_sec

        return jsonify({
            "reply": reply,
            "tokens": tokens,
            "raw": raw_response
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


 

@video_bp.route('/api/video/recommend', methods=['GET'])
def get_video_recommendations():
    """网页调用：获取相似视频"""
    try:
        # 将请求转发给视频向量节点
        filename=request.args.get('name')
        print("silimar video",filename)
        safe_name = quote(filename)
        # safe_name = filename
        node_url = f"{VIDEO_VECTOR_NODE}/api/video/similar?name={safe_name}&k=10&threshold=0.85"
        res = requests.get(node_url, timeout=5)
        return jsonify(res.json())
    except Exception as e:
        return jsonify({"recommendations": [], "error": "无法连接向量节点"})
import os
import sqlite3
import json
import time
import dashscope
import requests
import urllib.parse
import re
import csv
import io
import threading
from openai import OpenAI
from flask import Blueprint, request, jsonify, Response, stream_with_context
from concurrent.futures import ThreadPoolExecutor, as_completed

video_bp = Blueprint('video', __name__)
DB_PATH = 'universal_data.db'
RESOURCE_NODE_URL = "http://192.168.31.204:8100"

# DEFAULT_NLP_MODEL = 'deepseek-ai/DeepSeek-R1-0528-Qwen3-8B' 
DEFAULT_NLP_MODEL = 'deepseek-ai/DeepSeek-R1-Distill-Qwen-14、B' 
DEFAULT_NLP_MODEL = 'Qwen3-30B-A3B-Instruct-2507' 
# DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# DASHSCOPE_BASE_URL = "https://api.siliconflow.cn/v1"
DASHSCOPE_BASE_URL = "https://api.scnet.cn/api/llm/v1"
client = OpenAI(
    # api_key=os.getenv("SILICONFLOW_API_KEY"),
    api_key=os.getenv("CS_API_KEY"),
    base_url=DASHSCOPE_BASE_URL,
)

max_workers=6
executor = ThreadPoolExecutor(max_workers=max_workers)
state_lock = threading.Lock()

ai_scan_state = {
    "is_running": False,
    "total": 0,
    "processed": 0,       
    "success_count": 0,   
    "status_msg": "就绪，等待扫描",
    "total_time_sec": 0,
    "ai_model": DEFAULT_NLP_MODEL,
    "recent_results": []
}

def init_video_db():
    with sqlite3.connect(DB_PATH) as conn:
        # 创建主表
        conn.execute('CREATE TABLE IF NOT EXISTS video_store (filename TEXT PRIMARY KEY, tags TEXT, category TEXT)')
        
        # 🌟 修改 1: 尝试添加新字段 (ai_model, ai_time_sec, updated_at)
        # 使用 try-except 防止字段已存在时报错
        columns_to_add = [
            ("ai_model", "TEXT"),
            ("ai_time_sec", "REAL"),
            ("updated_at", "REAL")  # 🌟 新增：最后更新时间戳字段
        ]
        
        for col_name, col_type in columns_to_add:
            try:
                conn.execute(f'ALTER TABLE video_store ADD COLUMN {col_name} {col_type}')
            except sqlite3.OperationalError:
                pass # 字段已存在，跳过
        
        # 创建统计表
        conn.execute('CREATE TABLE IF NOT EXISTS video_stats (filename TEXT PRIMARY KEY, is_liked INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0, play_count INTEGER DEFAULT 0, last_played_at REAL DEFAULT 0)')

init_video_db()

def process_single_batch(batch, batch_id):
    global ai_scan_state
    
    if not ai_scan_state["is_running"]:
        return

    prompt = f"""你是一个短视频内容分析引擎。请根据以下视频文件名，为每个视频推断出 1 个【主分类】(如：影视，搞笑，学习，颜值，音乐，随拍) 和 1 到 7 个【子标签】根据文件名信息量确定子标签数量。
    务必返回纯 JSON，格式：{{"video.mp4": {{"category": "影视", "tags": ["混剪", "动作"]}}}}
    文件名列表：{json.dumps(batch, ensure_ascii=False)}"""
    
    batch_start_time = time.time()
    # 🌟 修改 2: 获取当前时间戳，用于记录更新时间
    current_timestamp = time.time() 
    
    try:
        completion = client.chat.completions.create(
            model=DEFAULT_NLP_MODEL,
            messages=[
                {'role': 'system', 'content': '你是一个只返回纯 JSON 的分析助手。'},
                {'role': 'user', 'content': prompt}
            ],
            response_format={ "type": "json_object" },
            max_tokens=1000,  # 限制响应Token上限，避免超长返回
            temperature=0.1   # 降低随机性，减少无意义的标签，间接减少Token
        )
        
        batch_duration = time.time() - batch_start_time
        # 防止除以零 (虽然 batch 通常不为空，但作为防御性编程)
        count = len(batch) if len(batch) > 0 else 1 
        elapsed = round(batch_duration / count, 2) # 直接保留2位小数通常更精确，或者 round(..., 1)
        result_text = completion.choices[0].message.content.strip()
        
        match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if match:
            ai_data = json.loads(match.group(0))
            last_fname, last_tags_str = "", ""
            
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                for fname, info in ai_data.items():
                    tags = info.get('tags', [])
                    if not isinstance(tags, list): tags = [tags]
                    
                    # 🌟 修改 3: 在 INSERT 语句中加入 updated_at 字段
                    conn.execute("""
                        INSERT OR REPLACE INTO video_store (filename, category, tags, ai_model, ai_time_sec, updated_at) 
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (fname, info.get('category', '未分类'), json.dumps(tags[:5], ensure_ascii=False), DEFAULT_NLP_MODEL, elapsed, current_timestamp))
                    
                    last_fname, last_tags_str = fname, " ".join([f"#{t}" for t in tags[:3]])
            
            with state_lock:
                ai_scan_state["success_count"] += len(ai_data)
                ai_scan_state["processed"] += len(batch)
                ai_scan_state["status_msg"] = f"✅ {last_fname[:20]}... {last_tags_str}"
                
                for fname, info in ai_data.items():
                    tags = info.get('tags', [])
                    if not isinstance(tags, list): tags = [tags]
                    ai_scan_state["recent_results"].insert(0, {
                        "filename": fname,
                        "category": info.get('category', '未分类'),
                        "ai_tags": tags[:5],
                        "updated_at": current_timestamp # 可选：如果需要实时状态里也显示时间
                    })
                ai_scan_state["recent_results"] = ai_scan_state["recent_results"][:5]
            
    except Exception as e:
        elapsed = round(time.time() - batch_start_time, 2)
        err_msg = str(e)
        with state_lock:
            ai_scan_state["processed"] += len(batch)
            ai_scan_state["status_msg"] = f"❌ API 报错：{err_msg[:40]}"
        if "RateLimit" in err_msg: time.sleep(5)

def ai_tag_videos_task():
    global ai_scan_state
    
    ai_scan_state.update({
        "is_running": True, "total": 0, "processed": 0, 
        "success_count": 0, "status_msg": "正在同步列表...", 
        "total_time_sec": 0, "ai_model": DEFAULT_NLP_MODEL,
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
        "status_msg": "🚀 多线程并发分析已启动..."
    })
    
    batch_size = 8
    batches = [untagged[i:i+batch_size] for i in range(0, len(untagged), batch_size)]
    
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

@video_bp.route('/api/video/list', methods=['GET'])
def get_video_list():
    filter_type = request.args.get('filter', 'all') 
    tag_filter = request.args.get('tag', '全部')
    page, limit = int(request.args.get('page', 1)), int(request.args.get('limit', 10))
    need_tags = request.args.get('need_tags', '0') == '1'
    
    try:
        resp = requests.get(f"{RESOURCE_NODE_URL}/api/videos/json", timeout=5, proxies={"http": None, "https": None})
        all_files = resp.json() if resp.status_code == 200 else []
    except Exception: all_files = []
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # 🌟 修改 4: 查询时也带上 updated_at (虽然前端列表没用到，但以防万一)
        cursor.execute('SELECT v.filename, v.category, v.tags, s.is_liked, s.is_deleted, s.play_count, v.updated_at FROM video_store v LEFT JOIN video_stats s ON v.filename = s.filename')
        db_data = {row[0]: {
            "category": row[1], 
            "tags": json.loads(row[2]) if row[2] else [], 
            "is_liked": row[3] or 0, 
            "is_deleted": row[4] or 0, 
            "play_count": row[5] or 0,
            "updated_at": row[6] # 可选：如果需要返回列表中包含时间
        } for row in cursor.fetchall()}
    
    def extract_fname_tags(fname): return [m.group(1) for m in re.finditer(r'#([^#\s.]+)', fname)]
    
    base_list = []
    for f in all_files:
        meta = db_data.get(f, {"category": "未分类", "tags": [], "is_liked": 0, "is_deleted": 0, "play_count": 0})
        if filter_type == 'disliked' and meta["is_deleted"] != 1: continue
        if filter_type != 'disliked' and meta["is_deleted"] == 1: continue
        
        fname_tags = extract_fname_tags(f)
        merged_tags = list(set(meta["tags"] + fname_tags))
        base_list.append({
            "filename": f, "url": f"/stream/video/{urllib.parse.quote(f, safe='')}", 
            "category": meta["category"], "ai_tags": meta["tags"],
            "filename_tags": fname_tags, "mergedTags": merged_tags, 
            "is_liked": bool(meta["is_liked"]), "play_count": meta["play_count"]
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

    filtered_list = [i for i in base_list if tag_filter == '全部' or i["category"] == tag_filter or tag_filter in i["mergedTags"]]
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
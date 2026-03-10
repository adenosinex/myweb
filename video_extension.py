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
from flask import Blueprint, request, jsonify, Response, stream_with_context
from concurrent.futures import ThreadPoolExecutor

video_bp = Blueprint('video', __name__)
DB_PATH = 'universal_data.db'
RESOURCE_NODE_URL = "http://192.168.31.204:8100"

# 🌟 核心修复：更正为阿里支持的真实模型名称
DEFAULT_NLP_MODEL = 'qwen3.5-27b' 

executor = ThreadPoolExecutor(max_workers=2)

# ================= 🌟 增强版 AI 任务状态机 =================
# 确保所有字段初始化，防止前端 JS 读取 undefined 报错
ai_scan_state = {
    "is_running": False,
    "total": 0,
    "processed": 0,       
    "success_count": 0,   
    "status_msg": "就绪，等待扫描",
    "total_time_sec": 0,
    "ai_model": DEFAULT_NLP_MODEL  
}

def init_video_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS video_store (filename TEXT PRIMARY KEY, tags TEXT, category TEXT)')
        try:
            conn.execute('ALTER TABLE video_store ADD COLUMN ai_model TEXT')
            conn.execute('ALTER TABLE video_store ADD COLUMN ai_time_sec REAL')
        except sqlite3.OperationalError:
            pass 
        conn.execute('CREATE TABLE IF NOT EXISTS video_stats (filename TEXT PRIMARY KEY, is_liked INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0, play_count INTEGER DEFAULT 0, last_played_at REAL DEFAULT 0)')
init_video_db()

def ai_tag_videos_task():
    global ai_scan_state
    
    # 初始化任务状态
    ai_scan_state.update({
        "is_running": True, "total": 0, "processed": 0, 
        "success_count": 0, "status_msg": "正在同步视频列表...", 
        "total_time_sec": 0, "ai_model": DEFAULT_NLP_MODEL
    })
    
    task_start_time = time.time()
    
    try:
        resp = requests.get(f"{RESOURCE_NODE_URL}/api/videos/json", timeout=10, proxies={"http": None, "https": None})
        if resp.status_code != 200: 
            ai_scan_state.update({"is_running": False, "status_msg": f"资源节点异常: {resp.status_code}"})
            return
        all_files = resp.json()
    except Exception as e: 
        ai_scan_state.update({"is_running": False, "status_msg": f"无法访问资源节点: {str(e)[:30]}"})
        return

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filename FROM video_store")
        existing = set(row[0] for row in cursor.fetchall())
        
    untagged = [f for f in all_files if f not in existing]
    
    if not untagged: 
        ai_scan_state.update({"is_running": False, "status_msg": "🎉 所有视频均已打标完成"})
        return

    ai_scan_state["total"] = len(untagged)
    batch_size = 10

    for i in range(0, len(untagged), batch_size):
        if not ai_scan_state["is_running"]:
            ai_scan_state["status_msg"] = "⚠️ 任务已手动强行终止"
            return
            
        batch = untagged[i:i+batch_size]
        ai_scan_state["status_msg"] = f"⏳ 正在分析: {batch[0][:20]}..."
        
        prompt = f"""
        你是一个短视频内容分析引擎。请根据以下视频文件名，为每个视频推断出 1 个【主分类】(如: 影视, 搞笑, 学习, 颜值, 音乐, 随拍) 
        和 3 到 5 个【子标签】(如: 混剪, 剧情, 舞蹈, 宠物, 编程 等)。
        务必返回纯JSON，格式：{{"video.mp4": {{"category": "影视", "tags": ["混剪", "动作"]}}}}
        文件名列表：{json.dumps(batch, ensure_ascii=False)}
        """
        
        batch_start_time = time.time()
        try:
            # 🌟 核心修复：增加对 Response 的安全性校验
            response = dashscope.Generation.call(model=DEFAULT_NLP_MODEL, prompt=prompt, result_format='message')
            
            if response is None:
                raise Exception("DashScope 返回为空，请检查网络或API Key")
            
            if response.status_code != 200:
                raise Exception(f"API错误: {response.message}")

            elapsed = round(time.time() - batch_start_time, 2)
            result_text = response.output.choices[0].message.content.strip()
            match = re.search(r'\{.*\}', result_text, re.DOTALL)
            
            if match:
                ai_data = json.loads(match.group(0))
                last_fname, last_tags_str = "", ""
                
                with sqlite3.connect(DB_PATH) as conn:
                    for fname, info in ai_data.items():
                        tags = info.get('tags', [])
                        if not isinstance(tags, list): tags = [tags]
                        
                        conn.execute("""
                            INSERT OR REPLACE INTO video_store (filename, category, tags, ai_model, ai_time_sec) 
                            VALUES (?, ?, ?, ?, ?)
                        """, (fname, info.get('category', '未分类'), json.dumps(tags[:5], ensure_ascii=False), DEFAULT_NLP_MODEL, elapsed))
                        
                        last_fname = fname
                        last_tags_str = " ".join([f"#{t}" for t in tags[:3]])
                
                ai_scan_state["success_count"] += len(ai_data)
                ai_scan_state["status_msg"] = f"✅ {last_fname[:25]}... {last_tags_str}"
            else:
                ai_scan_state["status_msg"] = f"❌ 返回格式异常 ({elapsed}s)"
                
        except Exception as e:
            elapsed = round(time.time() - batch_start_time, 2)
            err_msg = str(e)
            ai_scan_state["status_msg"] = f"❌ API报错: {err_msg[:40]}"
            # 如果是限流，额外等待
            if "RateQuota" in err_msg or "Throttling" in err_msg:
                time.sleep(5)
                
        ai_scan_state["processed"] += len(batch)
        ai_scan_state["total_time_sec"] = round(time.time() - task_start_time, 1)
        
        if ai_scan_state["processed"] < ai_scan_state["total"]:
            time.sleep(1.5) # 防封锁休眠
            
    if ai_scan_state["is_running"]:
        ai_scan_state["is_running"] = False
        ai_scan_state["status_msg"] = f"🎉 成功打标 {ai_scan_state['success_count']} 个视频！"

# ================= 接口：控制与统计 (保持原逻辑) =================
@video_bp.route('/api/video/scan', methods=['POST'])
def control_scan():
    global ai_scan_state
    req_data = request.get_json(silent=True) or {}
    if req_data.get('action') == 'start' and not ai_scan_state["is_running"]:
        executor.submit(ai_tag_videos_task)
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
        cursor.execute('SELECT v.filename, v.category, v.tags, s.is_liked, s.is_deleted, s.play_count FROM video_store v LEFT JOIN video_stats s ON v.filename = s.filename')
        db_data = {row[0]: {"category": row[1], "tags": json.loads(row[2]) if row[2] else [], "is_liked": row[3] or 0, "is_deleted": row[4] or 0, "play_count": row[5] or 0} for row in cursor.fetchall()}
    
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
    writer.writerow(['文件名', '主分类', 'AI标签', '提取标签', '播放次数', '是否喜欢', '是否隐藏', '最后活动时间', 'AI模型', 'AI耗时(秒)'])
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT v.filename, v.category, v.tags, s.play_count, s.is_liked, s.is_deleted, s.last_played_at, v.ai_model, v.ai_time_sec FROM video_store v LEFT JOIN video_stats s ON v.filename = s.filename')
        for row in cursor.fetchall():
            dt = time.strftime('%Y-%m-%d %H:%M', time.localtime(row[6])) if row[6] else '-'
            ai_tags = " ".join(json.loads(row[2])) if row[2] else ""
            f_tags = " ".join([m.group(1) for m in re.finditer(r'#([^#\s.]+)', row[0])])
            writer.writerow([row[0], row[1], ai_tags, f_tags, row[3] or 0, '是' if row[4] else '否', '是' if row[5] else '否', dt, row[7] or '-', row[8] or '-'])
    response = Response(output.getvalue().encode('utf-8-sig'), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=video_stats.csv'
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
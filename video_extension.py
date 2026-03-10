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

executor = ThreadPoolExecutor(max_workers=2)

# ================= 🌟 增强版 AI 任务状态机 =================
ai_scan_state = {
    "is_running": False,
    "total": 0,
    "processed": 0,       # 已处理（包含成功和失败）
    "success_count": 0,   # 成功打标的数量
    "status_msg": "就绪，等待扫描",
    "total_time_sec": 0   # 任务总耗时
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
    
    ai_scan_state.update({
        "is_running": True, "total": 0, "processed": 0, 
        "success_count": 0, "status_msg": "正在连接资源节点获取视频列表...", "total_time_sec": 0
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

    if not all_files:
        ai_scan_state.update({"is_running": False, "status_msg": "资源节点中没有找到视频"})
        return

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filename FROM video_store")
        existing = set(row[0] for row in cursor.fetchall())
        
    untagged = [f for f in all_files if f not in existing]
    
    if not untagged: 
        ai_scan_state.update({"is_running": False, "status_msg": "🎉 所有视频均已打标完成，无需扫描"})
        return

    ai_scan_state["total"] = len(untagged)
    batch_size = 10
    model_name = 'qwen-plus'

    for i in range(0, len(untagged), batch_size):
        if not ai_scan_state["is_running"]:
            ai_scan_state["status_msg"] = "⚠️ 任务已被手动强行终止"
            return
            
        batch = untagged[i:i+batch_size]
        ai_scan_state["status_msg"] = f"正在请求千问大模型分析第 {i+1}~{min(i+batch_size, len(untagged))} 个..."
        
        prompt = f"""
        你是一个短视频内容分析引擎。请根据以下视频文件名，为每个视频推断出 1 个【主分类】(如: 影视, 搞笑, 学习, 颜值, 音乐, 随拍) 
        和 3 到 5 个【子标签】(如: 混剪, 剧情, 舞蹈, 宠物, 编程 等)。
        务必返回纯JSON，格式：{{"video.mp4": {{"category": "影视", "tags": ["混剪", "动作"]}}}}
        文件名列表：{json.dumps(batch, ensure_ascii=False)}
        """
        
        batch_start_time = time.time()
        try:
            # 核心请求
            response = dashscope.Generation.call(model=model_name, prompt=prompt, result_format='message')
            elapsed = round(time.time() - batch_start_time, 2)
            
            result_text = response.output.choices[0].message.content.strip()
            match = re.search(r'\{.*\}', result_text, re.DOTALL)
            
            if match:
                ai_data = json.loads(match.group(0))
                with sqlite3.connect(DB_PATH) as conn:
                    for fname, info in ai_data.items():
                        tags = info.get('tags', [])
                        if not isinstance(tags, list): tags = [tags]
                        conn.execute("""
                            INSERT OR REPLACE INTO video_store (filename, category, tags, ai_model, ai_time_sec) 
                            VALUES (?, ?, ?, ?, ?)
                        """, (fname, info.get('category', '未分类'), json.dumps(tags[:5], ensure_ascii=False), model_name, elapsed))
                
                ai_scan_state["success_count"] += len(ai_data)
                ai_scan_state["status_msg"] = f"✅ 此批次成功 (耗时: {elapsed}s)"
            else:
                ai_scan_state["status_msg"] = f"❌ 返回格式异常 (耗时: {elapsed}s)"
                
        except Exception as e:
            elapsed = round(time.time() - batch_start_time, 2)
            err_msg = str(e)
            if "RateQuota" in err_msg or "Throttling" in err_msg:
                ai_scan_state["status_msg"] = f"⏳ 触发限流限制，等待后重试 ({elapsed}s)"
            else:
                ai_scan_state["status_msg"] = f"❌ API报错: {err_msg[:40]}..."
                
        # 无论成功失败，都推进处理进度，防止死循环卡住
        ai_scan_state["processed"] += len(batch)
        ai_scan_state["total_time_sec"] = round(time.time() - task_start_time, 1)
        
        # 核心防封锁：每批次处理完，强制休眠 1.5 秒，保护大模型 API 限额
        if ai_scan_state["processed"] < ai_scan_state["total"]:
            time.sleep(1.5)
            
    if ai_scan_state["is_running"]:
        ai_scan_state["is_running"] = False
        ai_scan_state["status_msg"] = f"🎉 扫描结束！成功打标 {ai_scan_state['success_count']} 个。"

# ================= 任务控制与状态查询 =================
@video_bp.route('/api/video/scan', methods=['POST'])
def control_scan():
    global ai_scan_state
    req_data = request.get_json(silent=True) or {}
    action = req_data.get('action', 'start')
    
    if action == 'start':
        if not ai_scan_state["is_running"]:
            executor.submit(ai_tag_videos_task)
        return jsonify({"status": "started"})
    elif action == 'stop':
        ai_scan_state["is_running"] = False
        ai_scan_state["status_msg"] = "正在强行刹车，请稍候..."
        return jsonify({"status": "stopped"})

@video_bp.route('/api/video/scan/status', methods=['GET'])
def get_scan_status():
    # 动态计算最新总耗时 (防止卡住时时间不走)
    return jsonify(ai_scan_state)

# ================= (后续的所有接口 list/sync/csv/proxy 保持你最新的代码即可) =================
@video_bp.route('/api/video/list', methods=['GET'])
def get_video_list():
    filter_type = request.args.get('filter', 'all') 
    tag_filter = request.args.get('tag', '全部')
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 10))
    try:
        resp = requests.get(f"{RESOURCE_NODE_URL}/api/videos/json", timeout=5, proxies={"http": None, "https": None})
        all_files = resp.json() if resp.status_code == 200 else []
    except Exception: all_files = []
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT v.filename, v.category, v.tags, s.is_liked, s.is_deleted, s.play_count, s.last_played_at FROM video_store v LEFT JOIN video_stats s ON v.filename = s.filename')
        db_data = {row[0]: {"category": row[1], "tags": json.loads(row[2]) if row[2] else [], "is_liked": row[3] or 0, "is_deleted": row[4] or 0, "play_count": row[5] or 0, "last_played_at": row[6] or 0} for row in cursor.fetchall()}
    
    def extract_fname_tags(fname): return [m.group(1) for m in re.finditer(r'#([^#\s.]+)', fname)]
    base_list = []
    for f in all_files:
        meta = db_data.get(f, {"category": "未分类", "tags": [], "is_liked": 0, "is_deleted": 0, "play_count": 0, "last_played_at": 0})
        if filter_type == 'disliked':
            if meta["is_deleted"] != 1: continue
        else:
            if meta["is_deleted"] == 1: continue 
            if filter_type == 'unplayed' and meta["play_count"] > 0: continue
            
        merged_tags = list(set(meta["tags"] + extract_fname_tags(f)))
        base_list.append({"filename": f, "url": f"/stream/video/{urllib.parse.quote(f, safe='')}", "category": meta["category"], "mergedTags": merged_tags, "is_liked": bool(meta["is_liked"]), "play_count": meta["play_count"], "last_played_at": meta["last_played_at"]})
    if filter_type == 'disliked': base_list.sort(key=lambda x: x["last_played_at"], reverse=True)
    tags_count = {}
    for item in base_list:
        if item["category"] and item["category"] != '未分类': tags_count[item["category"]] = tags_count.get(item["category"], 0) + 1
        for t in item["mergedTags"]: tags_count[t] = tags_count.get(t, 0) + 1
    filtered_list = []
    for item in base_list:
        if tag_filter != '全部':
            if item["category"] != tag_filter and tag_filter not in item["mergedTags"]: continue
        filtered_list.append(item)
    start = (page - 1) * limit
    end = start + limit
    return jsonify({"items": filtered_list[start:end], "tags_count": tags_count, "has_more": end < len(filtered_list), "total": len(filtered_list)})

@video_bp.route('/api/video/sync', methods=['POST'])
def sync_video_actions():
    events = request.json
    if not events: return jsonify({"status": "success"})
    with sqlite3.connect(DB_PATH) as conn:
        for ev in events:
            fname = ev.get('filename')
            action = ev.get('action') 
            conn.execute("INSERT OR IGNORE INTO video_stats (filename) VALUES (?)", (fname,))
            if action == 'play': conn.execute("UPDATE video_stats SET play_count = play_count + 1, last_played_at = ? WHERE filename = ?", (time.time(), fname))
            elif action == 'like': conn.execute("UPDATE video_stats SET is_liked = 1 WHERE filename = ?", (fname,))
            elif action == 'unlike': conn.execute("UPDATE video_stats SET is_liked = 0 WHERE filename = ?", (fname,))
            elif action == 'delete': conn.execute("UPDATE video_stats SET is_deleted = 1, last_played_at = ? WHERE filename = ?", (time.time(), fname))
            elif action == 'undelete': conn.execute("UPDATE video_stats SET is_deleted = 0 WHERE filename = ?", (fname,))
    return jsonify({"status": "success"})

@video_bp.route('/api/video/export_csv', methods=['GET'])
def export_video_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['文件名', '分类', '播放次数', '是否喜欢', '是否隐藏(删除)', '最后活动时间', 'AI模型', 'AI耗时(秒)'])
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT v.filename, v.category, s.play_count, s.is_liked, s.is_deleted, s.last_played_at, v.ai_model, v.ai_time_sec FROM video_store v LEFT JOIN video_stats s ON v.filename = s.filename')
        for row in cursor.fetchall():
            dt = time.strftime('%Y-%m-%d %H:%M', time.localtime(row[5])) if row[5] else '-'
            writer.writerow([row[0], row[1], row[2] or 0, '是' if row[3] else '否', '是' if row[4] else '否', dt, row[6] or '-', row[7] or '-'])
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
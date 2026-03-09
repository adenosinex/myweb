import os
import sqlite3
import json
import time
import dashscope
import requests
import urllib.parse
import csv
import io
from flask import Blueprint, request, jsonify, Response, stream_with_context
from concurrent.futures import ThreadPoolExecutor

video_bp = Blueprint('video', __name__)
DB_PATH = 'universal_data.db'

# ================= 配置区 =================
# 指向你的轻量级局域网资源节点
RESOURCE_NODE_URL = "http://192.168.31.204:8100"

executor = ThreadPoolExecutor(max_workers=2)

def init_video_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS video_store (
                filename TEXT PRIMARY KEY,
                tags TEXT,
                category TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS video_stats (
                filename TEXT PRIMARY KEY,
                is_liked INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0,
                play_count INTEGER DEFAULT 0,
                last_played_at REAL DEFAULT 0
            )
        ''')
init_video_db()

# ================= AI 异步打标任务 =================
def ai_tag_videos_task():
    try:
        resp = requests.get(f"{RESOURCE_NODE_URL}/api/videos/json", timeout=10, proxies={"http": None, "https": None})
        if resp.status_code != 200: return
        all_files = resp.json()
    except Exception: return

    if not all_files: return

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filename FROM video_store")
        existing = set(row[0] for row in cursor.fetchall())
        
    untagged = [f for f in all_files if f not in existing]
    if not untagged: return

    batch = untagged[:40]
    prompt = f"""
    你是一个短视频内容分析引擎。请根据以下视频文件名，为每个视频推断出 1 个【主分类】(如: 影视, 搞笑, 学习, 颜值, 音乐, 随拍) 
    和 3 到 5 个【子标签】(如: 混剪, 剧情, 舞蹈, 宠物, 编程 等)。
    务必返回纯JSON，格式：{{"video.mp4": {{"category": "影视", "tags": ["混剪", "动作"]}}}}
    文件名列表：{json.dumps(batch, ensure_ascii=False)}
    """
    
    try:
        response = dashscope.Generation.call(model='qwen-plus', prompt=prompt, result_format='message')
        result_text = response.output.choices[0].message.content.strip()
        import re
        match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if match:
            ai_data = json.loads(match.group(0))
            with sqlite3.connect(DB_PATH) as conn:
                for fname, info in ai_data.items():
                    tags = info.get('tags', [])
                    if not isinstance(tags, list): tags = [tags]
                    conn.execute(
                        "INSERT OR REPLACE INTO video_store (filename, category, tags) VALUES (?, ?, ?)",
                        (fname, info.get('category', '未分类'), json.dumps(tags[:5], ensure_ascii=False))
                    )
            print(f"[视频AI] 打标完成 {len(ai_data)} 个")
    except Exception as e:
        print(f"[视频AI失败] {str(e)}")

# ================= API 接口 =================
@video_bp.route('/api/video/scan', methods=['POST'])
def trigger_scan():
    executor.submit(ai_tag_videos_task)
    return jsonify({"status": "Scanning background"})

@video_bp.route('/api/video/list', methods=['GET'])
def get_video_list():
    filter_type = request.args.get('filter', 'all') # 'all', 'unplayed', 'disliked'
    
    try:
        resp = requests.get(f"{RESOURCE_NODE_URL}/api/videos/json", timeout=5, proxies={"http": None, "https": None})
        all_files = resp.json() if resp.status_code == 200 else []
    except Exception: all_files = []
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        query = '''
            SELECT v.filename, v.category, v.tags, s.is_liked, s.is_deleted, s.play_count 
            FROM video_store v LEFT JOIN video_stats s ON v.filename = s.filename
        '''
        # 如果是不喜欢列表，单独排序
        if filter_type == 'disliked':
            query += " WHERE s.is_deleted = 1 ORDER BY s.last_played_at DESC"
            
        cursor.execute(query)
        db_data = {row[0]: {"category": row[1], "tags": json.loads(row[2]) if row[2] else [], 
                            "is_liked": row[3] or 0, "is_deleted": row[4] or 0, "play_count": row[5] or 0} 
                   for row in cursor.fetchall()}
    
    result = []
    # 针对普通列表和未播放列表的逻辑
    if filter_type != 'disliked':
        for f in all_files:
            meta = db_data.get(f, {"category": "随拍", "tags": [], "is_liked": 0, "is_deleted": 0, "play_count": 0})
            if meta["is_deleted"] == 1: continue 
            if filter_type == 'unplayed' and meta["play_count"] > 0: continue
            
            url_safe_name = urllib.parse.quote(f, safe='') # 极致转义，防 # 截断
            result.append({
                "filename": f, "url": f"/stream/video/{url_safe_name}", 
                "category": meta["category"], "tags": meta["tags"],
                "is_liked": bool(meta["is_liked"]), "play_count": meta["play_count"]
            })
    else:
        # 不喜欢列表只读取数据库里标记过删除的
        for f, meta in db_data.items():
            if meta["is_deleted"] == 1:
                url_safe_name = urllib.parse.quote(f, safe='')
                result.append({
                    "filename": f, "url": f"/stream/video/{url_safe_name}", 
                    "category": meta["category"], "tags": meta["tags"],
                    "is_liked": bool(meta["is_liked"]), "play_count": meta["play_count"]
                })
    return jsonify(result)

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
    writer.writerow(['文件名', '分类', '播放次数', '是否喜欢', '是否隐藏(删除)', '最后活动时间'])
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT v.filename, v.category, s.play_count, s.is_liked, s.is_deleted, s.last_played_at
            FROM video_store v LEFT JOIN video_stats s ON v.filename = s.filename
        ''')
        for row in cursor.fetchall():
            dt = time.strftime('%Y-%m-%d %H:%M', time.localtime(row[5])) if row[5] else '-'
            writer.writerow([row[0], row[1], row[2] or 0, '是' if row[3] else '否', '是' if row[4] else '否', dt])
            
    response = Response(output.getvalue().encode('utf-8-sig'), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=video_stats.csv'
    return response

@video_bp.route('/stream/video/<path:video_name>', methods=['GET'])
def proxy_stream_video(video_name):
    # Flask 解码后重编码，防止二次转发时被资源节点截断
    encoded_name = urllib.parse.quote(video_name)
    url = f"{RESOURCE_NODE_URL}/stream/video/{encoded_name}"
    headers = {key: value for (key, value) in request.headers if key.lower() != 'host'}
    try:
        req = requests.get(url, headers=headers, stream=True, proxies={"http": None, "https": None})
        excluded = ['content-encoding', 'transfer-encoding', 'connection']
        resp_headers = [(name, value) for (name, value) in req.raw.headers.items() if name.lower() not in excluded]
        return Response(stream_with_context(req.iter_content(chunk_size=1024 * 1024)), status=req.status_code, headers=resp_headers)
    except Exception as e: return jsonify({"error": str(e)}), 500